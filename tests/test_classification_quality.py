"""Classification-quality tests.

The honesty-invariant suite proves scores are never invented. These tests
prove the opposite direction: when a map genuinely matches a category, the
relevance gate must actually open it. They would have caught two real bugs:
the structurally unsatisfiable Reading gate (ar_challenge maxed at ~0.57
without a mapper tag) and the marathon truncation deadlock (>6000 objects
could never pass stream gates). Every built-in preset must be reachable
from measured evidence alone.
"""

from __future__ import annotations

import pytest
from conftest import make_beatmap, make_beatmapset

import pattern_filters
import presets
import skillsets
from evidence import MEASURED
from features import derive_features
from models import (
    Candidate,
    DifficultyAttributes,
    PatternChain,
    SequenceAnalysis,
)
from relevance import evaluate_relevance


def _chain(kind: str, length: int, rhythm_quality: float = 0.95) -> PatternChain:
    return PatternChain(
        kind=kind,
        start_index=0,
        length=length,
        start_ms=0,
        end_ms=length * 120,
        median_gap_ms=120.0,
        rhythm_quality=rhythm_quality,
        compact_share=1.0,
    )


def _attributes(
    *,
    aim: float = 6.0,
    speed: float = 2.0,
    slider_factor: float = 2.1,
) -> DifficultyAttributes:
    return DifficultyAttributes(
        beatmap_id=0,
        star_rating=5.4,
        aim_difficulty=aim,
        speed_difficulty=speed,
        slider_factor=slider_factor,
        max_combo=1500,
    )


def _sequence(
    *,
    stream_chain_length: int = 0,
    stream_chain_count: int = 0,
    burst_chain_length: int = 0,
    burst_chain_count: int = 0,
    wide_jump_share: float = 0.0,
    flow_share: float = 0.0,
    angle_change_share: float = 0.0,
    notes_per_second: float = 0.0,
    rhythm_variability: float = 0.0,
) -> SequenceAnalysis:
    streams = tuple(
        _chain("stream", stream_chain_length)
        for _ in range(stream_chain_count)
    )
    bursts = tuple(
        _chain("burst", burst_chain_length) for _ in range(burst_chain_count)
    )
    notes = (
        sum(chain.length for chain in streams)
        + sum(chain.length for chain in bursts)
        or 1
    )
    return SequenceAnalysis(
        stream_chains=streams,
        burst_chains=bursts,
        analyzed_notes=notes,
        total_notes=notes,
        wide_jump_share=wide_jump_share,
        flow_share=flow_share,
        angle_change_share=angle_change_share,
        notes_per_second=notes_per_second,
        rhythm_variability=rhythm_variability,
    )


def _candidate(
    set_id: int,
    *,
    stars: float = 5.4,
    ar: float | None = 9.5,
    od: float | None = 9.0,
    cs: float | None = 4.0,
    hp: float | None = 5.0,
    bpm: float = 170.0,
    count_circles: int = 1100,
    count_sliders: int = 380,
    total_length: int = 180,
    tags: tuple[str, ...] = (),
    sequence: SequenceAnalysis | None = None,
    aim: float = 6.0,
    speed: float = 2.0,
    slider_factor: float = 2.1,
) -> Candidate:
    beatmapset = make_beatmapset(set_id, tags=tags)
    beatmap = make_beatmap(
        set_id * 10 + 1,
        set_id,
        stars=stars,
        ar=ar,
        od=od,
        cs=cs,
        hp=hp,
        bpm=bpm,
        count_circles=count_circles,
        count_sliders=count_sliders,
        total_length=total_length,
        hit_length=total_length - 10,
    )
    traits = derive_features(
        beatmap,
        beatmapset,
        _attributes(aim=aim, speed=speed, slider_factor=slider_factor),
        sequence=sequence,
        analysis_status=MEASURED,
        analyzer_version=1,
    )
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        sources=("ranked:recent",),
        traits=traits,
        skillsets=skillsets.evaluate_skills(traits),
        within_tolerance=True,
        unknown_fields=(),
    )


