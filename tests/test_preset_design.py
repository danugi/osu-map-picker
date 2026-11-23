from __future__ import annotations

import copy

import pytest

from conftest import make_beatmap, make_beatmapset

import filters as filters_module
import presets
from discovery import DiscoveryEngine, build_strategies
from models import (
    BeatmapFeatures,
    Candidate,
    DiscoverySettings,
    DifficultyAttributes,
    RankingSettings,
    SearchPage,
)
from ranking import RankingContext, evaluate_candidate
from relevance import evaluate_relevance
from skillsets import evaluate_skills, SKILLS
from storage import Storage


def _features(aim: float = 0.8, density: float = 0.5) -> BeatmapFeatures:
    return BeatmapFeatures(
        scores={
            "aim": aim,
            "aim_pressure": aim,
            "density": density,
            "stars": 0.55,
        },
        sources={
            "aim": "osu_api_attributes",
            "aim_pressure": "osu_api_attributes",
            "density": "beatmap_counts",
            "stars": "beatmap_stars",
        },
        confidence="medium",
    )


def _candidate(
    set_id: int = 1,
    *,
    status: str = "ranked",
    duration: int = 90,
    stars: float = 5.6,
    aim: float = 0.8,
    title: str = "Ordinary Aim Map",
) -> Candidate:
    beatmap = make_beatmap(
        10_000 + set_id,
        set_id,
        stars=stars,
        total_length=duration,
        hit_length=duration,
        status=status,
        count_circles=220,
        count_sliders=80,
    )
    beatmapset = make_beatmapset(
        set_id,
        title=title,
        status=status,
        beatmaps=(beatmap,),
    )
    features = _features(aim=aim)
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        traits=features,
        skillsets=evaluate_skills(features),
    )


def _farm_context(settings: RankingSettings) -> RankingContext:
    return RankingContext.from_user_data(settings, {}, {}, {})


def _farm_factor(candidate: Candidate, settings: RankingSettings):
    scored = evaluate_candidate(candidate, _farm_context(settings))
    return next(item for item in scored.factors if item.key == "farm_nm")


def _farm_only_settings() -> RankingSettings:
    settings = RankingSettings()
    for factor in settings.factors.values():
        factor.enabled = False
    settings.factors["farm_nm"].enabled = True
    settings.factors["farm_nm"].weight = 1.0
    settings.farm_target_seconds = 90
    settings.farm_tolerance_seconds = 30
    settings.comfort_stars = 5.6
    return settings


def test_aim_farm_is_ranked_only_for_pp_eligibility() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    assert farm.filters.statuses == ["ranked"]

    for status in ("ranked", "loved", "qualified"):
        candidate = _candidate(status=status)
        decision = filters_module.apply_filters(
            candidate.beatmapset,
            farm.filters,
            target_stars=farm.ranking.comfort_stars,
        )
        if status == "ranked":
            assert decision.passed is True
        else:
            assert decision.passed is False
            assert "статус" in decision.reason.casefold()


def test_aim_farm_gate_matches_weighted_aim_objective() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    assert farm.ranking.relevance_targets == ["aim_classic"]
    assert set(farm.ranking.relevance_targets) <= set(
        farm.ranking.skill_weights
    )

    candidate = _candidate(aim=0.9)
    assert evaluate_relevance(candidate, farm.ranking).passed is True

    missing = Candidate(
        beatmapset=candidate.beatmapset,
        beatmap=candidate.beatmap,
        traits=BeatmapFeatures(
            scores={"density": 0.5},
            sources={"density": "beatmap_counts"},
            confidence="low",
        ),
        skillsets=evaluate_skills(
            BeatmapFeatures(
                scores={"density": 0.5},
                sources={"density": "beatmap_counts"},
                confidence="low",
            )
        ),
    )
    check = evaluate_relevance(missing, farm.ranking)
    assert check.passed is False
    assert check.targets[0].score is None


def test_farm_duration_window_distinguishes_short_maps() -> None:
    settings = _farm_only_settings()
    values = {}
    for duration in (50, 90, 119, 150):
        factor = _farm_factor(
            _candidate(set_id=duration, duration=duration),
            settings,
        )
        assert factor.value is not None
        values[duration] = factor.value
    assert values[90] > values[50]
    assert values[90] > values[119]
    assert values[50] != values[119]
    assert values[90] > values[150]


def test_short_aim_heavy_map_outranks_longer_low_aim_map() -> None:
    settings = _farm_only_settings()
    short_aim = _farm_factor(
        _candidate(set_id=1, duration=90, aim=0.95),
        settings,
    )
    long_low_aim = _farm_factor(
        _candidate(set_id=2, duration=150, aim=0.15),
        settings,
    )
    assert short_aim.value is not None
    assert long_low_aim.value is not None
    assert short_aim.value > long_low_aim.value


