from __future__ import annotations

from typing import Callable

import pytest
from conftest import make_beatmap, make_beatmapset

import presets
from discovery import (
    DiscoveryEngine,
    build_strategies,
    collect_seeds,
)
from models import (
    DifficultyAttributes,
    DiscoverySettings,
    Filters,
    PatternChain,
    RankingSettings,
    SearchPage,
    SearchQuery,
    SequenceAnalysis,
)
from osu_api import OsuForbiddenError, OsuServerError
from storage import Storage


class StubApi:
    def __init__(
        self,
        responder: Callable[[SearchQuery], SearchPage],
        attributes: dict[int, DifficultyAttributes | Exception] | None = None,
    ) -> None:
        self.responder = responder
        self.attributes = attributes or {}
        self.search_calls: list[SearchQuery] = []
        self.attribute_calls: list[int] = []

    def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        return self.responder(query)

    def get_beatmap_attributes(self, beatmap_id: int) -> DifficultyAttributes:
        self.attribute_calls.append(beatmap_id)
        value = self.attributes.get(beatmap_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return DifficultyAttributes(
                beatmap_id=beatmap_id,
                star_rating=5.5,
                aim_difficulty=3.0,
                speed_difficulty=1.0,
            )
        return value


def settings(**overrides) -> DiscoverySettings:
    base = DiscoverySettings(
        max_requests=10,
        max_pages_per_strategy=2,
        max_candidates=100,
        fetch_attributes_limit=0,
        fetch_sequences_limit=0,
        cache_ttl_minutes=30,
        use_oldest=False,
        use_rating=False,
        use_popular=False,
        expand_seeds=False,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_build_strategies_flags_and_seeds() -> None:
    filters = Filters(statuses=["ranked", "loved"])
    strategies = build_strategies(
        filters,
        settings(use_oldest=True, use_rating=True, use_popular=True, expand_seeds=True),
        seeds=["Artist A", "Mapper B", "artist a"],
    )
    names = [strategy.name for strategy in strategies]
    assert names[:2] == ["ranked:recent", "loved:recent"]
    assert "ranked:oldest" in names
    assert "ranked:rating" in names
    assert "ranked:favourites" in names
    assert "seed:Artist A" in names
    assert "seed:Mapper B" in names
    assert len(names) == len(set(names))


def test_text_query_disables_seed_strategies() -> None:
    filters = Filters(statuses=["ranked"], query="crystalia")
    strategies = build_strategies(
        filters, settings(expand_seeds=True), seeds=["Artist A"]
    )
    assert all(strategy.query == "crystalia" for strategy in strategies)
    assert not any(strategy.name.startswith("seed:") for strategy in strategies)


def test_dedupe_across_strategies_and_pagination(storage: Storage) -> None:
    def responder(query: SearchQuery) -> SearchPage:
        if query.status == "ranked" and query.cursor_string is None:
            return SearchPage(
                beatmapsets=(make_beatmapset(1),),
                cursor_string="page2",
            )
        if query.status == "ranked" and query.cursor_string == "page2":
            return SearchPage(beatmapsets=(make_beatmapset(2),))
        if query.status == "loved":
            return SearchPage(
                beatmapsets=(make_beatmapset(1), make_beatmapset(3))
            )
        raise AssertionError(f"unexpected query {query}")

    api = StubApi(responder)
    engine = DiscoveryEngine(api, storage, settings())
    result = engine.run(Filters(statuses=["ranked", "loved"]), RankingSettings())
    assert result.total_fetched == 3
    assert [item.beatmapset.id for item in result.scored] == [1, 2, 3]
    assert len({item.beatmapset.id for item in result.scored}) == 3
    assert result.api_requests == 3
    assert result.complete is True


def test_page_limit_marks_result_incomplete(storage: Storage) -> None:
    def responder(query: SearchQuery) -> SearchPage:
        return SearchPage(
            beatmapsets=(make_beatmapset(1),),
            cursor_string="more",
        )

    api = StubApi(responder)
    engine = DiscoveryEngine(
        api, storage, settings(max_pages_per_strategy=1)
    )
    result = engine.run(Filters(statuses=["ranked"]), RankingSettings())
    assert result.complete is False
    assert any("страниц" in note for note in result.notes)


def test_request_budget_stops_search_and_reports(storage: Storage) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(api, storage, settings(max_requests=1))
    result = engine.run(
        Filters(statuses=["ranked", "loved"]), RankingSettings()
    )
    assert result.api_requests == 1
    assert result.complete is False
    assert any("Бюджет" in note for note in result.notes)
    assert len(api.search_calls) == 1


def test_search_cache_avoids_second_network_request(storage: Storage) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(api, storage, settings())
    first = engine.run(Filters(), RankingSettings())
    second = engine.run(Filters(), RankingSettings())
    assert first.api_requests == 3
    assert second.api_requests == 0
    assert second.cache_hits == 3
    assert len(api.search_calls) == 3
    assert [item.beatmapset.id for item in second.scored] == [1]


def test_attribute_budget_is_reserved_before_pagination(storage: Storage) -> None:
    def responder(query: SearchQuery) -> SearchPage:
        page = 0 if query.cursor_string is None else 1
        return SearchPage(
            beatmapsets=(make_beatmapset(10 + page),),
            cursor_string="next" if page == 0 else None,
        )

    api = StubApi(responder)
    engine = DiscoveryEngine(
        api,
        storage,
        settings(
            max_requests=4,
            max_pages_per_strategy=2,
            fetch_attributes_limit=2,
        ),
    )
    result = engine.run(Filters(statuses=["ranked"]), RankingSettings())
    assert len(api.search_calls) == 2
    assert len(api.attribute_calls) == 2
    assert result.api_requests == 4
    assert result.timings["fetch"] >= 0
    assert result.timings["attributes"] >= 0
    assert set(result.timings) == {
        "fetch",
        "filter",
        "attributes",
        "sequences",
        "rank",
        "total",
    }


def test_ranking_change_reuses_cached_pages_and_attributes(
    storage: Storage,
) -> None:
    beatmapset = make_beatmapset(1)
    beatmap_id = beatmapset.beatmaps[0].id
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(beatmapset,)),
    )
    engine = DiscoveryEngine(
        api, storage, settings(fetch_attributes_limit=1)
    )
    first = engine.run(Filters(), RankingSettings())
    assert first.api_requests == 4  # three search pages plus one attribute
    assert api.attribute_calls == [beatmap_id]

    changed = RankingSettings()
    changed.comfort_stars = 5.8
    second = engine.run(Filters(), changed)
    assert second.api_requests == 0
    assert second.cache_hits == 3
    assert len(api.search_calls) == 3
    assert api.attribute_calls == [beatmap_id]
    assert second.scored
    assert second.ranking_used is not None
    assert second.ranking_used.comfort_stars == 5.8


