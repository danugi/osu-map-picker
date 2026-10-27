from __future__ import annotations

import httpx
import pytest

from conftest import make_beatmap, make_beatmapset

import analysis
import library
import library_preset
import patterns
import presets
from models import SequenceAnalysis
from storage import Storage


def _add_set(
    store: Storage,
    set_id: int,
    title: str,
    *,
    stars: float = 5.0,
    duration: int = 120,
    status: str = "ranked",
    mode: str = "osu",
    labels: tuple[str, ...] | None = ("jump_aim",),
) -> int:
    beatmap = make_beatmap(
        set_id * 10 + 1,
        set_id,
        stars=stars,
        total_length=duration,
        status=status,
        mode=mode,
    )
    store.save_beatmapset(
        make_beatmapset(
            set_id,
            title=title,
            status=status,
            beatmaps=(beatmap,),
        )
    )
    if labels is not None:
        sequence = SequenceAnalysis(
            analyzed_notes=100,
            total_notes=100,
            labels=labels,
            analyzer_version=patterns.ANALYZER_VERSION,
        )
        store.save_analysis(
            beatmap.id,
            status="measured",
            source="cache",
            analyzer_version=patterns.ANALYZER_VERSION,
            file_mtime_ns=1,
            file_size=1,
            scope="",
            sequence_json=analysis.sequence_to_json(sequence),
        )
    return beatmap.id


def _preset(
    name: str,
    *,
    stars_min: float | None = None,
    stars_max: float | None = None,
    duration_max: int | None = None,
    statuses: list[str] | None = None,
    modes: list[str] | None = None,
):
    cfg = presets.default_config(name)
    cfg.description = name
    cfg.filters.star_mode = (
        "strict" if stars_min is not None or stars_max is not None else "off"
    )
    cfg.filters.stars_min = stars_min
    cfg.filters.stars_max = stars_max
    cfg.filters.duration_max = duration_max
    cfg.filters.statuses = list(statuses or ["ranked", "loved", "qualified"])
    cfg.filters.modes = list(modes or ["osu"])
    cfg.ranking.relevance_targets = []
    cfg.ranking.skill_weights = {}
    cfg.ranking.trait_targets = {}
    presets.validate_preset_config(cfg)
    return cfg


def test_preset_hard_filters_change_actual_selection(storage: Storage) -> None:
    _add_set(storage, 11, "Broad Song", stars=4.2, duration=120)
    _add_set(storage, 12, "High Song", stars=6.5, duration=100)
    _add_set(storage, 13, "Long Song", stars=5.5, duration=200)
    _add_set(
        storage,
        14,
        "Qualified Song",
        stars=5.0,
        duration=120,
        status="qualified",
    )
    _add_set(storage, 15, "Mania Song", stars=5.0, duration=120, mode="mania")

    wide = _preset("Wide Test", stars_min=3.5, stars_max=7.0, duration_max=300)
    narrow = _preset(
        "Narrow Test",
        stars_min=5.5,
        stars_max=6.9,
        duration_max=150,
        statuses=["ranked", "loved"],
    )
    wide_eval = library_preset.evaluate_preset(storage, wide)
    narrow_eval = library_preset.evaluate_preset(storage, narrow)

    assert set(wide_eval.set_order) == {11, 12, 13, 14}
    assert set(narrow_eval.set_order) == {12}
    assert wide_eval.set_order != narrow_eval.set_order

    filtered = library.LibraryFilters(
        scope="all",
        sort="preset_fit",
        page=1,
        per_page=8,
    )
    wide_page = library.query_sets(
        storage,
        filtered,
        preset_order=wide_eval.set_order,
        preset_beatmaps=wide_eval.beatmap_by_set,
    )
    narrow_page = library.query_sets(
        storage,
        filtered,
        preset_order=narrow_eval.set_order,
        preset_beatmaps=narrow_eval.beatmap_by_set,
    )
    assert set(item.beatmapset_id for item in wide_page.sets) == {
        11,
        12,
        13,
        14,
    }
    assert [item.title for item in narrow_page.sets] == ["High Song"]


def test_broad_preset_can_still_browse_whole_catalog(storage: Storage) -> None:
    _add_set(storage, 21, "Ranked Map", status="ranked")
    _add_set(storage, 22, "Qualified Map", status="qualified")
    _add_set(storage, 23, "Graveyard Map", status="graveyard")
    _add_set(storage, 24, "Mania Map", mode="mania", stars=5.5)

    broad = _preset(
        "Broad Everything",
        statuses=["ranked", "qualified", "graveyard", "loved", "pending"],
        modes=["osu", "mania"],
    )
    # Remove the default duration cap; this is the broad no-op preset case.
    broad.filters.duration_max = None
    broad.filters.star_mode = "off"
    broad.filters.stars_min = None
    broad.filters.stars_max = None
    presets.validate_preset_config(broad)
    evaluation = library_preset.evaluate_preset(storage, broad)
    assert set(evaluation.set_order) == {21, 22, 23, 24}

    manual = library.query_sets(
        storage,
        library.LibraryFilters(scope="all", per_page=20),
    )
    assert set(item.beatmapset_id for item in manual.sets) == {21, 22, 23, 24}