def test_farm_duration_window_does_not_require_title_marker() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    candidate = _candidate(title="No Cut Version Marker")
    decision = filters_module.apply_filters(
        candidate.beatmapset,
        farm.filters,
        target_stars=farm.ranking.comfort_stars,
    )
    assert decision.passed is True
    assert evaluate_relevance(candidate, farm.ranking).passed is True


def test_farm_targeted_searches_supplement_general_discovery() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    strategies = build_strategies(farm.filters, farm.discovery)
    names = [item.name for item in strategies]
    assert "ranked:recent" in names
    assert "ranked:rating" in names
    queries = {item.query for item in strategies if item.query}
    assert {"Nightcore & Cut Ver", "Cut Ver", "Sped Up"} <= queries
    # One request per (status, sort, query); no duplicate paid strategies.
    keys = [(item.status, item.sort, item.query) for item in strategies]
    assert len(keys) == len(set(keys))


def test_overlapping_discovery_queries_dedupe_candidates(
    storage: Storage,
) -> None:
    candidate = _candidate()
    storage.save_beatmapset(candidate.beatmapset)

    class Api:
        def __init__(self) -> None:
            self.search_calls = []

        def search_beatmapsets(self, query):
            self.search_calls.append(query)
            return SearchPage(beatmapsets=(candidate.beatmapset,))

        def get_beatmap_attributes(self, beatmap_id: int, *, ruleset: str = "osu"):
            return DifficultyAttributes(
                beatmap_id=beatmap_id,
                star_rating=5.6,
                aim_difficulty=4.5,
                speed_difficulty=1.0,
            )

    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    api = Api()
    engine = DiscoveryEngine(
        api,
        storage,
        DiscoverySettings(
            max_requests=18,
            max_pages_per_strategy=2,
            max_candidates=100,
            fetch_attributes_limit=2,
            fetch_sequences_limit=0,
            use_oldest=False,
            use_rating=True,
            use_popular=False,
            expand_seeds=False,
            title_queries=[
                "Nightcore & Cut Ver",
                "Cut Ver",
                "Sped Up",
            ],
        ),
    )
    result = engine.run(farm.filters, farm.ranking, record=False)
    assert result.count == 1
    assert len(api.search_calls) > 1
    assert any(call.query == "Cut Ver" for call in api.search_calls)


def test_every_mandatory_gate_target_is_a_weighted_skill() -> None:
    for preset in (*presets.builtin_presets(), *presets.legacy_builtin_presets()):
        targets = set(preset.ranking.relevance_targets)
        assert targets <= set(preset.ranking.skill_weights), preset.name


def test_broad_presets_do_not_inherit_specialized_gates() -> None:
    for name in ("All-rounder", "Discovery", "Any Taste"):
        preset = presets.known_builtin_by_name(name)
        assert preset is not None
        assert preset.ranking.relevance_targets == []
    mixed = presets.known_builtin_by_name("Aim")
    assert mixed is not None
    assert mixed.ranking.relevance_targets
    assert mixed.ranking.relevance_policy["primary_targets"]
    candidate = Candidate(
        beatmapset=make_beatmapset(99),
        beatmap=make_beatmap(999, 99),
    )
    assert evaluate_relevance(candidate, RankingSettings()).passed is True


def test_preset_config_serializes_farm_tolerance_and_title_queries() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    data = farm.to_dict()
    assert data["ranking"]["farm_tolerance_seconds"] == 30
    assert data["discovery"]["title_queries"] == [
        "Nightcore & Cut Ver",
        "Cut Ver",
        "Sped Up",
    ]
    loaded = type(farm).from_dict(copy.deepcopy(data))
    assert loaded.ranking.farm_tolerance_seconds == 30
    assert loaded.discovery.title_queries == data["discovery"]["title_queries"]


def _old_aim_farm_config() -> object:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    cfg = copy.deepcopy(farm)
    cfg.filters.statuses = ["ranked", "loved"]
    cfg.ranking.relevance_targets = [
        "aim_classic",
        "aim_control",
        "aim_precision",
    ]
    cfg.ranking.farm_target_seconds = 120
    cfg.ranking.farm_tolerance_seconds = 45
    cfg.ranking.ideal_length = 105
    cfg.ranking.length_tolerance = 45
    cfg.ranking.factors["mapping_fit"].weight = 1.2
    cfg.ranking.factors["farm_nm"].weight = 2.0
    cfg.ranking.factors["length"].weight = 0.85
    cfg.ranking.factors["taste"].weight = 0.7
    cfg.ranking.factors["community"].weight = 0.35
    cfg.discovery.max_requests = 12
    cfg.discovery.max_pages_per_strategy = 2
    cfg.discovery.fetch_attributes_limit = 4
    cfg.discovery.title_queries = []
    cfg.description = (
        "Короткие aim-карты для NM-фарма: до ~2:30, звёзды около вашего "
        "комфорта, jump-паттерны и разумная плотность. Моды не учитываются."
    )
    return cfg


