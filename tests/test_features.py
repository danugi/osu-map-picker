from __future__ import annotations

from conftest import make_beatmap, make_beatmapset

import gameplay
import patterns
from features import derive_traits, format_traits, trait_similarity
from models import DifficultyAttributes, PatternChain, SequenceAnalysis
from test_patterns import make_osu


def test_traits_from_official_attributes() -> None:
    beatmapset = make_beatmapset(1, tags=("aim", "jumps"))
    beatmap = make_beatmap(11, 1, stars=6.0, cs=4.0)
    attributes = DifficultyAttributes(
        beatmap_id=11,
        star_rating=6.0,
        aim_difficulty=3.0,
        speed_difficulty=1.0,
    )
    traits = derive_traits(beatmap, beatmapset, attributes)
    assert traits.confidence == "medium"
    assert traits.scores["aim"] == 0.75
    assert traits.scores["speed"] == 0.25
    assert "control" in traits.scores
    assert "density" in traits.scores
    # Density and speed are not evidence of a stream or burst pattern.
    assert "bursts" not in traits.scores
    assert "streams" not in traits.scores
    assert traits.sources["aim"] == "osu_api_attributes"


def test_sequence_analysis_is_the_only_stream_burst_source() -> None:
    beatmapset = make_beatmapset(1, tags=("stream", "bursts"))
    beatmap = make_beatmap(11, 1, stars=5.5, cs=4.0)
    notes = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(10)
    ]
    preview = gameplay.parse_osu(make_osu(notes), beatmap_id=11)
    analysis = patterns.analyze_sequences(preview)
    traits = derive_traits(beatmap, beatmapset, None, sequence=analysis)

    assert traits.scores["streams"] == 1.0
    assert traits.scores["bursts"] == 0.0
    assert traits.sources["streams"] == "osu_sequence"
    assert traits.sources["bursts"] == "osu_sequence"
    assert any("Анализ .osu" in note for note in traits.notes)
    assert any("stream" in line.casefold() for line in format_traits(traits))


def test_truncated_sequence_analysis_is_not_reported_as_high_confidence() -> None:
    beatmapset = make_beatmapset(1)
    beatmap = make_beatmap(11, 1)
    chain = PatternChain(
        kind="stream",
        start_index=0,
        length=20,
        start_ms=1000,
        end_ms=3375,
        median_gap_ms=125.0,
        rhythm_quality=1.0,
        compact_share=1.0,
    )
    analysis = SequenceAnalysis(
        stream_chains=(chain,),
        analyzed_notes=6000,
        total_notes=6000,
        truncated=True,
    )
    traits = derive_traits(beatmap, beatmapset, None, sequence=analysis)

    assert traits.sources["streams"] == "osu_sequence_partial"
    assert traits.confidence == "medium"


def test_traits_from_tags_are_low_confidence() -> None:
    beatmapset = make_beatmapset(1, tags=("aim", "jumps"))
    beatmap = make_beatmap(11, 1, stars=5.0, cs=4.0)
    traits = derive_traits(beatmap, beatmapset, None)
    assert traits.confidence == "low"
    assert traits.scores["aim"] == 1.0
    assert traits.sources["aim"] == "tags"
    assert any("тегам" in note for note in traits.notes)


def test_no_data_means_no_invented_traits() -> None:
    beatmapset = make_beatmapset(1, tags=())
    beatmap = make_beatmap(
        11,
        1,
        stars=None,
        cs=None,
        ar=None,
        od=None,
        hp=None,
        bpm=None,
        count_circles=None,
        count_sliders=None,
        count_spinners=None,
        hit_length=None,
        total_length=None,
    )
    traits = derive_traits(beatmap, beatmapset, None)
    assert traits.confidence == "none"
    assert traits.scores == {}
    assert not traits.available
    assert format_traits(traits) == ["Тип паттерна: недостаточно данных."]


def test_geometry_is_not_invented_from_tags() -> None:
    beatmapset = make_beatmapset(1, tags=("flow", "weird", "wide"))
    beatmap = make_beatmap(11, 1)
    traits = derive_traits(beatmap, beatmapset, None)
    assert "wide" not in traits.scores
    assert "flow" not in traits.scores
    assert any("flow/weird" in note for note in traits.notes)


def test_trait_similarity() -> None:
    beatmapset = make_beatmapset(1)
    beatmap = make_beatmap(11, 1, stars=6.0)
    attributes = DifficultyAttributes(
        beatmap_id=11, aim_difficulty=3.0, speed_difficulty=1.0
    )
    traits = derive_traits(beatmap, beatmapset, attributes)
    assert trait_similarity({"aim": 0.75}, traits) == 1.0
    assert trait_similarity({"aim": 0.0}, traits) == 0.25
    assert trait_similarity({"wide": 1.0}, traits) is None


def test_density_uses_hit_length() -> None:
    beatmapset = make_beatmapset(1)
    beatmap = make_beatmap(
        11,
        1,
        count_circles=400,
        count_sliders=0,
        count_spinners=0,
        hit_length=100,
    )
    traits = derive_traits(beatmap, beatmapset, None)
    # 4 objects/sec -> (4-2)/6 = 0.333
    assert traits.scores["density"] == 0.333
