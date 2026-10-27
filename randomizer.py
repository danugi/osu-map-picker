"""Randomized sampling for the search result screen.

This module is deliberately independent from Streamlit and from the osu! API.
It works on candidates that already passed the active strict filters and were
ranked by the normal recommendation engine. Randomization therefore changes
*which* suitable maps are shown, never *whether* a map is suitable.

Two different operations are provided:

* :func:`select_random_subset` builds a new sample from a candidate pool;
* :func:`shuffled_ids` only changes the order of the current result.

Sampling is weighted by recommendation score and by the fit of the user's
selected skillsets. It still explores lower-ranked maps instead of always
returning the same top cards.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from models import ScoredCandidate
from skillsets import skill_fit


@dataclass(frozen=True, slots=True)
class RandomSelection:
    """A fresh random subset plus honest information about its limits."""

    items: tuple[ScoredCandidate, ...]
    requested: int
    pool_size: int
    available: int
    excluded: int
    seed: int
    shortfall: bool
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.items)


def selection_weight(
    item: ScoredCandidate,
    skill_weights: Mapping[str, float] | None = None,
) -> float:
    """Return a positive sampling weight.

    Recommendation score dominates, while skill fit gives the selected
    skillsets a real say. Neither value is presented to the user as a
    probability or as an absolute rating.
    """

    score = max(0.0, min(1.0, float(item.score)))
    fit_value = 0.5
    weights = {
        key: float(value)
        for key, value in (skill_weights or {}).items()
        if float(value) > 0
    }
    if weights:
        fit = skill_fit(item.candidate.skillsets, weights)
        if fit is not None and fit.score is not None:
            fit_value = max(0.0, min(1.0, float(fit.score)))
    # Keep a floor for both signals so a low general score or a missing skill
    # never makes a card practically unreachable.  The score still spans a 4x
    # range and the selected skill fit spans a 2.86x range; a strong skill fit
    # can therefore beat a much higher general score without turning sampling
    # into a uniform draw.
    score_term = 0.25 + 0.75 * score
    fit_term = 0.35 + 0.65 * fit_value
    return max(1e-9, score_term * fit_term)


def _display_order(
    items: Sequence[ScoredCandidate],
    seed: int,
) -> list[ScoredCandidate]:
    """Shuffle a selected sample independently from its sampling keys."""

    ordered = list(items)
    random.Random(int(seed) ^ 0x5EED5EED).shuffle(ordered)
    return ordered


def _dedupe_by_set(
    items: Iterable[ScoredCandidate],
) -> list[ScoredCandidate]:
    seen: set[int] = set()
    result: list[ScoredCandidate] = []
    for item in items:
        set_id = item.beatmapset.id
        if set_id in seen:
            continue
        seen.add(set_id)
        result.append(item)
    return result


def _weighted_sample(
    items: Sequence[ScoredCandidate],
    count: int,
    seed: int,
    skill_weights: Mapping[str, float] | None,
    *,
    max_per_artist: int | None = None,
    max_per_mapper: int | None = None,
) -> list[ScoredCandidate]:
    """Weighted sampling without replacement (Efraimidis-Spirakis keys).

    Optional artist/mapper caps improve diversity. If the caps would prevent
    filling the requested batch, the deferred candidates are added instead of
    silently returning a short batch.
    """

    rng = random.Random(int(seed))
    keyed: list[tuple[float, int, ScoredCandidate]] = []
    for index, item in enumerate(items):
        weight = selection_weight(item, skill_weights)
        u = max(rng.random(), 1e-12)
        key = math.log(u) / weight
        # Larger keys are selected first; the index keeps ties deterministic.
        keyed.append((key, -index, item))
    keyed.sort(key=lambda row: (row[0], row[1]), reverse=True)

    artist_cap = (
        max(1, int(max_per_artist)) if max_per_artist is not None else None
    )
    mapper_cap = (
        max(1, int(max_per_mapper)) if max_per_mapper is not None else None
    )
    chosen: list[ScoredCandidate] = []
    deferred: list[ScoredCandidate] = []
    artist_counts: dict[str, int] = {}
    mapper_counts: dict[str, int] = {}
    for _, _, item in keyed:
        artist = item.beatmapset.display_artist
        mapper = item.beatmapset.creator
        if artist_cap is not None and artist_counts.get(artist, 0) >= artist_cap:
            deferred.append(item)
            continue
        if mapper_cap is not None and mapper_counts.get(mapper, 0) >= mapper_cap:
            deferred.append(item)
            continue
        chosen.append(item)
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        mapper_counts[mapper] = mapper_counts.get(mapper, 0) + 1
        if len(chosen) >= count:
            break
    if len(chosen) < count and deferred:
        # Diversity caps are soft: fill the batch rather than return a
        # misleading shortfall when the pool has enough distinct mapsets.
        # Relax one dimension at a time so a large shared mapper/artist does
        # not erase the other diversity signal.
        added_ids = {item.beatmapset.id for item in chosen}

        def fill(artist_limit: int | None, mapper_limit: int | None) -> None:
            for item in deferred:
                if len(chosen) >= count:
                    return
                if item.beatmapset.id in added_ids:
                    continue
                artist = item.beatmapset.display_artist
                mapper = item.beatmapset.creator
                if (
                    artist_limit is not None
                    and artist_counts.get(artist, 0) >= artist_limit
                ):
                    continue
                if (
                    mapper_limit is not None
                    and mapper_counts.get(mapper, 0) >= mapper_limit
                ):
                    continue
                chosen.append(item)
                added_ids.add(item.beatmapset.id)
                artist_counts[artist] = artist_counts.get(artist, 0) + 1
                mapper_counts[mapper] = mapper_counts.get(mapper, 0) + 1

        fill(artist_cap, None)
        fill(None, mapper_cap)
        fill(None, None)
    # Sampling keys decide membership; display order must not leak the
    # recommendation ranking back into the random result.
    return _display_order(chosen, seed)


def select_random_subset(
    items: Iterable[ScoredCandidate],
    count: int,
    *,
    seed: int,
    avoid_ids: Iterable[int] = (),
    skill_weights: Mapping[str, float] | None = None,
    max_per_artist: int | None = None,
    max_per_mapper: int | None = None,
) -> RandomSelection:
    """Select a new subset from already filtered and ranked candidates.

    ``avoid_ids`` normally contains recently shown beatmapsets. The function
    never relaxes filters and never invents fallback cards: when too few maps
    remain, it returns the honest shortfall. Artist/mapper caps are soft and
    only influence which of the eligible cards are sampled.
    """

    requested = max(0, int(count))
    pool = _dedupe_by_set(items)
    pool_size = len(pool)
    avoided = {int(value) for value in avoid_ids}
    available_items = [
        item for item in pool if item.beatmapset.id not in avoided
    ]
    available = len(available_items)
    excluded = pool_size - available

    if requested == 0 or not available_items:
        note = (
            "Нет новых подходящих карт: все они уже недавно показывались. "
            "Можно разрешить просмотренные карты или сначала расширить поиск."
            if pool_size
            else "В текущем пуле нет карт, прошедших строгие фильтры."
        )
        return RandomSelection(
            items=(),
            requested=requested,
            pool_size=pool_size,
            available=available,
            excluded=excluded,
            seed=int(seed),
            shortfall=True,
            note=note,
        )

    if requested >= available:
        chosen = _display_order(available_items, seed)
        note = (
            f"Новых кандидатов только {available} из запрошенных {requested}. "
            "Показаны все доступные; строгие фильтры не ослаблялись."
        )
        shortfall = available < requested
    else:
        chosen = _weighted_sample(
            available_items,
            requested,
            seed,
            skill_weights,
            max_per_artist=max_per_artist,
            max_per_mapper=max_per_mapper,
        )
        note = (
            f"Случайная подвыборка {len(chosen)} из {available} новых "
            f"кандидатов (всего в пуле {pool_size})."
        )
        shortfall = False

    return RandomSelection(
        items=tuple(chosen),
        requested=requested,
        pool_size=pool_size,
        available=available,
        excluded=excluded,
        seed=int(seed),
        shortfall=shortfall,
        note=note,
    )


def shuffled_ids(
    ids: Sequence[int],
    *,
    seed: int,
    previous: Sequence[int] = (),
) -> list[int]:
    """Shuffle ids and avoid returning the exact previous order."""

    order = [int(value) for value in ids]
    if len(order) < 2:
        return order
    rng = random.Random(int(seed))
    previous_list = [int(value) for value in previous]
    for _ in range(8):
        rng.shuffle(order)
        if order != previous_list:
            return order
    if order == previous_list:
        order = order[1:] + order[:1]
    return order