def test_strict_filters_are_reported_not_relaxed(storage: Storage) -> None:
    sets = (
        make_beatmapset(
            1, beatmaps=(make_beatmap(11, 1, stars=7.5),)
        ),
        make_beatmapset(
            2,
            beatmaps=(
                make_beatmap(21, 2, stars=5.0, total_length=300, hit_length=300),
            ),
        ),
        make_beatmapset(3),
    )
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    engine = DiscoveryEngine(api, storage, settings())
    result = engine.run(Filters(), RankingSettings())
    assert [item.beatmapset.id for item in result.scored] == [3]
    assert result.total_fetched == 3
    assert result.passed_filters == 1
    assert result.excluded["звёздность вне диапазона"] == 1
    assert result.excluded["длительность вне диапазона"] == 1
    assert len(result.exclusions) == 2
    assert not any("исключили все" in note for note in result.notes)


def test_candidate_limit_is_applied_and_reported(storage: Storage) -> None:
    sets = tuple(make_beatmapset(i) for i in range(1, 13))
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    engine = DiscoveryEngine(api, storage, settings(max_candidates=10))
    result = engine.run(Filters(), RankingSettings())
    assert result.count == 10
    assert result.complete is False
    assert any("Максимум кандидатов" in note for note in result.notes)


def test_empty_result_with_api_errors_is_explained(storage: Storage) -> None:
    def responder(query: SearchQuery) -> SearchPage:
        raise OsuServerError("osu! down", 503)

    api = StubApi(responder)
    engine = DiscoveryEngine(api, storage, settings())
    result = engine.run(Filters(), RankingSettings())
    assert result.count == 0
    assert result.errors
    assert any("не означает" in note for note in result.notes)


