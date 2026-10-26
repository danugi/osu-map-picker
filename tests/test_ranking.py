from __future__ import annotations

import math

import pytest
from conftest import make_beatmap, make_beatmapset

from models import AimTraits, BeatmapFeatures, Candidate, RankingSettings
from features import derive_features
from ranking import (
    RankingConfigError,
    RankingContext,
    diversify,
    evaluate_candidate,
    explain,
    validate_ranking,
)
from skillsets import evaluate_skills


def candidate(
    set_id: int,
    *,
    stars: float | None = 5.0,
    duration: int | None = 140,
    artist: str | None = None,
    creator: str | None = None,
    traits: dict[str, float] | None = None,
    bpm: float | None = 180.0,
) -> Candidate:
    beatmapset = make_beatmapset(
        set_id,
        artist=artist or f"Artist {set_id}",
        creator=creator or f"Mapper {set_id}",
    )
    beatmap = make_beatmap(
        set_id * 10 + 1,
        set_id,
        stars=stars,
        total_length=duration,
        hit_length=duration,
        bpm=bpm,
    )
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        traits=AimTraits(
            scores=traits or {},
            confidence="medium" if traits else "none",
        ),
    )


def context(
    settings: RankingSettings | None = None,
    *,
    feedback: dict[int, tuple[str, ...]] | None = None,
    sets: dict | None = None,
    seen: dict[int, int] | None = None,
    recent: tuple = (),
) -> RankingContext:
    return RankingContext.from_user_data(
        settings or RankingSettings(),
        feedback or {},
        sets or {},
        seen or {},
        recent_sets=recent,
    )


def skill_candidate(
    set_id: int, features: BeatmapFeatures, *, duration: int = 140
) -> Candidate:
    beatmapset = make_beatmapset(set_id)
    beatmap = make_beatmap(
        set_id * 10 + 1,
        set_id,
        stars=5.5,
        total_length=duration,
        hit_length=duration,
        bpm=180.0,
        count_circles=200,
        count_sliders=80,
    )
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        traits=features,
        skillsets=evaluate_skills(features),
    )


def only_factor(settings: RankingSettings, key: str, weight: float = 3.0) -> RankingSettings:
    for factor in settings.factors.values():
        factor.enabled = False
    settings.factors[key].enabled = True
    settings.factors[key].weight = weight
    return settings


def test_skillset_weights_change_ranking_order() -> None:
    aim_features = BeatmapFeatures(
        scores={"aim": 0.9, "aim_pressure": 0.85, "stars": 0.7, "density": 0.5},
        sources={
            "aim": "osu_api_attributes",
            "aim_pressure": "osu_api_attributes",
            "stars": "beatmap_stars",
            "density": "beatmap_counts",
        },
        confidence="medium",
    )
    speed_features = BeatmapFeatures(
        scores={
            "speed": 0.9,
            "speed_pressure": 0.85,
            "bpm": 0.8,
            "density": 0.6,
        },
        sources={
            "speed": "osu_api_attributes",
            "speed_pressure": "osu_api_attributes",
            "bpm": "beatmap_bpm",
            "density": "beatmap_counts",
        },
        confidence="medium",
    )
    aim_map = skill_candidate(1, aim_features)
    speed_map = skill_candidate(2, speed_features)

    aim_settings = only_factor(RankingSettings(), "mapping_fit")
    aim_settings.skill_weights = {"aim_classic": 1.0}
    aim_ctx = context(aim_settings)
    assert evaluate_candidate(aim_map, aim_ctx).score > evaluate_candidate(
        speed_map, aim_ctx
    ).score

    speed_settings = only_factor(RankingSettings(), "mapping_fit")
    speed_settings.skill_weights = {"speed_high_bpm": 1.0}
    speed_ctx = context(speed_settings)
    assert evaluate_candidate(speed_map, speed_ctx).score > evaluate_candidate(
        aim_map, speed_ctx
    ).score


