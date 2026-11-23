from __future__ import annotations

from pathlib import Path

import gameplay
import patterns
import presets
from models import BeatmapFeatures, Candidate
from relevance import evaluate_relevance
from skillsets import evaluate_skills

FIXTURES = Path(__file__).parent / "fixtures" / "flow"


def _analyze(beatmap_id: int):
    preview = gameplay.parse_osu(
        (FIXTURES / f"{beatmap_id}.osu").read_bytes(),
        beatmap_id=beatmap_id,
    )
    return preview, patterns.analyze_sequences(preview)


def _features(analysis):
    keys = (
        "flow_share",
        "flow_intensity",
        "flow_longest",
        "flow_run_count",
        "flow_quality",
    )
    raw = patterns.sequence_features(analysis)
    scores = {key: raw[key] for key in keys}
    return BeatmapFeatures(
        scores=scores,
        sources={key: "osu_sequence" for key in keys},
        statuses={key: "measured" for key in keys},
    )


def test_real_rubik_cube_flow_evidence_and_gate() -> None:
    preview, analysis = _analyze(2744037)
    assert preview.version == "6x8x8 Another"
    assert analysis.flow_share >= 0.20
    assert analysis.flow_longest >= 4
    features = _features(analysis)
    skill = evaluate_skills(features)["aim_flow"]
    assert skill.score is not None and skill.score > 0.55
    candidate = Candidate(beatmapset=None, beatmap=None, traits=features)
    preset = presets.builtin_by_name("Flow Aim")
    result = evaluate_relevance(candidate, preset.ranking)
    assert result.passed, result.reason


def test_real_sendan_life_flow_evidence_and_gate() -> None:
    preview, analysis = _analyze(928868)
    assert preview.version == "Pho's Insane"
    assert analysis.flow_share >= 0.20
    features = _features(analysis)
    skill = evaluate_skills(features)["aim_flow"]
    assert skill.score is not None and skill.score > 0.55
    candidate = Candidate(beatmapset=None, beatmap=None, traits=features)
    preset = presets.builtin_by_name("Flow Aim")
    result = evaluate_relevance(candidate, preset.ranking)
    assert result.passed, result.reason