def test_v10_migration_upgrades_untouched_aim_farm(storage: Storage) -> None:
    farm = storage.get_preset_by_name("Aim Farm")
    assert farm is not None
    old = _old_aim_farm_config()
    assert presets._preset_fingerprint(old) == presets._PRE_V10_FINGERPRINTS[
        "Aim Farm"
    ]
    storage.update_preset(farm.id, old)
    storage.set_preference("builtins_version", "9")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset_by_name("Aim Farm")
    assert upgraded is not None
    assert upgraded.config.filters.statuses == ["ranked"]
    assert upgraded.config.ranking.farm_target_seconds == 90
    assert upgraded.config.ranking.farm_tolerance_seconds == 30
    assert upgraded.config.discovery.title_queries == [
        "Nightcore & Cut Ver",
        "Cut Ver",
        "Sped Up",
    ]
    assert upgraded.config.ranking.relevance_targets == ["aim_classic"]
    assert presets.BUILTIN_PRESETS_VERSION == 20


def test_v10_migration_preserves_edited_builtin_and_user_data(
    storage: Storage,
) -> None:
    farm = storage.get_preset_by_name("Aim Farm")
    assert farm is not None
    edited = _old_aim_farm_config()
    edited.filters.stars_min = 4.2
    storage.update_preset(farm.id, edited)
    storage.save_beatmapset(make_beatmapset(500, title="User Map"))
    storage.set_feedback(500, ["like"])
    storage.set_favorite(500, True)
    custom = presets.default_config("My Farm")
    custom.filters.stars_min = 3.3
    storage.create_preset(custom, is_builtin=False)
    storage.set_preference("builtins_version", "9")

    presets.ensure_builtin_presets(storage)

    kept = storage.get_preset_by_name("Aim Farm")
    assert kept is not None
    assert kept.config.filters.stars_min == 4.2
    assert kept.config.filters.statuses == ["ranked", "loved"]
    assert kept.config.ranking.farm_target_seconds == 120
    mine = storage.get_preset_by_name("My Farm")
    assert mine is not None
    assert mine.config.filters.stars_min == 3.3
    assert storage.is_favorite(500) is True
    assert storage.get_feedback(500) == ("like",)

@pytest.mark.parametrize(
    ("name", "old_targets", "old_description"),
    [
        (
            "Speed",
            ["speed_high_bpm", "speed_density", "speed_consistency"],
            None,
        ),
        (
            "Reading",
            ["ar_challenge", "high_ar", "pattern_recognition"],
            "Чтение: AR-челлендж, высокая/низкая плотность и reading-теги. "
            "Реальная читаемость паттернов не измеряется метаданными API.",
        ),
        (
            "Streams & Stamina",
            ["stream_consistency", "stream_long", "speed_streams"],
            "Единый тренировочный профиль для потоков и выносливости: "
            "непрерывность stream-участков, длина, finger stamina и alternation. "
            "Длина остаётся мягким предпочтением, верхний предел можно изменить.",
        ),
    ],
)
def test_v10_migration_updates_untouched_changed_builtins(
    storage: Storage,
    name: str,
    old_targets: list[str],
    old_description: str | None,
) -> None:
    modern = presets.known_builtin_by_name(name)
    assert modern is not None
    old = copy.deepcopy(modern)
    old.ranking.relevance_targets = list(old_targets)
    if old_description is not None:
        old.description = old_description
    assert presets._preset_fingerprint(old) == presets._PRE_V10_FINGERPRINTS[name]
    preset = storage.get_preset_by_name(name)
    if preset is None:
        # Retired names (Streams & Stamina) are no longer seeded on a fresh
        # database; restore the row explicitly to exercise its migration.
        legacy = presets.known_builtin_by_name(name)
        assert legacy is not None
        preset = storage.create_preset(copy.deepcopy(legacy), is_builtin=True)
    storage.update_preset(preset.id, old)
    storage.set_preference("builtins_version", "9")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset_by_name(name)
    assert upgraded is not None
    assert upgraded.config.ranking.relevance_targets == (
        modern.ranking.relevance_targets
    )
    assert set(upgraded.config.ranking.relevance_targets) <= set(
        upgraded.config.ranking.skill_weights
    )


