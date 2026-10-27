"""Read-only local beatmap library backed by the existing SQLite catalog.

The library deliberately performs no API calls. It joins the catalog tables
with persisted analysis and queue state, and exposes one row per difficulty.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

import analysis
import patterns
from storage import Storage

ANALYZED = "analyzed"
PARTIAL = "partial"
PENDING = "pending"
PROCESSING = "processing"
UNAVAILABLE = "unavailable"
FAILED = "failed"
NOT_ANALYZED = "not_analyzed"

STATE_LABELS = {
    ANALYZED: "полностью проанализировано",
    PARTIAL: "частичный анализ",
    PENDING: "в очереди",
    PROCESSING: "анализируется",
    UNAVAILABLE: "источник недоступен",
    FAILED: "анализ не удался",
    NOT_ANALYZED: "не анализировалось",
}
STATE_SHORT = {
    ANALYZED: "анализ",
    PARTIAL: "частично",
    PENDING: "в очереди",
    PROCESSING: "в работе",
    UNAVAILABLE: "нет .osu",
    FAILED: "ошибка",
    NOT_ANALYZED: "не анализировалось",
}
STATE_TONE = {
    ANALYZED: "good",
    PARTIAL: "warn",
    PENDING: "muted",
    PROCESSING: "accent",
    UNAVAILABLE: "warn",
    FAILED: "bad",
    NOT_ANALYZED: "muted",
}

SCOPE_OPTIONS = (
    "analyzed",
    "all",
    "partial",
    "pending",
    "unavailable",
    "failed",
)
SCOPE_LABELS = {
    "analyzed": "Проанализировано",
    "all": "Весь каталог",
    "partial": "Частичные",
    "pending": "В очереди",
    "unavailable": "Нет .osu",
    "failed": "Ошибки",
}
SCOPE_STATES = {
    "analyzed": (ANALYZED, PARTIAL),
    "all": (),
    "partial": (PARTIAL,),
    "pending": (PENDING, PROCESSING, NOT_ANALYZED),
    "unavailable": (UNAVAILABLE,),
    "failed": (FAILED,),
    # Kept as a programmatic alias for callers that want both problems.
    "problems": (UNAVAILABLE, FAILED),
}

SORT_OPTIONS = (
    "recent_analysis",
    "stars_desc",
    "stars_asc",
    "bpm_desc",
    "length_desc",
    "title",
    "ranked",
)
SORT_LABELS = {
    "recent_analysis": "Недавно проанализированные",
    "stars_desc": "Звёзды: сначала сложные",
    "stars_asc": "Звёзды: сначала простые",
    "bpm_desc": "BPM: сначала быстрые",
    "length_desc": "Длина: сначала длинные",
    "title": "Название",
    "ranked": "Дата ранга",
}

_SORT_SQL = {
    "recent_analysis": (
        "last_analyzed IS NULL, last_analyzed DESC, max_stars DESC"
    ),
    "stars_desc": "max_stars IS NULL, max_stars DESC, title_key, artist_key",
    "stars_asc": "max_stars IS NULL, max_stars ASC, title_key, artist_key",
    "bpm_desc": "max_bpm IS NULL, max_bpm DESC, title_key",
    "length_desc": "max_length IS NULL, max_length DESC, title_key",
    "title": "artist_key, title_key",
    "ranked": "ranked_date IS NULL, ranked_date DESC, title_key",
}

_BASE_SELECT = """
SELECT
    bm.id AS beatmap_id,
    bm.beatmapset_id,
    bm.mode,
    bm.version,
    bm.difficulty_rating,
    bm.total_length,
    bm.hit_length,
    bm.bpm,
    bm.ar,
    bm.od,
    bm.cs,
    bm.hp,
    bm.max_combo,
    bm.status AS beatmap_status,
    bm.url,
    bs.artist,
    bs.artist_unicode,
    bs.title,
    bs.title_unicode,
    bs.creator,
    bs.status AS set_status,
    bs.bpm AS set_bpm,
    bs.cover_url,
    bs.preview_url,
    bs.ranked_date,
    COALESCE(attr.star_rating, bm.difficulty_rating) AS stars,
    a.status AS analysis_status,
    a.source AS analysis_source,
    a.scope AS analysis_scope,
    a.analyzed_at,
    a.attempts AS analysis_attempts,
    a.sequence_json,
    a.analyzer_version AS analysis_version,
    q.status AS queue_status,
    q.last_error AS queue_error,
    q.attempts AS queue_attempts,
    q.next_attempt_at,
    CASE
        WHEN a.status = 'measured'
             AND a.analyzer_version = :analyzer THEN 'analyzed'
        WHEN a.status = 'partial'
             AND a.analyzer_version = :analyzer THEN 'partial'
        WHEN q.status = 'unavailable' THEN 'unavailable'
        WHEN a.status = 'failed'
             AND a.analyzer_version = :analyzer THEN 'failed'
        WHEN q.status = 'processing' THEN 'processing'
        WHEN q.status = 'pending' THEN 'pending'
        WHEN q.status = 'failed' THEN 'failed'
        ELSE 'not_analyzed'
    END AS state