def test_preset_evaluation_makes_no_network_requests(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_set(storage, 61, "Offline Preset", stars=5.5)

    def forbidden(*args, **kwargs):
        raise AssertionError("preset Library evaluation used the network")

    monkeypatch.setattr(httpx, "get", forbidden)
    cfg = _preset("Offline Preset", stars_min=4.0, stars_max=6.9)
    evaluation = library_preset.evaluate_preset(storage, cfg)
    assert evaluation.set_order == (61,)
    assert evaluation.result.api_requests == 0


def test_missing_evidence_is_not_confirmed_failure(storage: Storage) -> None:
    _add_set(
        storage,
        31,
        "No Evidence",
        stars=5.5,
        duration=100,
        labels=None,
    )
    cfg = _preset("Missing Evidence Test", stars_min=4.0, stars_max=6.9)
    cfg.ranking.relevance_targets = ["stream_long"]
    cfg.ranking.skill_weights = {"stream_long": 1.0}
    presets.validate_preset_config(cfg)

    evaluation = library_preset.evaluate_preset(storage, cfg)

    assert evaluation.result.passed_filters == 1
    assert evaluation.result.passed_relevance == 0
    assert evaluation.result.scored == ()
    assert evaluation.missing_evidence >= 1
    assert evaluation.below_threshold == 0
    assert any(
        "недостаточно" in reason.casefold()
        or "не анализировалось" in reason.casefold()
        or "источник недоступен" in reason.casefold()
        for reason in evaluation.result.excluded
    )


def test_manual_filters_modify_ids_counts_and_patterns(storage: Storage) -> None:
    _add_set(storage, 41, "Jump Song", stars=6.0, labels=("jump_aim",))
    _add_set(storage, 42, "Stream Song", stars=5.0, labels=("streams",))
    _add_set(
        storage,
        43,
        "Qualified Jump",
        stars=6.2,
        status="qualified",
        labels=("jump_aim",),
    )

    base = library.LibraryFilters(scope="all", per_page=1)
    all_page = library.query_sets(storage, base)
    jump_page = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all", patterns=("jump_aim",), per_page=1
        ),
    )
    ranked_page = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all", statuses=("ranked",), per_page=1
        ),
    )
    query_page = library.query_sets(
        storage,
        library.LibraryFilters(scope="all", query="Stream", per_page=1),
    )
    assert all_page.total_sets == 3
    assert jump_page.total_sets == 2
    assert ranked_page.total_sets == 2
    assert [item.title for item in query_page.sets] == ["Stream Song"]


def test_pattern_filters_combine_with_preset_order(storage: Storage) -> None:
    _add_set(storage, 51, "Preset Jump", labels=("jump_aim",))
    _add_set(storage, 52, "Preset Streams", labels=("streams",))
    cfg = _preset("Pattern Preset", stars_min=4.0, stars_max=6.9)
    evaluation = library_preset.evaluate_preset(storage, cfg)
    assert set(evaluation.set_order) == {51, 52}

    filtered = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all", patterns=("streams",), per_page=8
        ),
        preset_order=evaluation.set_order,
        preset_beatmaps=evaluation.beatmap_by_set,
    )
    assert filtered.total_sets == 1
    assert [item.title for item in filtered.sets] == ["Preset Streams"]


def test_shuffle_is_stable_across_pages_and_covers_filtered_collection(
    storage: Storage,
) -> None:
    for index in range(12):
        _add_set(
            storage,
            100 + index,
            f"Shuffle Song {index:02d}",
            stars=5.0,
            labels=("jump_aim",),
        )

    seed = 987654321
    filters = library.LibraryFilters(
        scope="all",
        patterns=("jump_aim",),
        per_page=4,
        shuffle_seed=seed,
    )
    first = library.query_sets(storage, filters)
    first_ids = [item.beatmapset_id for item in first.sets]
    second = library.query_sets(storage, filters)
    assert first_ids == [item.beatmapset_id for item in second.sets]

    all_ids: list[int] = []
    for page_number in range(1, first.pages + 1):
        page = library.query_sets(
            storage,
            library.LibraryFilters(
                scope="all",
                patterns=("jump_aim",),
                per_page=4,
                page=page_number,
                shuffle_seed=seed,
            ),
        )
        all_ids.extend(item.beatmapset_id for item in page.sets)
    assert len(all_ids) == 12
    assert len(set(all_ids)) == 12
    assert set(all_ids) == {100 + index for index in range(12)}
    assert first.total_sets == 12
    assert first.total_difficulties == 12

    other = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all",
            patterns=("jump_aim",),
            per_page=4,
            shuffle_seed=seed + 1,
        ),
    )
    assert [item.beatmapset_id for item in other.sets] != first_ids


def test_shuffle_makes_no_network_requests(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        _add_set(storage, 200 + index, f"Offline Song {index}")

    def forbidden(*args, **kwargs):
        raise AssertionError("shuffle attempted a network request")

    monkeypatch.setattr(httpx, "get", forbidden)
    page = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all", per_page=2, shuffle_seed=42
        ),
    )
    assert page.total_sets == 3
    assert page.sets