def test_title_queries_are_bounded_and_validated() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    duplicate = copy.deepcopy(farm)
    duplicate.discovery.title_queries = ["Cut Ver", "cut ver"]
    with pytest.raises(presets.PresetError):
        presets.validate_preset_config(duplicate)
    too_many = copy.deepcopy(farm)
    too_many.discovery.title_queries = [f"query {index}" for index in range(7)]
    with pytest.raises(presets.PresetError):
        presets.validate_preset_config(too_many)


def test_old_preset_json_without_new_fields_uses_safe_defaults() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    data = farm.to_dict()
    data["ranking"].pop("farm_tolerance_seconds", None)
    data["discovery"].pop("title_queries", None)
    loaded = type(farm).from_dict(data)
    assert loaded.ranking.farm_tolerance_seconds == 45
    assert loaded.discovery.title_queries == []


def test_combined_legacy_gates_keep_their_named_components() -> None:
    expected = {
        "Aim + Bursts": "aim_bursts",
        "Reading + Streams": "stream_reading",
        "Jump Streams": "stream_jump",
    }
    for name, required_target in expected.items():
        preset = presets.known_builtin_by_name(name)
        assert preset is not None
        targets = set(preset.ranking.relevance_targets)
        assert targets <= set(preset.ranking.skill_weights), name
        assert required_target in targets, name
        assert required_target in preset.ranking.skill_weights, name


def test_descriptions_expose_measurement_limits() -> None:
    streams = presets.known_builtin_by_name("Streams")
    alternating = presets.builtin_by_name("Alternating")
    double_tap = presets.builtin_by_name("Double-tap / Burst Control")
    farm = presets.builtin_by_name("Aim Farm")
    assert streams is not None and ".osu" in streams.description
    assert alternating is not None and "прокси" in alternating.description
    assert double_tap is not None and ".osu" in double_tap.description
    assert farm is not None and "эвристика" in farm.description


def test_legacy_aim_farm_pp_is_ranked_only() -> None:
    legacy = presets.known_builtin_by_name("Aim Farm / PP")
    assert legacy is not None
    assert legacy.filters.statuses == ["ranked"]
    assert "Ranked" in legacy.description


def test_v10_migration_corrects_untouched_legacy_aim_farm(storage: Storage) -> None:
    old = presets._legacy_aim_farm_config()
    row = storage.create_preset(old, is_builtin=True)
    storage.set_preference("builtins_version", "9")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset(row.id)
    assert upgraded is not None
    assert upgraded.config.filters.statuses == ["ranked"]
    assert "Ranked" in upgraded.config.description


def test_stored_preset_drift_reports_search_affecting_differences() -> None:
    modern = presets.builtin_by_name("Aim Farm")
    assert modern is not None
    assert presets.preset_drift(modern, modern) == []
    changes = presets.preset_drift(_old_aim_farm_config(), modern)
    assert any("статусы" in line for line in changes)
    assert any("целевой гейт" in line for line in changes)
    assert any("discovery" in line for line in changes)


def test_v11_migration_updates_untouched_v10_aim_farm_limits(storage) -> None:
    farm = storage.get_preset_by_name("Aim Farm")
    assert farm is not None
    old = copy.deepcopy(presets.builtin_by_name("Aim Farm"))
    old.discovery.max_requests = 18
    old.discovery.max_pages_per_strategy = 2
    old.discovery.fetch_attributes_limit = 8
    storage.update_preset(farm.id, old)
    storage.set_preference("builtins_version", "10")

    presets.ensure_builtin_presets(storage)

    updated = storage.get_preset(farm.id)
    assert updated is not None
    assert presets.BUILTIN_PRESETS_VERSION == 20
    assert updated.config.discovery.max_requests == 56
    assert updated.config.discovery.max_pages_per_strategy == 3
    assert updated.config.discovery.fetch_attributes_limit == 40
    assert storage.get_preference("builtins_version") == "20"


def test_v12_migration_adds_policy_to_untouched_builtins(storage) -> None:
    names = (
        "Speed",
        "Finger Control",
        "Tech",
    )
    for name in names:
        preset = storage.get_preset_by_name(name)
        assert preset is not None
        old = copy.deepcopy(preset.config)
        old.ranking.relevance_targets = list(presets._PRE_V12_TARGETS[name])
        old.ranking.relevance_policy = {}
        storage.update_preset(preset.id, old)

    speed = storage.get_preset_by_name("Speed")
    assert speed is not None
    edited = copy.deepcopy(speed.config)
    edited.filters.stars_min = 4.25
    storage.update_preset(speed.id, edited)

    storage.set_preference("builtins_version", "11")
    presets.ensure_builtin_presets(storage)

    for name in names:
        if name == "Speed":
            continue
        preset = storage.get_preset_by_name(name)
        assert preset is not None
        modern = presets.builtin_by_name(name)
        assert modern is not None
        assert preset.config.ranking.relevance_targets == (
            modern.ranking.relevance_targets
        )
        assert preset.config.ranking.relevance_policy == (
            modern.ranking.relevance_policy
        )

    kept = storage.get_preset_by_name("Speed")
    assert kept is not None
    assert kept.config.filters.stars_min == 4.25
    assert kept.config.ranking.relevance_policy == {}
    assert storage.get_preference("builtins_version") == "20"