FROM beatmaps bm
JOIN beatmapsets bs ON bs.id = bm.beatmapset_id
LEFT JOIN beatmap_attributes attr ON attr.beatmap_id = bm.id
LEFT JOIN beatmap_analysis a ON a.beatmap_id = bm.id
LEFT JOIN analysis_queue q ON q.beatmap_id = bm.id
"""


@dataclass(frozen=True, slots=True)
class LibraryFilters:
    query: str = ""
    scope: str = "analyzed"
    statuses: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    stars_min: float | None = None
    stars_max: float | None = None
    bpm_min: float | None = None
    bpm_max: float | None = None
    length_min: float | None = None
    length_max: float | None = None
    mode: str | None = None
    sort: str = "recent_analysis"
    page: int = 1
    per_page: int = 12
    set_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    beatmap_id: int
    beatmapset_id: int
    mode: str
    version: str
    difficulty_rating: float | None
    total_length: int | None
    hit_length: int | None
    bpm: float | None
    ar: float | None
    od: float | None
    cs: float | None
    hp: float | None
    max_combo: int | None
    beatmap_status: str | None
    url: str | None
    artist: str
    artist_unicode: str
    title: str
    title_unicode: str
    creator: str
    set_status: str
    set_bpm: float | None
    cover_url: str
    preview_url: str
    ranked_date: str | None
    stars: float | None
    analysis_status: str | None
    analysis_source: str
    analysis_scope: str
    analyzed_at: str | None
    analysis_attempts: int
    sequence_json: str
    analysis_version: int | None
    queue_status: str | None
    queue_error: str
    queue_attempts: int
    next_attempt_at: str | None
    state: str

    @property
    def display_title(self) -> str:
        return self.title_unicode or self.title or "Без названия"

    @property
    def display_artist(self) -> str:
        return self.artist_unicode or self.artist or "Неизвестный артист"

    @property
    def effective_bpm(self) -> float | None:
        return self.bpm if self.bpm is not None else self.set_bpm

    @property
    def effective_length(self) -> int | None:
        return self.total_length or self.hit_length

    @property
    def effective_status(self) -> str:
        return self.beatmap_status or self.set_status or "unknown"

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def short_state(self) -> str:
        return STATE_SHORT.get(self.state, self.state)

    @property
    def sequence(self):
        if self.analysis_status not in {"measured", "partial"}:
            return None
        if self.analysis_version != patterns.ANALYZER_VERSION:
            return None
        return analysis.sequence_from_json(self.sequence_json)

    @property
    def labels(self) -> tuple[str, ...]:
        sequence = self.sequence
        if sequence is None:
            return ()
        return tuple(sequence.labels)

    @property
    def is_partial(self) -> bool:
        return self.state == PARTIAL

    @property
    def set_url(self) -> str:
        return f"https://osu.ppy.sh/beatmapsets/{self.beatmapset_id}"

    @property
    def beatmap_url(self) -> str:
        return self.url or f"{self.set_url}#osu/{self.beatmap_id}"


@dataclass(frozen=True, slots=True)
class LibrarySet:
    beatmapset_id: int
    artist: str
    artist_unicode: str
    title: str
    title_unicode: str
    creator: str
    set_status: str
    cover_url: str
    ranked_date: str | None
    entries: tuple[LibraryEntry, ...]

    @property
    def display_title(self) -> str:
        return self.title_unicode or self.title or "Без названия"

    @property
    def display_artist(self) -> str:
        return self.artist_unicode or self.artist or "Неизвестный артист"

    @property
    def set_url(self) -> str:
        return f"https://osu.ppy.sh/beatmapsets/{self.beatmapset_id}"


@dataclass(frozen=True, slots=True)
class LibraryPage:
    sets: tuple[LibrarySet, ...]
    total_sets: int
    total_difficulties: int
    page: int
    pages: int
    counts: dict[str, int]


def _entry_from_row(row) -> LibraryEntry:
    return LibraryEntry(
        beatmap_id=int(row["beatmap_id"]),
        beatmapset_id=int(row["beatmapset_id"]),
        mode=str(row["mode"] or "osu"),
        version=str(row["version"] or ""),
        difficulty_rating=row["difficulty_rating"],
        total_length=row["total_length"],
        hit_length=row["hit_length"],
        bpm=row["bpm"],
        ar=row["ar"],
        od=row["od"],
        cs=row["cs"],
        hp=row["hp"],
        max_combo=row["max_combo"],
        beatmap_status=row["beatmap_status"],
        url=row["url"],
        artist=str(row["artist"] or ""),
        artist_unicode=str(row["artist_unicode"] or ""),
        title=str(row["title"] or ""),
        title_unicode=str(row["title_unicode"] or ""),
        creator=str(row["creator"] or ""),
        set_status=str(row["set_status"] or ""),
        set_bpm=row["set_bpm"],
        cover_url=str(row["cover_url"] or ""),
        preview_url=str(row["preview_url"] or ""),
        ranked_date=row["ranked_date"],
        stars=row["stars"],
        analysis_status=row["analysis_status"],
        analysis_source=str(row["analysis_source"] or ""),
        analysis_scope=str(row["analysis_scope"] or ""),
        analyzed_at=row["analyzed_at"],
        analysis_attempts=int(row["analysis_attempts"] or 0),
        sequence_json=str(row["sequence_json"] or ""),
        analysis_version=row["analysis_version"],
        queue_status=row["queue_status"],
        queue_error=str(row["queue_error"] or ""),
        queue_attempts=int(row["queue_attempts"] or 0),
        next_attempt_at=row["next_attempt_at"],
        state=str(row["state"]),
    )


def _where_clause(filters: LibraryFilters) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"analyzer": patterns.ANALYZER_VERSION}
    query = filters.query.strip()
    if query:
        clauses.append(
            "("
            "LOWER(COALESCE(NULLIF(title_unicode,''), title)) LIKE :q "
            "OR LOWER(COALESCE(NULLIF(artist_unicode,''), artist)) LIKE :q "
            "OR LOWER(COALESCE(NULLIF(creator,''),'')) LIKE :q "
            "OR LOWER(COALESCE(NULLIF(version,''),'')) LIKE :q"
            ")"
        )
        params["q"] = f"%{query.casefold()}%"
    if filters.statuses:
        placeholders = ",".join(
            f":status_{index}" for index in range(len(filters.statuses))
        )
        clauses.append(
            f"COALESCE(NULLIF(beatmap_status,''), set_status, 'unknown') "
            f"IN ({placeholders})"
        )
        for index, value in enumerate(filters.statuses):
            params[f"status_{index}"] = value
    if filters.mode:
        clauses.append("mode = :mode")
        params["mode"] = filters.mode
    if filters.stars_min is not None:
        clauses.append("stars IS NOT NULL AND stars >= :stars_min")
        params["stars_min"] = float(filters.stars_min)
    if filters.stars_max is not None:
        clauses.append("stars IS NOT NULL AND stars <= :stars_max")
        params["stars_max"] = float(filters.stars_max)
    if filters.bpm_min is not None:
        clauses.append("COALESCE(bpm, set_bpm) >= :bpm_min")
        params["bpm_min"] = float(filters.bpm_min)
    if filters.bpm_max is not None:
        clauses.append("COALESCE(bpm, set_bpm) <= :bpm_max")
        params["bpm_max"] = float(filters.bpm_max)
    if filters.length_min is not None:
        clauses.append(
            "COALESCE(total_length, hit_length, 0) >= :length_min"
        )
        params["length_min"] = int(filters.length_min)
    if filters.length_max is not None:
        clauses.append(
            "COALESCE(total_length, hit_length, 0) <= :length_max"
        )
        params["length_max"] = int(filters.length_max)
    if filters.set_ids:
        placeholders = ",".join(
            f":set_{index}" for index in range(len(filters.set_ids))
        )
        clauses.append(f"beatmapset_id IN ({placeholders})")
        for index, value in enumerate(filters.set_ids):
            params[f"set_{index}"] = int(value)
    if filters.patterns:
        # Any selected pattern matches. Labels are stored as a JSON string
        # array; malformed or absent JSON simply does not match.
        parts = []
        for index, pattern in enumerate(filters.patterns):
            parts.append(f"sequence_json LIKE :pattern_{index}")
            params[f"pattern_{index}"] = f'%"{pattern}"%'
        clauses.append("(" + " OR ".join(parts) + ")")
    return (" AND ".join(clauses) or "1=1"), params


def _state_clause(scope: str) -> str:
    states = SCOPE_STATES.get(scope, ())
    if not states:
        return "1=1"
    return "state IN (" + ",".join(f"'{state}'" for state in states) + ")"


def counts(store: Storage) -> dict[str, int]:
    base = _BASE_SELECT
    params = {"analyzer": patterns.ANALYZER_VERSION}
    with store.connection() as conn:
        rows = conn.execute(
            f"WITH base AS ({base}) SELECT state, COUNT(*) AS n "
            "FROM base GROUP BY state",
            params,
        ).fetchall()
    raw = {str(row["state"]): int(row["n"]) for row in rows}
    analyzed = raw.get(ANALYZED, 0)
    partial = raw.get(PARTIAL, 0)
    return {
        "total": sum(raw.values()),
        "analyzed": analyzed,
        "partial": partial,
        "pending": (
            raw.get(PENDING, 0)
            + raw.get(PROCESSING, 0)
            + raw.get(NOT_ANALYZED, 0)
        ),
        "unavailable": raw.get(UNAVAILABLE, 0),
        "failed": raw.get(FAILED, 0),
    }


def query_sets(store: Storage, filters: LibraryFilters) -> LibraryPage:
    where, params = _where_clause(filters)
    base = _BASE_SELECT
    state = _state_clause(filters.scope)
    sort_sql = _SORT_SQL.get(filters.sort, _SORT_SQL["recent_analysis"])
    per_page = max(1, min(48, int(filters.per_page)))
    with store.connection() as conn:
        total_row = conn.execute(
            f"WITH base AS ({base}) "
            f"SELECT COUNT(DISTINCT beatmapset_id) AS sets, "
            f"COUNT(*) AS difficulties FROM base "
            f"WHERE {where} AND {state}",
            params,
        ).fetchone()
        total_sets = int(total_row["sets"] or 0)
        total_difficulties = int(total_row["difficulties"] or 0)
        pages = max(1, (total_sets + per_page - 1) // per_page)
        page = max(1, min(int(filters.page), pages))
        offset = (page - 1) * per_page
        set_rows = conn.execute(
            f"WITH base AS ({base}) "
            "SELECT beatmapset_id, COUNT(*) AS difficulties, "
            "MAX(stars) AS max_stars, MIN(stars) AS min_stars, "
            "MAX(COALESCE(bpm, set_bpm)) AS max_bpm, "
            "MAX(COALESCE(total_length, hit_length, 0)) AS max_length, "
            "MAX(analyzed_at) AS last_analyzed, "
            "MAX(ranked_date) AS ranked_date, "
            "MIN(LOWER(COALESCE(NULLIF(artist_unicode,''), artist))) "
            "AS artist_key, "
            "MIN(LOWER(COALESCE(NULLIF(title_unicode,''), title))) "
            "AS title_key "
            f"FROM base WHERE {where} AND {state} "
            "GROUP BY beatmapset_id "
            f"ORDER BY {sort_sql} "
            "LIMIT :limit OFFSET :offset",
            {**params, "limit": per_page, "offset": offset},
        ).fetchall()
        set_ids = [int(row["beatmapset_id"]) for row in set_rows]
        entries_by_set: dict[int, list[LibraryEntry]] = {
            set_id: [] for set_id in set_ids
        }
        if set_ids:
            placeholders = ",".join(
                f":entry_{index}" for index in range(len(set_ids))
            )
            entry_params = {
                **params,
                **{
                    f"entry_{index}": set_id
                    for index, set_id in enumerate(set_ids)
                },
            }
            entry_rows = conn.execute(
                f"WITH base AS ({base}) "
                f"SELECT * FROM base WHERE {where} AND {state} "
                f"AND beatmapset_id IN ({placeholders}) "
                "ORDER BY beatmapset_id, stars DESC, version",
                entry_params,
            ).fetchall()
            for row in entry_rows:
                entry = _entry_from_row(row)
                entries_by_set.setdefault(entry.beatmapset_id, []).append(
                    entry
                )
    sets: list[LibrarySet] = []
    for set_id in set_ids:
        entries = entries_by_set.get(set_id, [])
        if not entries:
            continue
        first = entries[0]
        sets.append(
            LibrarySet(
                beatmapset_id=set_id,
                artist=first.artist,
                artist_unicode=first.artist_unicode,
                title=first.title,
                title_unicode=first.title_unicode,
                creator=first.creator,
                set_status=first.set_status,
                cover_url=first.cover_url,
                ranked_date=first.ranked_date,
                entries=tuple(entries),
            )
        )
    return LibraryPage(
        sets=tuple(sets),
        total_sets=total_sets,
        total_difficulties=total_difficulties,
        page=page,
        pages=pages,
        counts=counts(store),
    )


def filter_counts(store: Storage, filters: LibraryFilters) -> dict[str, int]:
    """Counts for a filtered collection, used by the library summary."""

    page = query_sets(store, replace(filters, page=1, per_page=1))
    return {
        "sets": page.total_sets,
        "difficulties": page.total_difficulties,
    }


def known_statuses(store: Storage) -> tuple[str, ...]:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(bm.status,''), bs.status) "
            "AS status FROM beatmaps bm "
            "JOIN beatmapsets bs ON bs.id = bm.beatmapset_id "
            "ORDER BY status"
        ).fetchall()
    return tuple(
        str(row["status"]) for row in rows if row["status"]
    )


def known_modes(store: Storage) -> tuple[str, ...]:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT mode FROM beatmaps ORDER BY mode"
        ).fetchall()
    return tuple(str(row["mode"]) for row in rows if row["mode"])
