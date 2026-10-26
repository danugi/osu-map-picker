from __future__ import annotations

import copy
import json

import pytest
from conftest import make_beatmapset

import config
import presets
from models import PresetConfig
from storage import Storage

CANONICAL = {
    "Mixed Aim",
    "Discovery",
    "All-rounder",
    "Aim Farm",
    "Speed",
    "Streams & Stamina",
    "Bursts",
    "Finger Control",
    "Reading",
    "Accuracy",
    "Tech",
    "Flashlight",
}


def test_builtin_presets_are_curated_complete_and_valid() -> None:
    builtins = presets.builtin_presets()
    names = {preset.name for preset in builtins}
    assert len(builtins) == 12
    assert names == CANONICAL
    assert names.isdisjoint(presets.LEGACY_BUILTIN_NAMES)
    for preset in builtins:
        presets.validate_preset_config(preset)
        assert preset.schema_version == config.SETTINGS_SCHEMA_VERSION


def test_legacy_builtins_are_retained_but_not_offered_by_default(
    storage: Storage,
) -> None:
    legacy = presets.legacy_builtin_presets()
    legacy_names = {preset.name for preset in legacy}
    assert {"Any Taste", "Wide Aim", "Custom", "Streams", "Stamina"} <= legacy_names
    assert legacy_names == presets.LEGACY_BUILTIN_NAMES

    hidden_config = presets.known_builtin_by_name("Wide Aim")
    assert hidden_config is not None
    hidden = storage.create_preset(copy.deepcopy(hidden_config), is_builtin=True)
    visible = presets.visible_presets(storage, include_id=hidden.id)
    assert hidden.id in {preset.id for preset in visible}
    assert hidden.id not in {
        preset.id for preset in presets.visible_presets(storage)
    }
    assert hidden.id in {preset.id for preset in presets.legacy_presets(storage)}


def test_any_taste_legacy_profile_does_not_force_aim() -> None:
    preset = presets.known_builtin_by_name("Any Taste")
    assert preset is not None
    assert preset.ranking.trait_targets == {}
    assert preset.ranking.factors["mapping_fit"].enabled is False
    assert preset.ranking.factors["community"].weight == 0.9
    assert preset.ranking.preferred_genres == []


def test_aim_farm_is_nm_focused_without_mod_bonuses() -> None:
    preset = presets.builtin_by_name("Aim Farm")
    assert preset is not None
    assert preset.filters.duration_max == 150
    assert preset.ranking.farm_target_seconds == 120
    assert preset.ranking.skill_weights == {"aim_classic": 1.0}
    assert preset.ranking.trait_targets == {}
    assert preset.ranking.factors["farm_nm"].enabled is True
    assert preset.ranking.factors["mapping_fit"].enabled is True
    assert "NM" in preset.description
    assert "PP" not in preset.description
    # There is no mod field at all: mod-specific bonuses cannot sneak into the preset.
    assert not hasattr(preset.ranking, "mods")
    assert not hasattr(preset.filters, "mods")


def test_streams_and_stamina_is_one_train_profile() -> None:
    preset = presets.builtin_by_name("Streams & Stamina")
    assert preset is not None
    assert preset.ranking.skill_weights == {
        "stream_consistency": 0.90,
        "stamina_finger": 0.65,
        "stream_long": 0.55,
        "speed_stamina": 0.35,
    }
    assert preset.filters.duration_max == 360
    assert preset.ranking.factors["length"].weight == 0.45


def test_bursts_and_streams_presets_use_separate_evidence() -> None:
    bursts = presets.builtin_by_name("Bursts")
    streams = presets.builtin_by_name("Streams & Stamina")
    assert bursts is not None and streams is not None
    assert bursts.ranking.relevance_targets == [
        "burst_control",
        "speed_bursts",
        "speed_short_bursts",
    ]
    assert all(
        "stream" not in target for target in bursts.ranking.relevance_targets
    )
    assert bursts.ranking.skill_weights["burst_control"] == 1.0
    assert all(
        "burst" not in target
        for target in streams.ranking.relevance_targets
    )
    assert "stream_consistency" in streams.ranking.relevance_targets