def test_v13_migration_removes_raw_burst_feature_only_from_untouched(storage) -> None:
    legacy_bursts = presets.known_builtin_by_name("Bursts")
    assert legacy_bursts is not None
    burst = storage.create_preset(copy.deepcopy(legacy_bursts), is_builtin=True)
    old = copy.deepcopy(burst.config)
    old.ranking.relevance_policy = copy.deepcopy(
        presets._PRE_V13_BURSTS_POLICY
    )
    storage.update_preset(burst.id, old)
    storage.set_preference("builtins_version", "12")

    presets.ensure_builtin_presets(storage)

    updated = storage.get_preset_by_name("Bursts")
    assert updated is not None
    modern = presets.known_builtin_by_name("Bursts")
    assert modern is not None
    assert updated.config.ranking.relevance_policy == (
        modern.ranking.relevance_policy
    )
    assert "require_features_any" not in updated.config.ranking.relevance_policy
    assert storage.get_preference("builtins_version") == "20"

    edited = copy.deepcopy(updated.config)
    edited.ranking.relevance_policy = copy.deepcopy(
        presets._PRE_V13_BURSTS_POLICY
    )
    edited.filters.stars_min = 4.25
    storage.update_preset(burst.id, edited)
    storage.set_preference("builtins_version", "12")
    presets.ensure_builtin_presets(storage)

    kept = storage.get_preset_by_name("Bursts")
    assert kept is not None
    assert kept.config.filters.stars_min == 4.25
    # v14 rewrites retired skillset ids in edited presets too, while the
    # user's own edit (stars_min) is preserved untouched.
    assert kept.config.ranking.relevance_policy == {
        "primary_targets": ["burst_control", "speed_bursts"],
        "require_features_any": ["bursts"],
    }


def test_v14_migration_rewrites_retired_ids_in_user_presets(storage) -> None:
    """Retired near-duplicate ids must survive migration as their canonical
    survivor, in weights, gate targets and policy alike."""
    import skillsets
    from skillsets import SKILL_ALIASES

    config = presets.known_builtin_by_name("Bursts")
    assert config is not None
    config = copy.deepcopy(config)
    config.name = "My Bursts"
    config.ranking.skill_weights["speed_short_bursts"] = 0.45
    config.ranking.skill_weights["burst_control"] = 1.0
    config.ranking.relevance_targets.append("stream_short")
    config.ranking.relevance_policy["primary_targets"].append(
        "speed_short_bursts"
    )
    preset = storage.create_preset(config, is_builtin=False)
    storage.set_preference("builtins_version", "13")

    presets.ensure_builtin_presets(storage)

    migrated = storage.get_preset(preset.id)
    assert migrated is not None
    weights = migrated.config.ranking.skill_weights
    assert "speed_short_bursts" not in weights
    assert weights["burst_control"] == 1.0  # max(existing, alias) merge
    assert "stream_short" not in migrated.config.ranking.relevance_targets
    assert "stream_consistency" in migrated.config.ranking.relevance_targets
    policy_targets = migrated.config.ranking.relevance_policy["primary_targets"]
    assert "speed_short_bursts" not in policy_targets
    assert policy_targets.count("burst_control") == 1
    assert storage.get_preference("builtins_version") == "20"
    # Alias table stays consistent with the retirement list.
    assert set(SKILL_ALIASES) == {
        "speed_short_bursts",
        "stream_short",
        "rhythm_changes",
        "unusual_rhythms",
    }
    assert all(alias not in skillsets.SKILLS for alias in SKILL_ALIASES)
    assert all(target in skillsets.SKILLS for target in SKILL_ALIASES.values())


