"""Typed data models shared by the API client, discovery, ranking and UI.

The project uses dataclasses instead of a validation framework to keep the
dependency list small and the data flow easy to follow. Configuration
objects expose ``to_dict``/``from_dict`` and are validated by their owning
modules (``filters``, ``ranking``, ``presets``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Iterable

import config

MODES = ("osu", "taiko", "catch", "mania")
MODE_IDS = {"osu": 0, "taiko": 1, "catch": 2, "mania": 3}
STATUSES = ("ranked", "qualified", "loved", "pending", "wip", "graveyard")
STATUS_LABELS = {
    "ranked": "Ranked",
    "qualified": "Qualified",
    "loved": "Loved",
    "pending": "Pending",
    "wip": "WIP",
    "graveyard": "Graveyard",
}
STAR_MODES = ("strict", "soft", "off")
DURATION_FIELDS = ("total_length", "hit_length")
SORTS = (
    "ranked_desc",
    "ranked_asc",
    "rating_desc",
    "favourites_desc",
    "plays_desc",
    "difficulty_desc",
    "difficulty_asc",
)

GENRES = {
    1: "Unspecified",
    2: "Video Game",
    3: "Anime",
    4: "Rock",
    5: "Pop",
    6: "Other",
    7: "Novelty",
    9: "Hip Hop",
    10: "Electronic",
    11: "Metal",
    12: "Classical",
    13: "Folk",
    14: "Jazz",
}

VERDICTS = (
    "like",
    "fine",
    "boring",
    "music_dislike",
    "mapping_dislike",
    "too_hard",
    "too_easy",
    "played",
    "hide",
)

OUTCOME_FIELDS = (
    "beatmapset_id",
    "title",
    "artist",
    "creator",
    "difficulty_rating",
    "duration",
    "reasons",
    "sources",
    "score",
    "rank",
    "factors",
    "traits",
)

_VALID_FACTOR_KEYS = tuple(config.DEFAULT_RANKING_FACTORS)


class ModelError(ValueError):
    """Raised when stored or imported data has an invalid shape."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _check_keys(data: dict[str, Any], cls: type, path: str) -> None:
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ModelError(f"{path}: неизвестные поля: {', '.join(unknown)}")