# One honest fixture per built-in skill preset: a map that genuinely belongs
# to the category, built only from measured evidence.
_AIM_MAP: dict = {"aim": 6.0, "speed": 2.0}
# osu! streams tag group: continuous 10+ note streams and 5-9 note bursts
# share one curated preset, so its fixture carries both evidence kinds.
_STREAMS_MAP: dict = {
    "sequence": _sequence(
        stream_chain_length=24,
        stream_chain_count=20,
        burst_chain_length=7,
        burst_chain_count=40,
    ),
}
_BURSTS_MAP: dict = {
    "sequence": _sequence(burst_chain_length=7, burst_chain_count=40),
}
_STAMINA_MAP: dict = {
    "aim": 2.0,
    "speed": 5.5,
    "count_circles": 2200,
    "count_sliders": 200,
    "total_length": 300,
    "sequence": _sequence(stream_chain_length=24, stream_chain_count=4),
}
_PERFECT_MAPS: dict[str, dict] = {
    "Aim": _AIM_MAP,
    "Aim Farm": _AIM_MAP,
    "Speed": {"aim": 2.0, "speed": 5.5, "bpm": 200.0},
    "Streams": _STREAMS_MAP,
    "Stamina": _STAMINA_MAP,
    "Finger Control": {"aim": 2.0, "speed": 5.5},
    "Reading": {"ar": 10.0, "aim": 4.0, "speed": 3.0},
    "Accuracy": {
        "od": 9.5,
        "ar": 9.0,
        "count_circles": 300,
        "count_sliders": 100,
        "total_length": 240,
    },
    "Tech": {
        "count_sliders": 1200,
        "count_circles": 300,
        "bpm": 180.0,
    },
    "Flashlight": {
        "tags": ("flashlight",),
        "ar": 6.0,
        "count_circles": 300,
        "count_sliders": 100,
        "total_length": 240,
    },
    "All-rounder": {},
    "Discovery": {},
}

# Legacy profiles keep their own targets; a few high-value ones are covered.
_LEGACY_PERFECT_MAPS: dict[str, dict] = {
    "Streams & Stamina": {
        "sequence": _sequence(stream_chain_length=24, stream_chain_count=20),
    },
    "Bursts": _BURSTS_MAP,
    "Wide Aim": {
        "sequence": _sequence(
            wide_jump_share=0.85,
            notes_per_second=4.0,
        ),
    },
    "Flow Aim": {
        "sequence": _sequence(
            flow_share=0.85,
            notes_per_second=4.0,
        ),
    },
    "Aim Control": {
        "sequence": _sequence(
            angle_change_share=0.85,
            notes_per_second=4.0,
        ),
    },
    "Jump Streams": {
        "sequence": _sequence(
            stream_chain_length=24,
            stream_chain_count=12,
            wide_jump_share=0.8,
            notes_per_second=6.0,
        ),
    },
    "Low AR Reading": {"ar": 2.0, "aim": 4.0, "speed": 3.0},
    "High AR Reading": {"ar": 10.0, "aim": 4.0, "speed": 3.0},
}


def _preset_names() -> list[str]:
    return [cfg.name for cfg in presets.builtin_presets()]


@pytest.mark.parametrize(
    "name",
    sorted({**_PERFECT_MAPS, **_LEGACY_PERFECT_MAPS}),
)
def test_every_gate_is_reachable_from_measured_evidence(name: str) -> None:
    preset = presets.known_builtin_by_name(name)
    assert preset is not None, name
    fixture = (
        _PERFECT_MAPS[name]
        if name in _PERFECT_MAPS
        else _LEGACY_PERFECT_MAPS[name]
    )
    candidate = _candidate(1, **fixture)
    check = evaluate_relevance(candidate, preset.ranking)
    assert check.passed is True, (
        f"{name}: gate rejected a genuine {name} map: {check.reason}"
    )


def test_marathon_maps_can_pass_stream_gates() -> None:
    """Regression: a 7000-note marathon was force-truncated at 6000 objects,
    marked PARTIAL, and the gate refused to confirm it forever."""
    sequence = _sequence(stream_chain_length=70, stream_chain_count=100)
    assert sequence.analyzed_notes >= 7000
    candidate = _candidate(
        2,
        count_circles=6300,
        count_sliders=700,
        total_length=480,
        sequence=sequence,
    )
    assert candidate.traits.scores.get("streams") is not None
    streams = presets.known_builtin_by_name("Streams")
    assert streams is not None
    check = evaluate_relevance(candidate, streams.ranking)
    assert check.passed is True, check.reason
    # The retired hybrid keeps its marathon pass too.
    streams_stamina = presets.known_builtin_by_name("Streams & Stamina")
    assert streams_stamina is not None
    assert evaluate_relevance(candidate, streams_stamina.ranking).passed is True


