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
from skillsets import evaluate_skills
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

        def get_beatmap_attributes(self, beatmap_id):
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
    mixed = presets.known_builtin_by_name("Mixed Aim")
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
    assert presets.BUILTIN_PRESETS_VERSION == 12


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
    modern = presets.builtin_by_name(name)
    assert modern is not None
    old = copy.deepcopy(modern)
    old.ranking.relevance_targets = list(old_targets)
    if old_description is not None:
        old.description = old_description
    assert presets._preset_fingerprint(old) == presets._PRE_V10_FINGERPRINTS[name]
    preset = storage.get_preset_by_name(name)
    assert preset is not None
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
    streams = presets.builtin_by_name("Streams & Stamina")
    reading = presets.builtin_by_name("Reading")
    flashlight = presets.builtin_by_name("Flashlight")
    farm = presets.builtin_by_name("Aim Farm")
    assert streams is not None and ".osu" in streams.description
    assert reading is not None and (
        "не доказывается" in reading.description
        or "не измеряется" in reading.description
    )
    assert flashlight is not None and "без тега" in flashlight.description.casefold()
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
    assert presets.BUILTIN_PRESETS_VERSION == 12
    assert updated.config.discovery.max_requests == 56
    assert updated.config.discovery.max_pages_per_strategy == 3
    assert updated.config.discovery.fetch_attributes_limit == 40
    assert storage.get_preference("builtins_version") == "12"


def test_v12_migration_adds_policy_to_untouched_builtins(storage) -> None:
    names = (
        "Mixed Aim",
        "Speed",
        "Streams & Stamina",
        "Bursts",
        "Finger Control",
        "Reading",
        "Accuracy",
        "Tech",
        "Flashlight",
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
    assert storage.get_preference("builtins_version") == "12"


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
