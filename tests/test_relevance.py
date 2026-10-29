from __future__ import annotations

from typing import Callable

import pytest
from conftest import make_beatmap, make_beatmapset

import config
import evidence
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
    SequenceAnalysis,
    SkillScore,
)
from features import derive_features
from ranking import RankingConfigError, validate_ranking
from relevance import evaluate_relevance
from skillsets import evaluate_skills


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
        # Unit tests must never attempt a real .osu download. D3 makes
        # sequence analysis relevant for several presets; zero means cache-only.
        fetch_sequences_limit=0,
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
    features: dict[str, float] | None = None,
    feature_statuses: dict[str, str] | None = None,
    feature_sources: dict[str, str] | None = None,
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
        traits=BeatmapFeatures(
            scores=dict(features or {}),
            statuses=dict(feature_statuses or {}),
            sources=dict(feature_sources or {}),
        ),
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


def test_discovery_and_allrounder_have_no_target_gate() -> None:
    for name in ("Discovery", "All-rounder"):
        preset = presets.builtin_by_name(name)
        assert preset is not None
        assert preset.ranking.relevance_targets == []
        assert preset.ranking.relevance_policy == {}
    mixed = presets.builtin_by_name("Mixed Aim")
    assert mixed is not None
    assert mixed.ranking.relevance_targets
    assert mixed.ranking.relevance_policy["primary_targets"]
    candidate = score_candidate(1, {})
    check = evaluate_relevance(candidate, RankingSettings())
    assert check.passed is True
    assert check.targets == ()
    assert "не заданы" in check.reason


