"""SQLite persistence.

Design rules:

* reference data (beatmapsets / beatmaps / attributes), cache, presets and
  user data live in separate tables;
* clearing the search cache never touches ratings or favourites;
* deleting a preset never touches ratings, history or favourites;
* beatmapsets are upserted by API id, so repeated searches do not duplicate
  rows;
* schema changes go through numbered migrations tracked by
  ``PRAGMA user_version``;
* backups use SQLite's online backup API, and restore validates the file
  before replacing anything.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import config
from models import (
    VERDICTS,
    Beatmap,
    Beatmapset,
    Preset,
    PresetConfig,
    beatmapset_from_dict,
    beatmapset_to_dict,
    now_iso,
)

logger = logging.getLogger("osu_picker.storage")

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE beatmapsets (
            id INTEGER PRIMARY KEY,
            artist TEXT NOT NULL DEFAULT '',
            artist_unicode TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            title_unicode TEXT NOT NULL DEFAULT '',
            creator TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown',
            bpm REAL,
            favourite_count INTEGER,
            play_count INTEGER,
            rating REAL,
            genre_id INTEGER,
            language_id INTEGER,
            tags TEXT NOT NULL DEFAULT '[]',
            nsfw INTEGER NOT NULL DEFAULT 0,
            video INTEGER NOT NULL DEFAULT 0,
            storyboard INTEGER NOT NULL DEFAULT 0,
            preview_url TEXT NOT NULL DEFAULT '',
            cover_url TEXT NOT NULL DEFAULT '',
            ranked_date TEXT,
            submitted_date TEXT,
            last_updated TEXT,
            source TEXT NOT NULL DEFAULT 'osu_api',
            fetched_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_beatmapsets_status ON beatmapsets(status)",
        "CREATE INDEX idx_beatmapsets_creator ON beatmapsets(creator)",
        """
        CREATE TABLE beatmaps (
            id INTEGER PRIMARY KEY,
            beatmapset_id INTEGER NOT NULL REFERENCES beatmapsets(id) ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'osu',
            version TEXT NOT NULL DEFAULT '',
            difficulty_rating REAL,
            total_length INTEGER,
            hit_length INTEGER,
            bpm REAL,
            ar REAL,
            od REAL,
            cs REAL,
            hp REAL,
            max_combo INTEGER,
            count_circles INTEGER,
            count_sliders INTEGER,
            count_spinners INTEGER,
            status TEXT,
            url TEXT,
            owners TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX idx_beatmaps_set ON beatmaps(beatmapset_id)",
        "CREATE INDEX idx_beatmaps_mode_stars ON beatmaps(mode, difficulty_rating)",
        """
        CREATE TABLE beatmap_attributes (
            beatmap_id INTEGER PRIMARY KEY REFERENCES beatmaps(id) ON DELETE CASCADE,
            star_rating REAL,
            aim_difficulty REAL,
            speed_difficulty REAL,
            slider_factor REAL,
            max_combo INTEGER,
            source TEXT NOT NULL DEFAULT 'osu_api_attributes',
            fetched_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE search_cache (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_cache_expires ON search_cache(expires_at)",
        """
        CREATE TABLE presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            is_builtin INTEGER NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE feedback (
            beatmapset_id INTEGER NOT NULL REFERENCES beatmapsets(id) ON DELETE CASCADE,
            verdict TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (beatmapset_id, verdict)
        )
        """,
        "CREATE INDEX idx_feedback_verdict ON feedback(verdict)",
        """
        CREATE TABLE favorites (
            beatmapset_id INTEGER PRIMARY KEY REFERENCES beatmapsets(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            beatmapset_id INTEGER NOT NULL REFERENCES beatmapsets(id) ON DELETE CASCADE,
            shown_at TEXT NOT NULL,
            surface TEXT NOT NULL DEFAULT 'search',
            rank_score REAL,
            position INTEGER
        )
        """,
        "CREATE INDEX idx_history_shown ON history(shown_at)",
        "CREATE INDEX idx_history_set ON history(beatmapset_id)",
        """
        CREATE TABLE preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE search_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            cache_hits INTEGER NOT NULL DEFAULT 0,
            total_fetched INTEGER NOT NULL DEFAULT 0,
            passed_filters INTEGER NOT NULL DEFAULT 0,
            result_count INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0,
            strategies_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]',
            excluded_json TEXT NOT NULL DEFAULT '{}',
            filters_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
    )
}


class StorageError(RuntimeError):
    """Storage-level failure with a user-readable message."""


class UserDataImportError(ValueError):
    """Invalid user-data import. Nothing has been written."""


