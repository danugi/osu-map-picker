from __future__ import annotations

import json

import pytest
from conftest import make_beatmap, make_beatmapset

import config
import presets
from models import PresetConfig
from storage import Storage, StorageError, UserDataImportError


def test_schema_is_created_and_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "db.sqlite"
    store = Storage(path)
    assert store.schema_version() == config.DB_SCHEMA_VERSION
    with store.connection() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "beatmapsets",
        "beatmaps",
        "beatmap_attributes",
        "search_cache",
        "presets",
        "feedback",
        "favorites",
        "history",
        "preferences",
        "search_runs",
    } <= tables
    again = Storage(path)
    assert again.schema_version() == config.DB_SCHEMA_VERSION


def test_beatmapset_roundtrip_with_unicode(storage: Storage) -> None:
    beatmapset = make_beatmapset(
        10,
        artist="Ююёппе",
        artist_unicode="Yuyoyuppe",
        title="Палетт",
        title_unicode="Palette",
        creator="Маппер",
        tags=("айм", "jumps"),
        beatmaps=(
            make_beatmap(101, 10, stars=4.2, version="Normal"),
            make_beatmap(102, 10, stars=5.8, version="Insane"),
        ),
    )
    storage.save_beatmapset(beatmapset)
    loaded = storage.get_beatmapset(10)
    assert loaded is not None
    assert loaded.title_unicode == "Palette"
    assert loaded.display_title == "Palette"
    assert loaded.artist == "Ююёппе"
    assert loaded.display_artist == "Yuyoyuppe"
    assert loaded.tags == ("айм", "jumps")
    assert [b.version for b in loaded.beatmaps] == ["Normal", "Insane"]
    assert len(loaded.beatmaps) == 2


def test_attributes_can_be_loaded_only_for_needed_beatmaps(
    storage: Storage,
) -> None:
    storage.save_beatmapsets(
        [
            make_beatmapset(
                1, beatmaps=(make_beatmap(11, 1),)
            ),
            make_beatmapset(
                2, beatmaps=(make_beatmap(21, 2),)
            ),
        ]
    )
    storage.save_attributes(11, {"star_rating": 5.0})
    storage.save_attributes(21, {"star_rating": 6.0})
    assert set(storage.all_attributes()) == {11, 21}
    only = storage.all_attributes([11, 11])
    assert set(only) == {11}
    assert only[11]["star_rating"] == 5.0
    assert storage.all_attributes([]) == {}


def test_balanced_catalog_listing_spreads_across_ids(storage: Storage) -> None:
    storage.save_beatmapsets(
        [make_beatmapset(index) for index in range(1, 101)]
    )
    newest = storage.list_beatmapsets(limit=10)
    balanced = storage.list_beatmapsets(limit=10, balanced=True)
    newest_ids = [item.id for item in newest]
    balanced_ids = [item.id for item in balanced]
    assert len(balanced_ids) == 10
    assert balanced_ids != newest_ids
    # Newest-first would contain only ids 100..91; the balanced sample must
    # reach the older part of the catalog as well.
    assert min(balanced_ids) <= 10
    assert max(balanced_ids) >= 90
    assert all(item.beatmaps for item in balanced)


def test_refetch_does_not_duplicate_and_keeps_user_data(storage: Storage) -> None:
    beatmapset = make_beatmapset(1, title="Old")
    storage.save_beatmapset(beatmapset)
    storage.set_feedback(1, ["like"])
    storage.set_favorite(1, True)

    newer = make_beatmapset(
        1,
        title="New",
        fetched_at="2026-06-01T00:00:00+00:00",
        beatmaps=(make_beatmap(11, 1, stars=5.0, version="Insane"),),
    )
    storage.save_beatmapset(newer)
    assert storage.beatmapset_count() == 1
    loaded = storage.get_beatmapset(1)
    assert loaded is not None
    assert loaded.title == "New"
    with storage.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM beatmaps WHERE beatmapset_id = 1"
        ).fetchone()[0]
    assert count == 1
    assert storage.get_feedback(1) == ("like",)
    assert storage.is_favorite(1)


def test_older_payload_does_not_overwrite_newer_metadata(storage: Storage) -> None:
    storage.save_beatmapset(
        make_beatmapset(1, title="New", fetched_at="2026-06-01T00:00:00+00:00")
    )
    storage.save_beatmapset(
        make_beatmapset(1, title="Old", fetched_at="2025-01-01T00:00:00+00:00")
    )
    loaded = storage.get_beatmapset(1)
    assert loaded is not None
    assert loaded.title == "New"