def test_specialized_presets_use_their_own_weighted_targets() -> None:
    expected = {
        "Mixed Aim",
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
    target_sets: dict[str, tuple[str, ...]] = {}
    for name in expected:
        preset = presets.builtin_by_name(name)
        assert preset is not None
        targets = tuple(preset.ranking.relevance_targets)
        assert targets, name
        assert len(set(targets)) == len(targets), name
        # A mandatory gate may not pass a map on a skill that the preset does
        # not actually rank.
        assert set(targets) <= set(preset.ranking.skill_weights), name
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
    assert target_sets["Aim Farm"] == ("aim_classic",)

    legacy_streams = presets.known_builtin_by_name("Streams")
    assert legacy_streams is not None
    assert legacy_streams.ranking.relevance_targets
    assert set(legacy_streams.ranking.relevance_targets) <= set(
        legacy_streams.ranking.skill_weights
    )


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
    required = tuple(
        preset.ranking.relevance_policy.get("require_features_any", ())
    )
    features = {required[0]: 1.0} if required else {}
    check = evaluate_relevance(
        score_candidate(1, scores, features=features),
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
        features={"tag_tech": 1.0},
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
    assert "частичный анализ" in relevance_exclusion.reason.casefold()
    assert "Slider-heavy" in relevance_exclusion.reason
    stored = result.scored[0].candidate.relevance
    assert stored is not None and stored.passed is True
    assert stored.targets


@pytest.mark.parametrize("name", ["Discovery", "All-rounder"])
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


def _derived_candidate(
    set_id: int,
    *,
    tags: tuple[str, ...] = (),
    stars: float = 5.5,
    bpm: float = 180.0,
    ar: float = 9.0,
    od: float = 8.0,
    circles: int = 350,
    sliders: int = 150,
    sequence: SequenceAnalysis | None = None,
) -> Candidate:
    beatmapset = make_beatmapset(
        set_id,
        tags=tags,
        beatmaps=(
            make_beatmap(
                set_id * 10 + 1,
                set_id,
                stars=stars,
                bpm=bpm,
                ar=ar,
                od=od,
                total_length=120,
                hit_length=110,
                count_circles=circles,
                count_sliders=sliders,
                count_spinners=0,
            ),
        ),
    )
    beatmap = beatmapset.beatmaps[0]
    traits = derive_features(beatmap, beatmapset, None, sequence=sequence)
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        traits=traits,
        skillsets=evaluate_skills(traits),
        sequence=sequence,
    )


def test_aim_farm_gate_rejects_missing_aim_attributes() -> None:
    preset = presets.builtin_by_name("Aim Farm")
    assert preset is not None
    candidate = _derived_candidate(
        1,
        tags=(),
        circles=600,
        sliders=200,
    )
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is False
    assert all(target.score is None for target in check.targets)
    assert all(
        target.status in {"недостаточно данных", "не анализировалось"}
        for target in check.targets
    )
    assert (
        "недостаточно данных" in check.reason
        or "не анализировалось" in check.reason
    )


def test_speed_gate_rejects_missing_speed_attributes() -> None:
    preset = presets.builtin_by_name("Speed")
    assert preset is not None
    candidate = _derived_candidate(
        1,
        tags=(),
        bpm=240.0,
        circles=700,
        sliders=50,
    )
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is False
    assert all(target.score is None for target in check.targets)
    assert (
        "недостаточно данных" in check.reason
        or ".osu" in check.reason
        or "sequence" in check.reason
    )


def test_burst_target_without_sequence_is_not_analyzed() -> None:
    preset = presets.builtin_by_name("Bursts")
    assert preset is not None
    candidate = _derived_candidate(1)
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is False
    assert all(target.status == "не анализировалось" for target in check.targets)


def test_analyzed_burst_target_without_bursts_is_below_threshold() -> None:
    preset = presets.builtin_by_name("Bursts")
    assert preset is not None
    candidate = _derived_candidate(
        1,
        sequence=SequenceAnalysis(analyzed_notes=10, total_notes=10),
    )
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is False
    assert all(target.status == "ниже порога" for target in check.targets)


def test_tag_only_target_is_not_reported_as_confirmed() -> None:
    candidate = score_candidate(
        1,
        {"tech": 0.9},
        tags=("tech",),
        features={"tag_tech": 1.0},
        feature_statuses={"tag_tech": evidence.PROXY},
        feature_sources={"tag_tech": "tags"},
    )
    preset = presets.builtin_by_name("Tech")
    assert preset is not None
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is True
    target = next(item for item in check.targets if item.target_id == "tech")
    assert target.status == "выше порога (ограниченная поддержка)"


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


def test_mixed_aim_requires_an_aim_target() -> None:
    preset = presets.builtin_by_name("Mixed Aim")
    assert preset is not None
    scores = {
        "aim_classic": 0.20,
        "aim_control": 0.20,
        "aim_bursts": 0.10,
    }
    check = evaluate_relevance(score_candidate(1, scores), preset.ranking)
    assert check.passed is False
    assert "основной" in check.reason.casefold()

    alternative = evaluate_relevance(
        score_candidate(
            2,
            {"aim_classic": 0.10, "aim_control": 0.80, "aim_bursts": 0.10},
        ),
        preset.ranking,
    )
    assert alternative.passed is True
    assert alternative.best_target is not None
    assert alternative.best_target.target_id == "aim_control"


def test_reading_secondary_ar_targets_do_not_replace_ar_challenge() -> None:
    preset = presets.builtin_by_name("Reading")
    assert preset is not None
    check = evaluate_relevance(
        score_candidate(
            1,
            {"ar_challenge": 0.50, "high_ar": 1.0, "low_ar": 0.0},
        ),
        preset.ranking,
    )
    assert check.passed is False
    assert "Вторичные совпадения" in check.reason
    assert evaluate_relevance(
        score_candidate(
            2,
            {"ar_challenge": 0.80, "high_ar": 0.0, "low_ar": 0.0},
        ),
        preset.ranking,
    ).passed is True


def test_accuracy_high_od_without_precision_evidence_fails() -> None:
    preset = presets.builtin_by_name("Accuracy")
    assert preset is not None
    scores = {
        "accuracy_focused": 0.80,
        "od_precision": 0.90,
        "low_density_precision": 0.10,
    }
    check = evaluate_relevance(score_candidate(1, scores), preset.ranking)
    assert check.passed is False
    assert "precision/accuracy" in check.reason.casefold()

    with_tag = evaluate_relevance(
        score_candidate(2, scores, features={"tag_precision": 1.0}),
        preset.ranking,
    )
    assert with_tag.passed is True

    sparse = evaluate_relevance(
        score_candidate(3, scores, features={"low_density": 0.90}),
        preset.ranking,
    )
    assert sparse.passed is True


def test_tech_generic_high_difficulty_needs_pattern_evidence() -> None:
    preset = presets.builtin_by_name("Tech")
    assert preset is not None
    scores = {"tech": 0.80, "slider_heavy": 0.20, "rhythm_complexity": 0.20}
    check = evaluate_relevance(score_candidate(1, scores), preset.ranking)
    assert check.passed is False
    assert "tech" in check.reason.casefold()

    tagged = evaluate_relevance(
        score_candidate(2, scores, features={"tag_tech": 1.0}),
        preset.ranking,
    )
    assert tagged.passed is True

    slider = evaluate_relevance(
        score_candidate(3, scores, features={"slider_ratio": 0.80}),
        preset.ranking,
    )
    assert slider.passed is True


def test_finger_control_requires_finger_or_alt_metadata() -> None:
    preset = presets.builtin_by_name("Finger Control")
    assert preset is not None
    scores = {
        "finger_control": 0.80,
        "alternating": 0.10,
        "speed_control": 0.10,
    }
    check = evaluate_relevance(score_candidate(1, scores), preset.ranking)
    assert check.passed is False
    assert "finger control" in check.reason.casefold()
    assert evaluate_relevance(
        score_candidate(2, scores, tags=("fingercontrol",), features={"tag_finger": 1.0}),
        preset.ranking,
    ).passed is True


def test_flashlight_requires_its_tag_and_primary_target() -> None:
    preset = presets.builtin_by_name("Flashlight")
    assert preset is not None
    scores = {"fl_friendly": 0.80, "fl_reading": 0.10, "fl_aim": 0.10}
    assert evaluate_relevance(score_candidate(1, scores), preset.ranking).passed is False
    assert evaluate_relevance(
        score_candidate(2, scores, tags=("flashlight",), features={"tag_fl": 1.0}),
        preset.ranking,
    ).passed is True


def test_speed_and_sequence_policies_require_measured_support() -> None:
    for name, target, feature in (
        ("Speed", "speed_high_bpm", "speed_pressure"),
        ("Streams & Stamina", "stream_consistency", "streams"),
        ("Bursts", "burst_control", "bursts"),
    ):
        preset = presets.builtin_by_name(name)
        assert preset is not None
        scores = {
            target: 0.80,
            **{
                item: 0.10
                for item in preset.ranking.relevance_targets
                if item != target
            },
        }
        assert evaluate_relevance(score_candidate(1, scores), preset.ranking).passed is False
        assert evaluate_relevance(
            score_candidate(1, scores, features={feature: 1.0}),
            preset.ranking,
        ).passed is True


def test_aim_farm_legacy_policy_is_empty_and_fingerprint_stable() -> None:
    preset = presets.builtin_by_name("Aim Farm")
    assert preset is not None
    assert preset.ranking.relevance_targets == ["aim_classic"]
    assert preset.ranking.relevance_policy == {}
    assert presets._preset_fingerprint(preset) == (
        "1c6d419138c4f5c7590c7d12bd061c31a74c6b52064e7b446f932c75fc855294"
    )


def test_sequence_policy_reports_missing_partial_and_failed_analysis() -> None:
    preset = presets.builtin_by_name("Streams & Stamina")
    assert preset is not None
    expected = {
        "not_analyzed": "не анализировалось",
        "partial": "частичный анализ",
        "failed": "анализ не удался",
    }
    for status, text in expected.items():
        candidate = score_candidate(1, {})
        candidate.skillsets["stream_consistency"] = SkillScore(
            skill_id="stream_consistency",
            name="Stream consistency",
            score=None,
            status=status,
        )
        check = evaluate_relevance(candidate, preset.ranking)
        assert check.passed is False
        assert text in check.reason.casefold()


def test_accuracy_missing_precision_features_is_not_a_match() -> None:
    preset = presets.builtin_by_name("Accuracy")
    assert preset is not None
    check = evaluate_relevance(
        score_candidate(1, {"accuracy_focused": 0.80}),
        preset.ranking,
    )
    assert check.passed is False
    assert "precision/accuracy" in check.reason.casefold()


def _mark_partial(
    candidate: Candidate,
    target_id: str,
    score: float = 0.95,
) -> None:
    candidate.skillsets[target_id] = SkillScore(
        skill_id=target_id,
        name=target_id,
        score=score,
        confidence="medium",
        support="measured",
        sources=("osu_sequence_partial",),
        status=evidence.PARTIAL,
    )


def test_partial_stream_target_never_satisfies_streams_policy() -> None:
    preset = presets.builtin_by_name("Streams & Stamina")
    assert preset is not None
    candidate = score_candidate(
        1,
        {"stream_consistency": 0.10, "stream_long": 0.10},
        features={"streams": 0.95},
    )
    _mark_partial(candidate, "stream_consistency")
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is False
    target = next(
        item for item in check.targets if item.target_id == "stream_consistency"
    )
    assert target.score == 0.95
    assert target.matched is False
    assert target.status == "частичный анализ"
    assert "частичный анализ" in check.reason.casefold()


def test_partial_burst_target_never_satisfies_bursts_policy() -> None:
    preset = presets.builtin_by_name("Bursts")
    assert preset is not None
    candidate = score_candidate(
        1,
        {
            "burst_control": 0.10,
            "speed_bursts": 0.10,
            "speed_short_bursts": 0.10,
        },
        features={"bursts": 0.95},
    )
    _mark_partial(candidate, "burst_control")
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is False
    assert "частичный анализ" in check.reason.casefold()


def test_partial_required_features_never_satisfy_policy() -> None:
    streams = presets.builtin_by_name("Streams & Stamina")
    bursts = presets.builtin_by_name("Bursts")
    assert streams is not None and bursts is not None

    stream_candidate = score_candidate(
        1,
        {"stream_consistency": 0.90, "stream_long": 0.10},
        features={"streams": 0.99},
        feature_statuses={"streams": evidence.PARTIAL},
        feature_sources={"streams": "osu_sequence_partial"},
    )
    stream_check = evaluate_relevance(stream_candidate, streams.ranking)
    assert stream_check.passed is False
    assert "частичный анализ обязательных признаков" in stream_check.reason.casefold()

    burst_candidate = score_candidate(
        1,
        {
            "burst_control": 0.90,
            "speed_bursts": 0.10,
            "speed_short_bursts": 0.10,
        },
        features={"bursts": 0.99},
        feature_statuses={"bursts": evidence.PARTIAL},
        feature_sources={"bursts": "osu_sequence_partial"},
    )
    burst_check = evaluate_relevance(burst_candidate, bursts.ranking)
    assert burst_check.passed is False
    assert "частичный анализ обязательных признаков" in burst_check.reason.casefold()


def test_non_partial_alternative_can_still_satisfy_policy() -> None:
    preset = presets.builtin_by_name("Streams & Stamina")
    assert preset is not None
    candidate = score_candidate(
        1,
        {"stream_consistency": 0.95, "stream_long": 0.80},
        features={"streams": 0.95},
        feature_statuses={"streams": evidence.MEASURED},
        feature_sources={"streams": "osu_sequence"},
    )
    _mark_partial(candidate, "stream_consistency")
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is True
    assert check.best_target is not None
    assert check.best_target.target_id == "stream_long"

    tech = presets.builtin_by_name("Tech")
    assert tech is not None
    feature_candidate = score_candidate(
        2,
        {"tech": 0.85, "slider_heavy": 0.10, "rhythm_complexity": 0.10},
        features={"tag_tech": 0.99, "slider_ratio": 0.80},
        feature_statuses={
            "tag_tech": evidence.PARTIAL,
            "slider_ratio": evidence.MEASURED,
        },
        feature_sources={
            "tag_tech": "osu_sequence_partial",
            "slider_ratio": "beatmap_counts",
        },
    )
    feature_check = evaluate_relevance(feature_candidate, tech.ranking)
    assert feature_check.passed is True
    assert "доля слайдеров" in feature_check.reason.casefold()


def test_fully_analyzed_sequence_evidence_still_passes() -> None:
    preset = presets.builtin_by_name("Streams & Stamina")
    assert preset is not None
    candidate = score_candidate(
        1,
        {"stream_consistency": 0.90, "stream_long": 0.10},
        features={"streams": 0.99},
        feature_statuses={"streams": evidence.MEASURED},
        feature_sources={"streams": "osu_sequence"},
    )
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is True
    assert "обязательные признаки" in check.reason.casefold()
    assert "streams" in check.reason.casefold()


def test_invalid_relevance_policy_is_rejected() -> None:
    settings = RankingSettings()
    settings.relevance_targets = ["tech"]
    settings.relevance_policy = {"primary_targets": ["reading"]}
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)

    settings.relevance_policy = {"min_primary": 0}
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)

    settings.relevance_policy = {"require_features_any": ["no_such_feature"]}
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)

    settings.relevance_policy = {"unknown": True}
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)


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