def test_attribute_enrichment_and_cache(storage: Storage) -> None:
    beatmapset = make_beatmapset(1)
    beatmap_id = beatmapset.beatmaps[0].id
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(beatmapset,)),
        attributes={
            beatmap_id: DifficultyAttributes(
                beatmap_id=beatmap_id,
                star_rating=5.0,
                aim_difficulty=3.0,
                speed_difficulty=1.0,
            )
        },
    )
    engine = DiscoveryEngine(
        api, storage, settings(fetch_attributes_limit=1)
    )
    result = engine.run(Filters(), RankingSettings())
    assert api.attribute_calls == [beatmap_id]
    traits = result.scored[0].candidate.traits
    assert traits.scores["aim"] == 0.75
    assert traits.confidence == "medium"
    skillsets = result.scored[0].candidate.skillsets
    assert skillsets["aim_classic"].score is not None
    assert skillsets["aim_classic"].support == "measured"

    second = engine.run(Filters(), RankingSettings())
    assert api.attribute_calls == [beatmap_id]
    assert second.scored[0].candidate.traits.scores["aim"] == 0.75
    assert second.scored[0].candidate.skillsets["aim_classic"].score is not None


def test_attribute_403_degrades_gracefully(storage: Storage) -> None:
    beatmapset = make_beatmapset(1)
    beatmap_id = beatmapset.beatmaps[0].id
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(beatmapset,)),
        attributes={beatmap_id: OsuForbiddenError("no attributes", 403)},
    )
    engine = DiscoveryEngine(
        api, storage, settings(fetch_attributes_limit=1)
    )
    result = engine.run(Filters(), RankingSettings())
    assert result.count == 1
    assert any("attributes" in note for note in result.notes)
    assert result.scored[0].candidate.traits.scores.get("aim") is None


def test_collect_seeds_prefers_positive_feedback(storage: Storage) -> None:
    liked = make_beatmapset(1, artist="DJ TOTTO", creator="Nevo")
    disliked = make_beatmapset(2, artist="Bad", creator="BadMapper")
    storage.save_beatmapsets([liked, disliked])
    storage.set_feedback(1, ["like"])
    storage.set_feedback(2, ["mapping_dislike"])
    seeds = collect_seeds(
        storage,
        {1: liked, 2: disliked},
        storage.all_feedback(),
        storage.favorite_ids(),
    )
    assert "DJ TOTTO" in seeds
    assert "Nevo" in seeds
    assert "Bad" not in seeds


def test_collect_seeds_falls_back_to_favorites(storage: Storage) -> None:
    favorite = make_beatmapset(1, artist="Fav Artist", creator="Fav Mapper")
    storage.save_beatmapset(favorite)
    storage.set_favorite(1, True)
    seeds = collect_seeds(
        storage,
        {1: favorite},
        {},
        storage.favorite_ids(),
    )
    assert seeds[:2] == ["Fav Artist", "Fav Mapper"]


def test_engine_records_search_run(storage: Storage) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(api, storage, settings())
    engine.run(Filters(), RankingSettings(), record=True)
    last = storage.last_search_run()
    assert last is not None
    assert last["result_count"] == 1
    assert last["complete"] == 1


def test_stored_candidates_can_be_reranked_without_api(storage: Storage) -> None:
    beatmapsets = [make_beatmapset(index) for index in range(1, 13)]
    storage.save_beatmapsets(beatmapsets)
    engine = DiscoveryEngine(None, storage, settings(max_candidates=50))
    result = engine.rerank_stored(Filters(), RankingSettings())
    assert result.origin == "local_cache"
    assert result.api_requests == 0
    assert result.strategies == ("local-cache",)
    assert result.total_fetched == 12
    assert result.passed_filters == 12
    assert result.count == 12
    assert all(item.score >= 0 for item in result.scored)
    assert any("Локальный кеш" in note for note in result.notes)


def test_stored_rerank_honours_strict_filters(storage: Storage) -> None:
    slow = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, total_length=240, hit_length=230),)
    )
    fast = make_beatmapset(
        2, beatmaps=(make_beatmap(21, 2, total_length=90, hit_length=85),)
    )
    storage.save_beatmapsets([slow, fast])
    engine = DiscoveryEngine(None, storage, settings(max_candidates=50))
    filters = Filters(duration_max=150)
    result = engine.rerank_stored(filters, RankingSettings())
    assert [item.beatmapset.id for item in result.scored] == [2]
    assert result.total_fetched == 2
    assert result.passed_filters == 1


