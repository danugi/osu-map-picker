from __future__ import annotations

import json
import sys

import pytest

import config
import credentials
from storage import Storage


class FakeKeyring:
    osu_map_picker_secure_test_backend = True

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class InsecureKeyring(FakeKeyring):
    osu_map_picker_secure_test_backend = False


def test_secure_credential_save_load_status_delete(monkeypatch):
    monkeypatch.setenv("OSU_PICKER_KEYRING_SERVICE", "test-service")
    backend = FakeKeyring()
    credentials.save_credentials("12345", "super-secret-value", backend)
    loaded = credentials.load_credentials(backend)
    assert loaded is not None
    assert loaded.client_id == "12345"
    assert loaded.client_secret == "super-secret-value"
    assert "super-secret-value" not in repr(loaded)
    assert credentials.status(backend).configured is True
    credentials.delete_credentials(backend)
    assert credentials.load_credentials(backend) is None
    assert credentials.status(backend).configured is False


def test_insecure_backend_is_rejected_without_plaintext_fallback(monkeypatch):
    monkeypatch.setenv("OSU_PICKER_KEYRING_SERVICE", "test-service")
    backend = InsecureKeyring()
    with pytest.raises(credentials.CredentialError):
        credentials.save_credentials("12345", "secret", backend)
    assert not backend.values
    with pytest.raises(credentials.CredentialError):
        credentials.load_credentials(backend)


def test_corrupt_stored_credentials_are_reported(monkeypatch):
    monkeypatch.setenv("OSU_PICKER_KEYRING_SERVICE", "test-service")
    backend = FakeKeyring()
    backend.set_password("test-service", credentials.ACCOUNT, "{broken")
    with pytest.raises(credentials.CredentialError):
        credentials.load_credentials(backend)
    status = credentials.status(backend)
    assert status.configured is False
    assert "поврежд" in status.error


def test_environment_credentials_take_precedence(monkeypatch):
    monkeypatch.setenv("OSU_CLIENT_ID", "env-id")
    monkeypatch.setenv("OSU_CLIENT_SECRET", "env-secret")
    monkeypatch.setattr(
        credentials,
        "load_credentials",
        lambda backend=None: pytest.fail("saved credentials must not be read"),
    )
    api = config.load_api_config()
    assert api.client_id == "env-id"
    assert api.client_secret == "env-secret"
    assert api.source == "environment"


def test_saved_credentials_are_used_only_without_environment(monkeypatch):
    monkeypatch.delenv("OSU_CLIENT_ID", raising=False)
    monkeypatch.delenv("OSU_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(
        credentials,
        "load_credentials",
        lambda backend=None: credentials.Credentials("local-id", "local-secret"),
    )
    api = config.load_api_config()
    assert api.configured is True
    assert api.source == "local"
    assert api.client_secret == "local-secret"


def test_partial_environment_never_mixes_with_saved_credentials(monkeypatch):
    monkeypatch.setenv("OSU_CLIENT_ID", "env-id")
    monkeypatch.delenv("OSU_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(
        credentials,
        "load_credentials",
        lambda backend=None: credentials.Credentials("local-id", "local-secret"),
    )
    with pytest.raises(ValueError, match="вместе"):
        config.load_api_config()


def test_keyring_secret_is_json_and_not_stored_by_config(monkeypatch):
    monkeypatch.setenv("OSU_PICKER_KEYRING_SERVICE", "test-service")
    backend = FakeKeyring()
    credentials.save_credentials("12345", "super-secret-value", backend)
    raw = next(iter(backend.values.values()))
    assert json.loads(raw)["client_secret"] == "super-secret-value"
    # ApiConfig must keep the secret out of repr/string diagnostics.
    api = config.ApiConfig(
        client_id="12345",
        client_secret="super-secret-value",
        source="local",
    )
    assert "super-secret-value" not in repr(api)
    assert "super-secret-value" not in str(api)


def test_secret_is_not_in_sqlite_export_or_database_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("OSU_PICKER_KEYRING_SERVICE", "test-service")
    backend = FakeKeyring()
    credentials.save_credentials("12345", "super-secret-value", backend)
    store = Storage(str(tmp_path / "app.db"))
    store.set_preference("note", "ordinary")
    exported = json.dumps(store.export_user_data(), ensure_ascii=False)
    assert "super-secret-value" not in exported
    assert b"super-secret-value" not in (tmp_path / "app.db").read_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows release backend")
def test_release_uses_windows_credential_manager():
    assert "WinVaultKeyring" in credentials.backend_description()