def test_gates_discriminate_wrong_maps() -> None:
    """A map that matches one category must not pass every gate."""
    aim_map = _candidate(3, **_AIM_MAP)
    streams = presets.known_builtin_by_name("Streams")
    assert streams is not None
    check = evaluate_relevance(aim_map, streams.ranking)
    assert check.passed is False

    stream_map = _candidate(
        4,
        aim=2.0,
        speed=4.0,
        sequence=_sequence(stream_chain_length=24, stream_chain_count=20),
    )
    aim_farm = presets.known_builtin_by_name("Aim Farm")
    assert aim_farm is not None
    assert evaluate_relevance(stream_map, aim_farm.ranking).passed is False

    tech_map = _candidate(
        5,
        count_sliders=1200,
        count_circles=300,
        bpm=180.0,
    )
    accuracy = presets.known_builtin_by_name("Accuracy")
    assert accuracy is not None
    assert evaluate_relevance(tech_map, accuracy.ranking).passed is False


def test_pattern_maps_reference_live_skills_only() -> None:
    """SKILL_PATTERN_MAP must never drift from the live skillset taxonomy
    (the tech -> angle_control lie); TRAIT_PATTERN_MAP keys stay within the
    legacy trait vocabulary."""
    for skill_id in pattern_filters.SKILL_PATTERN_MAP:
        assert skill_id in skillsets.SKILLS, skill_id
    from features import TRAITS

    for trait_id in pattern_filters.TRAIT_PATTERN_MAP:
        assert trait_id in TRAITS or trait_id in ("wide", "flow", "sliders"), (
            trait_id
        )
    for labels in pattern_filters.SKILL_PATTERN_MAP.values():
        for label in labels:
            assert label in pattern_filters.SUPPORTED_PATTERNS, label


def test_streams_gate_opens_for_streams_and_bursts_alike() -> None:
    """osu! streams/bursts is the 5-9 note tier of the streams group: both
    evidence kinds must open the merged gate, each on its own."""
    streams = presets.known_builtin_by_name("Streams")
    assert streams is not None
    pure_streams = _candidate(
        6,
        sequence=_sequence(stream_chain_length=24, stream_chain_count=20),
    )
    check = evaluate_relevance(pure_streams, streams.ranking)
    assert check.passed is True, check.reason
    assert check.best_target is not None
    assert check.best_target.target_id == "stream_consistency"

    pure_bursts = _candidate(7, **_BURSTS_MAP)
    check = evaluate_relevance(pure_bursts, streams.ranking)
    assert check.passed is True, check.reason
    assert check.best_target is not None
    assert check.best_target.target_id == "burst_control"


def test_stamina_gate_stays_independent_of_bursts() -> None:
    """Stamina is its own preset: a short burst-only map is not endurance."""
    stamina = presets.known_builtin_by_name("Stamina")
    assert stamina is not None
    short_bursts = _candidate(
        8,
        count_circles=400,
        count_sliders=100,
        total_length=95,
        **_BURSTS_MAP,
    )
    check = evaluate_relevance(short_bursts, stamina.ranking)
    assert check.passed is False, check.reason
    # The endurance fixture still opens the stamina gate.
    endurance = _candidate(9, **_STAMINA_MAP)
    assert evaluate_relevance(endurance, stamina.ranking).passed is True


def test_retired_skillset_ids_resolve_to_live_survivors() -> None:
    for alias, canonical in skillsets.SKILL_ALIASES.items():
        assert alias not in skillsets.SKILLS
        assert canonical in skillsets.SKILLS
        # The survivor must actually measure what the alias promised: same
        # evidence features for the burst/stream twins.
        if alias == "speed_short_bursts":
            assert "bursts" in skillsets.SKILLS[canonical].feature_names
        if alias == "stream_short":
            assert "streams" in skillsets.SKILLS[canonical].feature_names


def test_no_two_skills_share_identical_weights() -> None:
    """Guard against taxonomy duplication regressions: no two skills may
    weigh the exact same feature set (the four rhythm clones)."""
    seen: dict[tuple, str] = {}
    for definition in skillsets.SKILL_DEFINITIONS:
        if definition.components:
            continue
        key = tuple(sorted(definition.feature_weights))
        assert key not in seen, (
            f"{definition.id} duplicates {seen[key]}: {key}"
        )
        seen[key] = definition.id
