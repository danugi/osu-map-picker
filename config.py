"""Central configuration for Personal osu! Map Picker.

All default values live here so they are not duplicated across modules.
Secrets are read from environment variables / .env and are never written
to the database, logs, or exported settings.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True inside a PyInstaller onedir build."""

    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory beside the executable, or the source checkout."""

    if is_frozen():
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def is_portable_build() -> bool:
    """Whether first-run desktop setup should be shown.

    Source/development runs and hosted deployments are not prompted.  The
    environment switch is useful for testing a source checkout as a portable
    build; it is documented as a local-only release switch.
    """

    if os.getenv("OSU_PICKER_HOSTED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if is_frozen():
        return True
    return os.getenv("OSU_PICKER_PORTABLE", "").strip() in {"1", "true", "yes"}


# .env is loaded from beside the executable (portable) or the checkout.  It is
# deliberately not read from PyInstaller's temporary extraction directory.
load_dotenv(app_dir() / ".env")

APP_NAME = "Personal osu! Map Picker"
APP_VERSION = "0.4.0"
SETTINGS_SCHEMA_VERSION = 1
DB_SCHEMA_VERSION = 3

OSU_SITE = "https://osu.ppy.sh"
OSU_API_BASE = "https://osu.ppy.sh/api/v2"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
BEATMAPSET_URL = OSU_SITE + "/beatmapsets/{id}"

# --- API defaults ---------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_MIN_REQUEST_INTERVAL = 1.1  # osu! asks for <= 60 requests/minute
DEFAULT_MAX_RETRIES = 2
DEFAULT_CACHE_TTL_MINUTES = 30
# An explicit online search also has an overall time budget. The deadline is
# checked between network operations, so it cannot interrupt an in-flight
# request; worst-case overrun is bounded by one request chain (timeout and
# retries) plus local ranking.
DEFAULT_DISCOVERY_DEADLINE_SECONDS = 120.0

# --- Background analysis worker ------------------------------------------
# One daemon thread analyzes missing/stale .osu files through a persistent
# SQLite queue. Network work is never performed inside a Streamlit rerun.
DEFAULT_WORKER_POLL_SECONDS = 2.0
DEFAULT_ANALYSIS_BATCH_SIZE = 8
DEFAULT_ANALYSIS_CONCURRENCY = 4
DEFAULT_ANALYSIS_MAX_ATTEMPTS = 3
DEFAULT_ANALYSIS_LEASE_SECONDS = 180.0
DEFAULT_ANALYSIS_RETRY_MINUTES = (5, 30)
DEFAULT_WORKER_DOWNLOAD_INTERVAL_SECONDS = 1.0
DEFAULT_AUTO_DISCOVERY_INTERVAL_MINUTES = 60
DEFAULT_AUTO_DISCOVERY_MAX_REQUESTS = 4
DEFAULT_AUTO_DISCOVERY_DEADLINE_SECONDS = 60.0

# --- Filter defaults (the user's starting profile) ------------------------
DEFAULT_MODES = ("osu",)
DEFAULT_STATUSES = ("ranked", "loved", "qualified")
DEFAULT_STAR_MIN = 4.0
DEFAULT_STAR_MAX = 6.9
DEFAULT_STAR_MODE = "strict"  # strict | soft | off
DEFAULT_STAR_TOLERANCE = 0.3
DEFAULT_DURATION_FIELD = "total_length"
DEFAULT_DURATION_MIN = None
DEFAULT_DURATION_MAX = 180
DEFAULT_EXCLUDE_NSFW = True
DEFAULT_HIDE_SEEN = False
DEFAULT_HIDE_PLAYED = False
DEFAULT_HIDE_FAVORITES = False

# --- Discovery defaults ---------------------------------------------------
# The request budget is shared by search pages and difficulty attributes.
# Four requests are reserved for attributes, so a default search spends at
# most eight pages on discovery. osu! asks for <= 60 requests/minute, which
# makes request count the dominant cost even on a fast connection.
DEFAULT_MAX_REQUESTS = 12
DEFAULT_MAX_PAGES_PER_STRATEGY = 2
DEFAULT_MAX_CANDIDATES = 400
DEFAULT_FETCH_ATTRIBUTES_LIMIT = 4
# Public .osu files are downloaded only when the active preset needs sequence
# evidence (Streams/Bursts). The cap is separate from the OAuth API budget.
DEFAULT_FETCH_SEQUENCES_LIMIT = 20
DEFAULT_USE_OLDEST = True
DEFAULT_USE_RATING = True
DEFAULT_USE_POPULAR = True
DEFAULT_EXPAND_SEEDS = True

# --- Ranking defaults -----------------------------------------------------
# Each factor: weight (0..5) and enabled flag. Weights are relative.
DEFAULT_RANKING_FACTORS: dict[str, dict[str, object]] = {
    "mapping_fit": {"weight": 1.3, "enabled": True},
    "farm_nm": {"weight": 0.0, "enabled": False},
    "difficulty": {"weight": 1.0, "enabled": True},
    "taste": {"weight": 1.1, "enabled": True},
    "feedback": {"weight": 1.4, "enabled": True},
    "music": {"weight": 0.5, "enabled": True},
    "length": {"weight": 0.35, "enabled": True},
    "novelty": {"weight": 0.7, "enabled": True},
    "community": {"weight": 0.25, "enabled": True},
    "repeat_penalty": {"weight": 0.8, "enabled": True},
}

FACTOR_DESCRIPTIONS = {
    "mapping_fit": "Соответствие выбранным навыкам (универсальные skillsets).",
    "farm_nm": "Эвристика NM-фарма: короткая aim-карта около вашего комфорта.",
    "difficulty": "Близость звёздности к комфортной точке пресета.",
    "taste": "Сходство с артистами, мапперами и жанрами, которые вы сами оценивали.",
    "feedback": "Ваши прямые оценки этой карты (кайф, скучно, музыка, сложность).",
    "music": "Соответствие выбранным жанрам и музыкальному профилю.",
    "length": "Близость длительности к предпочитаемому окну.",
    "novelty": "Новые/незнакомые мапперы и свежесть публикации.",
    "community": "Рейтинг и популярность карты (слабый сигнал качества).",
    "repeat_penalty": "Штраф за уже показанные и похожие на них карты.",
}

DEFAULT_DIVERSITY = 0.45
DEFAULT_MAX_PER_ARTIST = 2
DEFAULT_MAX_PER_MAPPER = 3
DEFAULT_COMFORT_STARS = 5.4
DEFAULT_COMFORT_WIDTH = 0.9
DEFAULT_IDEAL_LENGTH = 140
DEFAULT_LENGTH_TOLERANCE = 70
DEFAULT_FARM_TARGET_SECONDS = 120
DEFAULT_FARM_TOLERANCE_SECONDS = 45

# A specialized preset may name up to three target skillsets. A map must be
# above this share for at least one target before it can be ranked. The check
# is strict (>), so exactly 50% never passes. The default is deliberately
# above 50%; lower values are rejected by ranking validation.
DEFAULT_RELEVANCE_THRESHOLD = 0.55

# --- Paths ----------------------------------------------------------------
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


class DataDirectoryError(ValueError):
    """Data directory cannot be created/verified, including the OS cause."""

    def __init__(
        self,
        path: Path,
        operation: str,
        cause: OSError | None = None,
        *,
        detail: str | None = None,
    ):
        self.path = Path(path)
        self.operation = operation
        self.cause = cause
        explanation = (
            detail
            if detail is not None
            else os_error_detail(cause)
            if cause is not None
            else "неизвестно"
        )
        super().__init__(
            f"Папка данных недоступна: {self.path}. Операция: {operation}. "
            f"Причина: {explanation}. Существующая база не будет перезаписана "
            "или заменена пустой."
        )


def os_error_detail(exc: OSError | BaseException) -> str:
    """Human-readable OS error including errno/WinError; never secrets."""

    parts: list[str] = []
    winerror = getattr(exc, "winerror", None)
    errno_value = getattr(exc, "errno", None)
    if winerror is not None:
        parts.append(f"WinError {winerror}")
    if errno_value is not None:
        parts.append(f"errno {errno_value}")
    text = str(exc).strip()
    if text:
        parts.append(text)
    return "; ".join(parts) or type(exc).__name__


def portable_data_dir() -> Path:
    """Stable per-user location for packaged builds.

    Never use ``sys._MEIPASS`` here: that directory is deleted by PyInstaller
    when the process exits and must not contain persistent user data.
    """

    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        base = Path(local_appdata)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "OsuMapPicker"


def data_dir() -> Path:
    raw = os.getenv("OSU_PICKER_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    if is_frozen():
        return portable_data_dir()
    return DEFAULT_DATA_DIR


def ensure_data_dir() -> Path:
    """Create and write-probe the configured data directory.

    This is deliberately not a fallback mechanism. If a path exists but is not
    a directory, is read-only, or cannot be written, the caller receives the
    real OS cause. The database/backups inside an existing directory are never
    touched or replaced.
    """

    path = data_dir()
    if path.exists() and not path.is_dir():
        raise DataDirectoryError(
            path,
            "проверка типа пути",
            detail="путь существует, но не является папкой",
        )
    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DataDirectoryError(path, "создание папки", exc) from exc
    if not path.is_dir():
        raise DataDirectoryError(path, "проверка папки")
    probe = (
        path
        / f".osu-map-picker-write-test-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        with probe.open("x", encoding="utf-8") as handle:
            handle.write("write probe")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise DataDirectoryError(path, "проверка записи", exc) from exc
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def db_path() -> Path:
    raw = os.getenv("OSU_PICKER_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_dir() / "osu_picker.db"


def backups_dir() -> Path:
    return data_dir() / "backups"


def logs_dir() -> Path:
    return data_dir() / "logs"


@dataclass(frozen=True)
class ApiConfig:
    """OAuth client-credentials configuration. Never log `client_secret`."""

    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    timeout: float = DEFAULT_REQUEST_TIMEOUT
    min_interval: float = DEFAULT_MIN_REQUEST_INTERVAL
    max_retries: int = DEFAULT_MAX_RETRIES
    api_base: str = OSU_API_BASE
    token_url: str = OSU_TOKEN_URL
    # "environment", "local" or "none". The secret itself is never included
    # in diagnostics or error text.
    source: str = "none"
    credential_error: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    def validate(self) -> None:
        if not self.configured:
            raise ValueError(
                "Client ID и Client Secret должны быть заданы в Настройки → "
                "API или в OSU_CLIENT_ID / OSU_CLIENT_SECRET."
            )
        if self.timeout <= 0:
            raise ValueError("timeout должен быть больше нуля")
        if self.min_interval < 0:
            raise ValueError("min_interval не может быть отрицательным")
        if self.max_retries < 0:
            raise ValueError("max_retries не может быть отрицательным")


def _credential_source(
    env_id: str, env_secret: str
) -> tuple[str, str, str, str]:
    """Resolve explicit env precedence over locally saved credentials.

    Returns ``(client_id, client_secret, source, error)``.  A complete
    environment pair always wins.  A partial environment pair is an explicit
    configuration error; it is never silently completed from Credential
    Manager.
    """

    if env_id and env_secret:
        return env_id, env_secret, "environment", ""
    if env_id or env_secret:
        raise ValueError(
            "OSU_CLIENT_ID и OSU_CLIENT_SECRET должны быть заданы вместе "
            "(или оба удалены, чтобы использовать локальные credentials)."
        )
    try:
        import credentials as credential_store
    except ImportError:
        return "", "", "none", ""
    try:
        saved = credential_store.load_credentials()
    except credential_store.CredentialError as exc:
        return "", "", "none", str(exc)
    if saved is None:
        return "", "", "none", ""
    return saved.client_id, saved.client_secret, "local", ""


def load_api_config() -> ApiConfig:
    def _float(name: str, default: float) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} должен быть числом") from exc

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} должен быть целым числом") from exc

    env_id = os.getenv("OSU_CLIENT_ID", "").strip()
    env_secret = os.getenv("OSU_CLIENT_SECRET", "").strip()
    client_id, client_secret, source, credential_error = _credential_source(
        env_id, env_secret
    )
    return ApiConfig(
        client_id=client_id,
        client_secret=client_secret,
        timeout=_float("OSU_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT),
        min_interval=_float("OSU_MIN_INTERVAL_SECONDS", DEFAULT_MIN_REQUEST_INTERVAL),
        max_retries=_int("OSU_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        source=source,
        credential_error=credential_error,
    )


def cache_ttl_minutes() -> int:
    raw = os.getenv("OSU_CACHE_TTL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_MINUTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL_MINUTES