class Storage:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else config.db_path()
        if str(self.path) == ":memory:":
            raise StorageError(
                "Для этого приложения нужен файл БД, :memory: не поддерживается"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # -- infrastructure ----------------------------------------------------
    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.connection() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def _migrate(self) -> None:
        version = self.schema_version()
        if version > config.DB_SCHEMA_VERSION:
            raise StorageError(
                f"База данных новее приложения (schema {version} > "
                f"{config.DB_SCHEMA_VERSION}). Обновите приложение."
            )
        for target in range(version + 1, config.DB_SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(target)
            if statements is None:
                raise StorageError(f"Нет миграции для версии {target}")
            logger.info("применяю миграцию БД %s", target)
            with self.connection() as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")

    # -- beatmapsets -------------------------------------------------------
    def save_beatmapset(self, beatmapset: Beatmapset) -> None:
        self.save_beatmapsets([beatmapset])

    def save_beatmapsets(self, beatmapsets: Iterable[Beatmapset]) -> int:
        saved = 0
        with self.connection() as conn:
            for beatmapset in beatmapsets:
                values = (
                    beatmapset.id,
                    beatmapset.artist,
                    beatmapset.artist_unicode,
                    beatmapset.title,
                    beatmapset.title_unicode,
                    beatmapset.creator,
                    beatmapset.status,
                    beatmapset.bpm,
                    beatmapset.favourite_count,
                    beatmapset.play_count,
                    beatmapset.rating,
                    beatmapset.genre_id,
                    beatmapset.language_id,
                    json.dumps(list(beatmapset.tags), ensure_ascii=False),
                    int(beatmapset.nsfw),
                    int(beatmapset.video),
                    int(beatmapset.storyboard),
                    beatmapset.preview_url,
                    beatmapset.cover_url,
                    beatmapset.ranked_date,
                    beatmapset.submitted_date,
                    beatmapset.last_updated,
                    beatmapset.source,
                    beatmapset.fetched_at,
                )
                conn.execute(
                    """
                    INSERT INTO beatmapsets (
                        id, artist, artist_unicode, title, title_unicode, creator,
                        status, bpm, favourite_count, play_count, rating, genre_id,
                        language_id, tags, nsfw, video, storyboard, preview_url,
                        cover_url, ranked_date, submitted_date, last_updated,
                        source, fetched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        artist=excluded.artist,
                        artist_unicode=excluded.artist_unicode,
                        title=excluded.title,
                        title_unicode=excluded.title_unicode,
                        creator=excluded.creator,
                        status=excluded.status,
                        bpm=excluded.bpm,
                        favourite_count=excluded.favourite_count,
                        play_count=excluded.play_count,
                        rating=excluded.rating,
                        genre_id=excluded.genre_id,
                        language_id=excluded.language_id,
                        tags=excluded.tags,
                        nsfw=excluded.nsfw,
                        video=excluded.video,
                        storyboard=excluded.storyboard,
                        preview_url=excluded.preview_url,
                        cover_url=excluded.cover_url,
                        ranked_date=excluded.ranked_date,
                        submitted_date=excluded.submitted_date,
                        last_updated=excluded.last_updated,
                        source=excluded.source,
                        fetched_at=excluded.fetched_at
                    WHERE excluded.fetched_at >= beatmapsets.fetched_at
                    """,
                    values,
                )
                for beatmap in beatmapset.beatmaps:
                    conn.execute(
                        """
                        INSERT INTO beatmaps (
                            id, beatmapset_id, mode, version, difficulty_rating,
                            total_length, hit_length, bpm, ar, od, cs, hp,
                            max_combo, count_circles, count_sliders, count_spinners,
                            status, url, owners, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            beatmapset_id=excluded.beatmapset_id,
                            mode=excluded.mode,
                            version=excluded.version,
                            difficulty_rating=excluded.difficulty_rating,
                            total_length=excluded.total_length,
                            hit_length=excluded.hit_length,
                            bpm=excluded.bpm,
                            ar=excluded.ar,
                            od=excluded.od,
                            cs=excluded.cs,
                            hp=excluded.hp,
                            max_combo=excluded.max_combo,
                            count_circles=excluded.count_circles,
                            count_sliders=excluded.count_sliders,
                            count_spinners=excluded.count_spinners,
                            status=excluded.status,
                            url=excluded.url,
                            owners=excluded.owners,
                            updated_at=excluded.updated_at
                        """,
                        (
                            beatmap.id,
                            beatmap.beatmapset_id,
                            beatmap.mode,
                            beatmap.version,
                            beatmap.difficulty_rating,
                            beatmap.total_length,
                            beatmap.hit_length,
                            beatmap.bpm,
                            beatmap.ar,
                            beatmap.od,
                            beatmap.cs,
                            beatmap.hp,
                            beatmap.max_combo,
                            beatmap.count_circles,
                            beatmap.count_sliders,
                            beatmap.count_spinners,
                            beatmap.status,
                            beatmap.url,
                            json.dumps(list(beatmap.owners), ensure_ascii=False),
                            now_iso(),
                        ),
                    )
                saved += 1
        return saved

    @staticmethod
    def _row_to_beatmap(row: sqlite3.Row) -> Beatmap:
        try:
            owners = tuple(json.loads(row["owners"] or "[]"))
        except (ValueError, TypeError):
            owners = ()
        return Beatmap(
            id=int(row["id"]),
            beatmapset_id=int(row["beatmapset_id"]),
            mode=row["mode"] or "osu",
            version=row["version"] or "",
            difficulty_rating=row["difficulty_rating"],
            total_length=row["total_length"],
            hit_length=row["hit_length"],
            bpm=row["bpm"],
            ar=row["ar"],
            od=row["od"],
            cs=row["cs"],
            hp=row["hp"],
            max_combo=row["max_combo"],
            count_circles=row["count_circles"],
            count_sliders=row["count_sliders"],
            count_spinners=row["count_spinners"],
            status=row["status"],
            url=row["url"],
            owners=owners,
        )

    def _row_to_beatmapset(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        beatmap_rows: list[sqlite3.Row] | None = None,
    ) -> Beatmapset:
        if beatmap_rows is None:
            beatmap_rows = conn.execute(
                "SELECT * FROM beatmaps WHERE beatmapset_id = ? "
                "ORDER BY difficulty_rating IS NULL, difficulty_rating",
                (row["id"],),
            ).fetchall()
        try:
            tags = tuple(json.loads(row["tags"] or "[]"))
        except (ValueError, TypeError):
            tags = ()
        return Beatmapset(
            id=int(row["id"]),
            artist=row["artist"],
            artist_unicode=row["artist_unicode"],
            title=row["title"],
            title_unicode=row["title_unicode"],
            creator=row["creator"],
            status=row["status"],
            bpm=row["bpm"],
            favourite_count=row["favourite_count"],
            play_count=row["play_count"],
            rating=row["rating"],
            genre_id=row["genre_id"],
            language_id=row["language_id"],
            tags=tags,
            nsfw=bool(row["nsfw"]),
            video=bool(row["video"]),
            storyboard=bool(row["storyboard"]),
            preview_url=row["preview_url"],
            cover_url=row["cover_url"],
            ranked_date=row["ranked_date"],
            submitted_date=row["submitted_date"],
            last_updated=row["last_updated"],
            beatmaps=tuple(self._row_to_beatmap(b) for b in beatmap_rows),
            source=row["source"],
            fetched_at=row["fetched_at"],
        )

    def get_beatmapset(self, beatmapset_id: int) -> Beatmapset | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM beatmapsets WHERE id = ?", (int(beatmapset_id),)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_beatmapset(conn, row)

    def get_beatmapsets(self, ids: Iterable[int]) -> dict[int, Beatmapset]:
        result: dict[int, Beatmapset] = {}
        for set_id in dict.fromkeys(int(i) for i in ids):
            beatmapset = self.get_beatmapset(set_id)
            if beatmapset is not None:
                result[set_id] = beatmapset
        return result

    def list_beatmapsets(
        self, limit: int = 5000, *, balanced: bool = False
    ) -> list[Beatmapset]:
        """Return stored beatmapsets for offline re-ranking.

        The default result is newest-first and bounded. ``balanced=True``
        instead takes an evenly spaced deterministic sample across the whole
        id range, so a catalog larger than ``limit`` is not permanently
        represented by its newest slice only. Beatmap rows are loaded only for
        the selected sets and in bounded chunks.
        """
        bounded = max(1, min(int(limit), 20000))
        with self.connection() as conn:
            if balanced:
                rows = conn.execute(
                    """
                    WITH ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (ORDER BY id) - 1 AS _row,
                               COUNT(*) OVER () AS _total
                        FROM beatmapsets
                    )
                    SELECT * FROM ranked
                    WHERE _total <= ?
                       OR ((_row + 1) * ?) / _total >
                          (_row * ?) / _total
                    ORDER BY id
                    LIMIT ?
                    """,
                    (bounded, bounded, bounded, bounded),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM beatmapsets "
                    "ORDER BY fetched_at DESC, id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            if not rows:
                return []
            selected = [int(row["id"]) for row in rows]
            grouped: dict[int, list[sqlite3.Row]] = {}
            # SQLite builds allow a large parameter list, but chunking keeps
            # this safe on older builds and bounds each statement.
            for start in range(0, len(selected), 400):
                chunk = selected[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                beatmap_rows = conn.execute(
                    "SELECT * FROM beatmaps "
                    f"WHERE beatmapset_id IN ({placeholders}) "
                    "ORDER BY beatmapset_id, difficulty_rating IS NULL, "
                    "difficulty_rating",
                    chunk,
                ).fetchall()
                for beatmap_row in beatmap_rows:
                    grouped.setdefault(
                        int(beatmap_row["beatmapset_id"]), []
                    ).append(beatmap_row)
            return [
                self._row_to_beatmapset(
                    conn, row, grouped.get(int(row["id"]), [])
                )
                for row in rows
            ]

    def beatmapset_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM beatmapsets").fetchone()
            return int(row["n"]) if row else 0

    # -- attributes --------------------------------------------------------
    def save_attributes(self, beatmap_id: int, attributes: dict[str, Any]) -> None:
        with self.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM beatmaps WHERE id = ?", (int(beatmap_id),)
            ).fetchone()
            if exists is None:
                raise StorageError(
                    f"Нельзя сохранить attributes: beatmap {beatmap_id} не сохранён"
                )
            conn.execute(
                """
                INSERT INTO beatmap_attributes (
                    beatmap_id, star_rating, aim_difficulty, speed_difficulty,
                    slider_factor, max_combo, source, fetched_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(beatmap_id) DO UPDATE SET
                    star_rating=excluded.star_rating,
                    aim_difficulty=excluded.aim_difficulty,
                    speed_difficulty=excluded.speed_difficulty,
                    slider_factor=excluded.slider_factor,
                    max_combo=excluded.max_combo,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                (
                    int(beatmap_id),
                    attributes.get("star_rating"),
                    attributes.get("aim_difficulty"),
                    attributes.get("speed_difficulty"),
                    attributes.get("slider_factor"),
                    attributes.get("max_combo"),
                    attributes.get("source", "osu_api_attributes"),
                    attributes.get("fetched_at", now_iso()),
                ),
            )

    def get_attributes(self, beatmap_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM beatmap_attributes WHERE beatmap_id = ?",
                (int(beatmap_id),),
            ).fetchone()
            return dict(row) if row else None

    def all_attributes(
        self, beatmap_ids: Iterable[int] | None = None
    ) -> dict[int, dict[str, Any]]:
        """Return stored attributes, optionally bounded to the needed ids."""

        if beatmap_ids is None:
            with self.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM beatmap_attributes"
                ).fetchall()
            return {int(row["beatmap_id"]): dict(row) for row in rows}

        wanted = list(dict.fromkeys(int(value) for value in beatmap_ids))
        result: dict[int, dict[str, Any]] = {}
        with self.connection() as conn:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT * FROM beatmap_attributes "
                    f"WHERE beatmap_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[int(row["beatmap_id"])] = dict(row)
        return result

    # -- search cache ------------------------------------------------------
    def cache_get(self, key: str) -> dict[str, Any] | None:
        now = now_iso()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM search_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                return None
            try:
                data = json.loads(row["payload"])
            except (ValueError, TypeError):
                conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
                return None
            return data if isinstance(data, dict) else None

    def cache_set(self, key: str, payload: dict[str, Any], ttl_minutes: int) -> None:
        if ttl_minutes <= 0:
            return
        created = datetime.now(timezone.utc)
        expires = created + timedelta(minutes=ttl_minutes)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO search_cache (key, payload, created_at, expires_at)
                VALUES (?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    payload=excluded.payload,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                (
                    key,
                    json.dumps(payload, ensure_ascii=False),
                    created.replace(microsecond=0).isoformat(),
                    expires.replace(microsecond=0).isoformat(),
                ),
            )

    def cache_clear(self) -> dict[str, int]:
        with self.connection() as conn:
            cache = conn.execute("DELETE FROM search_cache").rowcount
            attrs = conn.execute("DELETE FROM beatmap_attributes").rowcount
        return {"search_cache": cache, "attributes": attrs}

    def cache_stats(self) -> dict[str, Any]:
        now = now_iso()
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM search_cache").fetchone()["n"]
            expired = conn.execute(
                "SELECT COUNT(*) AS n FROM search_cache WHERE expires_at <= ?", (now,)
            ).fetchone()["n"]
            oldest = conn.execute(
                "SELECT MIN(created_at) AS v FROM search_cache"
            ).fetchone()["v"]
            newest = conn.execute(
                "SELECT MAX(created_at) AS v FROM search_cache"
            ).fetchone()["v"]
            attrs = conn.execute(
                "SELECT COUNT(*) AS n FROM beatmap_attributes"
            ).fetchone()["n"]
        size = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                size += path.stat().st_size
        return {
            "entries": int(total),
            "expired": int(expired),
            "oldest": oldest,
            "newest": newest,
            "attributes": int(attrs),
            "db_bytes": size,
        }

    def prune_cache(self) -> int:
        with self.connection() as conn:
            return conn.execute(
                "DELETE FROM search_cache WHERE expires_at <= ?", (now_iso(),)
            ).rowcount

    # -- presets -----------------------------------------------------------
    @staticmethod
    def _row_to_preset(row: sqlite3.Row) -> Preset:
        try:
            raw = json.loads(row["config_json"])
            preset_config = PresetConfig.from_dict(raw)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageError(f"Повреждённый пресет id={row['id']}: {exc}") from exc
        return Preset(
            id=int(row["id"]),
            config=preset_config,
            is_builtin=bool(row["is_builtin"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_presets(self) -> list[Preset]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM presets ORDER BY is_builtin DESC, id ASC"
            ).fetchall()
        presets: list[Preset] = []
        for row in rows:
            try:
                presets.append(self._row_to_preset(row))
            except StorageError as exc:
                logger.error("%s", exc)
        return presets

    def get_preset(self, preset_id: int) -> Preset | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM presets WHERE id = ?", (int(preset_id),)
            ).fetchone()
        return self._row_to_preset(row) if row else None

    def get_preset_by_name(self, name: str) -> Preset | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM presets WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
        return self._row_to_preset(row) if row else None

    def create_preset(
        self, preset_config: PresetConfig, is_builtin: bool = False
    ) -> Preset:
        stamp = now_iso()
        with self.connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO presets
                        (name, is_builtin, config_json, created_at, updated_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        preset_config.name,
                        int(is_builtin),
                        json.dumps(preset_config.to_dict(), ensure_ascii=False),
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"Пресет с именем «{preset_config.name}» уже существует"
                ) from exc
            preset_id = int(cursor.lastrowid)
        preset = self.get_preset(preset_id)
        assert preset is not None
        return preset

    def update_preset(self, preset_id: int, preset_config: PresetConfig) -> Preset:
        with self.connection() as conn:
            try:
                result = conn.execute(
                    """
                    UPDATE presets SET name = ?, config_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        preset_config.name,
                        json.dumps(preset_config.to_dict(), ensure_ascii=False),
                        now_iso(),
                        int(preset_id),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"Пресет с именем «{preset_config.name}» уже существует"
                ) from exc
            if result.rowcount == 0:
                raise StorageError(f"Пресет id={preset_id} не найден")
        preset = self.get_preset(preset_id)
        assert preset is not None
        return preset

    def delete_preset(self, preset_id: int) -> bool:
        with self.connection() as conn:
            result = conn.execute(
                "DELETE FROM presets WHERE id = ?", (int(preset_id),)
            )
            return result.rowcount > 0

    # -- feedback ----------------------------------------------------------
    def set_feedback(
        self, beatmapset_id: int, verdicts: Iterable[str]
    ) -> tuple[str, ...]:
        clean = tuple(dict.fromkeys(v for v in verdicts if v))
        unknown = set(clean) - set(VERDICTS)
        if unknown:
            raise StorageError(f"Неизвестные оценки: {', '.join(sorted(unknown))}")
        stamp = now_iso()
        with self.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM beatmapsets WHERE id = ?", (int(beatmapset_id),)
            ).fetchone()
            if exists is None:
                raise StorageError(
                    f"Beatmapset {beatmapset_id} не сохранён; сначала загрузите карту"
                )
            if clean:
                placeholders = ",".join("?" for _ in clean)
                conn.execute(
                    "DELETE FROM feedback WHERE beatmapset_id = ? "
                    f"AND verdict NOT IN ({placeholders})",
                    (int(beatmapset_id), *clean),
                )
            else:
                conn.execute(
                    "DELETE FROM feedback WHERE beatmapset_id = ?",
                    (int(beatmapset_id),),
                )
            for verdict in clean:
                created = conn.execute(
                    "SELECT created_at FROM feedback "
                    "WHERE beatmapset_id = ? AND verdict = ?",
                    (int(beatmapset_id), verdict),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO feedback
                        (beatmapset_id, verdict, created_at, updated_at)
                    VALUES (?,?,?,?)
                    ON CONFLICT(beatmapset_id, verdict) DO UPDATE SET
                        updated_at=excluded.updated_at
                    """,
                    (
                        int(beatmapset_id),
                        verdict,
                        created["created_at"] if created else stamp,
                        stamp,
                    ),
                )
        return clean

    def remove_feedback(self, beatmapset_id: int, verdict: str | None = None) -> int:
        with self.connection() as conn:
            if verdict is None:
                result = conn.execute(
                    "DELETE FROM feedback WHERE beatmapset_id = ?",
                    (int(beatmapset_id),),
                )
            else:
                result = conn.execute(
                    "DELETE FROM feedback WHERE beatmapset_id = ? AND verdict = ?",
                    (int(beatmapset_id), verdict),
                )
            return result.rowcount

    def get_feedback(self, beatmapset_id: int) -> tuple[str, ...]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT verdict FROM feedback WHERE beatmapset_id = ? ORDER BY verdict",
                (int(beatmapset_id),),
            ).fetchall()
        return tuple(row["verdict"] for row in rows)

    def all_feedback(self) -> dict[int, tuple[str, ...]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT beatmapset_id, verdict FROM feedback ORDER BY beatmapset_id"
            ).fetchall()
        result: dict[int, list[str]] = {}
        for row in rows:
            result.setdefault(int(row["beatmapset_id"]), []).append(row["verdict"])
        return {key: tuple(value) for key, value in result.items()}

    def feedback_rows(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT f.beatmapset_id, f.verdict, f.created_at, f.updated_at,
                       s.artist, s.title, s.creator
                FROM feedback f JOIN beatmapsets s ON s.id = f.beatmapset_id
                ORDER BY f.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def feedback_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM feedback GROUP BY verdict"
            ).fetchall()
        return {row["verdict"]: int(row["n"]) for row in rows}

    # -- favorites ---------------------------------------------------------
    def set_favorite(self, beatmapset_id: int, value: bool) -> bool:
        with self.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM beatmapsets WHERE id = ?", (int(beatmapset_id),)
            ).fetchone()
            if exists is None:
                raise StorageError(
                    f"Beatmapset {beatmapset_id} не сохранён; сначала загрузите карту"
                )
            if value:
                conn.execute(
                    "INSERT INTO favorites (beatmapset_id, created_at) VALUES (?,?) "
                    "ON CONFLICT(beatmapset_id) DO NOTHING",
                    (int(beatmapset_id), now_iso()),
                )
            else:
                conn.execute(
                    "DELETE FROM favorites WHERE beatmapset_id = ?",
                    (int(beatmapset_id),),
                )
        return value

    def toggle_favorite(self, beatmapset_id: int) -> bool:
        current = self.is_favorite(beatmapset_id)
        return self.set_favorite(beatmapset_id, not current)

    def is_favorite(self, beatmapset_id: int) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM favorites WHERE beatmapset_id = ?",
                (int(beatmapset_id),),
            ).fetchone()
            return row is not None

    def favorite_ids(self) -> frozenset[int]:
        with self.connection() as conn:
            rows = conn.execute("SELECT beatmapset_id FROM favorites").fetchall()
        return frozenset(int(row["beatmapset_id"]) for row in rows)

    def favorite_rows(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT f.beatmapset_id, f.created_at, s.artist, s.title, s.creator,
                       s.status, s.bpm, s.genre_id
                FROM favorites f JOIN beatmapsets s ON s.id = f.beatmapset_id
                ORDER BY f.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    # -- history -----------------------------------------------------------
    def record_history(
        self,
        items: Iterable[tuple[int, float | None, int | None]],
        surface: str = "search",
    ) -> int:
        stamp = now_iso()
        count = 0
        with self.connection() as conn:
            for set_id, score, position in items:
                conn.execute(
                    """
                    INSERT INTO history
                        (beatmapset_id, shown_at, surface, rank_score, position)
                    VALUES (?,?,?,?,?)
                    """,
                    (int(set_id), stamp, surface, score, position),
                )
                count += 1
        return count

    def seen_counts(self) -> dict[int, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT beatmapset_id, COUNT(*) AS n FROM history "
                "GROUP BY beatmapset_id"
            ).fetchall()
        return {int(row["beatmapset_id"]): int(row["n"]) for row in rows}

    def seen_ids(self) -> frozenset[int]:
        return frozenset(self.seen_counts())

    def recent_sets(
        self,
        limit: int = 40,
        *,
        surfaces: Iterable[str] | None = None,
    ) -> tuple[Beatmapset, ...]:
        """Return recently shown sets, optionally limited to history surfaces.

        Random Selection uses ``surfaces=("random", "random_pick")`` so a
        normal explicit search does not consume the bounded randomization
        window. The default keeps the broader behaviour used by novelty and
        repeat-penalty ranking.
        """

        selected = tuple(dict.fromkeys(str(item) for item in (surfaces or ())))
        where = ""
        params: list[object] = []
        if selected:
            where = " WHERE surface IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        params.append(int(limit))
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT beatmapset_id, MAX(shown_at) AS last_shown
                FROM history{where} GROUP BY beatmapset_id
                ORDER BY last_shown DESC LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[Beatmapset] = []
        for row in rows:
            beatmapset = self.get_beatmapset(int(row["beatmapset_id"]))
            if beatmapset is not None:
                result.append(beatmapset)
        return tuple(result)

    def history_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT h.id, h.beatmapset_id, h.shown_at, h.surface, h.rank_score,
                       h.position, s.artist, s.title, s.creator, s.status
                FROM history h JOIN beatmapsets s ON s.id = h.beatmapset_id
                ORDER BY h.shown_at DESC, h.id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def history_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM history").fetchone()
            return int(row["n"]) if row else 0

    # -- preferences -------------------------------------------------------
    def get_preference(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM preferences WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_preference(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO preferences (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def all_preferences(self) -> dict[str, str]:
        with self.connection() as conn:
            rows = conn.execute("SELECT key, value FROM preferences").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # -- search runs -------------------------------------------------------
    def record_search_run(self, result: Any) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO search_runs (
                    started_at, finished_at, requests, cache_hits, total_fetched,
                    passed_filters, result_count, complete, strategies_json,
                    errors_json, excluded_json, filters_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.started_at,
                    result.finished_at,
                    result.api_requests,
                    result.cache_hits,
                    result.total_fetched,
                    result.passed_filters,
                    result.count,
                    int(result.complete),
                    json.dumps(list(result.strategies), ensure_ascii=False),
                    json.dumps(list(result.errors), ensure_ascii=False),
                    json.dumps(result.excluded, ensure_ascii=False),
                    json.dumps(
                        result.filters_used.to_dict() if result.filters_used else {},
                        ensure_ascii=False,
                    ),
                ),
            )
            return int(cursor.lastrowid)

    def last_search_run(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM search_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ("strategies_json", "errors_json", "excluded_json", "filters_json"):
            try:
                data[key.replace("_json", "")] = json.loads(data.pop(key))
            except (ValueError, TypeError):
                data[key.replace("_json", "")] = None
        return data

    def search_run_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM search_runs").fetchone()
            return int(row["n"]) if row else 0

    # -- export / import ---------------------------------------------------
    def export_user_data(self) -> dict[str, Any]:
        feedback = self.export_feedback_rows()
        favorites = self.export_favorite_rows()
        referenced = {row["beatmapset_id"] for row in feedback} | {
            row["beatmapset_id"] for row in favorites
        }
        referenced |= {
            int(row["beatmapset_id"]) for row in self.history_rows(limit=1000)
        }
        beatmapsets = [
            beatmapset_to_dict(beatmapset)
            for beatmapset in self.get_beatmapsets(sorted(referenced)).values()
        ]
        presets = [
            {
                "name": preset.name,
                "is_builtin": preset.is_builtin,
                "config": preset.config.to_dict(),
                "created_at": preset.created_at,
                "updated_at": preset.updated_at,
            }
            for preset in self.list_presets()
        ]
        return {
            "app": config.APP_NAME,
            "app_version": config.APP_VERSION,
            "schema_version": config.SETTINGS_SCHEMA_VERSION,
            "exported_at": now_iso(),
            "presets": presets,
            "feedback": feedback,
            "favorites": favorites,
            "preferences": self.all_preferences(),
            "history": self.history_rows(limit=1000),
            "beatmapsets": beatmapsets,
        }

    def export_feedback_rows(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT beatmapset_id, verdict, created_at, updated_at FROM feedback"
            ).fetchall()
        return [dict(row) for row in rows]

    def export_favorite_rows(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT beatmapset_id, created_at FROM favorites"
            ).fetchall()
        return [dict(row) for row in rows]

    def import_user_data(
        self, data: dict[str, Any], overwrite_presets: bool = False
    ) -> dict[str, int]:
        if not isinstance(data, dict):
            raise UserDataImportError("Импорт: ожидался JSON-объект")
        if data.get("schema_version") != config.SETTINGS_SCHEMA_VERSION:
            raise UserDataImportError(
                "Импорт: неподдерживаемая версия схемы "
                f"{data.get('schema_version')!r}"
            )
        try:
            from presets import validate_preset_config

            raw_presets = data.get("presets", [])
            if not isinstance(raw_presets, list):
                raise UserDataImportError("Импорт: presets должен быть массивом")
            parsed_presets: list[PresetConfig] = []
            for index, item in enumerate(raw_presets):
                if isinstance(item, dict) and "config" in item:
                    cfg = PresetConfig.from_dict(
                        item["config"], f"presets[{index}].config"
                    )
                else:
                    cfg = PresetConfig.from_dict(item, f"presets[{index}]")
                validate_preset_config(cfg)
                parsed_presets.append(cfg)

            raw_feedback = data.get("feedback", [])
            if not isinstance(raw_feedback, list):
                raise UserDataImportError("Импорт: feedback должен быть массивом")
            parsed_feedback: list[tuple[int, str]] = []
            for index, item in enumerate(raw_feedback):
                if not isinstance(item, dict):
                    raise UserDataImportError(
                        f"Импорт: feedback[{index}] должен быть объектом"
                    )
                set_id = int(item.get("beatmapset_id", 0))
                verdict = item.get("verdict")
                if set_id <= 0 or verdict not in VERDICTS:
                    raise UserDataImportError(
                        f"Импорт: некорректная оценка feedback[{index}]"
                    )
                parsed_feedback.append((set_id, str(verdict)))

            raw_favorites = data.get("favorites", [])
            if not isinstance(raw_favorites, list):
                raise UserDataImportError("Импорт: favorites должен быть массивом")
            parsed_favorites: list[int] = []
            for index, item in enumerate(raw_favorites):
                if isinstance(item, dict):
                    set_id = int(item.get("beatmapset_id", 0))
                else:
                    set_id = int(item)
                if set_id <= 0:
                    raise UserDataImportError(f"Импорт: некорректный favorite[{index}]")
                parsed_favorites.append(set_id)

            raw_sets = data.get("beatmapsets", [])
            if not isinstance(raw_sets, list):
                raise UserDataImportError("Импорт: beatmapsets должен быть массивом")
            parsed_sets: list[Beatmapset] = []
            for index, item in enumerate(raw_sets):
                if not isinstance(item, dict):
                    raise UserDataImportError(
                        f"Импорт: beatmapsets[{index}] должен быть объектом"
                    )
                parsed_sets.append(beatmapset_from_dict(item))

            raw_preferences = data.get("preferences", {})
            if not isinstance(raw_preferences, dict):
                raise UserDataImportError("Импорт: preferences должен быть объектом")
            preferences = {str(k): str(v) for k, v in raw_preferences.items()}

            raw_history = data.get("history", [])
            if not isinstance(raw_history, list):
                raise UserDataImportError("Импорт: history должен быть массивом")
            parsed_history: list[tuple[int, str, str, float | None, int | None]] = []
            for index, item in enumerate(raw_history):
                if not isinstance(item, dict):
                    raise UserDataImportError(
                        f"Импорт: history[{index}] должен быть объектом"
                    )
                set_id = int(item.get("beatmapset_id", 0))
                if set_id <= 0:
                    raise UserDataImportError(f"Импорт: некорректный history[{index}]")
                parsed_history.append(
                    (
                        set_id,
                        str(item.get("shown_at") or now_iso()),
                        str(item.get("surface") or "import"),
                        item.get("rank_score"),
                        item.get("position"),
                    )
                )
        except UserDataImportError:
            raise
        except Exception as exc:
            raise UserDataImportError(f"Импорт отклонён: {exc}") from exc

        # Referential validation before the first write, so a failed import
        # cannot leave a half-applied state.
        parsed_set_ids = {beatmapset.id for beatmapset in parsed_sets}
        referenced: set[int] = (
            {row[0] for row in parsed_feedback}
            | set(parsed_favorites)
            | {row[0] for row in parsed_history}
        )
        with self.connection() as conn:
            existing_ids = {
                int(row["id"])
                for row in conn.execute("SELECT id FROM beatmapsets").fetchall()
            }
        missing = referenced - parsed_set_ids - existing_ids
        if missing:
            preview = ", ".join(str(i) for i in sorted(missing)[:5])
            raise UserDataImportError(
                "Импорт отклонён: в файле нет метаданных beatmapset для "
                f"оценок/избранного ({preview}). Импортируйте полный экспорт."
            )

        saved_sets = self.save_beatmapsets(parsed_sets)
        created_presets = 0
        updated_presets = 0
        renamed = 0
        for cfg in parsed_presets:
            existing = self.get_preset_by_name(cfg.name)
            if existing is None:
                self.create_preset(cfg, is_builtin=False)
                created_presets += 1
                continue
            if overwrite_presets:
                self.update_preset(existing.id, cfg)
                updated_presets += 1
                continue
            base = f"{cfg.name} (imported)"
            name = base
            suffix = 2
            while self.get_preset_by_name(name) is not None:
                name = f"{base} {suffix}"
                suffix += 1
            renamed_cfg = PresetConfig.from_dict({**cfg.to_dict(), "name": name})
            self.create_preset(renamed_cfg, is_builtin=False)
            created_presets += 1
            renamed += 1

        with self.connection() as conn:
            for set_id, verdict in parsed_feedback:
                conn.execute(
                    """
                    INSERT INTO feedback
                        (beatmapset_id, verdict, created_at, updated_at)
                    VALUES (?,?,?,?)
                    ON CONFLICT(beatmapset_id, verdict) DO NOTHING
                    """,
                    (set_id, verdict, now_iso(), now_iso()),
                )
            for set_id in parsed_favorites:
                conn.execute(
                    "INSERT INTO favorites (beatmapset_id, created_at) VALUES (?,?) "
                    "ON CONFLICT(beatmapset_id) DO NOTHING",
                    (set_id, now_iso()),
                )
            for key, value in preferences.items():
                conn.execute(
                    "INSERT INTO preferences (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            for set_id, shown_at, surface, score, position in parsed_history:
                conn.execute(
                    """
                    INSERT INTO history
                        (beatmapset_id, shown_at, surface, rank_score, position)
                    VALUES (?,?,?,?,?)
                    """,
                    (set_id, shown_at, surface, score, position),
                )
        return {
            "beatmapsets": saved_sets,
            "presets_created": created_presets,
            "presets_updated": updated_presets,
            "presets_renamed": renamed,
            "feedback": len(parsed_feedback),
            "favorites": len(parsed_favorites),
            "history": len(parsed_history),
        }

    # -- backup / restore --------------------------------------------------
    def backup_to(self, destination: str | Path | None = None) -> Path:
        if destination is None:
            backup_dir = self.path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = backup_dir / f"osu_picker-{stamp}.db"
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        return destination

    @staticmethod
    def validate_backup(path: str | Path) -> int:
        candidate = Path(path)
        if not candidate.exists() or not candidate.is_file():
            raise StorageError("Файл резервной копии не найден")
        try:
            conn = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise StorageError(f"Не удалось открыть резервную копию: {exc}") from exc
        try:
            try:
                check = conn.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise StorageError(
                    "Файл не похож на базу данных SQLite"
                ) from exc
            if not check or check[0] != "ok":
                raise StorageError("Резервная копия повреждена (integrity_check)")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version <= 0:
                raise StorageError("В файле нет схемы Map Picker")
            if version > config.DB_SCHEMA_VERSION:
                raise StorageError(
                    f"Копия создана более новой версией (schema {version})"
                )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {"beatmapsets", "feedback", "favorites", "presets"}
            if not required <= tables:
                raise StorageError("В файле нет обязательных таблиц Map Picker")
            return version
        finally:
            conn.close()

    def restore_from(
        self, source: str | Path, make_safety_backup: bool = True
    ) -> Path | None:
        source_path = Path(source)
        self.validate_backup(source_path)
        safety: Path | None = None
        if make_safety_backup:
            safety = self.backup_to()
        with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as src:
            with self.connection() as dst:
                src.backup(dst)
        self._migrate()
        return safety

    def database_info(self) -> dict[str, Any]:
        stats = self.cache_stats()
        return {
            "path": str(self.path),
            "size_bytes": stats["db_bytes"],
            "schema_version": self.schema_version(),
            "beatmapsets": self.beatmapset_count(),
            "presets": len(self.list_presets()),
            "history": self.history_count(),
            "searches": self.search_run_count(),
        }

    # -- maintenance -------------------------------------------------------
    def vacuum(self) -> None:
        with self.connection() as conn:
            conn.execute("VACUUM")

    def delete_database_files(self) -> None:  # pragma: no cover - destructive
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                os.remove(path)
