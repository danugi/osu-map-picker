from __future__ import annotations

from conftest import make_beatmap, make_beatmapset

from features import derive_features
from models import BeatmapFeatures
from skillsets import (
    FAMILY_ORDER,
    SKILLS,
    SKILL_DEFINITIONS,
    evaluate_skills,
    skill_fit,
    skills_by_family,
)


def measured_features() -> BeatmapFeatures:
    return BeatmapFeatures(
        scores={
            "aim": 0.8,
            "speed": 0.2,
            "aim_pressure": 0.75,
            "speed_pressure": 0.2,
            "stars": 0.7,
            "density": 0.6,
            "bpm": 0.5,
        },
        sources={
            "aim": "osu_api_attributes",
            "speed": "osu_api_attributes",
            "aim_pressure": "osu_api_attributes",
            "speed_pressure": "osu_api_attributes",
            "stars": "beatmap_stars",
            "density": "beatmap_counts",
            "bpm": "beatmap_bpm",
        },
        confidence="medium",
    )


def sequence_features(
    *,
    streams: float = 0.0,
    bursts: float = 0.0,
) -> BeatmapFeatures:
    values = {
        "streams": streams,
        "bursts": bursts,
        "stream_longest": streams,
        "stream_chain_count": streams,
        "stream_rhythm_quality": streams,
        "burst_count": bursts,
        "burst_median": bursts,
        "burst_tempo": bursts,
        "burst_rhythm_quality": bursts,
    }
    return BeatmapFeatures(
        scores=values,
        sources={key: "osu_sequence" for key in values},
        confidence="high",
    )


def test_stream_and_burst_scores_are_independent() -> None:
    stream_skills = {
        "speed_streams",
        "stream_consistency",
        "stream_long",
        "stream_short",
        "stream_control",
    }
    burst_skills = {
        "speed_short_bursts",
        "speed_bursts",
        "burst_control",
        "complex_bursts",
    }
    for skill_id in stream_skills:
        assert "bursts" not in SKILLS[skill_id].feature_names, skill_id
    for skill_id in burst_skills:
        assert "streams" not in SKILLS[skill_id].feature_names, skill_id

    stream_only = evaluate_skills(sequence_features(streams=1.0))
    assert stream_only["stream_long"].score is not None
    assert stream_only["stream_long"].score > 0.55
    assert stream_only["speed_bursts"].score == 0.0
    assert stream_only["burst_control"].score == 0.0

    burst_only = evaluate_skills(sequence_features(bursts=1.0))
    assert burst_only["burst_control"].score is not None
    assert burst_only["burst_control"].score > 0.55
    assert burst_only["speed_streams"].score == 0.0
    assert burst_only["stream_long"].score == 0.0


def test_catalog_covers_required_families_and_compact_aim_taxonomy() -> None:
    assert len(SKILL_DEFINITIONS) == 76
    assert tuple(skills_by_family("aim")) and len(skills_by_family("aim")) == 6
    assert {item.id for item in skills_by_family("aim")} == {
        "aim_classic",
        "aim_wide",
        "aim_flow",
        "aim_control",
        "aim_precision",
        "aim_bursts",
    }
    forbidden = {"sharp_jumps", "awkward_angles", "cross_screen", "high_spacing"}
    assert forbidden.isdisjoint(SKILLS)
    assert {item.family for item in SKILL_DEFINITIONS} == set(FAMILY_ORDER)
    for definition in SKILL_DEFINITIONS:
        assert definition.description
        assert definition.support in {"measured", "proxy", "limited"}
        assert definition.applicable_filters
        if definition.components:
            assert all(component in SKILLS for component, _ in definition.components)
        else:
            assert definition.feature_weights


def test_single_skillset_uses_available_features_and_reports_confidence() -> None:
    skills = evaluate_skills(measured_features())
    classic = skills["aim_classic"]
    assert classic.score is not None
    assert 0.7 <= classic.score <= 1.0
    assert classic.confidence in {"low", "medium"}
    assert classic.support == "measured"
    assert classic.evidence
    assert classic.skill_id == "aim_classic"


def test_tag_only_evidence_is_low_confidence_but_not_aim_proof() -> None:
    beatmapset = make_beatmapset(1, tags=("aim", "stream", "tech"))
    beatmap = make_beatmap(11, 1, stars=5.0, bpm=180.0)
    features = derive_features(beatmap, beatmapset, None)
    skills = evaluate_skills(features)
    # A mapper tag is allowed to remain a low-confidence hint for a broad
    # category, but it must not satisfy mandatory official aim evidence.
    assert skills["tech"].score is not None
    assert skills["tech"].confidence == "low"
    assert skills["aim_classic"].score is None
    assert "aim_pressure" in skills["aim_classic"].missing
    # Tags do not create measured wide/flow geometry.
    assert "wide" not in features.scores
    assert "flow" not in features.scores


def test_missing_required_feature_is_not_invented() -> None:
    empty = BeatmapFeatures()
    skills = evaluate_skills(empty)
    assert skills["variable_rhythm"].score is None
    assert skills["fl_friendly"].score is None
    assert "tag_rhythm" in skills["variable_rhythm"].missing
    assert "tag_fl" in skills["fl_friendly"].missing