def test_v11_migration_preserves_edited_v10_aim_farm(storage) -> None:
    farm = storage.get_preset_by_name("Aim Farm")
    assert farm is not None
    edited = copy.deepcopy(presets.builtin_by_name("Aim Farm"))
    edited.discovery.max_requests = 18
    edited.discovery.max_pages_per_strategy = 2
    edited.discovery.fetch_attributes_limit = 8
    edited.filters.stars_min = 4.5
    storage.update_preset(farm.id, edited)
    storage.set_preference("builtins_version", "10")

    presets.ensure_builtin_presets(storage)

    kept = storage.get_preset(farm.id)
    assert kept is not None
    assert kept.config.filters.stars_min == 4.5
    assert kept.config.discovery.max_requests == 18


def test_v15_migration_renames_untouched_mixed_aim_to_aim(storage) -> None:
    """A v14 database keeps Mixed Aim with the exact pre-v15 body; the v15
    rename rewrites it in place so no duplicate Aim row appears."""
    mixed = storage.get_preset_by_name("Aim")
    assert mixed is not None
    old_body = presets._apply_relevance_defaults(
        presets._legacy_mixed_aim_skillset()
    )
    storage.update_preset(mixed.id, old_body)
    storage.set_preference("builtins_version", "14")

    presets.ensure_builtin_presets(storage)

    renamed = storage.get_preset(mixed.id)
    assert renamed is not None
    assert renamed.config.name == "Aim"
    assert renamed.config.ranking.skill_weights == {
        "aim_classic": 0.60,
        "aim_control": 0.20,
        "aim_bursts": 0.20,
    }
    assert renamed.config.ranking.relevance_targets == [
        "aim_classic",
        "aim_control",
        "aim_bursts",
    ]
    assert storage.get_preset_by_name("Mixed Aim") is None
    assert storage.get_preference("builtins_version") == "20"


def test_v15_rename_keeps_edited_mixed_aim_and_seeds_aim(storage) -> None:
    mixed = storage.get_preset_by_name("Aim")
    assert mixed is not None
    edited = presets._apply_relevance_defaults(
        presets._legacy_mixed_aim_skillset()
    )
    edited.filters.stars_min = 4.2
    storage.update_preset(mixed.id, edited)
    storage.set_preference("builtins_version", "14")

    presets.ensure_builtin_presets(storage)

    kept = storage.get_preset_by_name("Mixed Aim")
    assert kept is not None
    assert kept.config.filters.stars_min == 4.2
    fresh = storage.get_preset_by_name("Aim")
    assert fresh is not None and fresh.id != kept.id
    assert fresh.config.ranking.skill_weights == {
        "aim_classic": 0.60,
        "aim_control": 0.20,
        "aim_bursts": 0.20,
    }


def test_v15_rename_does_not_run_when_aim_row_already_exists(storage) -> None:
    mixed = storage.get_preset_by_name("Aim")
    assert mixed is not None
    old_body = presets._apply_relevance_defaults(
        presets._legacy_mixed_aim_skillset()
    )
    storage.update_preset(mixed.id, old_body)
    custom_aim = storage.create_preset(
        copy.deepcopy(presets.builtin_by_name("Aim")), is_builtin=False
    )
    storage.set_preference("builtins_version", "14")

    presets.ensure_builtin_presets(storage)

    # A user preset already owns the name Aim: the untouched Mixed Aim row
    # is preserved as a hidden legacy row instead of colliding.
    kept = storage.get_preset_by_name("Mixed Aim")
    assert kept is not None
    assert kept.id == mixed.id
    assert kept.config.name == "Mixed Aim"
    assert storage.get_preset(custom_aim.id) is not None


def test_v16_migration_merges_bursts_into_untouched_legacy_streams(storage) -> None:
    """A pre-v8 Streams row (no gate targets) is upgraded to the merged
    osu! streams-group body with the measured-evidence gate."""
    seeded = storage.get_preset_by_name("Streams")
    assert seeded is not None
    storage.delete_preset(seeded.id)
    legacy = presets._legacy_streams_config()
    row = storage.create_preset(copy.deepcopy(legacy), is_builtin=True)
    storage.set_preference("builtins_version", "15")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset(row.id)
    assert upgraded is not None
    assert upgraded.config.ranking.skill_weights == {
        "stream_consistency": 1.0,
        "burst_control": 0.7,
        "stream_long": 0.6,
        "stream_burst": 0.45,
        "speed_bursts": 0.3,
    }
    assert upgraded.config.ranking.relevance_targets == [
        "stream_consistency",
        "stream_long",
        "burst_control",
    ]
    assert upgraded.config.ranking.relevance_policy["require_features_any"] == [
        "streams",
        "bursts",
    ]
    assert storage.get_preference("builtins_version") == "20"


