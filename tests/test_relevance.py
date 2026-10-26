from __future__ import annotations

from typing import Callable

import pytest
from conftest import make_beatmap, make_beatmapset

import config
import presets
from discovery import DiscoveryEngine
from models import (
    BeatmapFeatures,
    Candidate,
    DiscoverySettings,
    Filters,
    RankingSettings,
    SearchPage,
    SearchQuery,
    SkillScore,
)
from ranking import RankingConfigError, validate_ranking
from relevance import evaluate_relevance


class StubApi:
    def __init__(self, responder: Callable[[SearchQuery], SearchPage]) -> None:
        self.responder = responder
        self.search_calls: list[SearchQuery] = []

    def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        return self.responder(query)


def search_settings() -> DiscoverySettings:
    return DiscoverySettings(
        max_requests=1,
        max_pages_per_strategy=1,
        max_candidates=100,
        fetch_attributes_limit=0,
        cache_ttl_minutes=0,
        use_oldest=False,
        use_rating=False,
        use_popular=False,
        expand_seeds=False,
    )


def relevance_settings(
    targets: tuple[str, ...],
    threshold: float = config.DEFAULT_RELEVANCE_THRESHOLD,
) -> RankingSettings:
    settings = RankingSettings()
    settings.relevance_targets = list(targets)
    settings.relevance_threshold = threshold
    return settings


def score_candidate(
    set_id: int,
    scores: dict[str, float | None],
    *,
    tags: tuple[str, ...] = (),
    title: str = "Title",
    rating: float = 8.4,
    play_count: int = 12_000,
    stars: float = 5.5,
) -> Candidate:
    beatmapset = make_beatmapset(
        set_id,
        tags=tags,
        title=title,
        rating=rating,
        play_count=play_count,
    )
    beatmap = make_beatmap(
        set_id * 10 + 1,
        set_id,
        stars=stars,
        total_length=120,
        hit_length=110,
    )
    skillsets: dict[str, SkillScore] = {}
    for index, (skill_id, value) in enumerate(scores.items()):
        skillsets[skill_id] = SkillScore(
            skill_id=skill_id,
            name=skill_id,
            score=value,
            confidence="low" if value is not None else "none",
            support="proxy",
            sources=("tags",) if tags else ("derived_proxy",),
            missing=() if value is not None else (skill_id,),
        )
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        traits=BeatmapFeatures(),
        skillsets=skillsets,
    )


def test_default_threshold_is_strictly_above_fifty_percent() -> None:
    assert config.DEFAULT_RELEVANCE_THRESHOLD > 0.5
    settings = RankingSettings()
    assert settings.relevance_threshold == config.DEFAULT_RELEVANCE_THRESHOLD


def test_exactly_fifty_percent_does_not_pass_even_at_half_threshold() -> None:
    candidate = score_candidate(
        1,
        {"stream_consistency": 0.5, "stamina_finger": 0.5, "stream_long": 0.5},
    )
    check = evaluate_relevance(
        candidate,
        relevance_settings(
            ("stream_consistency", "stamina_finger", "stream_long"),
            threshold=0.5,
        ),
    )
    assert check.passed is False
    assert [target.display_score for target in check.targets] == [50, 50, 50]
    assert all(target.matched is False for target in check.targets)


def test_one_target_above_threshold_is_enough() -> None:
    candidate = score_candidate(
        1,
        {"stream_consistency": 0.56, "stamina_finger": 0.2, "stream_long": 0.1},
    )
    check = evaluate_relevance(
        candidate,
        relevance_settings(
            ("stream_consistency", "stamina_finger", "stream_long")
        ),
    )
    assert check.passed is True
    assert check.best_target is not None
    assert check.best_target.target_id == "stream_consistency"
    assert check.best_target.display_score == 56


def test_threshold_is_configurable_and_still_strict() -> None:
    candidate = score_candidate(
        1,
        {"tech": 0.70, "slider_heavy": 0.2, "rhythm_complexity": 0.1},
    )
    targets = ("tech", "slider_heavy", "rhythm_complexity")
    assert evaluate_relevance(candidate, relevance_settings(targets, 0.75)).passed is False
    assert evaluate_relevance(candidate, relevance_settings(targets, 0.65)).passed is True