def test_non_sequence_presets_do_not_require_stream_or_burst_evidence() -> None:
    for name in ("Aim Farm", "Speed"):
        preset = presets.builtin_by_name(name)
        assert preset is not None
        assert all(
            "stream" not in target and "burst" not in target
            for target in preset.ranking.relevance_targets
        ), name
    for name in ("Stamina", "Flow Aim", "Rhythm Control"):
        preset = presets.known_builtin_by_name(name)
        assert preset is not None
        assert all(
            "stream" not in target and "burst" not in target
            for target in preset.ranking.relevance_targets
        ), name


def _legacy_aim_farm() -> PresetConfig:
    return presets._legacy_aim_farm_config()


def _legacy_mixed_aim() -> PresetConfig:
    return presets._legacy_mixed_aim_config()


def test_v2_migration_adds_presets_and_renames_the_legacy_aim_farm(
    storage: Storage,
) -> None:
    canonical = storage.get_preset_by_name("Aim Farm")
    assert canonical is not None
    storage.delete_preset(canonical.id)
    aim = storage.create_preset(_legacy_aim_farm(), is_builtin=True)
    assert aim.name == "Aim Farm / PP"
    for name in presets._V2_NEW_NAMES:
        old = storage.get_preset_by_name(name)
        if old is not None:
            storage.delete_preset(old.id)
    storage.set_preference("builtins_version", "1")

    created = presets.ensure_builtin_presets(storage)
    assert created == len(presets._V2_NEW_NAMES)
    assert storage.get_preset_by_name("Aim Farm / PP") is None
    upgraded = storage.get_preset_by_name("Aim Farm")
    assert upgraded is not None
    assert upgraded.config.ranking.skill_weights == {"aim_classic": 1.0}
    assert upgraded.config.ranking.factors["farm_nm"].enabled is True


def test_v2_migration_does_not_overwrite_edited_aim_farm(storage: Storage) -> None:
    canonical = storage.get_preset_by_name("Aim Farm")
    assert canonical is not None
    storage.delete_preset(canonical.id)
    aim = storage.create_preset(_legacy_aim_farm(), is_builtin=True)
    edited = _legacy_aim_farm()
    edited.ranking.comfort_stars = 5.95
    edited.filters.bpm_min = 150.0
    storage.update_preset(aim.id, edited)
    for name in presets._V2_NEW_NAMES:
        old = storage.get_preset_by_name(name)
        if old is not None:
            storage.delete_preset(old.id)
    storage.set_preference("builtins_version", "1")

    created = presets.ensure_builtin_presets(storage)
    assert created == len(presets._V2_NEW_NAMES) + 1
    kept = storage.get_preset_by_name("Aim Farm / PP")
    assert kept is not None
    assert kept.config.ranking.skill_weights == {}
    assert kept.config.ranking.comfort_stars == 5.95
    fresh = storage.get_preset_by_name("Aim Farm")
    assert fresh is not None
    assert fresh.id != kept.id
    assert fresh.config.ranking.skill_weights == {"aim_classic": 1.0}


