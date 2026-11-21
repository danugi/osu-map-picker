from __future__ import annotations

from conftest import make_beatmap, make_beatmapset

import analysis
import library
import patterns
from models import SequenceAnalysis
from storage import Storage


def _save_analysis(
    store: Storage,
    beatmap_id: int,
    status: str,
    labels: tuple[str, ...] = (),
) -> None:
    sequence = SequenceAnalysis(
        analyzed_notes=100,
        total_notes=100,
        labels=labels,
        analyzer_version=patterns.ANALYZER_VERSION,
    )
    store.save_analysis(
        beatmap_id,
        status=status,
        source="download",
        analyzer_version=patterns.ANALYZER_VERSION,
        file_mtime_ns=1,
        file_size=1,
        scope="",
        sequence_json=analysis.sequence_to_json(sequence),
        attempts=0,
    )


def _seed_library(store: Storage) -> dict[str, int]:
    ids: dict[str, int] = {}
    set_one = make_beatmapset(
        1,
        title="Two Difficulties",
        artist="Artist One",
        creator="Mapper One",
        beatmaps=(
            make_beatmap(11, 1, stars=6.5, version="Insane", bpm=180),
            make_beatmap(12, 1, stars=5.1, version="Hard", bpm=170),
        ),
    )
    store.save_beatmapset(set_one)
    ids["insane"] = 11
    ids["hard"] = 12
    _save_analysis(
        store, 11, "measured", ("streams", "jump_aim")
    )
    _save_analysis(store, 12, "partial", ("bursts",))

    set_two = make_beatmapset(
        2,
        title="Problem Map",
        artist="Artist Two",
        creator="Mapper Two",
        beatmaps=(make_beatmap(21, 2, stars=4.0, version="Easy"),),
    )
    store.save_beatmapset(set_two)
    ids["failed"] = 21
    _save_analysis(store, 21, "failed")

    set_three = make_beatmapset(
        3,
        title="Queued Map",
        artist="Artist Three",
        creator="Mapper Three",
        beatmaps=(make_beatmap(31, 3, stars=7.0, version="Extra"),),
    )
    store.save_beatmapset(set_three)
    ids["pending"] = 31
    store.enqueue_analysis([(31, 100, "test")])

    set_four = make_beatmapset(
        4,
        title="Unknown Map",
        artist="Artist Four",
        creator="Mapper Four",
        beatmaps=(make_beatmap(41, 4, stars=3.0, version="Normal"),),
    )
    store.save_beatmapset(set_four)
    ids["not_analyzed"] = 41
    return ids


def test_counts_merge_analysis_and_queue_state(storage: Storage) -> None:
    _seed_library(storage)
    counts = library.counts(storage)
    assert counts["total"] == 5
    assert counts["analyzed"] == 1
    assert counts["partial"] == 1
    assert counts["pending"] == 2
    assert counts["failed"] == 1
    assert counts["unavailable"] == 0


def test_analyzed_scope_groups_difficulties_by_set(storage: Storage) -> None:
    _seed_library(storage)
    page = library.query_sets(
        storage, library.LibraryFilters(scope="analyzed", per_page=12)
    )
    assert page.total_sets == 1
    assert page.total_difficulties == 2
    assert page.sets[0].display_title == "Two Difficulties"
    assert [entry.version for entry in page.sets[0].entries] == [
        "Insane",
        "Hard",
    ]
    assert page.sets[0].entries[0].stars == 6.5
    assert page.sets[0].entries[0].labels == ("streams", "jump_aim")
    assert page.sets[0].entries[1].is_partial is True


def test_scope_and_pattern_filters_are_database_backed(
    storage: Storage,
) -> None:
    _seed_library(storage)
    streams = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="analyzed", patterns=("streams",), per_page=12
        ),
    )
    assert streams.total_difficulties == 1
    assert streams.sets[0].entries[0].version == "Insane"

    combined = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="analyzed",
            patterns=("streams", "bursts"),
            per_page=12,
        ),
    )
    assert combined.total_difficulties == 2
    assert {
        entry.version
        for item in combined.sets
        for entry in item.entries
    } == {"Insane", "Hard"}
    unsupported = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all", patterns=("variable_rhythm",), per_page=12
        ),
    )
    assert unsupported.total_difficulties == 0

    pending = library.query_sets(
        storage, library.LibraryFilters(scope="pending", per_page=12)
    )
    assert pending.total_difficulties == 2
    assert {entry.state for item in pending.sets for entry in item.entries} == {
        "pending",
        "not_analyzed",
    }

    problems = library.query_sets(
        storage, library.LibraryFilters(scope="failed", per_page=12)
    )
    assert problems.total_difficulties == 1
    assert problems.sets[0].entries[0].state == "failed"