def test_multiple_skillsets_normalize_by_weights_and_report_missing() -> None:
    skills = evaluate_skills(measured_features())
    fit = skill_fit(skills, {"aim_classic": 1.0, "speed_high_bpm": 3.0})
    assert fit is not None
    assert 0.0 <= fit.score <= 1.0
    assert fit.coverage == 1.0
    # Weighting speed three times should move the score toward speed.
    aim_only = skill_fit(skills, {"aim_classic": 1.0})
    assert aim_only is not None and fit.score < aim_only.score
    assert set(fit.available) == {"aim_classic", "speed_high_bpm"}

    partial = skill_fit(skills, {"fl_friendly": 1.0, "aim_classic": 1.0})
    assert partial is not None
    assert partial.coverage == 0.5
    assert partial.missing == ("fl_friendly",)
    assert partial.confidence == "low"


def test_composite_skill_combines_components_and_marks_missing() -> None:
    skills = evaluate_skills(measured_features())
    speed_aim = skills["speed_aim"]
    assert speed_aim.score is not None
    assert speed_aim.evidence
    assert any("Classic Aim" in line for line in speed_aim.evidence)

    only_aim = BeatmapFeatures(
        scores={"aim": 0.8, "aim_pressure": 0.7, "stars": 0.7},
        sources={
            "aim": "osu_api_attributes",
            "aim_pressure": "osu_api_attributes",
            "stars": "beatmap_stars",
        },
        confidence="medium",
    )
    partial = evaluate_skills(only_aim)["speed_aim"]
    # Speed + aim is a target skill: missing speed evidence must not be
    # compensated by the aim component alone.
    assert partial.score is None
    assert "speed_high_bpm" in partial.missing
    assert partial.confidence == "none"


def test_difficulty_variety_requires_multiple_difficulties() -> None:
    first = make_beatmap(11, 1, stars=4.0)
    second = make_beatmap(12, 1, stars=7.0)
    beatmapset = make_beatmapset(1, beatmaps=(first, second))
    features = derive_features(first, beatmapset, None)
    assert features.get("difficulty_variety") is not None
    variety = features.get("difficulty_variety") or 0.0
    assert 0.7 <= variety <= 0.8

    single = make_beatmapset(2, beatmaps=(make_beatmap(21, 2, stars=5.0),))
    assert derive_features(single.beatmaps[0], single, None).get(
        "difficulty_variety"
    ) is None


def test_aim_classic_requires_official_aim_pressure() -> None:
    features = BeatmapFeatures(
        scores={"aim": 0.9, "stars": 0.8, "density": 0.7},
        sources={
            "aim": "tags",
            "stars": "beatmap_stars",
            "density": "beatmap_counts",
        },
        confidence="low",
    )
    skill = evaluate_skills(features)["aim_classic"]
    assert skill.score is None
    assert "aim_pressure" in skill.missing


def test_speed_high_bpm_requires_official_speed_pressure() -> None:
    features = BeatmapFeatures(
        scores={"bpm": 0.9, "speed": 0.8, "density": 0.7},
        sources={
            "bpm": "beatmap_bpm",
            "speed": "tags",
            "density": "beatmap_counts",
        },
        confidence="low",
    )
    skill = evaluate_skills(features)["speed_high_bpm"]
    assert skill.score is None
    assert "speed_pressure" in skill.missing


def test_required_any_accepts_one_reliable_feature() -> None:
    features = BeatmapFeatures(
        scores={"control": 0.8, "cs": 0.6},
        sources={"control": "osu_api_attributes", "cs": "beatmap_cs"},
        confidence="medium",
    )
    skill = evaluate_skills(features)["aim_control"]
    assert skill.score is not None
    assert "control" in skill.evidence[0] or "control" in " ".join(skill.evidence)


def test_stream_control_requires_sequence_evidence() -> None:
    features = BeatmapFeatures(
        scores={"od": 0.9, "slider_ratio": 0.8},
        sources={"od": "beatmap_od", "slider_ratio": "beatmap_counts"},
        confidence="low",
    )
    skill = evaluate_skills(features)["stream_control"]
    assert skill.score is None
    assert "streams" in skill.missing


def test_double_tap_control_requires_burst_sequence_evidence() -> None:
    features = BeatmapFeatures(
        scores={"tag_finger": 1.0},
        sources={"tag_finger": "tags"},
        confidence="low",
    )
    skill = evaluate_skills(features)["double_tap_control"]
    assert skill.score is None
    assert "bursts" in skill.missing


def test_od_precision_requires_od_not_only_cs() -> None:
    features = BeatmapFeatures(
        scores={"cs": 0.9},
        sources={"cs": "beatmap_cs"},
        confidence="low",
    )
    skill = evaluate_skills(features)["od_precision"]
    assert skill.score is None
    assert "od" in skill.missing


def test_ar_challenge_requires_measured_ar() -> None:
    features = BeatmapFeatures(
        scores={"tag_reading": 1.0},
        sources={"tag_reading": "tags"},
        confidence="low",
    )
    skill = evaluate_skills(features)["ar_challenge"]
    assert skill.score is None
    assert "high_ar" in skill.missing and "low_ar" in skill.missing


def test_stream_consistency_requires_streams_field() -> None:
    features = BeatmapFeatures(
        scores={"stream_longest": 0.8, "stream_rhythm_quality": 0.9},
        sources={
            "stream_longest": "osu_sequence",
            "stream_rhythm_quality": "osu_sequence",
        },
        confidence="high",
    )
    skill = evaluate_skills(features)["stream_consistency"]
    assert skill.score is None
    assert "streams" in skill.missing