def test_v3_migration_upgrades_unedited_legacy_aim_profiles(
    storage: Storage,
) -> None:
    mixed = storage.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    storage.update_preset(mixed.id, _legacy_mixed_aim())
    storage.create_preset(presets._legacy_wide_aim_config(), is_builtin=True)
    storage.set_preference("builtins_version", "2")

    presets.ensure_builtin_presets(storage)

    upgraded_mixed = storage.get_preset_by_name("Mixed Aim")
    upgraded_wide = storage.get_preset_by_name("Wide Aim")
    assert upgraded_mixed is not None and upgraded_wide is not None
    assert upgraded_mixed.config.ranking.trait_targets == {}
    assert upgraded_mixed.config.ranking.skill_weights == {
        "aim_classic": 0.60,
        "aim_control": 0.20,
        "aim_bursts": 0.20,
    }
    # Wide Aim is now a legacy-only row. It is preserved exactly as stored
    # instead of being silently rewritten into a profile that is no longer
    # offered in the curated catalog.
    assert upgraded_wide.config.ranking.trait_targets == {
        "aim": 0.9,
        "density": 0.35,
    }
    assert upgraded_wide.config.ranking.skill_weights == {}


def test_v3_migration_preserves_edited_legacy_aim_profile(storage: Storage) -> None:
    mixed = storage.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    edited = _legacy_mixed_aim()
    edited.filters.stars_min = 4.2
    storage.update_preset(mixed.id, edited)
    storage.set_preference("builtins_version", "2")

    presets.ensure_builtin_presets(storage)

    kept = storage.get_preset_by_name("Mixed Aim")
    assert kept is not None
    assert kept.config.filters.stars_min == 4.2
    assert kept.config.ranking.trait_targets == {
        "aim": 0.72,
        "speed": 0.45,
        "density": 0.55,
    }
    assert kept.config.ranking.skill_weights == {}


def test_v5_migration_adds_streams_and_stamina_without_touching_edits(
    empty_storage: Storage,
) -> None:
    for preset in presets.builtin_presets():
        if preset.name == "Streams & Stamina":
            continue
        empty_storage.create_preset(copy.deepcopy(preset), is_builtin=True)
    mixed = empty_storage.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    edited = PresetConfig.from_dict(mixed.config.to_dict())
    edited.filters.stars_min = 4.2
    empty_storage.update_preset(mixed.id, edited)
    empty_storage.set_preference("builtins_seeded", "1")
    empty_storage.set_preference("builtins_version", "4")

    created = presets.ensure_builtin_presets(empty_storage)
    assert created == 1
    assert empty_storage.get_preset_by_name("Streams & Stamina") is not None
    kept = empty_storage.get_preset_by_name("Mixed Aim")
    assert kept is not None and kept.config.filters.stars_min == 4.2


def test_v7_migration_reduces_only_the_old_default_search_budget(
    empty_storage: Storage,
) -> None:
    base = presets.builtin_by_name("Mixed Aim")
    assert base is not None

    old = copy.deepcopy(base)
    old.name = "Old Defaults"
    old.discovery.max_requests = 20
    old.discovery.max_pages_per_strategy = 2
    old.discovery.fetch_attributes_limit = 8
    old.discovery.use_oldest = False
    old_id = empty_storage.create_preset(old, is_builtin=True).id

    custom = copy.deepcopy(base)
    custom.name = "Custom Budget"
    custom.discovery.max_requests = 30
    custom.discovery.max_pages_per_strategy = 2
    custom.discovery.fetch_attributes_limit = 8
    custom.filters.stars_min = 4.3
    custom_id = empty_storage.create_preset(custom, is_builtin=True).id

    empty_storage.set_preference("builtins_seeded", "1")
    empty_storage.set_preference("builtins_version", "6")
    presets.ensure_builtin_presets(empty_storage)

    upgraded = empty_storage.get_preset(old_id)
    assert upgraded is not None
    assert upgraded.config.discovery.max_requests == config.DEFAULT_MAX_REQUESTS
    assert (
        upgraded.config.discovery.max_pages_per_strategy
        == config.DEFAULT_MAX_PAGES_PER_STRATEGY
    )
    assert (
        upgraded.config.discovery.fetch_attributes_limit
        == config.DEFAULT_FETCH_ATTRIBUTES_LIMIT
    )
    # Non-default custom values and filters are not rewritten.
    assert upgraded.config.discovery.use_oldest is False

    kept = empty_storage.get_preset(custom_id)
    assert kept is not None
    assert kept.config.discovery.max_requests == 30
    assert kept.config.discovery.fetch_attributes_limit == 8
    assert kept.config.filters.stars_min == 4.3


