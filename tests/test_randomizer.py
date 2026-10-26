from __future__ import annotations

from conftest import make_beatmap, make_beatmapset

from models import Candidate, ScoredCandidate
from randomizer import (
    select_random_subset,
    selection_weight,
    shuffled_ids,
)


def scored(beatmapset_id: int, score: float = 0.7) -> ScoredCandidate:
    beatmapset = make_beatmapset(
        beatmapset_id,
        beatmaps=(
            make_beatmap(beatmapset_id * 10 + 1, beatmapset_id),
            make_beatmap(beatmapset_id * 10 + 2, beatmapset_id, version="Another"),
        ),
    )
    return ScoredCandidate(
        Candidate(beatmapset, beatmapset.beatmaps[0]),
        score,
        (),
    )


def test_subset_dedupes_by_beatmapset_and_keeps_recommendation_order() -> None:
    items = [
        scored(1, 0.9),
        scored(2, 0.8),
        scored(1, 0.7),
        scored(3, 0.6),
    ]
    selection = select_random_subset(items, 2, seed=11)
    assert selection.pool_size == 3
    assert selection.available == 3
    assert selection.count == 2
    assert len({item.beatmapset.id for item in selection.items}) == 2
    indexes = [items.index(item) for item in selection.items]
    assert indexes == sorted(indexes)


def test_subset_never_returns_avoided_ids_and_reports_shortfall() -> None:
    items = [scored(index) for index in range(1, 6)]
    selection = select_random_subset(
        items,
        4,
        seed=1,
        avoid_ids={1, 2, 3, 4},
    )
    assert selection.count == 1
    assert selection.available == 1
    assert selection.excluded == 4
    assert selection.shortfall is True
    assert selection.items[0].beatmapset.id == 5
    assert "только 1" in selection.note


def test_subset_returns_nothing_instead_of_inventing_cards() -> None:
    items = [scored(1), scored(2)]
    selection = select_random_subset(items, 5, seed=2, avoid_ids={1, 2})
    assert selection.items == ()
    assert selection.count == 0
    assert selection.shortfall is True
    assert "Нет новых подходящих карт" in selection.note

    empty = select_random_subset([], 5, seed=2)
    assert empty.items == ()
    assert "строгие фильтры" in empty.note


def test_seed_is_deterministic_and_strictly_picks_from_the_pool() -> None:
    items = [scored(index) for index in range(1, 31)]
    first = select_random_subset(items, 10, seed=123)
    second = select_random_subset(items, 10, seed=123)
    other = select_random_subset(items, 10, seed=124)
    assert first.items == second.items
    assert first.items != other.items
    allowed = {item.beatmapset.id for item in items}
    for selection in (first, second, other):
        assert len(selection.items) == 10
        assert {item.beatmapset.id for item in selection.items} <= allowed


def test_selection_weight_is_positive_and_prefers_score() -> None:
    low = scored(1, 0.1)
    high = scored(2, 0.9)
    assert selection_weight(low) > 0
    assert selection_weight(high) > selection_weight(low)


def test_shuffle_changes_order_without_dropping_ids() -> None:
    ids = [5, 7, 11, 13, 17, 19]
    shuffled = shuffled_ids(ids, seed=9, previous=ids)
    assert sorted(shuffled) == sorted(ids)
    assert shuffled != ids
    assert shuffled_ids(ids, seed=9, previous=ids) == shuffled
    assert shuffled_ids([], seed=9) == []
    assert shuffled_ids([4], seed=9) == [4]


def test_shuffle_avoids_the_exact_previous_order_even_with_two_items() -> None:
    assert shuffled_ids([1, 2], seed=1, previous=[1, 2]) == [2, 1]
    assert shuffled_ids([1, 2], seed=1, previous=[2, 1]) == [1, 2]