def test_v16_migration_upgrades_streams_row_with_legacy_targets(storage) -> None:
    """Rows rewritten by the v10 fingerprint migration carry the old legacy
    target list; they are upgraded too."""
    seeded = storage.get_preset_by_name("Streams")
    assert seeded is not None
    storage.delete_preset(seeded.id)
    legacy = presets._legacy_streams_config()
    legacy.ranking.relevance_targets = [
        "stream_consistency",
        "stream_long",
        "stream_burst",
    ]
    row = storage.create_preset(copy.deepcopy(legacy), is_builtin=True)
    storage.set_preference("builtins_version", "15")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset(row.id)
    assert upgraded is not None
    assert upgraded.config.ranking.relevance_targets == [
        "stream_consistency",
        "stream_long",
        "burst_control",
    ]
    assert "burst_control" in upgraded.config.ranking.skill_weights


def test_v16_migration_keeps_edited_legacy_streams(storage) -> None:
    seeded = storage.get_preset_by_name("Streams")
    assert seeded is not None
    storage.delete_preset(seeded.id)
    edited = presets._legacy_streams_config()
    edited.filters.stars_min = 4.2
    row = storage.create_preset(copy.deepcopy(edited), is_builtin=True)
    storage.set_preference("builtins_version", "15")

    presets.ensure_builtin_presets(storage)

    kept = storage.get_preset(row.id)
    assert kept is not None
    assert kept.config.filters.stars_min == 4.2
    assert "burst_control" not in kept.config.ranking.skill_weights


def test_v16_migration_restores_the_stamina_gate(storage) -> None:
    """A promoted legacy Stamina row must not lose its target gate."""
    seeded = storage.get_preset_by_name("Stamina")
    assert seeded is not None
    storage.delete_preset(seeded.id)
    legacy = copy.deepcopy(presets._stamina())
    legacy.description = presets._PRE_V17_STAMINA_DESCRIPTION
    legacy.ranking.skill_weights = dict(presets._PRE_V17_STAMINA_SKILL_WEIGHTS)
    legacy.ranking.relevance_targets = []
    legacy.ranking.relevance_policy = {}
    row = storage.create_preset(copy.deepcopy(legacy), is_builtin=True)
    storage.set_preference("builtins_version", "15")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset(row.id)
    assert upgraded is not None
    assert upgraded.config.ranking.relevance_targets == [
        "stream_endurance",
        "stream_long",
    ]


def test_v17_migration_adds_stream_endurance_only_to_untouched(storage) -> None:
    stamina = storage.get_preset_by_name("Stamina")
    assert stamina is not None
    old = copy.deepcopy(stamina.config)
    old.description = presets._PRE_V17_STAMINA_DESCRIPTION
    old.ranking.skill_weights = dict(presets._PRE_V17_STAMINA_SKILL_WEIGHTS)
    old.ranking.relevance_targets = list(presets._PRE_V17_STAMINA_TARGETS)
    old.ranking.relevance_policy = {}
    storage.update_preset(stamina.id, old)
    storage.set_preference("builtins_version", "16")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset(stamina.id)
    assert upgraded is not None
    assert "stream_endurance" in upgraded.config.ranking.skill_weights
    assert upgraded.config.ranking.relevance_targets == [
        "stream_endurance",
        "stream_long",
    ]

    edited = copy.deepcopy(old)
    edited.filters.duration_max = 250
    storage.update_preset(stamina.id, edited)
    storage.set_preference("builtins_version", "16")
    presets.ensure_builtin_presets(storage)
    kept = storage.get_preset(stamina.id)
    assert kept is not None
    assert kept.config.filters.duration_max == 250
    assert kept.config.ranking.relevance_targets == list(
        presets._PRE_V17_STAMINA_TARGETS
    )


def test_v18_migration_promotes_hierarchy_and_fixes_streams_policy_only_when_untouched(
    storage,
) -> None:
    streams = storage.get_preset_by_name("Streams")
    assert streams is not None
    old = copy.deepcopy(streams.config)
    old.ranking.relevance_policy = copy.deepcopy(
        presets._PRE_V18_STREAMS_POLICY
    )
    storage.update_preset(streams.id, old)
    reading = presets.known_builtin_by_name("Reading")
    assert reading is not None
    if storage.get_preset_by_name("Reading") is None:
        storage.create_preset(copy.deepcopy(reading), is_builtin=True)
    custom = storage.create_preset(
        presets.default_config("My hierarchy custom"), is_builtin=False
    )
    storage.set_preference("builtins_version", "17")

    presets.ensure_builtin_presets(storage)

    upgraded = storage.get_preset(streams.id)
    assert upgraded is not None
    assert upgraded.config.ranking.relevance_policy["feature_minimums"] == {
        "streams": 0.0,
        "bursts": 0.0,
    }
    assert storage.get_preference("builtins_version") == "20"
    assert storage.get_preset_by_name("Jump Aim") is not None
    assert storage.get_preset_by_name("Continuous Streams") is not None
    assert storage.get_preset_by_name("Reading") is not None  # legacy row kept
    assert "Reading" in {
        preset.name for preset in presets.legacy_presets(storage)
    }
    assert "Reading" not in {
        preset.name for preset in presets.visible_presets(storage)
    }
    assert storage.get_preset(custom.id) is not None

    edited = copy.deepcopy(upgraded.config)
    edited.ranking.relevance_policy = {
        "primary_targets": ["burst_control"],
    }
    storage.update_preset(streams.id, edited)
    storage.set_preference("builtins_version", "17")
    presets.ensure_builtin_presets(storage)
    kept = storage.get_preset(streams.id)
    assert kept is not None
    assert kept.config.ranking.relevance_policy == {
        "primary_targets": ["burst_control"],
    }


