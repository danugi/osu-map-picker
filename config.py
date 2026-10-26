"""Central configuration for Personal osu! Map Picker.

All default values live here so they are not duplicated across modules.
Secrets are read from environment variables / .env and are never written
to the database, logs, or exported settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

APP_NAME = "Personal osu! Map Picker"
APP_VERSION = "0.4.0"
SETTINGS_SCHEMA_VERSION = 1
DB_SCHEMA_VERSION = 1

OSU_SITE = "https://osu.ppy.sh"
OSU_API_BASE = "https://osu.ppy.sh/api/v2"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
BEATMAPSET_URL = OSU_SITE + "/beatmapsets/{id}"

# --- API defaults ---------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_MIN_REQUEST_INTERVAL = 1.1  # osu! asks for <= 60 requests/minute
DEFAULT_MAX_RETRIES = 2
DEFAULT_CACHE_TTL_MINUTES = 30

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

# A specialized preset may name up to three target skillsets. A map must be
# above this share for at least one target before it can be ranked. The check
# is strict (>), so exactly 50% never passes. The default is deliberately
# above 50%; lower values are rejected by ranking validation.
DEFAULT_RELEVANCE_THRESHOLD = 0.55

# --- Paths ----------------------------------------------------------------
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def data_dir() -> Path:
    raw = os.getenv("OSU_PICKER_DATA_DIR", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR


def db_path() -> Path:
    raw = os.getenv("OSU_PICKER_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_dir() / "osu_picker.db"


def backups_dir() -> Path:
    return data_dir() / "backups"


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

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())

    def validate(self) -> None:
        if not self.configured:
            raise ValueError(
                "OSU_CLIENT_ID и OSU_CLIENT_SECRET должны быть заданы в .env"
            )
        if self.timeout <= 0:
            raise ValueError("timeout должен быть больше нуля")
        if self.min_interval < 0:
            raise ValueError("min_interval не может быть отрицательным")
        if self.max_retries < 0:
            raise ValueError("max_retries не может быть отрицательным")


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

    return ApiConfig(
        client_id=os.getenv("OSU_CLIENT_ID", "").strip(),
        client_secret=os.getenv("OSU_CLIENT_SECRET", "").strip(),
        timeout=_float("OSU_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT),
        min_interval=_float("OSU_MIN_INTERVAL_SECONDS", DEFAULT_MIN_REQUEST_INTERVAL),
        max_retries=_int("OSU_MAX_RETRIES", DEFAULT_MAX_RETRIES),
    )


def cache_ttl_minutes() -> int:
    raw = os.getenv("OSU_CACHE_TTL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_MINUTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL_MINUTES