def test_old_json_without_skillset_fields_still_loads() -> None:
    preset = presets.builtin_by_name("Mixed Aim")
    assert preset is not None
    data = preset.to_dict()
    data["ranking"].pop("skill_weights", None)
    data["ranking"].pop("farm_target_seconds", None)
    data["ranking"].pop("relevance_targets", None)
    data["ranking"].pop("relevance_threshold", None)
    loaded = PresetConfig.from_dict(data)
    assert loaded.ranking.skill_weights == {}
    assert loaded.ranking.farm_target_seconds == config.DEFAULT_FARM_TARGET_SECONDS
    assert loaded.ranking.relevance_targets == []
    assert loaded.ranking.relevance_threshold == config.DEFAULT_RELEVANCE_THRESHOLD


def test_v8_migration_adds_targets_only_to_untouched_builtins(
    storage: Storage,
) -> None:
    tech = storage.get_preset_by_name("Tech")
    speed = storage.get_preset_by_name("Speed")
    assert tech is not None and speed is not None

    untouched = copy.deepcopy(tech.config)
    untouched.ranking.relevance_targets = []
    storage.update_preset(tech.id, untouched)

    edited = copy.deepcopy(speed.config)
    edited.ranking.relevance_targets = []
    edited.filters.stars_min = 4.25
    storage.update_preset(speed.id, edited)

    storage.set_preference("builtins_version", "7")
    presets.ensure_builtin_presets(storage)

    upgraded_tech = storage.get_preset(tech.id)
    assert upgraded_tech is not None
    assert upgraded_tech.config.ranking.relevance_targets == [
        "tech",
        "slider_heavy",
        "rhythm_complexity",
    ]

    kept_speed = storage.get_preset(speed.id)
    assert kept_speed is not None
    assert kept_speed.config.ranking.relevance_targets == []
    assert kept_speed.config.filters.stars_min == 4.25


def test_v9_migration_adds_bursts_and_updates_untouched_sequence_targets(
    empty_storage: Storage,
) -> None:
    for preset in presets.builtin_presets():
        if preset.name == "Bursts":
            continue
        config = copy.deepcopy(preset)
        if config.name == "Streams & Stamina":
            config.ranking.relevance_targets = [
                "stream_consistency",
                "stamina_finger",
                "stream_long",
            ]
        if config.name == "Speed":
            config.ranking.relevance_targets = [
                "speed_high_bpm",
                "speed_bursts",
                "speed_density",
            ]
        if config.name == "Aim Farm":
            config.filters.stars_min = 4.2
        empty_storage.create_preset(config, is_builtin=True)
    empty_storage.set_preference("builtins_seeded", "1")
    empty_storage.set_preference("builtins_version", "8")

    created = presets.ensure_builtin_presets(empty_storage)
    assert created == 1

    bursts = empty_storage.get_preset_by_name("Bursts")
    assert bursts is not None
    assert bursts.config.ranking.relevance_targets == [
        "burst_control",
        "speed_bursts",
        "speed_short_bursts",
    ]
    streams = empty_storage.get_preset_by_name("Streams & Stamina")
    assert streams is not None
    assert streams.config.ranking.relevance_targets == [
        "stream_consistency",
        "stream_long",
        "speed_streams",
    ]
    speed = empty_storage.get_preset_by_name("Speed")
    assert speed is not None
    assert speed.config.ranking.relevance_targets == [
        "speed_high_bpm",
        "speed_density",
        "speed_consistency",
    ]
    # A user-edited built-in keeps its filters and is not silently rewritten.
    farm = empty_storage.get_preset_by_name("Aim Farm")
    assert farm is not None and farm.config.filters.stars_min == 4.2