def _as_str_list(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    raise ModelError(f"{path}: ожидался список строк")


def _as_int_list(value: Any, path: str) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ModelError(f"{path}: ожидался список целых чисел")
    result: list[int] = []
    for part in parts:
        try:
            result.append(int(part))
        except (TypeError, ValueError) as exc:
            raise ModelError(f"{path}: '{part}' не является целым числом") from exc
    return result


def _opt_float(value: Any, path: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ModelError(f"{path}: ожидалось число")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ModelError(f"{path}: ожидалось число") from exc


def _opt_int(value: Any, path: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ModelError(f"{path}: ожидалось целое число")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ModelError(f"{path}: ожидалось целое число") from exc


def _bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ModelError(f"{path}: ожидалось true/false")


# --------------------------------------------------------------------------
# osu! data
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Beatmap:
    """A single difficulty. This is where star rating and length live."""

    id: int
    beatmapset_id: int
    mode: str = "osu"
    version: str = ""
    difficulty_rating: float | None = None
    total_length: int | None = None
    hit_length: int | None = None
    bpm: float | None = None
    ar: float | None = None
    od: float | None = None
    cs: float | None = None
    hp: float | None = None
    max_combo: int | None = None
    count_circles: int | None = None
    count_sliders: int | None = None
    count_spinners: int | None = None
    status: str | None = None
    url: str | None = None
    owners: tuple[str, ...] = ()

    @property
    def object_count(self) -> int | None:
        parts = (self.count_circles, self.count_sliders, self.count_spinners)
        known = [p for p in parts if p is not None]
        return sum(known) if known else None

    @property
    def url_official(self) -> str:
        return f"{config.OSU_SITE}/b/{self.id}"

    def duration(self, field_name: str) -> int | None:
        return self.total_length if field_name == "total_length" else self.hit_length


@dataclass(frozen=True, slots=True)
class Beatmapset:
    """Beatmapset metadata returned by the official API."""

    id: int
    artist: str = ""
    artist_unicode: str = ""
    title: str = ""
    title_unicode: str = ""
    creator: str = ""
    status: str = "unknown"
    bpm: float | None = None
    favourite_count: int | None = None
    play_count: int | None = None
    rating: float | None = None
    genre_id: int | None = None
    language_id: int | None = None
    tags: tuple[str, ...] = ()
    nsfw: bool = False
    video: bool = False
    storyboard: bool = False
    preview_url: str = ""
    cover_url: str = ""
    ranked_date: str | None = None
    submitted_date: str | None = None
    last_updated: str | None = None
    beatmaps: tuple[Beatmap, ...] = ()
    source: str = "osu_api"
    fetched_at: str = field(default_factory=now_iso)

    @property
    def url(self) -> str:
        return config.BEATMAPSET_URL.format(id=self.id)

    @property
    def display_title(self) -> str:
        return self.title_unicode or self.title or "Без названия"

    @property
    def display_artist(self) -> str:
        return self.artist_unicode or self.artist or "Неизвестный исполнитель"

    @property
    def genre_name(self) -> str | None:
        return GENRES.get(self.genre_id) if self.genre_id else None

    def difficulties(self, mode: str | None = None) -> tuple[Beatmap, ...]:
        if mode is None:
            return self.beatmaps
        return tuple(b for b in self.beatmaps if b.mode == mode)


@dataclass(frozen=True, slots=True)
class DifficultyAttributes:
    """Optional output of POST /beatmaps/{id}/attributes."""

    beatmap_id: int
    star_rating: float | None = None
    aim_difficulty: float | None = None
    speed_difficulty: float | None = None
    slider_factor: float | None = None
    max_combo: int | None = None
    source: str = "osu_api_attributes"
    fetched_at: str = field(default_factory=now_iso)

    @property
    def aim_share(self) -> float | None:
        if self.aim_difficulty is None or self.speed_difficulty is None:
            return None
        total = self.aim_difficulty + self.speed_difficulty
        if total <= 0:
            return None
        return max(0.0, min(1.0, self.aim_difficulty / total))


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Parameters sent to /beatmapsets/search.

    The official documentation marks this endpoint as ``TODO`` and only
    documents ``cursor_string``. The other names are isolated here and are
    based on the public osu! website search and the maintained ossapi
    wrapper; see README ("API uncertainty").
    """

    mode: str = "osu"
    status: str = "ranked"
    sort: str = "ranked_desc"
    query: str | None = None
    nsfw: bool = False
    cursor: dict[str, Any] | None = None
    cursor_string: str | None = None

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "m": MODE_IDS[self.mode],
            "s": self.status,
            "sort": self.sort,
        }
        if self.query:
            params["q"] = self.query
        if not self.nsfw:
            params["nsfw"] = "hide"
        if self.cursor_string:
            params["cursor_string"] = self.cursor_string
        if self.cursor:
            for key, value in self.cursor.items():
                if value is not None:
                    params[f"cursor[{key}]"] = value
        return params

    def cache_params(self) -> dict[str, Any]:
        params = {k: v for k, v in self.to_params().items() if not k.startswith("cursor")}
        return params


@dataclass(frozen=True, slots=True)
class SearchPage:
    beatmapsets: tuple[Beatmapset, ...]
    cursor: dict[str, Any] | None = None
    cursor_string: str | None = None
    total: int | None = None
    recommended_difficulty: float | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    raw_count: int = 0

    @property
    def has_more(self) -> bool:
        return bool(self.cursor_string or self.cursor)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class Filters:
    modes: list[str] = field(default_factory=lambda: list(config.DEFAULT_MODES))
    statuses: list[str] = field(default_factory=lambda: list(config.DEFAULT_STATUSES))
    stars_min: float | None = config.DEFAULT_STAR_MIN
    stars_max: float | None = config.DEFAULT_STAR_MAX
    star_mode: str = config.DEFAULT_STAR_MODE
    star_tolerance: float = config.DEFAULT_STAR_TOLERANCE
    duration_field: str = config.DEFAULT_DURATION_FIELD
    duration_min: float | None = config.DEFAULT_DURATION_MIN
    duration_max: float | None = config.DEFAULT_DURATION_MAX
    bpm_min: float | None = None
    bpm_max: float | None = None
    ar_min: float | None = None
    ar_max: float | None = None
    od_min: float | None = None
    od_max: float | None = None
    cs_min: float | None = None
    cs_max: float | None = None
    hp_min: float | None = None
    hp_max: float | None = None
    published_after: str | None = None
    published_before: str | None = None
    include_artists: list[str] = field(default_factory=list)
    exclude_artists: list[str] = field(default_factory=list)
    include_mappers: list[str] = field(default_factory=list)
    exclude_mappers: list[str] = field(default_factory=list)
    include_genres: list[int] = field(default_factory=list)
    exclude_genres: list[int] = field(default_factory=list)
    exclude_nsfw: bool = config.DEFAULT_EXCLUDE_NSFW
    hide_seen: bool = config.DEFAULT_HIDE_SEEN
    hide_played: bool = config.DEFAULT_HIDE_PLAYED
    hide_favorites: bool = config.DEFAULT_HIDE_FAVORITES
    only_favorites: bool = False
    exclude_beatmapset_ids: list[int] = field(default_factory=list)
    query: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str = "filters") -> "Filters":
        if not isinstance(data, dict):
            raise ModelError(f"{path}: ожидался объект")
        _check_keys(data, cls, path)
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            p = f"{path}.{f.name}"
            if f.name in {"modes", "statuses", "include_artists", "exclude_artists",
                          "include_mappers", "exclude_mappers"}:
                kwargs[f.name] = _as_str_list(value, p)
            elif f.name in {"include_genres", "exclude_genres", "exclude_beatmapset_ids"}:
                kwargs[f.name] = _as_int_list(value, p)
            elif f.name in {"stars_min", "stars_max", "star_tolerance", "duration_min",
                            "duration_max", "bpm_min", "bpm_max", "ar_min", "ar_max",
                            "od_min", "od_max", "cs_min", "cs_max", "hp_min", "hp_max"}:
                kwargs[f.name] = _opt_float(value, p)
            elif f.name in {"star_mode", "duration_field", "query"}:
                if value is None:
                    kwargs[f.name] = ""
                elif not isinstance(value, str):
                    raise ModelError(f"{p}: ожидалась строка")
                else:
                    kwargs[f.name] = value
            elif f.name in {"published_after", "published_before"}:
                if value in (None, ""):
                    kwargs[f.name] = None
                elif not isinstance(value, str):
                    raise ModelError(f"{p}: ожидалась дата YYYY-MM-DD")
                else:
                    kwargs[f.name] = value
            else:
                kwargs[f.name] = _bool(value, p)
        return cls(**kwargs)


@dataclass
class FactorConfig:
    weight: float
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"weight": self.weight, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Any, path: str) -> "FactorConfig":
        if not isinstance(data, dict):
            raise ModelError(f"{path}: ожидался объект {{weight, enabled}}")
        unknown = set(data) - {"weight", "enabled"}
        if unknown:
            raise ModelError(f"{path}: неизвестные поля: {', '.join(sorted(unknown))}")
        weight = _opt_float(data.get("weight"), f"{path}.weight")
        if weight is None:
            raise ModelError(f"{path}.weight: обязательное число")
        enabled = _bool(data.get("enabled", True), f"{path}.enabled")
        return cls(weight=weight, enabled=enabled)


@dataclass
class RankingSettings:
    factors: dict[str, FactorConfig] = field(
        default_factory=lambda: {
            key: FactorConfig(float(value["weight"]), bool(value["enabled"]))
            for key, value in config.DEFAULT_RANKING_FACTORS.items()
        }
    )
    diversity: float = config.DEFAULT_DIVERSITY
    max_per_artist: int = config.DEFAULT_MAX_PER_ARTIST
    max_per_mapper: int = config.DEFAULT_MAX_PER_MAPPER
    comfort_stars: float = config.DEFAULT_COMFORT_STARS
    comfort_width: float = config.DEFAULT_COMFORT_WIDTH
    ideal_length: int = config.DEFAULT_IDEAL_LENGTH
    length_tolerance: int = config.DEFAULT_LENGTH_TOLERANCE
    preferred_genres: list[int] = field(default_factory=list)
    # Legacy aim-profile targets, kept so old presets and exports keep working.
    trait_targets: dict[str, float] = field(default_factory=dict)
    # Universal skillset weights, e.g. {"aim_classic": 1.0, "stream_consistency": 0.5}.
    skill_weights: dict[str, float] = field(default_factory=dict)
    # Optional mandatory pre-ranking gate for specialized presets. Each id is
    # a skillset whose measured/proxy score must exceed relevance_threshold
    # for at least one target. Empty means "no gate" (Mixed Aim, All-rounder).
    relevance_targets: list[str] = field(default_factory=list)
    relevance_threshold: float = config.DEFAULT_RELEVANCE_THRESHOLD
    farm_target_seconds: int = config.DEFAULT_FARM_TARGET_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "factors": {k: v.to_dict() for k, v in self.factors.items()},
            "diversity": self.diversity,
            "max_per_artist": self.max_per_artist,
            "max_per_mapper": self.max_per_mapper,
            "comfort_stars": self.comfort_stars,
            "comfort_width": self.comfort_width,
            "ideal_length": self.ideal_length,
            "length_tolerance": self.length_tolerance,
            "preferred_genres": list(self.preferred_genres),
            "trait_targets": dict(self.trait_targets),
            "skill_weights": dict(self.skill_weights),
            "relevance_targets": list(self.relevance_targets),
            "relevance_threshold": self.relevance_threshold,
            "farm_target_seconds": self.farm_target_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str = "ranking") -> "RankingSettings":
        if not isinstance(data, dict):
            raise ModelError(f"{path}: ожидался объект")
        _check_keys(data, cls, path)
        factors_data = data.get("factors", {})
        if not isinstance(factors_data, dict):
            raise ModelError(f"{path}.factors: ожидался объект")
        unknown = set(factors_data) - set(_VALID_FACTOR_KEYS)
        if unknown:
            raise ModelError(
                f"{path}.factors: неизвестные факторы: {', '.join(sorted(unknown))}"
            )
        factors = {
            key: FactorConfig.from_dict(value, f"{path}.factors.{key}")
            for key, value in factors_data.items()
        }
        genres = _as_int_list(data.get("preferred_genres", []), f"{path}.preferred_genres")
        targets_raw = data.get("trait_targets", {}) or {}
        if not isinstance(targets_raw, dict):
            raise ModelError(f"{path}.trait_targets: ожидался объект")
        targets: dict[str, float] = {}
        for key, value in targets_raw.items():
            number = _opt_float(value, f"{path}.trait_targets.{key}")
            if number is None:
                raise ModelError(f"{path}.trait_targets.{key}: ожидалось число")
            targets[str(key)] = number
        skills_raw = data.get("skill_weights", {}) or {}
        if not isinstance(skills_raw, dict):
            raise ModelError(f"{path}.skill_weights: ожидался объект")
        skills: dict[str, float] = {}
        for key, value in skills_raw.items():
            number = _opt_float(value, f"{path}.skill_weights.{key}")
            if number is None:
                raise ModelError(f"{path}.skill_weights.{key}: ожидалось число")
            skills[str(key)] = number
        relevance_targets = _as_str_list(
            data.get("relevance_targets", []),
            f"{path}.relevance_targets",
        )
        threshold = _opt_float(
            data.get("relevance_threshold", config.DEFAULT_RELEVANCE_THRESHOLD),
            f"{path}.relevance_threshold",
        )
        if threshold is None:
            threshold = config.DEFAULT_RELEVANCE_THRESHOLD
        return cls(
            factors=factors,
            diversity=float(data.get("diversity", config.DEFAULT_DIVERSITY)),
            max_per_artist=int(data.get("max_per_artist", config.DEFAULT_MAX_PER_ARTIST)),
            max_per_mapper=int(data.get("max_per_mapper", config.DEFAULT_MAX_PER_MAPPER)),
            comfort_stars=float(data.get("comfort_stars", config.DEFAULT_COMFORT_STARS)),
            comfort_width=float(data.get("comfort_width", config.DEFAULT_COMFORT_WIDTH)),
            ideal_length=int(data.get("ideal_length", config.DEFAULT_IDEAL_LENGTH)),
            length_tolerance=int(
                data.get("length_tolerance", config.DEFAULT_LENGTH_TOLERANCE)
            ),
            preferred_genres=genres,
            trait_targets=targets,
            skill_weights=skills,
            relevance_targets=relevance_targets,
            relevance_threshold=threshold,
            farm_target_seconds=int(
                data.get("farm_target_seconds", config.DEFAULT_FARM_TARGET_SECONDS)
            ),
        )


@dataclass
class DiscoverySettings:
    max_requests: int = config.DEFAULT_MAX_REQUESTS
    max_pages_per_strategy: int = config.DEFAULT_MAX_PAGES_PER_STRATEGY
    max_candidates: int = config.DEFAULT_MAX_CANDIDATES
    fetch_attributes_limit: int = config.DEFAULT_FETCH_ATTRIBUTES_LIMIT
    fetch_sequences_limit: int = config.DEFAULT_FETCH_SEQUENCES_LIMIT
    cache_ttl_minutes: int = config.DEFAULT_CACHE_TTL_MINUTES
    use_oldest: bool = config.DEFAULT_USE_OLDEST
    use_rating: bool = config.DEFAULT_USE_RATING
    use_popular: bool = config.DEFAULT_USE_POPULAR
    expand_seeds: bool = config.DEFAULT_EXPAND_SEEDS

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], path: str = "discovery"
    ) -> "DiscoverySettings":
        if not isinstance(data, dict):
            raise ModelError(f"{path}: ожидался объект")
        _check_keys(data, cls, path)
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            if isinstance(getattr(cls(), f.name), bool):
                kwargs[f.name] = _bool(data[f.name], f"{path}.{f.name}")
            else:
                number = _opt_int(data[f.name], f"{path}.{f.name}")
                if number is None:
                    raise ModelError(f"{path}.{f.name}: обязательное число")
                kwargs[f.name] = number
        return cls(**kwargs)


@dataclass
class PresetConfig:
    name: str
    description: str = ""
    filters: Filters = field(default_factory=Filters)
    ranking: RankingSettings = field(default_factory=RankingSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    schema_version: int = config.SETTINGS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "filters": self.filters.to_dict(),
            "ranking": self.ranking.to_dict(),
            "discovery": self.discovery.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str = "preset") -> "PresetConfig":
        if not isinstance(data, dict):
            raise ModelError(f"{path}: ожидался объект")
        _check_keys(data, cls, path)
        version = data.get("schema_version", config.SETTINGS_SCHEMA_VERSION)
        if version != config.SETTINGS_SCHEMA_VERSION:
            raise ModelError(
                f"{path}.schema_version: поддерживается только "
                f"{config.SETTINGS_SCHEMA_VERSION}, получено {version!r}"
            )
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ModelError(f"{path}.name: требуется непустое имя")
        description = data.get("description", "")
        if not isinstance(description, str):
            raise ModelError(f"{path}.description: ожидалась строка")
        return cls(
            name=name.strip(),
            description=description,
            filters=Filters.from_dict(data.get("filters", {}) or {}, f"{path}.filters"),
            ranking=RankingSettings.from_dict(
                data.get("ranking", {}) or {}, f"{path}.ranking"
            ),
            discovery=DiscoverySettings.from_dict(
                data.get("discovery", {}) or {}, f"{path}.discovery"
            ),
            schema_version=version,
        )


@dataclass(frozen=True, slots=True)
class Preset:
    id: int
    config: PresetConfig
    is_builtin: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def name(self) -> str:
        return self.config.name


# --------------------------------------------------------------------------
# Discovery / ranking results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BeatmapFeatures:
    """Measured mapping features with explicit provenance.

    Scores are only produced for features we can actually derive from the
    API metadata, object counts, circles/OD/AR and mapper tags. Geometry
    (wide/flow/awkward angles) is *not* inferred from stars or BPM; those
    skills stay limited until local ``.osu`` analysis is available.
    """

    scores: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    confidence: str = "none"  # none | low | medium | high
    notes: tuple[str, ...] = ()

    def get(self, key: str) -> float | None:
        return self.scores.get(key)

    @property
    def available(self) -> bool:
        return bool(self.scores)


# Backwards-compatible name used by older presets/tests/imports.
AimTraits = BeatmapFeatures


@dataclass(frozen=True, slots=True)
class PatternChain:
    """One maximal run of fast, compact, rhythmically even hit objects."""

    kind: str  # stream | burst
    start_index: int
    length: int
    start_ms: int
    end_ms: int
    median_gap_ms: float
    rhythm_quality: float
    compact_share: float


@dataclass(frozen=True, slots=True)
class SequenceAnalysis:
    """Sequence evidence extracted from one difficulty's hit objects.

    Streams and bursts are disjoint maximal chains: a chain of 10+ notes is a
    stream, a chain of 5-9 notes is a burst. The same note can never be in both.
    """

    stream_chains: tuple[PatternChain, ...] = ()
    burst_chains: tuple[PatternChain, ...] = ()
    analyzed_notes: int = 0
    total_notes: int = 0
    truncated: bool = False

    @property
    def stream_notes(self) -> int:
        return sum(chain.length for chain in self.stream_chains)

    @property
    def burst_notes(self) -> int:
        return sum(chain.length for chain in self.burst_chains)

    @property
    def complete(self) -> bool:
        return not self.truncated


@dataclass(frozen=True, slots=True)
class SkillScore:
    """Fit of one skillset for one beatmap, with evidence and confidence."""

    skill_id: str
    name: str
    score: float | None
    confidence: str = "none"  # none | low | medium | high
    support: str = "limited"  # measured | proxy | limited
    evidence: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    # Machine-readable provenance of the features used for this score.
    # Values are keys from features.SOURCE_LABELS ("tags", "derived_proxy",
    # "osu_api_attributes", ...). Keeping them separate from evidence lets the
    # UI say whether a match came from the API, a mapper tag or a proxy.
    sources: tuple[str, ...] = ()

    @property
    def display_score(self) -> int | None:
        if self.score is None:
            return None
        return int(round(max(0.0, min(1.0, self.score)) * 100))


@dataclass(frozen=True, slots=True)
class FactorScore:
    key: str
    label: str
    value: float | None
    weight: float
    explanation: str
    contribution: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "weight": self.weight,
            "explanation": self.explanation,
            "contribution": self.contribution,
        }


@dataclass(frozen=True, slots=True)
class TargetMatch:
    """Result of checking one preset target tag/feature for one candidate."""

    target_id: str
    label: str
    score: float | None
    threshold: float
    matched: bool = False
    status: str = "недостаточно данных"
    source: str = "недостаточно данных"
    confidence: str = "none"
    support: str = "limited"
    evidence: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def display_score(self) -> int | None:
        if self.score is None:
            return None
        return int(round(max(0.0, min(1.0, self.score)) * 100))


@dataclass(frozen=True, slots=True)
class RelevanceResult:
    """Pre-ranking target gate for a specialized preset."""

    targets: tuple[TargetMatch, ...] = ()
    threshold: float = 0.0
    passed: bool = True
    best_target: TargetMatch | None = None
    reason: str = ""

    @property
    def active(self) -> bool:
        return bool(self.targets)

    @property
    def exclusion_reason(self) -> str:
        if self.passed:
            return ""
        if not self.active:
            return "целевые теги не заданы"
        return self.reason or "соответствие целевым тегам ниже порога"


@dataclass(frozen=True, slots=True)
class Candidate:
    beatmapset: Beatmapset
    beatmap: Beatmap
    sources: tuple[str, ...] = ()
    attributes: DifficultyAttributes | None = None
    traits: BeatmapFeatures = field(default_factory=BeatmapFeatures)
    skillsets: dict[str, SkillScore] = field(default_factory=dict)
    within_tolerance: bool = True
    unknown_fields: tuple[str, ...] = ()
    relevance: RelevanceResult | None = None
    sequence: SequenceAnalysis | None = None

    @property
    def key(self) -> int:
        return self.beatmapset.id


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    factors: tuple[FactorScore, ...]
    reasons: tuple[str, ...] = ()
    penalty: float = 0.0
    diversified_score: float | None = None

    @property
    def beatmapset(self) -> Beatmapset:
        return self.candidate.beatmapset

    @property
    def beatmap(self) -> Beatmap:
        return self.candidate.beatmap

    @property
    def display_score(self) -> int:
        return int(round(max(0.0, min(1.0, self.score)) * 100))


@dataclass(frozen=True, slots=True)
class Exclusion:
    beatmapset_id: int
    title: str
    reason: str
    stage: str = "filter"


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    scored: tuple[ScoredCandidate, ...] = ()
    total_fetched: int = 0
    passed_filters: int = 0
    # Candidates that survived the mandatory target-tag gate. Equal to
    # passed_filters when the active preset has no target tags.
    passed_relevance: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    exclusions: tuple[Exclusion, ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    complete: bool = False
    # Network operations attempted, including failed attempts. Cache hits are
    # counted in ``cache_hits`` and never consume a network-request slot.
    api_requests: int = 0
    requests_succeeded: int = 0
    requests_failed: int = 0
    pages_attempted: int = 0
    # Actual HTTP attempts inside the client (including retries and transport
    # failures) and token-endpoint calls. A logical API call can be several
    # HTTP attempts, so these are reported separately from the budget counter.
    http_attempts: int = 0
    oauth_requests: int = 0
    cache_hits: int = 0
    # Why discovery stopped: complete, request_budget, page_limit, deadline,
    # network_failure, rate_limit, forbidden, request_failed, candidate_limit.
    stop_reason: str = ""
    deadline_seconds: float = 0.0
    strategies: tuple[str, ...] = ()
    started_at: str = field(default_factory=now_iso)
    finished_at: str = field(default_factory=now_iso)
    filters_used: Filters | None = None
    ranking_used: RankingSettings | None = None
    # "api" for a discovery run, "local_cache" when stored candidates were
    # re-ranked without a network search. Kept explicit so the UI never
    # presents a local pool as a fresh exhaustive API result.
    origin: str = "api"
    # Stage timings in seconds. Keys are "fetch", "filter", "attributes",
    # "sequences", "rank" and "total". They are diagnostic only and never
    # affect ranking.
    timings: dict[str, float] = field(default_factory=dict)
    # Sequence analysis is reported separately from API/search completeness.
    # Missing sequence evidence is unknown, not evidence of absence.
    sequence_required: bool = False
    sequences_complete: bool = True
    sequence_analyzed: int = 0
    sequence_unanalyzed: int = 0
    sequence_downloads: int = 0
    sequence_cache_hits: int = 0

    @property
    def count(self) -> int:
        return len(self.scored)


@dataclass(frozen=True, slots=True)
class TasteProfile:
    rated_sets: int = 0
    liked: tuple[tuple[int, str, str], ...] = ()
    disliked: tuple[tuple[int, str, str], ...] = ()
    artist_affinity: dict[str, float] = field(default_factory=dict)
    creator_affinity: dict[str, float] = field(default_factory=dict)
    genre_affinity: dict[int, float] = field(default_factory=dict)
    tag_affinity: dict[str, float] = field(default_factory=dict)
    skill_affinity: dict[str, float] = field(default_factory=dict)
    skill_counts: dict[str, int] = field(default_factory=dict)
    confidence: str = "none"
    notes: tuple[str, ...] = ()
    ignored_verdicts: tuple[str, ...] = ()

    @property
    def enough_data(self) -> bool:
        return self.confidence in {"medium", "high"}

    def top_artists(self, n: int = 5) -> list[tuple[str, float]]:
        return sorted(self.artist_affinity.items(), key=lambda x: (-x[1], x[0]))[:n]

    def top_creators(self, n: int = 5) -> list[tuple[str, float]]:
        return sorted(self.creator_affinity.items(), key=lambda x: (-x[1], x[0]))[:n]

    def top_genres(self, n: int = 5) -> list[tuple[int, float]]:
        return sorted(self.genre_affinity.items(), key=lambda x: (-x[1], x[0]))[:n]

    def top_skills(self, n: int = 8) -> list[tuple[str, float]]:
        return sorted(self.skill_affinity.items(), key=lambda x: (-abs(x[1]), x[0]))[:n]


def by_id(items: Iterable[Any], key: str = "id") -> dict[int, Any]:
    return {int(getattr(item, key)): item for item in items}


# --------------------------------------------------------------------------
# Serialization helpers (used by SQLite storage and the search cache)
# --------------------------------------------------------------------------


def beatmap_to_dict(beatmap: Beatmap) -> dict[str, Any]:
    return {
        "id": beatmap.id,
        "beatmapset_id": beatmap.beatmapset_id,
        "mode": beatmap.mode,
        "version": beatmap.version,
        "difficulty_rating": beatmap.difficulty_rating,
        "total_length": beatmap.total_length,
        "hit_length": beatmap.hit_length,
        "bpm": beatmap.bpm,
        "ar": beatmap.ar,
        "od": beatmap.od,
        "cs": beatmap.cs,
        "hp": beatmap.hp,
        "max_combo": beatmap.max_combo,
        "count_circles": beatmap.count_circles,
        "count_sliders": beatmap.count_sliders,
        "count_spinners": beatmap.count_spinners,
        "status": beatmap.status,
        "url": beatmap.url,
        "owners": list(beatmap.owners),
    }


def beatmap_from_dict(data: dict[str, Any]) -> Beatmap:
    return Beatmap(
        id=int(data["id"]),
        beatmapset_id=int(data.get("beatmapset_id", 0)),
        mode=str(data.get("mode") or "osu"),
        version=str(data.get("version") or ""),
        difficulty_rating=data.get("difficulty_rating"),
        total_length=data.get("total_length"),
        hit_length=data.get("hit_length"),
        bpm=data.get("bpm"),
        ar=data.get("ar"),
        od=data.get("od"),
        cs=data.get("cs"),
        hp=data.get("hp"),
        max_combo=data.get("max_combo"),
        count_circles=data.get("count_circles"),
        count_sliders=data.get("count_sliders"),
        count_spinners=data.get("count_spinners"),
        status=data.get("status"),
        url=data.get("url"),
        owners=tuple(data.get("owners") or ()),
    )


def beatmapset_to_dict(beatmapset: Beatmapset) -> dict[str, Any]:
    return {
        "id": beatmapset.id,
        "artist": beatmapset.artist,
        "artist_unicode": beatmapset.artist_unicode,
        "title": beatmapset.title,
        "title_unicode": beatmapset.title_unicode,
        "creator": beatmapset.creator,
        "status": beatmapset.status,
        "bpm": beatmapset.bpm,
        "favourite_count": beatmapset.favourite_count,
        "play_count": beatmapset.play_count,
        "rating": beatmapset.rating,
        "genre_id": beatmapset.genre_id,
        "language_id": beatmapset.language_id,
        "tags": list(beatmapset.tags),
        "nsfw": beatmapset.nsfw,
        "video": beatmapset.video,
        "storyboard": beatmapset.storyboard,
        "preview_url": beatmapset.preview_url,
        "cover_url": beatmapset.cover_url,
        "ranked_date": beatmapset.ranked_date,
        "submitted_date": beatmapset.submitted_date,
        "last_updated": beatmapset.last_updated,
        "beatmaps": [beatmap_to_dict(b) for b in beatmapset.beatmaps],
        "source": beatmapset.source,
        "fetched_at": beatmapset.fetched_at,
    }


def beatmapset_from_dict(data: dict[str, Any]) -> Beatmapset:
    return Beatmapset(
        id=int(data["id"]),
        artist=str(data.get("artist") or ""),
        artist_unicode=str(data.get("artist_unicode") or ""),
        title=str(data.get("title") or ""),
        title_unicode=str(data.get("title_unicode") or ""),
        creator=str(data.get("creator") or ""),
        status=str(data.get("status") or "unknown"),
        bpm=data.get("bpm"),
        favourite_count=data.get("favourite_count"),
        play_count=data.get("play_count"),
        rating=data.get("rating"),
        genre_id=data.get("genre_id"),
        language_id=data.get("language_id"),
        tags=tuple(data.get("tags") or ()),
        nsfw=bool(data.get("nsfw")),
        video=bool(data.get("video")),
        storyboard=bool(data.get("storyboard")),
        preview_url=str(data.get("preview_url") or ""),
        cover_url=str(data.get("cover_url") or ""),
        ranked_date=data.get("ranked_date"),
        submitted_date=data.get("submitted_date"),
        last_updated=data.get("last_updated"),
        beatmaps=tuple(beatmap_from_dict(b) for b in data.get("beatmaps") or ()),
        source=str(data.get("source") or "osu_api"),
        fetched_at=str(data.get("fetched_at") or now_iso()),
    )


def search_page_to_dict(page: SearchPage) -> dict[str, Any]:
    return {
        "beatmapsets": [beatmapset_to_dict(b) for b in page.beatmapsets],
        "cursor": page.cursor,
        "cursor_string": page.cursor_string,
        "total": page.total,
        "recommended_difficulty": page.recommended_difficulty,
        "error": page.error,
        "warnings": list(page.warnings),
        "raw_count": page.raw_count,
    }


def search_page_from_dict(data: dict[str, Any]) -> SearchPage:
    return SearchPage(
        beatmapsets=tuple(
            beatmapset_from_dict(b) for b in data.get("beatmapsets") or ()
        ),
        cursor=data.get("cursor"),
        cursor_string=data.get("cursor_string"),
        total=data.get("total"),
        recommended_difficulty=data.get("recommended_difficulty"),
        error=data.get("error"),
        warnings=tuple(data.get("warnings") or ()),
        raw_count=int(data.get("raw_count") or 0),
    )
