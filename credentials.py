"""Secure local credential storage for the portable Windows build.

Only the Windows Credential Manager backend is accepted for saved credentials.
If keyring does not expose that backend, the application reports an error and
continues in offline/local-catalog mode; it never falls back to a plaintext
file. Secrets are never logged, written to SQLite, exported, or placed in a
filename.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

SERVICE = "osu-map-picker"
ACCOUNT = "osu-oauth"
_SECURE_MODULE = "keyring.backends.Windows"
_SECURE_CLASS = "WinVaultKeyring"


class CredentialError(RuntimeError):
    """Credential storage is unavailable or contains invalid data."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str = field(repr=False)

    def __repr__(self) -> str:  # never expose the secret through debugging
        return f"Credentials(client_id={self.client_id!r}, client_secret=<hidden>)"


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    backend: str
    error: str = ""


def _keyring_module() -> Any:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - release dependency is pinned
        raise CredentialError(
            "Модуль keyring не установлен; безопасное хранилище недоступно."
        ) from exc
    return keyring


def _secure_backend(backend: Any) -> bool:
    """Accept Windows Credential Manager and explicit test doubles only."""

    module = type(backend).__module__
    name = type(backend).__name__
    if module == _SECURE_MODULE and name == _SECURE_CLASS:
        return True
    # Test doubles can explicitly opt in; the application never passes one.
    return bool(getattr(backend, "osu_map_picker_secure_test_backend", False))


def _backend(backend: Any = None) -> Any:
    if backend is None:
        backend = _keyring_module().get_keyring()
    if not _secure_backend(backend):
        raise CredentialError(
            "Безопасное хранилище Windows Credential Manager недоступно. "
            "Сохранение секрета отключено; используйте .env или исправьте "
            "Windows Credential Manager."
        )
    return backend


def credential_service() -> str:
    """Allow tests/deployments to isolate the Credential Manager namespace."""

    return os.getenv("OSU_PICKER_KEYRING_SERVICE", "").strip() or SERVICE


def backend_description(backend: Any = None) -> str:
    try:
        selected = _backend(backend)
    except CredentialError as exc:
        return str(exc)
    return f"{type(selected).__module__}.{type(selected).__name__}"


def status(backend: Any = None) -> CredentialStatus:
    try:
        selected = _backend(backend)
    except CredentialError as exc:
        return CredentialStatus(False, "", str(exc))
    try:
        credentials = load_credentials(selected)
    except CredentialError as exc:
        return CredentialStatus(False, backend_description(selected), str(exc))
    return CredentialStatus(
        credentials is not None,
        backend_description(selected),
    )


def load_credentials(backend: Any = None) -> Credentials | None:
    selected = _backend(backend)
    try:
        payload = selected.get_password(credential_service(), ACCOUNT)
    except Exception as exc:  # keyring backends expose different error types
        raise CredentialError(
            "Не удалось прочитать credentials из Windows Credential Manager."
        ) from exc
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise CredentialError(
            "Сохранённые credentials повреждены; удалите их и введите заново."
        ) from exc
    if not isinstance(data, dict):
        raise CredentialError(
            "Сохранённые credentials повреждены; удалите их и введите заново."
        )
    client_id = str(data.get("client_id", "")).strip()
    client_secret = str(data.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        raise CredentialError(
            "Сохранённые credentials неполные; удалите их и введите заново."
        )
    return Credentials(client_id, client_secret)


def save_credentials(
    client_id: str,
    client_secret: str,
    backend: Any = None,
) -> None:
    client_id = str(client_id).strip()
    client_secret = str(client_secret).strip()
    if not client_id or not client_secret:
        raise CredentialError("Client ID и Client Secret не могут быть пустыми.")
    selected = _backend(backend)
    payload = json.dumps(
        {"client_id": client_id, "client_secret": client_secret},
        ensure_ascii=False,
    )
    try:
        selected.set_password(credential_service(), ACCOUNT, payload)
    except Exception as exc:
        raise CredentialError(
            "Не удалось сохранить credentials в Windows Credential Manager."
        ) from exc


def delete_credentials(backend: Any = None) -> None:
    selected = _backend(backend)
    try:
        selected.delete_password(credential_service(), ACCOUNT)
    except Exception as exc:
        if type(exc).__name__ == "PasswordDeleteError":
            return
        raise CredentialError(
            "Не удалось удалить credentials из Windows Credential Manager."
        ) from exc
