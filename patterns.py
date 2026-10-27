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
from functools import lru_cache
from pathlib import Path
from statistics import median, pstdev
from typing import Callable

import gameplay
from models import PatternChain, PatternSection, SequenceAnalysis

ANALYZER_VERSION = 2

STREAM_MIN_NOTES = 10
BURST_MIN_NOTES = 5
BURST_MAX_NOTES = 9
# Geometry thresholds are deliberately conservative and expressed in circle
# diameters so CS changes the physical spacing rather than the label.
JUMP_DIAMETERS = 1.2
WIDE_JUMP_DIAMETERS = 2.0
FLOW_ANGLE_DEGREES = 35.0
ANGLE_CHANGE_DEGREES = 90.0
SLIDER_HEAVY_SHARE = 0.35
HIGH_DENSITY_NOTES_PER_SECOND = 5.0
SECTION_OBJECTS = 24
MAX_SECTIONS = 32

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
        "jump_share",
        "wide_jump_share",
        "flow_share",
        "angle_change_share",
        "slider_share",
        "notes_per_second",
        "pattern_variety",
        "rhythm_variability",
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


def _angle_degrees(
    previous: gameplay.PreviewObject,
    current: gameplay.PreviewObject,
    following: gameplay.PreviewObject,
) -> float | None:
    ax, ay = current.x - previous.x, current.y - previous.y
    bx, by = following.x - current.x, following.y - current.y
    norm_a = math.hypot(ax, ay)
    norm_b = math.hypot(bx, by)
    if norm_a <= 1e-6 or norm_b <= 1e-6:
        return None
    cosine = (ax * bx + ay * by) / (norm_a * norm_b)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _geometry(
    notes: list[gameplay.PreviewObject], cs: float
) -> tuple[dict[str, float], tuple[str, ...], tuple[PatternSection, ...]]:
    """Measure spatial/timing properties on the analyzed objects.

    These are descriptive measurements, not proof that a map belongs to a
    subjective genre. Thresholds are documented next to the constants above.
    """

    if len(notes) < 2:
        return {}, (), ()
    diameter = _circle_diameter(cs)
    distances: list[float] = []
    angles: list[float] = []
    gaps: list[float] = []
    for index in range(1, len(notes)):
        previous, current = notes[index - 1], notes[index]
        distance = (
            math.hypot(current.x - previous.x, current.y - previous.y)
            / diameter
        )
        distances.append(distance)
        gap = float(current.start - previous.start)
        if gap > 0:
            gaps.append(gap)
        if index + 1 < len(notes):
            angle = _angle_degrees(previous, current, notes[index + 1])
            if angle is not None:
                angles.append(angle)
    jump_share = (
        sum(1 for value in distances if value >= JUMP_DIAMETERS)
        / len(distances)
        if distances
        else 0.0
    )
    wide_share = (
        sum(1 for value in distances if value >= WIDE_JUMP_DIAMETERS)
        / len(distances)
        if distances
        else 0.0
    )
    flow_share = (
        sum(1 for value in angles if value <= FLOW_ANGLE_DEGREES)
        / len(angles)
        if angles
        else 0.0
    )
    angle_share = (
        sum(1 for value in angles if value >= ANGLE_CHANGE_DEGREES)
        / len(angles)
        if angles
        else 0.0
    )
    slider_share = (
        sum(1 for note in notes if note.kind == "slider") / len(notes)
    )
    span = max(1.0, float(notes[-1].start - notes[0].start))
    notes_per_second = len(notes) / (span / 1000.0)
    rhythm_variability = 0.0
    if len(gaps) > 1:
        mean_gap = sum(gaps) / len(gaps)
        rhythm_variability = (
            pstdev(gaps) / mean_gap if mean_gap > 0 else 0.0
        )
    metrics = {
        "jump_share": _clamp(jump_share),
        "wide_jump_share": _clamp(wide_share),
        "flow_share": _clamp(flow_share),
        "angle_change_share": _clamp(angle_share),
        "slider_share": _clamp(slider_share),
        "notes_per_second": max(0.0, notes_per_second),
        "rhythm_variability": _clamp(rhythm_variability),
    }
    return metrics, (), ()


