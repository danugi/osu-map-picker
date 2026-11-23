from __future__ import annotations

import analysis as analysis_store
import patterns
import presets
from conftest import make_beatmap, make_beatmapset
from discovery import DiscoveryEngine
from models import (
    DiscoverySettings,
    PatternChain,
    SequenceAnalysis,
)
from storage import Storage


def _chain(index: int, kind: str, length: int) -> PatternChain:
    return PatternChain(
        kind=kind,
        start_index=index * 40,
        length=length,
        start_ms=1000 + index * 1000,
        end_ms=1000 + index * 1000 + length * 120,
        median_gap_ms=125.0,
        rhythm_quality=1.0,
        compact_share=1.0,
    )


def _shared_pool(storage: Storage) -> None:
    beatmap = make_beatmap(
        11,
        1,
        stars=5.5,
        total_length=170,
        hit_length=165,
        ar=9.5,
        od=9.0,
        count_circles=520,
        count_sliders=80,
    )
    beatmapset = make_beatmapset(
        1,
        title="Shared evidence",
        beatmaps=(beatmap,),
    )
    storage.save_beatmapset(beatmapset)
    storage.save_attributes(
        beatmap.id,
        {
            "star_rating": 5.5,
            "aim_difficulty": 3.8,
            "speed_difficulty": 2.8,
            "slider_factor": 1.3,
            "max_combo": 900,
        },
    )
    sequence = SequenceAnalysis(
        stream_chains=tuple(_chain(index, "stream", 24) for index in range(6)),
        burst_chains=tuple(_chain(index, "burst", 6) for index in range(4)),
        analyzed_notes=480,
        total_notes=480,
        jump_share=0.90,
        wide_jump_share=0.70,
        flow_share=0.70,
        flow_longest=12,
        flow_run_count=3,
        flow_quality=0.80,
        flow_coverage=0.70,
        flow_shape=0.80,
        flow_longest_share=0.60,
        angle_change_share=0.70,
        rhythm_variability=0.80,
    )
    storage.save_analysis(
        beatmap.id,
        status="measured",
        source="cache",
        analyzer_version=patterns.ANALYZER_VERSION,
        file_mtime_ns=None,
        file_size=None,
        scope="fixture",
        sequence_json=analysis_store.sequence_to_json(sequence),
    )


def _run(storage: Storage, preset_name: str):
    preset = presets.builtin_by_name(preset_name)
    assert preset is not None
    engine = DiscoveryEngine(
        None,
        storage,
        DiscoverySettings(
            max_candidates=100,
            fetch_attributes_limit=0,
            fetch_sequences_limit=0,
            use_oldest=False,
            use_rating=False,
            use_popular=False,
            expand_seeds=False,
        ),
    )
    return engine.rerank_stored(
        preset.filters,
        preset.ranking,
        all_candidates=True,
    )


def test_same_measured_candidate_is_independently_eligible_for_many_presets(
    storage: Storage,
) -> None:
    _shared_pool(storage)
    results = {
        name: _run(storage, name)
        for name in (
            "Aim",
            "Jump Aim",
            "Wide Aim",
            "Flow Aim",
            "Aim Control",
            "Streams",
            "Continuous Streams",
            "Stamina",
        )
    }
    for name, result in results.items():
        ids = {item.beatmapset.id for item in result.scored}
        assert 1 in ids, (name, result.excluded, result.notes)

    # The shared evidence is not consumed or rewritten by any preset.
    traits = {
        item.candidate.beatmap.id: item.candidate.traits.scores
        for result in results.values()
        for item in result.scored
    }
    assert traits[11]["streams"] > 0
    assert traits[11]["bursts"] > 0


def test_streams_and_stamina_overlap_without_global_dedup(storage: Storage) -> None:
    _shared_pool(storage)
    streams = _run(storage, "Streams")
    stamina = _run(storage, "Stamina")
    assert {item.beatmapset.id for item in streams.scored} == {1}
    assert {item.beatmapset.id for item in stamina.scored} == {1}

    # Running a second preset must not remove the first preset's evidence or
    # mutate its scored candidate.
    assert streams.scored[0].candidate.traits.scores == (
        stamina.scored[0].candidate.traits.scores
    )
    assert streams.scored[0].candidate.sequence is not None
    assert stamina.scored[0].candidate.sequence is not None
    assert streams.scored[0].candidate.beatmap.id == 11
    assert stamina.scored[0].candidate.beatmap.id == 11


def test_parent_and_child_are_both_selectable_profiles(storage: Storage) -> None:
    _shared_pool(storage)
    aim = _run(storage, "Aim")
    jump = _run(storage, "Jump Aim")
    assert {item.candidate.beatmapset.id for item in aim.scored} == {1}
    assert {item.candidate.beatmapset.id for item in jump.scored} == {1}
    assert aim.scored[0].candidate.beatmap.id == jump.scored[0].candidate.beatmap.id


def test_hierarchy_metadata_roundtrips_through_preset_config() -> None:
    from models import PresetConfig

    jump = presets.builtin_by_name("Jump Aim")
    assert jump is not None
    assert jump.catalog_id == "aim.jump"
    assert jump.parent_id == "aim"
    assert jump.category_id == "train"
    assert jump.display_order == 11
    loaded = PresetConfig.from_dict(jump.to_dict())
    assert loaded.catalog_id == jump.catalog_id
    assert loaded.parent_id == jump.parent_id
    assert loaded.category_id == jump.category_id
    assert loaded.display_order == jump.display_order