def test_pagination_is_stable_and_page_is_clamped(storage: Storage) -> None:
    for set_id in range(1, 11):
        beatmap = make_beatmap(
            1000 + set_id, set_id, stars=3.0 + set_id / 10
        )
        storage.save_beatmapset(
            make_beatmapset(
                set_id, title=f"Set {set_id:02d}", beatmaps=(beatmap,)
            )
        )
        _save_analysis(storage, 1000 + set_id, "measured")
    first = library.query_sets(
        storage,
        library.LibraryFilters(scope="all", per_page=4, page=1),
    )
    second = library.query_sets(
        storage,
        library.LibraryFilters(scope="all", per_page=4, page=2),
    )
    too_far = library.query_sets(
        storage,
        library.LibraryFilters(scope="all", per_page=4, page=999),
    )
    assert first.total_sets == 10
    assert first.pages == 3
    assert first.page == 1
    assert second.page == 2
    assert too_far.page == 3
    assert {item.beatmapset_id for item in first.sets}.isdisjoint(
        {item.beatmapset_id for item in second.sets}
    )


def test_library_sorting_and_text_filter(storage: Storage) -> None:
    _seed_library(storage)
    page = library.query_sets(
        storage,
        library.LibraryFilters(
            scope="all", query="problem", sort="stars_desc", per_page=12
        ),
    )
    assert page.total_sets == 1
    assert page.sets[0].display_title == "Problem Map"
    ordered = library.query_sets(
        storage, library.LibraryFilters(scope="all", sort="stars_desc")
    )
    stars = [
        entry.stars for item in ordered.sets for entry in item.entries
    ]
    assert stars == sorted(stars, reverse=True)


def test_unavailable_scope_is_separate_from_failed(storage: Storage) -> None:
    beatmap = make_beatmap(51, 5, version="Missing")
    storage.save_beatmapset(
        make_beatmapset(5, title="Unavailable Source", beatmaps=(beatmap,))
    )
    storage.enqueue_analysis([(51, 100, "test")])
    storage.finish_analysis(
        51,
        status="unavailable",
        attempts=0,
        next_attempt_at=None,
        error="no .osu",
    )
    counts = library.counts(storage)
    assert counts["unavailable"] == 1
    assert counts["failed"] == 0
    page = library.query_sets(
        storage,
        library.LibraryFilters(scope="unavailable", per_page=12),
    )
    assert page.total_difficulties == 1
    assert page.sets[0].entries[0].state == "unavailable"


def test_offline_browse_makes_no_network_calls(
    storage: Storage, monkeypatch
) -> None:
    _seed_library(storage)

    def forbidden(*args, **kwargs):
        raise AssertionError("library must not use network")

    monkeypatch.setattr("httpx.get", forbidden)
    page = library.query_sets(
        storage, library.LibraryFilters(scope="analyzed", per_page=12)
    )
    assert page.sets[0].entries[0].display_title == "Two Difficulties"
    assert page.counts["total"] == 5


def test_query_sets_can_skip_global_counts(
    storage: Storage, monkeypatch
) -> None:
    _seed_library(storage)

    def forbidden(*args, **kwargs):
        raise AssertionError("global counts must not be queried")

    monkeypatch.setattr(library, "counts", forbidden)
    page = library.query_sets(
        storage,
        library.LibraryFilters(scope="analyzed", per_page=2),
        include_counts=False,
    )
    assert page.counts == {}
    assert page.total_sets == 1
    assert page.total_difficulties == 2
    assert page.sets[0].display_title == "Two Difficulties"
    filtered = library.filter_counts(
        storage, library.LibraryFilters(scope="analyzed", per_page=2)
    )
    assert filtered == {"sets": 1, "difficulties": 2}