def _section_labels(
    notes: list[gameplay.PreviewObject], cs: float
) -> tuple[PatternSection, ...]:
    """Bound, evenly spaced section summaries for mixed-pattern detection."""

    if len(notes) <= SECTION_OBJECTS:
        windows = [(0, len(notes))]
    else:
        total = len(notes)
        count = min(
            MAX_SECTIONS, max(1, math.ceil(total / SECTION_OBJECTS))
        )
        step = max(1, total // count)
        starts = list(range(0, max(1, total - SECTION_OBJECTS + 1), step))
        windows = [
            (start, min(total, start + SECTION_OBJECTS)) for start in starts
        ][:MAX_SECTIONS]
    sections: list[PatternSection] = []
    for start, end in windows:
        chunk = notes[start:end]
        metrics, _, _ = _geometry(chunk, cs)
        labels = _labels_from_metrics(metrics)
        sections.append(
            PatternSection(
                start_ms=chunk[0].start,
                end_ms=chunk[-1].start,
                object_count=len(chunk),
                labels=labels,
                jump_share=metrics.get("jump_share", 0.0),
                wide_jump_share=metrics.get("wide_jump_share", 0.0),
                flow_share=metrics.get("flow_share", 0.0),
                angle_change_share=metrics.get("angle_change_share", 0.0),
            )
        )
    return tuple(sections)


def _labels_from_metrics(metrics: dict[str, float]) -> tuple[str, ...]:
    labels: list[str] = []
    # Thresholds are intentionally stricter than "the value is non-zero".
    # They are descriptive heuristics, not genre proof.
    if metrics.get("jump_share", 0.0) >= 0.65:
        labels.append("jump_aim")
    if metrics.get("wide_jump_share", 0.0) >= 0.50:
        labels.append("wide_aim")
    if metrics.get("flow_share", 0.0) >= 0.30:
        labels.append("flow_aim")
    if metrics.get("angle_change_share", 0.0) >= 0.60:
        labels.append("angle_control")
    if metrics.get("slider_share", 0.0) >= SLIDER_HEAVY_SHARE:
        labels.append("slider_heavy")
    if metrics.get("notes_per_second", 0.0) >= HIGH_DENSITY_NOTES_PER_SECOND:
        labels.append("high_density")
    return tuple(labels)


def _primary_pattern(labels: tuple[str, ...]) -> str:
    for candidate in (
        "flow_aim",
        "wide_aim",
        "angle_control",
        "slider_heavy",
        "high_density",
        "jump_aim",
    ):
        if candidate in labels:
            return candidate
    return ""


def _combined_labels(
    metrics: dict[str, float],
    stream_chains: list[PatternChain],
    burst_chains: list[PatternChain],
    sections: tuple[PatternSection, ...],
) -> tuple[str, ...]:
    labels = list(_labels_from_metrics(metrics))
    if stream_chains:
        labels.append("streams")
    if burst_chains:
        labels.append("bursts")
    primary_sections = {
        _primary_pattern(section.labels)
        for section in sections
        if section.labels
    }
    primary_sections.discard("")
    if len(primary_sections) >= 2:
        labels.append("mixed_patterns")
    return tuple(dict.fromkeys(labels))


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
        metrics, _, sections = _geometry(notes, preview.circle_size)
        return SequenceAnalysis(
            analyzed_notes=total_notes,
            total_notes=total_notes,
            truncated=preview.truncated,
            jump_share=metrics.get("jump_share", 0.0),
            wide_jump_share=metrics.get("wide_jump_share", 0.0),
            flow_share=metrics.get("flow_share", 0.0),
            angle_change_share=metrics.get("angle_change_share", 0.0),
            slider_share=metrics.get("slider_share", 0.0),
            notes_per_second=metrics.get("notes_per_second", 0.0),
            rhythm_variability=metrics.get("rhythm_variability", 0.0),
            labels=_combined_labels(metrics, [], [], sections),
            sections=sections,
            analyzer_version=ANALYZER_VERSION,
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

    metrics, _, _ = _geometry(notes, preview.circle_size)
    sections = _section_labels(notes, preview.circle_size)
    return SequenceAnalysis(
        stream_chains=tuple(stream_chains),
        burst_chains=tuple(burst_chains),
        analyzed_notes=total_notes,
        total_notes=total_notes,
        truncated=preview.truncated,
        jump_share=metrics.get("jump_share", 0.0),
        wide_jump_share=metrics.get("wide_jump_share", 0.0),
        flow_share=metrics.get("flow_share", 0.0),
        angle_change_share=metrics.get("angle_change_share", 0.0),
        slider_share=metrics.get("slider_share", 0.0),
        notes_per_second=metrics.get("notes_per_second", 0.0),
        rhythm_variability=metrics.get("rhythm_variability", 0.0),
        labels=_combined_labels(
            metrics, stream_chains, burst_chains, sections
        ),
        sections=sections,
        analyzer_version=ANALYZER_VERSION,
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
        "jump_share": _clamp(analysis.jump_share),
        "wide_jump_share": _clamp(analysis.wide_jump_share),
        "flow_share": _clamp(analysis.flow_share),
        "angle_change_share": _clamp(analysis.angle_change_share),
        "slider_share": _clamp(analysis.slider_share),
        "notes_per_second": _clamp(
            analysis.notes_per_second / HIGH_DENSITY_NOTES_PER_SECOND
        ),
        "pattern_variety": _clamp(len(analysis.labels) / 6.0),
        "rhythm_variability": _clamp(analysis.rhythm_variability),
    }


@lru_cache(maxsize=256)
def _cached_analysis(
    cache_dir: str,
    beatmap_id: int,
    mtime_ns: int,
    size: int,
    analyzer_version: int,
) -> SequenceAnalysis:
    """Parse and analyze one unchanged cached ``.osu`` file.

    The mtime/size pair is part of the key, so a replaced file is reparsed.
    The bound keeps memory finite during long sessions.
    """

    preview = gameplay.load_cached_preview(beatmap_id, Path(cache_dir))
    return analyze_sequences(preview)


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
        return analyze_sequences(preview)
    path = Path(cache_dir) / f"{int(beatmap_id)}.osu"
    try:
        stat = path.stat()
    except OSError:
        # Preserve the existing GameplayError wording for a missing file.
        preview = gameplay.load_cached_preview(beatmap_id, cache_dir)
        return analyze_sequences(preview)
    return _cached_analysis(
        str(Path(cache_dir).resolve()),
        int(beatmap_id),
        stat.st_mtime_ns,
        stat.st_size,
        ANALYZER_VERSION,
    )