def test_cross_category_skills_contribute_together() -> None:
    best = skill_candidate(
        1,
        BeatmapFeatures(
            scores={
                "aim": 0.9,
                "aim_pressure": 0.9,
                "stars": 0.7,
                "density": 0.7,
                "streams": 0.9,
                "stamina": 0.9,
            },
            sources={
                "aim": "osu_api_attributes",
                "aim_pressure": "osu_api_attributes",
                "stars": "beatmap_stars",
                "density": "beatmap_counts",
                "streams": "osu_api_attributes",
                "stamina": "beatmap_length",
            },
            confidence="medium",
        ),
    )
    aim_only = skill_candidate(
        2,
        BeatmapFeatures(
            scores={
                "aim": 0.9,
                "aim_pressure": 0.9,
                "stars": 0.7,
                "density": 0.2,
                "streams": 0.1,
                "stamina": 0.1,
            },
            sources={
                "aim": "osu_api_attributes",
                "aim_pressure": "osu_api_attributes",
                "stars": "beatmap_stars",
                "density": "beatmap_counts",
                "streams": "osu_api_attributes",
                "stamina": "beatmap_length",
            },
            confidence="medium",
        ),
    )
    stream_only = skill_candidate(
        3,
        BeatmapFeatures(
            scores={
                "aim": 0.1,
                "aim_pressure": 0.1,
                "stars": 0.7,
                "density": 0.8,
                "streams": 0.9,
                "stamina": 0.9,
            },
            sources={
                "aim": "osu_api_attributes",
                "aim_pressure": "osu_api_attributes",
                "stars": "beatmap_stars",
                "density": "beatmap_counts",
                "streams": "osu_api_attributes",
                "stamina": "beatmap_length",
            },
            confidence="medium",
        ),
    )
    settings = only_factor(RankingSettings(), "mapping_fit")
    settings.skill_weights = {"aim_classic": 1.0, "stream_consistency": 1.0}
    ctx = context(settings)
    best_score = evaluate_candidate(best, ctx).score
    assert best_score > evaluate_candidate(aim_only, ctx).score
    assert best_score > evaluate_candidate(stream_only, ctx).score


def test_mapping_fit_reports_selected_skill_without_data() -> None:
    empty = BeatmapFeatures()
    target = skill_candidate(1, empty)
    settings = only_factor(RankingSettings(), "mapping_fit")
    settings.skill_weights = {"fl_friendly": 1.0}
    scored = evaluate_candidate(target, context(settings))
    factor = next(item for item in scored.factors if item.key == "mapping_fit")
    assert factor.value is None
    assert "не хватает" in factor.explanation
    assert scored.score == 0.5  # no invented value; neutral fallback only


def test_nm_farm_factor_prefers_short_comfortable_aim() -> None:
    features = BeatmapFeatures(
        scores={
            "aim": 0.85,
            "aim_pressure": 0.8,
            "density": 0.55,
        },
        sources={
            "aim": "osu_api_attributes",
            "aim_pressure": "osu_api_attributes",
            "density": "beatmap_counts",
        },
        confidence="medium",
    )
    short = skill_candidate(1, features, duration=100)
    long = skill_candidate(2, features, duration=420)
    settings = only_factor(RankingSettings(), "farm_nm")
    settings.comfort_stars = 5.5
    ctx = context(settings)
    short_scored = evaluate_candidate(short, ctx)
    long_scored = evaluate_candidate(long, ctx)
    assert short_scored.score > long_scored.score
    farm = next(item for item in short_scored.factors if item.key == "farm_nm")
    assert farm.value is not None and farm.value > 0.7
    assert "расчёт производительности" in farm.explanation
    assert "PP" not in farm.explanation
    assert "моды" in farm.explanation or "без модов" in farm.explanation


def test_nm_farm_without_aim_evidence_is_missing() -> None:
    target = skill_candidate(
        1,
        BeatmapFeatures(
            scores={"density": 0.5},
            sources={"density": "beatmap_counts"},
            confidence="low",
        ),
    )
    settings = only_factor(RankingSettings(), "farm_nm")
    scored = evaluate_candidate(target, context(settings))
    farm = next(item for item in scored.factors if item.key == "farm_nm")
    assert farm.value is None
    assert "нельзя честно оценить" in farm.explanation


