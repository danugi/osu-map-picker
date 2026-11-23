from __future__ import annotations

import math
import os

import pytest

import gameplay
import patterns
import presets
from models import BeatmapFeatures, Candidate
from relevance import evaluate_relevance
from skillsets import evaluate_skills


def make_osu(
    notes: list[tuple[int, int, int]],
    *,
    cs: float = 4.0,
    beat_length: int = 500,
) -> str:
    """Build a minimal osu!standard file from (start, x, y) circle rows."""

    objects = "\n".join(
        f"{x},{y},{start},1,0,0:0:0:0:" for start, x, y in notes
    )
    return f"""osu file format v14

[General]
Mode: 0

[Metadata]
Artist:Sequence Artist
Title:Sequence Test
Creator:Mapper
Version:Test

[Difficulty]
ApproachRate:9
CircleSize:{cs}
OverallDifficulty:8

[TimingPoints]
0,{beat_length},4,2,1,60,1,0

[HitObjects]
{objects}
"""


def analyze(notes: list[tuple[int, int, int]], **kwargs):
    preview = gameplay.parse_osu(make_osu(notes, **kwargs), beatmap_id=1)
    return patterns.analyze_sequences(preview)


def test_stream_requires_ten_continuous_compact_quarter_notes() -> None:
    notes = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100 + (index % 2) * 40)
        for index in range(10)
    ]
    analysis = analyze(notes)

    assert len(analysis.stream_chains) == 1
    assert analysis.stream_chains[0].length == 10
    assert analysis.burst_chains == ()
    features = patterns.sequence_features(analysis)
    assert features["streams"] == pytest.approx(1.0)
    assert features["bursts"] == pytest.approx(0.0)
    assert features["stream_longest"] > 0.0


def test_bursts_are_five_to_nine_notes_and_never_streams() -> None:
    first = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(6)
    ]
    second = [
        (3000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(7)
    ]
    analysis = analyze(first + second)

    assert analysis.stream_chains == ()
    assert [chain.length for chain in analysis.burst_chains] == [6, 7]
    features = patterns.sequence_features(analysis)
    assert features["bursts"] == pytest.approx(1.0)
    assert features["streams"] == pytest.approx(0.0)
    assert features["burst_count"] > 0.0


def test_a_stream_and_a_burst_are_measured_separately() -> None:
    stream = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(12)
    ]
    burst = [
        (5000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(6)
    ]
    analysis = analyze(stream + burst)

    assert [chain.length for chain in analysis.stream_chains] == [12]
    assert [chain.length for chain in analysis.burst_chains] == [6]
    features = patterns.sequence_features(analysis)
    assert features["streams"] == pytest.approx(12 / 18)
    assert features["bursts"] == pytest.approx(6 / 18)
    assert features["streams"] > 0.0 and features["bursts"] > 0.0


def test_wide_jumps_do_not_become_a_stream() -> None:
    notes = [
        (1000 + index * 125, 0 if index % 2 else 500, 0 if index % 2 else 380)
        for index in range(12)
    ]
    analysis = analyze(notes)

    assert analysis.stream_chains == ()
    assert analysis.burst_chains == ()


def test_half_beat_notes_do_not_become_a_stream_or_burst() -> None:
    notes = [
        (1000 + index * 250, 100 + (index % 2) * 60, 100)
        for index in range(12)
    ]
    analysis = analyze(notes)

    assert analysis.stream_chains == ()
    assert analysis.burst_chains == ()


def test_slow_quarter_notes_are_not_fast_enough_to_be_streams_or_bursts() -> None:
    notes = [
        (1000 + index * 250, 100 + (index % 2) * 60, 100)
        for index in range(12)
    ]
    analysis = analyze(notes, beat_length=1000)

    assert analysis.stream_chains == ()
    assert analysis.burst_chains == ()


def test_inconsistent_intervals_break_chains_instead_of_inventing_a_stream() -> None:
    notes = [
        (1000 + index * (125 if index % 2 else 200), 100, 100)
        for index in range(12)
    ]
    analysis = analyze(notes)

    assert analysis.stream_chains == ()
    assert analysis.burst_chains == ()


def test_sequence_features_report_truncated_analysis() -> None:
    notes = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(gameplay.MAX_PREVIEW_OBJECTS + 5)
    ]
    analysis = analyze(notes)

    assert analysis.truncated is True
    assert analysis.total_notes == gameplay.MAX_PREVIEW_OBJECTS


def test_geometry_labels_multiple_patterns() -> None:
    # A compact fast chain followed by wide zig-zag jumps: more than one
    # measured category must be allowed at once.
    notes = [
        (
            1000 + index * 125,
            80 + index * 60,
            256 + int(110 * math.sin(index * 0.25)),
        )
        for index in range(24)
    ]
    notes += [
        (5000 + index * 300, 100 if index % 2 else 400, 100)
        for index in range(24)
    ]
    analysis = analyze(notes)
    assert "streams" in analysis.labels
    assert analysis.wide_jump_share > 0.2
    assert "wide_aim" in analysis.labels
    assert "flow_aim" in analysis.labels
    assert set(analysis.labels) <= set(patterns.DETECTED_LABELS)
    assert analysis.sections


