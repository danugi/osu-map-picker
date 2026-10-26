from __future__ import annotations

import pytest

import gameplay
import patterns


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