def test_v16_retired_rows_stay_hidden_but_resolvable(storage) -> None:
    """Bursts and Streams & Stamina rows survive as hidden legacy rows."""
    bursts = presets.known_builtin_by_name("Bursts")
    hybrid = presets.known_builtin_by_name("Streams & Stamina")
    assert bursts is not None and hybrid is not None
    assert "Bursts" in presets.LEGACY_BUILTIN_NAMES
    assert "Streams & Stamina" in presets.LEGACY_BUILTIN_NAMES
    # Their bodies are untouched by the merge.
    assert bursts.ranking.skill_weights == {
        "burst_control": 1.0,
        "speed_bursts": 0.65,
    }
    assert hybrid.ranking.skill_weights == {
        "stream_consistency": 0.90,
        "stamina_finger": 0.65,
        "stream_long": 0.55,
        "speed_stamina": 0.35,
    }


def test_v19_migration_updates_only_untouched_flow_aim(storage) -> None:
    row = storage.get_preset_by_name("Flow Aim")
    assert row is not None
    old = copy.deepcopy(row.config)
    old.description = (
        "Прокси для связного aim: Classic Aim и Flow Aim вместе. Это не "
        "измерение flow-геометрии; структура движений остаётся ограниченной."
    )
    old.ranking.skill_weights = {"aim_flow": 0.70, "aim_classic": 0.30}
    old.ranking.relevance_policy = {
        "primary_targets": ["aim_flow"],
        "require_features_any": ["flow_share"],
        "feature_minimums": {"flow_share": 0.0},
    }
    assert presets._preset_fingerprint(old) == (
        "2a783c105408abd43b34fc3b2808f97f185a2eaee259b1ed63515fb491bf2e8e"
    )
    storage.update_preset(row.id, old)
    storage.set_preference("builtins_version", "18")
    presets.ensure_builtin_presets(storage)

    updated = storage.get_preset_by_name("Flow Aim")
    assert updated is not None
    assert updated.config.ranking.relevance_policy["feature_minimums"][
        "flow_share"
    ] == 0.20
    assert updated.config.ranking.skill_weights["aim_flow"] == 0.75
    assert storage.get_preference("builtins_version") == "20"


def test_v19_migration_keeps_edited_flow_aim(storage) -> None:
    row = storage.get_preset_by_name("Flow Aim")
    assert row is not None
    edited = copy.deepcopy(row.config)
    edited.ranking.skill_weights = {"aim_flow": 0.99, "aim_classic": 0.01}
    storage.update_preset(row.id, edited)
    storage.set_preference("builtins_version", "18")
    presets.ensure_builtin_presets(storage)
    kept = storage.get_preset_by_name("Flow Aim")
    assert kept is not None
    assert kept.config.ranking.skill_weights["aim_flow"] == 0.99


def test_v20_migration_refreshes_untouched_flow_gate(storage) -> None:
    row = storage.get_preset_by_name("Flow Aim")
    assert row is not None
    old = copy.deepcopy(row.config)
    old.ranking.relevance_policy = {
        "primary_targets": ["aim_flow"],
        "require_features_any": ["flow_share"],
        "feature_minimums": {"flow_share": 0.20},
    }
    assert presets._preset_fingerprint(old) == (
        "b5e21e0ee982f08de45e2587b3ccf988341c8abf495f80694ba39c5114c30294"
    )
    storage.update_preset(row.id, old)
    storage.set_preference("builtins_version", "19")
    presets.ensure_builtin_presets(storage)
    updated = storage.get_preset_by_name("Flow Aim")
    assert updated is not None
    assert updated.config.ranking.relevance_policy["require_features_all"] == [
        "flow_share",
        "flow_coverage",
        "flow_longest",
    ]
    assert storage.get_preference("builtins_version") == "20"