def test_stored_rerank_keeps_user_history_filters(storage: Storage) -> None:
    storage.save_beatmapsets([make_beatmapset(index) for index in range(1, 13)])
    storage.record_history([(1, 80.0, 1)])
    engine = DiscoveryEngine(None, storage, settings(max_candidates=50))
    hidden = engine.rerank_stored(Filters(hide_seen=True), RankingSettings())
    visible = engine.rerank_stored(Filters(), RankingSettings())
    assert hidden.total_fetched == 12
    assert hidden.passed_filters == 11
    assert 1 not in {item.beatmapset.id for item in hidden.scored}
    assert 1 in {item.beatmapset.id for item in visible.scored}
    assert [item.score for item in visible.scored] == [
        item.score for item in engine.rerank_stored(Filters(), RankingSettings()).scored
    ]


def _sequence(kind: str, length: int = 12) -> SequenceAnalysis:
    chain = PatternChain(
        kind=kind,
        start_index=0,
        length=length,
        start_ms=1000,
        end_ms=1000 + (length - 1) * 125,
        median_gap_ms=125.0,
        rhythm_quality=1.0,
        compact_share=1.0,
    )
    if kind == "stream":
        return SequenceAnalysis(
            stream_chains=(chain,),
            analyzed_notes=length,
            total_notes=length,
        )
    return SequenceAnalysis(
        burst_chains=(chain,),
        analyzed_notes=length,
        total_notes=length,
    )


def test_sequence_enrichment_keeps_streams_and_bursts_separate(
    storage: Storage,
) -> None:
    sets = (
        make_beatmapset(1, title="Stream map"),
        make_beatmapset(2, title="Burst map"),
    )
    analyses = {11: _sequence("stream", 12), 21: _sequence("burst", 6)}
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        return analyses[beatmap_id]

    def engine() -> DiscoveryEngine:
        api = StubApi(lambda query: SearchPage(beatmapsets=sets))
        return DiscoveryEngine(
            api,
            storage,
            settings(fetch_sequences_limit=10),
            sequence_loader=loader,
        )

    streams = presets.builtin_by_name("Streams & Stamina")
    bursts = presets.builtin_by_name("Bursts")
    assert streams is not None and bursts is not None

    stream_result = engine().run(streams.filters, streams.ranking)
    assert [item.beatmapset.id for item in stream_result.scored] == [1]

    burst_result = engine().run(bursts.filters, bursts.ranking)
    assert [item.beatmapset.id for item in burst_result.scored] == [2]
    assert calls and all(allow for _, allow in calls)


def test_sequence_downloads_are_skipped_when_no_target_needs_them(
    storage: Storage,
) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),)),
        attributes={
            11: DifficultyAttributes(
                beatmap_id=11,
                star_rating=5.0,
                aim_difficulty=3.0,
                speed_difficulty=1.0,
            )
        },
    )
    calls: list[int] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append(beatmap_id)
        return _sequence("stream")

    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_attributes_limit=1, fetch_sequences_limit=10),
        sequence_loader=loader,
    )
    result = engine.run(farm.filters, farm.ranking)
    assert result.count == 1
    assert calls == []


def test_any_taste_preset_works_without_traits(storage: Storage) -> None:
    preset = presets.known_builtin_by_name("Any Taste")
    assert preset is not None
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(api, storage, preset.discovery)
    result = engine.run(preset.filters, preset.ranking)
    assert result.count == 1
    assert result.scored[0].score > 0


def test_aim_farm_uses_short_nm_profile_without_pp_surface(
    storage: Storage,
) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    engine = DiscoveryEngine(api, storage, settings(fetch_attributes_limit=1))
    result = engine.run(farm.filters, farm.ranking)
    assert result.count == 1
    candidate = result.scored[0].candidate
    assert not hasattr(candidate, "nm_pp")
    farm_factor = next(
        factor for factor in result.scored[0].factors if factor.key == "farm_nm"
    )
    assert "PP" not in farm_factor.explanation
    assert "NM" in farm_factor.explanation


def test_strategy_offset_rotates_sources_for_random_selection(
    storage: Storage,
) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(
        api,
        storage,
        settings(use_oldest=True, use_rating=True, max_requests=1),
    )
    filters = Filters(statuses=["ranked"])
    engine.run(filters, RankingSettings(), strategy_offset=1)
    assert api.search_calls[-1].sort == "ranked_asc"
    engine.run(filters, RankingSettings(), strategy_offset=2)
    assert api.search_calls[-1].sort == "rating_desc"