def test_performance_point_surface_is_absent() -> None:
    from pathlib import Path

    fields = Candidate.__dataclass_fields__
    assert "nm_pp" not in fields
    assert "nm_pp_scenario" not in fields
    scored = evaluate_candidate(candidate(1), context())
    assert not any(
        factor.key in {"pp", "nm_pp"} or factor.key.startswith("pp_")
        for factor in scored.factors
    )
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8").lower()
    assert "rosu" not in requirements
    assert not (Path(__file__).resolve().parents[1] / "pp.py").exists()


def test_weight_change_changes_order() -> None:
    comfortable = candidate(1, stars=5.4, duration=400)
    hard_short = candidate(2, stars=6.5, duration=140)
    ctx = context()

    settings_a = RankingSettings()
    settings_a.factors["difficulty"].weight = 3.0
    settings_a.factors["length"].weight = 0.1
    ctx_a = context(settings_a)
    assert evaluate_candidate(comfortable, ctx_a).score > evaluate_candidate(
        hard_short, ctx_a
    ).score

    settings_b = RankingSettings()
    settings_b.factors["difficulty"].weight = 0.1
    settings_b.factors["length"].weight = 3.0
    ctx_b = context(settings_b)
    assert evaluate_candidate(hard_short, ctx_b).score > evaluate_candidate(
        comfortable, ctx_b
    ).score


def test_positive_and_negative_feedback_change_score() -> None:
    target = candidate(1)
    sets = {1: target.beatmapset}
    positive = evaluate_candidate(
        target, context(feedback={1: ("like",)}, sets=sets)
    )
    negative = evaluate_candidate(
        target, context(feedback={1: ("mapping_dislike",)}, sets=sets)
    )
    neutral = evaluate_candidate(target, context())
    assert positive.score > neutral.score > negative.score
    feedback_factor = [f for f in positive.factors if f.key == "feedback"][0]
    assert feedback_factor.value == 1.0


def test_score_contributions_sum_to_unclamped_score_and_skip_unknown() -> None:
    settings = RankingSettings()
    scored = evaluate_candidate(candidate(41, traits={"aim": 0.7, "density": 0.5}), context(settings))
    assert 0.0 < scored.score < 1.0
    contributions = sum(
        factor.contribution
        for factor in scored.factors
        if factor.value is not None
    )
    assert math.isclose(contributions, scored.score, abs_tol=1e-9)
    for factor in scored.factors:
        if factor.value is None:
            assert factor.contribution == 0.0


def test_missing_features_are_reported_but_do_not_crash() -> None:
    beatmapset = make_beatmapset(9, genre_id=None, rating=None, play_count=None,
                                favourite_count=None, bpm=None,
                                ranked_date=None, submitted_date=None)
    beatmap = make_beatmap(
        91,
        9,
        stars=None,
        total_length=None,
        hit_length=None,
        bpm=None,
        ar=None,
        od=None,
        cs=None,
        hp=None,
        count_circles=None,
        count_sliders=None,
        count_spinners=None,
    )
    target = Candidate(beatmapset, beatmap)
    scored = evaluate_candidate(target, context())
    assert math.isfinite(scored.score)
    assert 0.0 <= scored.score <= 1.0
    assert 0 <= scored.display_score <= 100
    assert any(factor.value is None for factor in scored.factors)
    assert any("Нет данных" in reason for reason in scored.reasons)
    lines = explain(scored)
    assert any("нет данных" in line for line in lines)


def test_too_hard_shifts_comfort_without_touching_taste() -> None:
    target = candidate(1, stars=6.5)
    sets = {1: target.beatmapset}
    neutral = evaluate_candidate(target, context(sets=sets))
    too_hard = evaluate_candidate(
        target, context(feedback={1: ("too_hard",)}, sets=sets)
    )
    neutral_difficulty = [f for f in neutral.factors if f.key == "difficulty"][0]
    hard_difficulty = [f for f in too_hard.factors if f.key == "difficulty"][0]
    assert hard_difficulty.value < neutral_difficulty.value
    assert "слишком сложно" in hard_difficulty.explanation
    taste = [f for f in too_hard.factors if f.key == "taste"][0]
    assert taste.value is None  # one workflow/difficulty verdict is not taste
    assert too_hard.score < neutral.score