def test_feedback_set_replace_and_clear(storage: Storage) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    storage.set_feedback(1, ["like", "too_hard"])
    assert storage.get_feedback(1) == ("like", "too_hard")
    storage.set_feedback(1, ["like"])
    assert storage.get_feedback(1) == ("like",)
    storage.set_feedback(1, [])
    assert storage.get_feedback(1) == ()
    with pytest.raises(StorageError):
        storage.set_feedback(1, ["nonsense"])


def test_feedback_requires_saved_beatmapset(storage: Storage) -> None:
    with pytest.raises(StorageError):
        storage.set_feedback(999, ["like"])


def test_favorites_toggle(storage: Storage) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    assert not storage.is_favorite(1)
    assert storage.toggle_favorite(1) is True
    assert storage.is_favorite(1)
    assert storage.favorite_ids() == frozenset({1})
    assert storage.toggle_favorite(1) is False
    assert storage.favorite_ids() == frozenset()


def test_cache_hit_miss_and_expiry(storage: Storage) -> None:
    storage.cache_set("key", {"value": 1}, ttl_minutes=10)
    assert storage.cache_get("key") == {"value": 1}
    storage.cache_set("zero", {"value": 2}, ttl_minutes=0)
    assert storage.cache_get("zero") is None
    with storage.connection() as conn:
        conn.execute(
            "UPDATE search_cache SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE key = 'key'"
        )
    assert storage.cache_get("key") is None
    with storage.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
    assert count == 0


def test_corrupted_cache_row_is_discarded(storage: Storage) -> None:
    with storage.connection() as conn:
        conn.execute(
            "INSERT INTO search_cache (key, payload, created_at, expires_at) "
            "VALUES ('bad', '{not json', '2026-01-01T00:00:00+00:00', "
            "'2999-01-01T00:00:00+00:00')"
        )
    assert storage.cache_get("bad") is None


def test_clearing_cache_keeps_user_data(storage: Storage) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    storage.set_feedback(1, ["like"])
    storage.set_favorite(1, True)
    storage.cache_set("key", {"x": 1}, 10)
    preset = storage.get_preset_by_name("Mixed Aim")
    assert preset is not None
    result = storage.cache_clear()
    assert result["search_cache"] == 1
    assert storage.get_feedback(1) == ("like",)
    assert storage.is_favorite(1)
    assert storage.get_preset(preset.id) is not None


def test_history_and_seen_counts(storage: Storage) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    storage.save_beatmapset(make_beatmapset(2))
    storage.record_history([(1, 0.9, 1), (2, 0.8, 2)])
    storage.record_history([(1, 0.7, 1)])
    assert storage.seen_counts() == {1: 2, 2: 1}
    assert storage.seen_ids() == frozenset({1, 2})
    assert storage.history_count() == 3
    recent = storage.recent_sets()
    assert {item.id for item in recent} == {1, 2}


def test_recent_sets_can_be_limited_to_randomization_surfaces(
    storage: Storage,
) -> None:
    for set_id in (1, 2, 3):
        storage.save_beatmapset(make_beatmapset(set_id))
    storage.record_history(
        [(1, 0.9, 1), (2, 0.8, 2), (3, 0.7, 3)],
        surface="search",
    )
    storage.record_history([(1, 0.9, 1)], surface="random")
    storage.record_history([(2, 0.8, 1)], surface="random_pick")

    random_recent = storage.recent_sets(
        60, surfaces=("random", "random_pick")
    )
    assert {item.id for item in random_recent} == {1, 2}
    assert {item.id for item in storage.recent_sets(60)} == {1, 2, 3}


def test_preset_crud_does_not_touch_ratings(storage: Storage) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    storage.set_feedback(1, ["fine"])
    preset = storage.get_preset_by_name("Mixed Aim")
    assert preset is not None
    edited = PresetConfig.from_dict(preset.config.to_dict())
    edited.name = "Renamed"
    storage.update_preset(preset.id, edited)
    assert storage.get_feedback(1) == ("fine",)
    assert storage.delete_preset(preset.id)
    assert storage.get_feedback(1) == ("fine",)


def test_search_run_is_recorded(storage: Storage) -> None:
    from models import DiscoveryResult

    result = DiscoveryResult(
        total_fetched=5,
        passed_filters=2,
        api_requests=3,
        cache_hits=1,
        complete=False,
        strategies=("ranked:recent",),
        errors=("boom",),
        excluded={"длительность вне диапазона": 3},
        filters_used=None,
    )
    storage.record_search_run(result)
    last = storage.last_search_run()
    assert last is not None
    assert last["requests"] == 3
    assert last["strategies"] == ["ranked:recent"]
    assert last["errors"] == ["boom"]
    assert last["excluded"]["длительность вне диапазона"] == 3