def test_unknown_target_data_is_not_an_invented_match() -> None:
    candidate = score_candidate(
        1,
        {"tech": None, "slider_heavy": None, "rhythm_complexity": None},
    )
    check = evaluate_relevance(
        candidate,
        relevance_settings(("tech", "slider_heavy", "rhythm_complexity")),
    )
    assert check.passed is False
    assert all(target.display_score is None for target in check.targets)
    assert all(target.status == "недостаточно данных" for target in check.targets)
    assert "недостаточно данных" in check.reason


def test_title_words_are_not_evidence_for_a_target() -> None:
    candidate = score_candidate(
        1,
        {"tech": None, "slider_heavy": None, "rhythm_complexity": None},
        title="Tech Streams Reading Speed Finger Control",
    )
    check = evaluate_relevance(
        candidate,
        relevance_settings(("tech", "slider_heavy", "rhythm_complexity")),
    )
    assert check.passed is False
    assert all(target.display_score is None for target in check.targets)


def test_target_source_keeps_tag_and_proxy_provenance() -> None:
    candidate = score_candidate(
        1,
        {"tech": 0.8, "slider_heavy": 0.6, "rhythm_complexity": 0.5},
        tags=("tech",),
    )
    check = evaluate_relevance(
        candidate,
        relevance_settings(("tech", "slider_heavy", "rhythm_complexity")),
    )
    assert check.passed is True
    assert "тег" in check.targets[0].source.casefold()


def test_mixed_aim_and_allrounder_have_no_target_gate() -> None:
    for name in ("Mixed Aim", "All-rounder"):
        preset = presets.builtin_by_name(name)
        assert preset is not None
        assert preset.ranking.relevance_targets == []
    candidate = score_candidate(1, {})
    check = evaluate_relevance(candidate, RankingSettings())
    assert check.passed is True
    assert check.targets == ()
    assert "не заданы" in check.reason


def test_specialized_presets_use_their_own_three_targets() -> None:
    expected = {
        "Aim Farm",
        "Speed",
        "Streams & Stamina",
        "Bursts",
        "Finger Control",
        "Reading",
        "Accuracy",
        "Tech",
    }
    target_sets: dict[str, tuple[str, ...]] = {}
    for name in expected:
        preset = presets.builtin_by_name(name)
        assert preset is not None
        targets = tuple(preset.ranking.relevance_targets)
        assert len(targets) == 3, name
        assert len(set(targets)) == 3, name
        target_sets[name] = targets

    assert target_sets["Tech"] != target_sets["Reading"]
    assert "tech" in target_sets["Tech"]
    assert "stream_consistency" in target_sets["Streams & Stamina"]
    assert "burst_control" in target_sets["Bursts"]
    assert all(
        "stream" not in target for target in target_sets["Bursts"]
    )
    assert all(
        "burst" not in target for target in target_sets["Streams & Stamina"]
    )
    assert "finger_control" in target_sets["Finger Control"]
    assert "speed_high_bpm" in target_sets["Speed"]
    assert "aim_classic" in target_sets["Aim Farm"]

    legacy_streams = presets.known_builtin_by_name("Streams")
    assert legacy_streams is not None
    assert len(legacy_streams.ranking.relevance_targets) == 3


@pytest.mark.parametrize(
    "name",
    [
        "Aim Farm",
        "Speed",
        "Streams & Stamina",
        "Bursts",
        "Finger Control",
        "Reading",
        "Accuracy",
        "Tech",
        "Flashlight",
    ],
)
def test_each_specialized_preset_passes_on_its_own_target(name: str) -> None:
    preset = presets.builtin_by_name(name)
    assert preset is not None
    targets = tuple(preset.ranking.relevance_targets)
    scores = {
        target: (0.80 if index == 0 else 0.10)
        for index, target in enumerate(targets)
    }
    check = evaluate_relevance(
        score_candidate(1, scores),
        preset.ranking,
    )
    assert check.passed is True
    assert check.best_target is not None
    assert check.best_target.target_id == targets[0]


def test_switching_preset_changes_the_target_criteria() -> None:
    candidate = score_candidate(
        1,
        {"tech": 0.8, "slider_heavy": 0.7, "rhythm_complexity": 0.6,
         "ar_challenge": 0.1, "pattern_recognition": 0.1,
         "visual_complexity": 0.1},
    )
    tech = presets.builtin_by_name("Tech")
    reading = presets.builtin_by_name("Reading")
    assert tech is not None and reading is not None
    assert evaluate_relevance(candidate, tech.ranking).passed is True
    assert evaluate_relevance(candidate, reading.ranking).passed is False