def test_builtins_are_seeded_once(storage: Storage) -> None:
    stored = storage.list_presets()
    assert len(stored) == 12
    assert all(preset.is_builtin for preset in stored)
    assert {preset.name for preset in stored} == CANONICAL
    mixed = storage.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    assert mixed.config.ranking.trait_targets == {}
    assert mixed.config.ranking.skill_weights == {
        "aim_classic": 0.60,
        "aim_control": 0.20,
        "aim_bursts": 0.20,
    }
    assert presets.ensure_builtin_presets(storage) == 0


def test_deleted_builtin_is_not_recreated_on_startup(storage: Storage) -> None:
    preset = storage.get_preset_by_name("Tech")
    assert preset is not None
    assert storage.delete_preset(preset.id)
    assert presets.ensure_builtin_presets(storage) == 0
    assert storage.get_preset_by_name("Tech") is None
    restored = presets.restore_builtin_presets(storage)
    assert restored["created"] == 1
    assert storage.get_preset_by_name("Tech") is not None


def test_duplicate_is_independent(storage: Storage) -> None:
    original = storage.get_preset_by_name("Mixed Aim")
    assert original is not None
    copy_preset = presets.duplicate_preset(storage, original.id, "My Mixed")
    assert copy_preset.name == "My Mixed"
    assert copy_preset.is_builtin is False

    edited = PresetConfig.from_dict(copy_preset.config.to_dict())
    edited.filters.stars_min = 3.0
    edited.ranking.factors["community"].weight = 1.5
    storage.update_preset(copy_preset.id, edited)

    fresh_original = storage.get_preset(original.id)
    assert fresh_original is not None
    assert fresh_original.config.filters.stars_min == 4.0
    assert fresh_original.config.ranking.factors["community"].weight == 0.25


def test_create_custom_from_base_and_rename(storage: Storage) -> None:
    base = storage.get_preset_by_name("Reading")
    assert base is not None
    created = presets.create_preset(storage, "My Reading", base.id)
    assert created.config.ranking.trait_targets == {}
    assert created.config.ranking.skill_weights == base.config.ranking.skill_weights
    renamed = presets.rename_preset(storage, created.id, "My Reading 2")
    assert renamed.name == "My Reading 2"
    with pytest.raises(presets.PresetError):
        presets.rename_preset(storage, created.id, "Mixed Aim")


def test_reset_preset_to_default(storage: Storage) -> None:
    preset = storage.get_preset_by_name("Mixed Aim")
    assert preset is not None
    edited = PresetConfig.from_dict(preset.config.to_dict())
    edited.filters.stars_min = 2.0
    edited.description = "changed"
    storage.update_preset(preset.id, edited)
    restored = presets.reset_preset_to_default(storage, preset.id)
    assert restored.config.filters.stars_min == 4.0
    assert restored.config.description.startswith("Основной баланс")


def test_legacy_reset_uses_original_legacy_config(storage: Storage) -> None:
    legacy = presets.known_builtin_by_name("Wide Aim")
    assert legacy is not None
    preset = storage.create_preset(copy.deepcopy(legacy), is_builtin=True)
    edited = PresetConfig.from_dict(preset.config.to_dict())
    edited.filters.stars_min = 1.0
    storage.update_preset(preset.id, edited)
    restored = presets.reset_preset_to_default(storage, preset.id)
    assert restored.config.filters.stars_min == 4.5
    assert restored.config.ranking.trait_targets == {}
    assert restored.config.ranking.skill_weights == {
        "aim_wide": 0.70,
        "aim_classic": 0.30,
    }


def test_deleting_preset_keeps_user_data(storage: Storage) -> None:
    beatmapset = make_beatmapset(1)
    storage.save_beatmapset(beatmapset)
    storage.set_feedback(1, ["like"])
    storage.set_favorite(1, True)
    storage.record_history([(1, 0.8, 1)])
    preset = storage.get_preset_by_name("Mixed Aim")
    assert preset is not None
    storage.delete_preset(preset.id)
    assert storage.get_feedback(1) == ("like",)
    assert storage.is_favorite(1)
    assert storage.history_count() == 1
    assert len(storage.list_presets()) == 11


