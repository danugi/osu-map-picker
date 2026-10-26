"""Sequence-level stream and burst evidence from a parsed ``.osu`` file.

This module is deliberately conservative. A stream is a continuous, compact,
rhythmically even tap chain of at least 10 notes. A burst is the same kind of
chain with 5-9 notes. A maximal chain belongs to exactly one category, so a
stream can never inflate the burst score and vice versa.

The evidence is the hit-object timeline plus the uninherited beat length at
each object. Mapper tags, BPM, star rating, density and popularity are never
used here.
"""

from __future__ import annotations

import math
from pathlib import Path
from statistics import median, pstdev
from typing import Callable

import gameplay
from models import PatternChain, SequenceAnalysis

STREAM_MIN_NOTES = 10
BURST_MIN_NOTES = 5
BURST_MAX_NOTES = 9

# A fast chain is at most 0.40 beat between consecutive notes. That includes
# 1/4, 1/3, 1/6 and 1/8, but excludes 1/2 jumps even at high BPM.
MAX_GAP_BEATS = 0.40
MIN_GAP_MS = 30.0
# Even a 1/4 chain is not a tap stream when it is this slow. 180 ms keeps
# 90 BPM 1/4 (167 ms) eligible and rejects 60 BPM 1/4 (250 ms).
MAX_GAP_MS = 180.0
# Consecutive intervals must be within 20% of each other. A rhythm change is a
# chain boundary, not extra evidence for a stream.
MAX_GAP_RATIO = 1.20
# Compact means no more than 2.6 hit-circle diameters between starts.
MAX_SPACING_DIAMETERS = 2.6
MIN_COMPACT_SHARE = 0.85
# A rhythm-quality coefficient of variation of 0.25 or more scores zero.
MAX_RHYTHM_CV = 0.25