def test_seen_penalty_lowers_score() -> None:
    target = candidate(1)
    fresh = evaluate_candidate(target, context())
    seen = evaluate_candidate(target, context(seen={1: 3}))
    assert seen.score < fresh.score
    penalty_factor = [f for f in seen.factors if f.key == "repeat_penalty"][0]
    assert penalty_factor.value is not None and penalty_factor.value > 0
    assert seen.penalty > 0


def test_similar_recent_cards_are_penalised() -> None:
    target = candidate(1, artist="Same Artist", creator="Same Mapper")
    recent = (
        make_beatmapset(2, artist="Same Artist", creator="Same Mapper"),
    )
    scored = evaluate_candidate(target, context(recent=recent))
    factor = [f for f in scored.factors if f.key == "repeat_penalty"][0]
    assert factor.value is not None and factor.value > 0.5


def test_diversification_limits_same_artist() -> None:
    scored = [
        evaluate_candidate(candidate(i, artist="Same Artist", creator="Same Mapper"), context())
        for i in range(1, 4)
    ] + [
        evaluate_candidate(candidate(i, artist=f"Other {i}"), context())
        for i in range(4, 7)
    ]
    no_diversity = RankingSettings()
    no_diversity.diversity = 0.0
    no_diversity.max_per_artist = 1
    ordered = diversify(scored, no_diversity)
    assert len({item.beatmapset.display_artist for item in ordered[:3]}) == 1

    with_diversity = RankingSettings()
    with_diversity.diversity = 0.85
    with_diversity.max_per_artist = 1
    diversified = diversify(scored, with_diversity)
    assert len({item.beatmapset.display_artist for item in diversified[:3]}) == 3


def test_diversification_relaxation_is_reported() -> None:
    scored = [
        evaluate_candidate(candidate(i, artist="Same Artist"), context())
        for i in range(1, 4)
    ]
    settings = RankingSettings()
    settings.max_per_artist = 1
    settings.diversity = 0.8
    notes: list[str] = []
    result = diversify(scored, settings, notes=notes)
    assert len(result) == 3
    assert any("ослаблены" in note for note in notes)


def test_explanation_is_transparent_and_not_probability() -> None:
    scored = evaluate_candidate(candidate(1), context())
    lines = explain(scored)
    assert "не вероятность" in lines[0]
    assert any("Комфорт сложности" in line for line in lines)
    assert all("nan" not in line.lower() for line in lines)


def test_disabled_factor_is_not_evaluated() -> None:
    settings = RankingSettings()
    settings.factors["difficulty"].enabled = False
    scored = evaluate_candidate(candidate(1), context(settings))
    assert "difficulty" not in {factor.key for factor in scored.factors}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.factors["difficulty"].__setattr__("weight", -1),
        lambda s: s.factors["difficulty"].__setattr__("weight", 6),
        lambda s: s.__setattr__("diversity", 1.5),
        lambda s: s.__setattr__("max_per_artist", 0),
        lambda s: s.__setattr__("comfort_width", 0),
        lambda s: s.__setattr__("ideal_length", 1),
        lambda s: s.__setattr__("preferred_genres", [999]),
        lambda s: s.__setattr__("trait_targets", {"nonsense": 0.5}),
        lambda s: s.__setattr__("skill_weights", {"nonsense": 1.0}),
        lambda s: s.__setattr__("skill_weights", {"aim_classic": 6.0}),
        lambda s: s.__setattr__("skill_weights", {"aim_classic": 0.0}),
        lambda s: s.__setattr__("farm_target_seconds", 5),
    ],
)
def test_invalid_ranking_settings(mutate) -> None:
    settings = RankingSettings()
    mutate(settings)
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)


def test_no_enabled_factor_is_invalid() -> None:
    settings = RankingSettings()
    for factor in settings.factors.values():
        factor.enabled = False
    with pytest.raises(RankingConfigError):
        validate_ranking(settings)