def test_preset_json_export_import_roundtrip(storage: Storage) -> None:
    custom = presets.create_preset(storage, "My Profile", None)
    selected = [
        preset
        for preset in storage.list_presets()
        if preset.name in {"Mixed Aim", "Reading"} or preset.id == custom.id
    ]
    text = presets.export_presets_json(selected)
    parsed = presets.import_presets_json(text)
    assert {preset.name for preset in parsed} == {"Mixed Aim", "Reading", "My Profile"}
    assert [p.to_dict() for p in parsed] == [p.config.to_dict() for p in selected]


def test_single_preset_export_import() -> None:
    preset = presets.known_builtin_by_name("Weird Aim")
    assert preset is not None
    text = json.dumps(preset.to_dict(), ensure_ascii=False)
    parsed = presets.import_presets_json(text)
    assert parsed[0].name == "Weird Aim"


def test_nested_export_preset_can_be_imported(storage: Storage) -> None:
    legacy = presets.known_builtin_by_name("Wide Aim")
    assert legacy is not None
    preset = storage.create_preset(copy.deepcopy(legacy), is_builtin=True)
    text = json.dumps(presets.export_preset(preset), ensure_ascii=False)
    parsed = presets.import_presets_json(text)
    assert len(parsed) == 1
    assert parsed[0].name == "Wide Aim"
    assert parsed[0].ranking.trait_targets == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"schema_version": 999, "presets": []}),
        json.dumps({"schema_version": 1, "presets": []}),
        json.dumps({"schema_version": 1, "presets": [{"name": ""}]}),
        json.dumps(
            {
                "schema_version": 1,
                "presets": [
                    {
                        "name": "Bad",
                        "filters": {"stars_min": 7.0, "stars_max": 4.0},
                    }
                ],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "presets": [
                    {
                        "name": "Bad factor",
                        "ranking": {
                            "factors": {"magic": {"weight": 1, "enabled": True}}
                        },
                    }
                ],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "presets": [
                    {
                        "name": "Bad discovery",
                        "discovery": {"max_requests": 999},
                    }
                ],
            }
        ),
    ],
)
def test_invalid_import_is_rejected(payload: str) -> None:
    with pytest.raises(presets.PresetError):
        presets.import_presets_json(payload)


def test_invalid_import_does_not_touch_database(storage: Storage) -> None:
    before = [(p.id, p.name, p.updated_at) for p in storage.list_presets()]
    with pytest.raises(presets.PresetError):
        presets.import_presets_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "presets": [{"name": "Broken", "filters": {"statuses": []}}],
                }
            )
        )
    after = [(p.id, p.name, p.updated_at) for p in storage.list_presets()]
    assert before == after


def test_validate_rejects_control_characters_and_long_names() -> None:
    cfg = copy.deepcopy(presets.default_config("ok"))
    cfg.name = "bad\nname"
    with pytest.raises(presets.PresetError):
        presets.validate_preset_config(cfg)
    cfg.name = "x" * 81
    with pytest.raises(presets.PresetError):
        presets.validate_preset_config(cfg)


def test_new_presets_have_honest_explicit_logic() -> None:
    streams = presets.builtin_by_name("Streams & Stamina")
    finger = presets.builtin_by_name("Finger Control")
    farm = presets.builtin_by_name("Aim Farm")
    assert streams is not None and streams.filters.duration_max == 360
    assert finger is not None and finger.ranking.skill_weights
    assert farm is not None and farm.filters.duration_max == 150
    assert farm.ranking.factors["farm_nm"].enabled
    assert len({preset.name for preset in presets.builtin_presets()}) == 12