def test_geometry_detects_flow_and_wide_jumps_separately() -> None:
    flow = analyze(
        [
            (
                1000 + index * 250,
                80 + index * 65,
                256 + int(110 * math.sin(index * 0.30)),
            )
            for index in range(20)
        ]
    )
    wide = analyze(
        [(1000 + index * 250, 100 if index % 2 else 400, 100) for index in range(10)]
    )
    assert flow.flow_share > 0.5
    assert "flow_aim" in flow.labels
    assert wide.wide_jump_share > 0.5
    assert "wide_aim" in wide.labels


def test_mixed_sections_are_reported() -> None:
    # First 24 notes are compact and straight, next 24 are wide zig-zag jumps.
    notes = [
        (
            1000 + index * 125,
            80 + index * 60,
            256 + int(110 * math.sin(index * 0.25)),
        )
        for index in range(24)
    ]
    notes += [
        (5000 + index * 300, 100 if index % 2 else 400, 100)
        for index in range(24)
    ]
    analysis = analyze(notes)
    assert "flow_aim" in analysis.labels
    assert "wide_aim" in analysis.labels
    assert len(analysis.sections) >= 2
    assert any("wide_aim" in section.labels for section in analysis.sections)


def test_analyzed_slow_map_is_not_a_stream() -> None:
    analysis = analyze([(1000 + index * 600, 100, 100) for index in range(10)])
    assert analysis.stream_chains == ()
    assert analysis.burst_chains == ()
    features = patterns.sequence_features(analysis)
    assert features["streams"] == 0.0