def test_export_import_user_data_roundtrip(tmp_path) -> None:
    source = Storage(tmp_path / "source.db")
    presets.ensure_builtin_presets(source)
    source.save_beatmapset(make_beatmapset(1, artist="Ююёппе"))
    source.set_feedback(1, ["like", "music_dislike"])
    source.set_favorite(1, True)
    source.set_preference("selected_preset", "1")
    source.record_history([(1, 0.8, 1)])
    custom = presets.create_preset(source, "Мой пресет")
    cfg = PresetConfig.from_dict(custom.config.to_dict())
    cfg.filters.stars_max = 6.5
    source.update_preset(custom.id, cfg)

    exported = source.export_user_data()
    assert exported["schema_version"] == config.SETTINGS_SCHEMA_VERSION
    assert any(p["name"] == "Мой пресет" for p in exported["presets"])
    assert exported["beatmapsets"][0]["id"] == 1

    target = Storage(tmp_path / "target.db")
    summary = target.import_user_data(exported)
    assert summary["beatmapsets"] == 1
    assert target.get_feedback(1) == ("like", "music_dislike")
    assert target.is_favorite(1)
    assert target.get_preset_by_name("Мой пресет") is not None
    assert target.get_preference("selected_preset") == "1"
    assert target.history_count() == 1
    loaded = target.get_beatmapset(1)
    assert loaded is not None and loaded.display_artist == "Ююёппе"


def test_import_rejects_wrong_schema_without_writes(empty_storage: Storage) -> None:
    with pytest.raises(UserDataImportError):
        empty_storage.import_user_data({"schema_version": 999})
    assert empty_storage.list_presets() == []


def test_import_rejects_missing_beatmapset_metadata_atomically(tmp_path) -> None:
    target = Storage(tmp_path / "target.db")
    presets.ensure_builtin_presets(target)
    before = [(p.id, p.name) for p in target.list_presets()]
    payload = {
        "schema_version": config.SETTINGS_SCHEMA_VERSION,
        "presets": [presets.default_config("Will not be created").to_dict()],
        "feedback": [{"beatmapset_id": 12345, "verdict": "like"}],
        "favorites": [],
        "history": [],
        "beatmapsets": [],
    }
    with pytest.raises(UserDataImportError):
        target.import_user_data(payload)
    assert [(p.id, p.name) for p in target.list_presets()] == before
    assert target.get_feedback(12345) == ()


def test_backup_and_restore(storage: Storage, tmp_path) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    storage.set_feedback(1, ["like"])
    storage.set_favorite(1, True)
    storage.record_history([(1, 0.9, 1)])

    backup = storage.backup_to(tmp_path / "backup.db")
    assert backup.exists()
    assert Storage.validate_backup(backup) == config.DB_SCHEMA_VERSION

    storage.set_feedback(1, ["hide"])
    storage.set_favorite(1, False)
    safety = storage.restore_from(backup)
    assert safety is not None and safety.exists()
    assert storage.get_feedback(1) == ("like",)
    assert storage.is_favorite(1)
    assert storage.history_count() == 1


def test_validate_backup_rejects_garbage(storage: Storage, tmp_path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_text("this is not sqlite", encoding="utf-8")
    with pytest.raises(StorageError):
        Storage.validate_backup(garbage)
    with pytest.raises(StorageError):
        Storage.validate_backup(tmp_path / "missing.db")


def test_corrupted_preset_row_is_skipped(storage: Storage) -> None:
    preset = storage.get_preset_by_name("Mixed Aim")
    assert preset is not None
    with storage.connection() as conn:
        conn.execute(
            "UPDATE presets SET config_json = 'not json' WHERE id = ?",
            (preset.id,),
        )
    names = [item.name for item in storage.list_presets()]
    assert "Mixed Aim" not in names
    with pytest.raises(StorageError):
        storage.get_preset(preset.id)


def test_v2_migration_adds_analysis_table_without_losing_catalog(
    tmp_path,
) -> None:
    path = tmp_path / "v1.db"
    store = Storage(path)
    store.save_beatmapset(make_beatmapset(7, title="Preserved"))
    store.set_favorite(7, True)
    with store.connection() as conn:
        conn.execute("DROP TABLE beatmap_analysis")
        conn.execute("PRAGMA user_version = 1")
    upgraded = Storage(path)
    assert upgraded.schema_version() == config.DB_SCHEMA_VERSION
    preserved = upgraded.get_beatmapset(7)
    assert preserved is not None and preserved.title == "Preserved"
    assert 7 in upgraded.favorite_ids()
    with upgraded.connection() as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='beatmap_analysis'"
        ).fetchone()
    assert table is not None


def test_database_info(storage: Storage) -> None:
    storage.save_beatmapset(make_beatmapset(1))
    info = storage.database_info()
    assert info["schema_version"] == config.DB_SCHEMA_VERSION
    assert info["beatmapsets"] == 1
    assert info["presets"] == 12
    assert info["path"].endswith(".db")