SEQUENCE_FEATURE_KEYS = frozenset(
    {
        "streams",
        "bursts",
        "stream_longest",
        "stream_chain_count",
        "stream_rhythm_quality",
        "burst_count",
        "burst_median",
        "burst_tempo",
        "burst_rhythm_quality",
    }
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def _circle_diameter(cs: float) -> float:
    radius = max(8.0, min(64.0, 54.4 - 4.48 * float(cs)))
    return 2.0 * radius


def _spacing(previous: gameplay.PreviewObject, current: gameplay.PreviewObject, cs: float) -> float:
    distance = math.hypot(current.x - previous.x, current.y - previous.y)
    return distance / _circle_diameter(cs)


def _rhythm_quality(gaps: list[float]) -> float:
    if not gaps:
        return 0.0
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap <= 0:
        return 0.0
    coefficient = pstdev(gaps) / mean_gap if len(gaps) > 1 else 0.0
    return _clamp(1.0 - coefficient / MAX_RHYTHM_CV)


def _chain(
    kind: str,
    notes: list[gameplay.PreviewObject],
    start: int,
    end: int,
    *,
    cs: float,
) -> PatternChain | None:
    length = end - start
    if length < BURST_MIN_NOTES:
        return None
    gaps = [
        float(notes[index + 1].start - notes[index].start)
        for index in range(start, end - 1)
    ]
    spacings = [
        _spacing(notes[index], notes[index + 1], cs)
        for index in range(start, end - 1)
    ]
    compact_share = sum(
        1 for value in spacings if value <= MAX_SPACING_DIAMETERS
    ) / len(spacings)
    if compact_share + 1e-9 < MIN_COMPACT_SHARE:
        return None
    return PatternChain(
        kind=kind,
        start_index=start,
        length=length,
        start_ms=notes[start].start,
        end_ms=notes[end - 1].start,
        median_gap_ms=float(median(gaps)),
        rhythm_quality=_rhythm_quality(gaps),
        compact_share=compact_share,
    )


def analyze_sequences(preview: gameplay.BeatmapPreview) -> SequenceAnalysis:
    """Find separate stream and burst chains in one parsed difficulty."""

    # Spinners are not tap objects: skipping them also creates a natural break
    # around the spinner's time span.
    notes = sorted(
        (item for item in preview.objects if item.kind != "spinner"),
        key=lambda item: (item.start, item.end, item.x, item.y),
    )
    total_notes = len(notes)
    if total_notes < BURST_MIN_NOTES:
        return SequenceAnalysis(
            analyzed_notes=total_notes,
            total_notes=total_notes,
            truncated=preview.truncated,
        )

    runs: list[tuple[int, int]] = []
    run_start = 0
    previous_gap: float | None = None
    for index in range(1, total_notes):
        previous = notes[index - 1]
        current = notes[index]
        gap = float(current.start - previous.start)
        beat_length = max(
            1.0, (previous.beat_length + current.beat_length) / 2.0
        )
        gap_beats = gap / beat_length
        fast = (
            gap >= MIN_GAP_MS
            and gap <= MAX_GAP_MS
            and gap_beats <= MAX_GAP_BEATS
        )
        consistent = (
            previous_gap is None
            or max(gap, previous_gap) / max(1e-9, min(gap, previous_gap))
            <= MAX_GAP_RATIO
        )
        if fast and consistent:
            previous_gap = gap
            continue
        runs.append((run_start, index))
        run_start = index
        previous_gap = None
    runs.append((run_start, total_notes))

    stream_chains: list[PatternChain] = []
    burst_chains: list[PatternChain] = []
    for start, end in runs:
        length = end - start
        if length < BURST_MIN_NOTES:
            continue
        if length >= STREAM_MIN_NOTES:
            kind = "stream"
            bucket = stream_chains
        elif length <= BURST_MAX_NOTES:
            kind = "burst"
            bucket = burst_chains
        else:
            # The categories overlap only at 9/10, so this is defensive.
            continue
        chain = _chain(
            kind, notes, start, end, cs=preview.circle_size
        )
        if chain is not None:
            bucket.append(chain)

    return SequenceAnalysis(
        stream_chains=tuple(stream_chains),
        burst_chains=tuple(burst_chains),
        analyzed_notes=total_notes,
        total_notes=total_notes,
        truncated=preview.truncated,
    )


def _weighted_quality(chains: tuple[PatternChain, ...]) -> float:
    if not chains:
        return 0.0
    total = sum(chain.length for chain in chains)
    return sum(chain.rhythm_quality * chain.length for chain in chains) / max(
        1, total
    )


def sequence_features(analysis: SequenceAnalysis) -> dict[str, float]:
    """Normalized stream/burst features. Missing patterns are real zeros."""

    total = max(1, analysis.total_notes)
    stream_lengths = [chain.length for chain in analysis.stream_chains]
    burst_lengths = [chain.length for chain in analysis.burst_chains]
    burst_gaps = [chain.median_gap_ms for chain in analysis.burst_chains]
    return {
        "streams": _clamp(analysis.stream_notes / total),
        "bursts": _clamp(analysis.burst_notes / total),
        "stream_longest": _clamp(
            (max(stream_lengths) if stream_lengths else 0) / 24.0
        ),
        "stream_chain_count": _clamp(len(stream_lengths) / 6.0),
        "stream_rhythm_quality": _weighted_quality(analysis.stream_chains),
        "burst_count": _clamp(len(burst_lengths) / 8.0),
        "burst_median": _clamp(
            (median(burst_lengths) if burst_lengths else 0.0) / 9.0
        ),
        "burst_tempo": _clamp(
            (240.0 - (median(burst_gaps) if burst_gaps else 240.0)) / 180.0
        ),
        "burst_rhythm_quality": _weighted_quality(analysis.burst_chains),
    }


def load_analysis(
    beatmap_id: int,
    cache_dir: Path,
    *,
    fetcher: Callable[[str], bytes] | None = None,
    allow_download: bool = True,
) -> SequenceAnalysis:
    """Load and analyze one difficulty, optionally from cache only."""

    if allow_download:
        preview = gameplay.load_preview(
            beatmap_id, cache_dir, fetcher=fetcher
        )
    else:
        preview = gameplay.load_cached_preview(beatmap_id, cache_dir)
    return analyze_sequences(preview)