def test_high_recommendation_cannot_compensate_for_failed_targets(
    storage,
) -> None:
    tech_set = make_beatmapset(
        1,
        title="Ordinary map",
        tags=("tech",),
        rating=6.0,
        play_count=100,
        favourite_count=2,
        beatmaps=(
            make_beatmap(
                11,
                1,
                stars=5.5,
                total_length=120,
                hit_length=90,
                bpm=180.0,
                count_circles=350,
                count_sliders=150,
                count_spinners=0,
            ),
        ),
    )
    popular_set = make_beatmapset(
        2,
        title="Very popular map",
        tags=(),
        rating=10.0,
        play_count=50_000_000,
        favourite_count=2_000_000,
        beatmaps=(
            make_beatmap(
                21,
                2,
                stars=5.5,
                total_length=120,
                hit_length=120,
                bpm=180.0,
                count_circles=80,
                count_sliders=5,
                count_spinners=0,
            ),
        ),
    )
    too_hard_tech = make_beatmapset(
        3,
        title="Tech but outside strict stars",
        tags=("tech",),
        beatmaps=(
            make_beatmap(
                31,
                3,
                stars=8.0,
                total_length=120,
                hit_length=90,
                bpm=180.0,
                count_circles=350,
                count_sliders=150,
                count_spinners=0,
            ),
        ),
    )
    api = StubApi(
        lambda query: SearchPage(
            beatmapsets=(tech_set, popular_set, too_hard_tech)
        )
    )
    preset = presets.builtin_by_name("Tech")
    assert preset is not None
    engine = DiscoveryEngine(api, storage, search_settings())
    result = engine.run(preset.filters, preset.ranking)

    ids = [item.beatmapset.id for item in result.scored]
    assert ids == [1]
    assert result.passed_filters == 2
    assert result.passed_relevance == 1
    assert result.excluded["звёздность вне диапазона"] == 1
    assert result.excluded["соответствие целевым тегам ниже порога"] == 1
    relevance_exclusion = next(
        item for item in result.exclusions if item.stage == "relevance"
    )
    assert relevance_exclusion.beatmapset_id == 2
    assert "порог" in relevance_exclusion.reason
    assert "Tech" in relevance_exclusion.reason
    stored = result.scored[0].candidate.relevance
    assert stored is not None and stored.passed is True
    assert stored.targets


@pytest.mark.parametrize("name", ["Mixed Aim", "All-rounder"])
def test_generic_presets_do_not_filter_untagged_maps(name, storage) -> None:
    sets = tuple(
        make_beatmapset(
            index,
            tags=(),
            title=f"Untagged {index}",
            beatmaps=(
                make_beatmap(
                    index * 10 + 1,
                    index,
                    stars=5.0,
                    total_length=120,
                    hit_length=110,
                ),
            ),
        )
        for index in (1, 2)
    )
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    preset = presets.builtin_by_name(name)
    assert preset is not None
    engine = DiscoveryEngine(api, storage, search_settings())
    result = engine.run(preset.filters, preset.ranking)
    assert {item.beatmapset.id for item in result.scored} == {1, 2}
    assert result.passed_filters == 2
    assert result.passed_relevance == 2
    assert all(
        item.candidate.relevance is None for item in result.scored
    )


def test_strict_filters_run_before_relevance(storage) -> None:
    # The target gate must never rescue a map that fails stars/duration/status.
    preset = presets.builtin_by_name("Tech")
    assert preset is not None
    slow_tech = make_beatmapset(
        1,
        tags=("tech",),
        beatmaps=(
            make_beatmap(
                11,
                1,
                stars=5.5,
                total_length=400,
                hit_length=390,
                bpm=180.0,
                count_circles=350,
                count_sliders=150,
                count_spinners=0,
            ),
        ),
    )
    api = StubApi(lambda query: SearchPage(beatmapsets=(slow_tech,)))
    engine = DiscoveryEngine(api, storage, search_settings())
    result = engine.run(preset.filters, preset.ranking)
    assert result.scored == ()
    assert result.excluded["длительность вне диапазона"] == 1
    assert result.excluded.get("соответствие целевым тегам ниже порога", 0) == 0


def test_invalid_relevance_configuration_is_rejected() -> None:
    settings = RankingSettings()
    settings.relevance_targets = ["unknown_skill"]
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)

    settings = RankingSettings()
    settings.relevance_targets = ["tech", "tech"]
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)

    settings = RankingSettings()
    settings.relevance_targets = ["tech"]
    settings.relevance_threshold = 0.4
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)