def test_cached_analysis_is_reused_until_the_file_changes(
    tmp_path, monkeypatch
) -> None:
    patterns._cached_analysis.cache_clear()
    path = tmp_path / "42.osu"
    path.write_text(
        make_osu([(1000 + index * 125, 100, 100) for index in range(10)]),
        encoding="utf-8",
    )
    calls = 0
    real_loader = gameplay.load_cached_preview

    def counting_loader(beatmap_id, cache_dir):
        nonlocal calls
        calls += 1
        return real_loader(beatmap_id, cache_dir)

    monkeypatch.setattr(gameplay, "load_cached_preview", counting_loader)
    first = patterns.load_analysis(42, tmp_path, allow_download=False)
    second = patterns.load_analysis(42, tmp_path, allow_download=False)
    assert first == second
    assert calls == 1

    # Replacing the file must invalidate the process cache.
    path.write_text(
        make_osu([(2000 + index * 125, 100, 100) for index in range(10)]),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = patterns.load_analysis(42, tmp_path, allow_download=False)
    assert calls == 2
    assert third.analyzed_notes == first.analyzed_notes
    assert third.stream_chains[0].start_ms == 2000


def test_tiny_cursor_movements_do_not_create_flow_evidence() -> None:
    notes = [(1000 + index * 200, 100 + index * 4, 100) for index in range(20)]
    analysis = analyze(notes)
    assert analysis.flow_share == 0.0
    assert analysis.flow_longest == 0
    assert "flow_aim" not in analysis.labels


def test_reversing_zigzag_is_valid_continuous_flow() -> None:
    notes = [
        (1000 + index * 250, 100 if index % 2 else 400, 100)
        for index in range(20)
    ]
    analysis = analyze(notes, beat_length=500)
    assert analysis.flow_share > 0.80
    assert analysis.flow_longest >= 14
    assert "flow_aim" in analysis.labels
    assert _flow_gate(analysis).passed


def test_gradual_curve_is_flow_not_only_a_straight_line() -> None:
    import math

    notes = [
        (
            1000 + index * 240,
            80 + index * 65,
            256 + int(110 * math.sin(index * 0.30)),
        )
        for index in range(24)
    ]
    analysis = analyze(notes)
    assert analysis.flow_share > 0.50
    assert analysis.flow_longest >= 8
    assert "flow_aim" in analysis.labels


def _flow_gate(analysis):
    keys = (
        "flow_share",
        "flow_coverage",
        "flow_longest",
        "flow_longest_share",
        "flow_run_count",
        "flow_quality",
        "flow_spatial_demand",
    )
    raw = patterns.sequence_features(analysis)
    features = BeatmapFeatures(
        scores={key: raw[key] for key in keys},
        sources={key: "osu_sequence" for key in keys},
        statuses={key: "measured" for key in keys},
    )
    skills = evaluate_skills(features)
    candidate = Candidate(
        beatmapset=None,
        beatmap=None,
        traits=features,
        skillsets=skills,
    )
    return evaluate_relevance(
        candidate, presets.builtin_by_name("Flow Aim").ranking
    )


def test_separated_high_spaced_stream_sections_are_not_flow() -> None:
    # Four regular high-spaced tap chains. Straight, even spacing and even
    # rhythm are stream evidence, not a connected flow trajectory.
    notes: list[tuple[int, int, int]] = []
    for section in range(4):
        base = 1000 + section * 3000
        notes.extend(
            (base + index * 160, 60 + index * 120, 120)
            for index in range(10)
        )
        notes.extend(
            (base + 1800 + index * 250, 460, 300 if index % 2 else 120)
            for index in range(3)
        )
    analysis = analyze(notes)
    assert analysis.flow_longest < 14
    assert "streams" in analysis.labels
    assert not _flow_gate(analysis).passed


def test_long_straight_note_flow_qualifies_without_a_shape_requirement() -> None:
    notes = [
        (1000 + index * 90, 80 + index * 240, 100)
        for index in range(40)
    ]
    analysis = analyze(notes, beat_length=300)
    assert analysis.flow_longest >= 30
    assert _flow_gate(analysis).passed


def test_multiple_short_flow_coincidences_do_not_pass() -> None:
    notes: list[tuple[int, int, int]] = []
    for section in range(5):
        base = 1000 + section * 2200
        notes.extend(
            (
                base + index * 200,
                100 + index * 45,
                256 + int(30 * math.sin(index * 0.8)),
            )
            for index in range(5)
        )
        notes.append((base + 1200, 470, 470))
    analysis = analyze(notes)
    assert analysis.flow_longest == 0
    assert analysis.flow_run_count == 0
    assert not _flow_gate(analysis).passed


def test_long_angular_zigzag_is_valid_flow() -> None:
    notes = [
        (1000 + index * 180, 100 if index % 2 else 480, 100)
        for index in range(30)
    ]
    analysis = analyze(notes)
    assert analysis.flow_longest >= 20
    assert _flow_gate(analysis).passed


def test_flow_with_an_ordinary_stream_still_qualifies() -> None:
    notes = [
        (
            1000 + index * 240,
            80 + index * 65,
            256 + int(110 * math.sin(index * 0.30)),
        )
        for index in range(24)
    ]
    notes.extend(
        (8000 + index * 160, 60 + index * 120, 420)
        for index in range(20)
    )
    analysis = analyze(notes)
    assert analysis.flow_longest >= 15
    assert "streams" in analysis.labels
    assert _flow_gate(analysis).passed


def test_multiple_genuine_flow_passages_qualify() -> None:
    notes = []
    for section in range(2):
        base = 1000 + section * 5000
        notes.extend(
            (
                base + index * 240,
                80 + index * 65,
                256 + int(110 * math.sin(index * 0.30)),
            )
            for index in range(20)
        )
        notes.extend(
            (base + 4200 + index * 250, 470, 120 if index % 2 else 400)
            for index in range(4)
        )
    analysis = analyze(notes)
    assert analysis.flow_longest >= 15
    assert analysis.flow_run_count >= 2
    assert _flow_gate(analysis).passed


def test_triangular_flow_trajectory_qualifies() -> None:
    notes = [
        (1000 + index * 220, 100 if index % 2 else 450, 100)
        for index in range(20)
    ]
    analysis = analyze(notes)
    assert analysis.flow_longest >= 18
    assert _flow_gate(analysis).passed


def test_half_heart_like_curved_flow_qualifies() -> None:
    import math

    notes = [
        (
            1000 + index * 220,
            256 + int(400 * math.cos(math.pi * index / 23)),
            300 - int(300 * math.sin(math.pi * index / 23)),
        )
        for index in range(24)
    ]
    analysis = analyze(notes)
    assert analysis.flow_longest >= 20
    assert _flow_gate(analysis).passed


def test_mixed_shapes_with_sharp_turns_form_one_flow_passage() -> None:
    notes = [
        (1000 + index * 200, 100 if index % 2 else 450, 100)
        for index in range(14)
    ]
    notes += [
        (
            4000 + index * 200,
            100 + index * 70,
            450 - index * 40,
        )
        for index in range(14)
    ]
    analysis = analyze(notes)
    assert analysis.flow_longest >= 20
    assert _flow_gate(analysis).passed


def test_sparse_shaped_path_does_not_qualify() -> None:
    notes = [
        (1000 + index * 900, 100 if index % 2 else 450, 100)
        for index in range(10)
    ]
    analysis = analyze(notes, beat_length=500)
    assert analysis.flow_share == 0.0
    assert not _flow_gate(analysis).passed


def test_conventional_aim_with_isolated_jumps_does_not_qualify() -> None:
    notes = [
        (1000 + index * 500, 80 + (index % 2) * 380, 80 + (index % 3) * 150)
        for index in range(16)
    ]
    analysis = analyze(notes, beat_length=400)
    assert analysis.flow_share == 0.0
    assert not _flow_gate(analysis).passed


def test_flow_with_irregular_tapping_stays_eligible_without_finger_control() -> None:
    notes = []
    for index in range(24):
        gap = 150 if index % 3 else 260
        notes.append(
            (
                1000 + sum(
                    (150 if part % 3 else 260) for part in range(index)
                ),
                80 + index * 65,
                256 + int(110 * math.sin(index * 0.30)),
            )
        )
    analysis = analyze(notes)
    assert analysis.flow_longest >= 20
    assert _flow_gate(analysis).passed
