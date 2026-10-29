from __future__ import annotations

import copy
from typing import Callable

import pytest
from conftest import make_beatmap, make_beatmapset

import analysis as analysis_store
import evidence
import patterns
import presets
from discovery import (
    DiscoveryEngine,
    _apply_relevance_gate,
    build_strategies,
    collect_seeds,
)
from features import derive_features
from gameplay import GameplayError
from skillsets import evaluate_skills
from models import (
    Candidate,
    DifficultyAttributes,
    DiscoverySettings,
    Exclusion,
    Filters,
    PatternChain,
    RankingSettings,
    SearchPage,
    SearchQuery,
    SequenceAnalysis,
)
from osu_api import (
    OsuApiError,
    OsuForbiddenError,
    OsuNetworkError,
    OsuServerError,
)
from ranking import RankingContext
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
        self.http_attempts = 0
        self.token_requests = 0

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
    assert result.total_fetched == 4
    assert result.raw_rows == 4
    assert result.unique_raw == 3
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


def test_failed_page_attempts_consume_the_request_budget(storage: Storage) -> None:
    def fail(query: SearchQuery) -> SearchPage:
        raise OsuApiError("simulated API error", 400)

    api = StubApi(fail)
    engine = DiscoveryEngine(api, storage, settings(max_requests=2))
    result = engine.run(
        Filters(statuses=["ranked", "loved", "qualified"]),
        RankingSettings(),
    )
    assert len(api.search_calls) == 2
    assert result.api_requests == 2
    assert result.requests_failed == 2
    assert result.requests_succeeded == 0
    assert result.complete is False
    assert result.stop_reason == "request_budget"
    assert result.errors


def test_result_reports_http_retries_and_oauth_separately(
    storage: Storage,
) -> None:
    class RetryingApi(StubApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            # One logical API call, three actual HTTP attempts inside the
            # client, plus one token request.
            self.http_attempts += 3
            self.token_requests += 1
            return super().search_beatmapsets(query)

    api = RetryingApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(api, storage, settings(max_requests=1))
    result = engine.run(Filters(statuses=["ranked"]), RankingSettings())
    assert result.api_requests == 1
    assert result.http_attempts == 3
    assert result.oauth_requests == 1


def test_persistent_network_failure_fails_fast_across_strategies(
    storage: Storage,
) -> None:
    def fail(query: SearchQuery) -> SearchPage:
        raise OsuNetworkError("simulated timeout")

    api = StubApi(fail)
    engine = DiscoveryEngine(api, storage, settings(max_requests=12))
    result = engine.run(
        Filters(statuses=["ranked", "loved", "qualified"]),
        RankingSettings(),
    )
    # The first strategy already exhausted the client's finite retries. The
    # remaining strategies must not repeat the same timeout cost.
    assert len(api.search_calls) == 1
    assert api.attribute_calls == []
    assert result.api_requests == 1
    assert result.requests_failed == 1
    assert result.complete is False
    assert result.stop_reason == "network_failure"
    assert any("Сетевые запросы" in note for note in result.notes)


def test_cache_hits_do_not_consume_a_tight_request_budget(
    storage: Storage,
) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    warm = DiscoveryEngine(api, storage, settings(max_requests=3))
    first = warm.run(Filters(), RankingSettings())
    assert first.api_requests == 3
    assert len(api.search_calls) == 3

    tight = DiscoveryEngine(api, storage, settings(max_requests=1))
    second = tight.run(Filters(), RankingSettings())
    assert second.api_requests == 0
    assert second.requests_succeeded == 0
    assert second.requests_failed == 0
    assert second.cache_hits == 3
    assert second.complete is True
    assert len(api.search_calls) == 3


def test_deadline_stops_before_network_work(storage: Storage) -> None:
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_requests=10),
        deadline_seconds=0,
    )
    result = engine.run(Filters(), RankingSettings())
    assert api.search_calls == []
    assert api.attribute_calls == []
    assert result.api_requests == 0
    assert result.complete is False
    assert result.stop_reason == "deadline"
    assert result.deadline_seconds == 0
    assert any("Дедлайн" in note for note in result.notes)


def test_failed_attribute_request_counts_against_budget(
    storage: Storage,
) -> None:
    beatmapset = make_beatmapset(1)
    beatmap_id = beatmapset.beatmaps[0].id
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(beatmapset,)),
        attributes={beatmap_id: OsuApiError("simulated 400", 400)},
    )
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_requests=2, fetch_attributes_limit=1),
    )
    result = engine.run(Filters(), RankingSettings())
    assert api.attribute_calls == [beatmap_id]
    assert result.api_requests == 2
    assert result.requests_succeeded == 1
    assert result.requests_failed == 1
    assert result.complete is False
    assert result.stop_reason in {"request_budget", "request_failed"}


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
    assert result.total_fetched == 9
    assert result.unique_raw == 3
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


def test_successive_searches_accumulate_the_persistent_catalog(
    storage: Storage,
) -> None:
    def responder(query: SearchQuery) -> SearchPage:
        set_id = 1 if query.status == "ranked" else 2
        return SearchPage(beatmapsets=(make_beatmapset(set_id),))

    api = StubApi(responder)
    engine = DiscoveryEngine(api, storage, settings())
    engine.run(Filters(statuses=["ranked"]), RankingSettings())
    engine.run(Filters(statuses=["loved"]), RankingSettings())
    assert storage.beatmapset_count() == 2
    assert storage.get_beatmapset(1) is not None
    assert storage.get_beatmapset(2) is not None


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


def test_rerank_all_candidates_keeps_the_whole_eligible_pool(
    storage: Storage,
) -> None:
    storage.save_beatmapsets(
        [make_beatmapset(index) for index in range(1, 61)]
    )
    engine = DiscoveryEngine(None, storage, settings(max_candidates=10))
    capped = engine.rerank_stored(Filters(), RankingSettings())
    broad = engine.rerank_stored(
        Filters(), RankingSettings(), all_candidates=True
    )
    assert capped.count == 10
    assert broad.count == 60
    assert broad.passed_relevance == 60
    assert any("обрезки до топ-N нет" in note for note in broad.notes)
    assert not any("ограничен 10" in note for note in broad.notes)


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


def _save_sequence(
    storage: Storage,
    candidate: Candidate,
    sequence: SequenceAnalysis,
    *,
    status: str = evidence.MEASURED,
) -> None:
    storage.save_beatmapsets([candidate.beatmapset])
    storage.save_analysis(
        candidate.beatmap.id,
        status=status,
        source="test",
        analyzer_version=patterns.ANALYZER_VERSION,
        file_mtime_ns=None,
        file_size=None,
        scope="",
        sequence_json=analysis_store.sequence_to_json(sequence),
    )


def test_local_analyzed_candidate_fills_short_api_result(storage: Storage) -> None:
    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None
    candidate = _candidate(1, sequence=_sequence("burst", 9))
    _save_sequence(storage, candidate, candidate.sequence or _sequence("burst", 9))
    api = StubApi(lambda query: SearchPage(beatmapsets=()))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_candidates=50, fetch_sequences_limit=0),
        local_catalog_supplement=True,
    )
    filters = copy.deepcopy(bursts.filters)
    filters.statuses = ["ranked"]
    result = engine.run(filters, bursts.ranking)
    assert result.local_topup is True
    assert result.local_catalog_added == 1
    assert result.local_catalog_duplicates == 0
    assert result.origin == "mixed"
    assert len(api.search_calls) == 1
    assert result.api_requests == 1
    assert result.catalog_before == 1
    assert result.catalog_after == 1
    assert result.scored[0].beatmapset.id == 1
    assert result.scored[0].candidate.analysis is not None
    assert result.scored[0].candidate.analysis.status == evidence.MEASURED


def test_local_topup_reuses_all_persisted_analyses_beyond_limit(
    storage: Storage,
) -> None:
    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None
    candidates = []
    for set_id in range(1, 26):
        rating = 8.0
        sequence = _sequence("burst", 9)
        candidate = _candidate(set_id, rating=rating, sequence=sequence)
        candidates.append(candidate)
        _save_sequence(storage, candidate, sequence)
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        raise AssertionError("persisted analyses must not be re-parsed")

    api = StubApi(lambda query: SearchPage(beatmapsets=()))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_candidates=100, fetch_sequences_limit=20),
        sequence_loader=loader,
        local_catalog_supplement=True,
    )
    filters = copy.deepcopy(bursts.filters)
    filters.statuses = ["ranked"]
    result = engine.run(filters, bursts.ranking)
    assert result.local_topup is True
    assert result.local_catalog_considered == 25
    assert result.local_catalog_added == 25
    assert 3 in {item.beatmapset.id for item in result.scored}
    assert calls == []
    assert result.sequence_downloads == 0
    assert result.sequence_attempted == 0
    ctx = RankingContext.from_user_data(bursts.ranking, {}, {}, {})
    ordered_ids = [
        item.beatmapset.id for item in engine._fair_sequence_order(candidates, ctx)
    ]
    assert ordered_ids.index(3) >= 20


def test_local_sequence_reuse_does_not_raise_new_analysis_limit(
    storage: Storage,
) -> None:
    storage.save_beatmapsets(
        [make_beatmapset(index) for index in range(1, 26)]
    )
    calls: list[int] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append(beatmap_id)
        return _sequence("stream", 12)

    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    engine = DiscoveryEngine(
        None,
        storage,
        settings(max_candidates=100, fetch_sequences_limit=20),
        sequence_loader=loader,
        sequence_cache_probe=lambda beatmap_id: True,
    )
    engine.rerank_stored(streams.filters, streams.ranking)
    assert len(calls) == 20


def test_local_topup_deduplicates_and_keeps_api_metadata(storage: Storage) -> None:
    local_one = make_beatmapset(1, title="Local old title")
    local_two = make_beatmapset(2, title="Local second")
    storage.save_beatmapsets([local_one, local_two])
    api_set = make_beatmapset(1, title="API fresh title")
    api = StubApi(lambda query: SearchPage(beatmapsets=(api_set,)))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_candidates=50, fetch_sequences_limit=0),
        local_catalog_supplement=True,
    )
    result = engine.run(Filters(statuses=["ranked"]), RankingSettings())
    assert result.count == 2
    assert [item.beatmapset.id for item in result.scored] == [1, 2]
    assert result.scored[0].beatmapset.title == "API fresh title"
    assert result.local_catalog_added == 1
    assert result.local_catalog_duplicates == 1
    assert len(api.search_calls) == 1
    assert result.api_requests == 1


def test_local_duplicate_keeps_api_metadata_and_cached_analysis(
    storage: Storage,
) -> None:
    local_analysis = _sequence("burst", 9)
    local_candidate = _candidate(1, sequence=local_analysis)
    storage.save_beatmapsets(
        [local_candidate.beatmapset, make_beatmapset(2, title="Local extra")]
    )
    storage.save_analysis(
        local_candidate.beatmap.id,
        status=evidence.MEASURED,
        source="test",
        analyzer_version=patterns.ANALYZER_VERSION,
        file_mtime_ns=None,
        file_size=None,
        scope="",
        sequence_json=analysis_store.sequence_to_json(local_analysis),
    )
    api_set = make_beatmapset(1, title="API title")
    api = StubApi(lambda query: SearchPage(beatmapsets=(api_set,)))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_candidates=50, fetch_sequences_limit=20),
        local_catalog_supplement=True,
    )
    result = engine.run(Filters(statuses=["ranked"]), RankingSettings())
    assert result.scored[0].beatmapset.title == "API title"
    assert result.scored[0].candidate.analysis is not None
    assert result.scored[0].candidate.analysis.status == evidence.MEASURED
    assert result.local_catalog_duplicates == 1
    assert len(api.search_calls) == 1


def test_unanalyzed_local_candidate_does_not_bypass_gate(storage: Storage) -> None:
    storage.save_beatmapsets([make_beatmapset(1)])
    api = StubApi(lambda query: SearchPage(beatmapsets=()))
    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None
    engine = DiscoveryEngine(
        api,
        storage,
        settings(max_candidates=50, fetch_sequences_limit=0),
        local_catalog_supplement=True,
    )
    result = engine.run(bursts.filters, bursts.ranking)
    assert result.local_topup is True
    assert result.local_catalog_added == 0
    assert result.count == 0
    assert any(
        "Локальный каталог проверен" in note for note in result.notes
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

    discovery = presets.builtin_by_name("Discovery")
    assert discovery is not None
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_attributes_limit=1, fetch_sequences_limit=10),
        sequence_loader=loader,
    )
    result = engine.run(discovery.filters, discovery.ranking)
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


def _candidate(
    set_id: int,
    *,
    rating: float = 8.0,
    play_count: int = 1_000,
    stars: float = 5.5,
    bpm: float = 180.0,
    sequence: SequenceAnalysis | None = None,
) -> Candidate:
    beatmapset = make_beatmapset(
        set_id,
        rating=rating,
        play_count=play_count,
        beatmaps=(
            make_beatmap(
                set_id * 10 + 1,
                set_id,
                stars=stars,
                bpm=bpm,
                total_length=120,
                hit_length=110,
                count_circles=350,
                count_sliders=150,
                count_spinners=0,
            ),
        ),
    )
    beatmap = beatmapset.beatmaps[0]
    traits = derive_features(beatmap, beatmapset, None, sequence=sequence)
    return Candidate(
        beatmapset=beatmapset,
        beatmap=beatmap,
        traits=traits,
        skillsets=evaluate_skills(traits),
        sequence=sequence,
    )


def test_sequence_selection_does_not_take_only_top_general_scores(
    storage: Storage,
) -> None:
    sets = tuple(
        make_beatmapset(
            index,
            rating=10.0 - index * 0.7,
            play_count=1_000_000 // index,
        )
        for index in range(1, 7)
    )
    calls: list[int] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append(beatmap_id)
        return SequenceAnalysis()

    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_sequences_limit=2),
        sequence_loader=loader,
    )
    engine.run(streams.filters, streams.ranking)
    assert len(calls) == 2
    # The second slot must not simply be the next-highest Recommendation
    # score. Stratification deliberately includes a different score band.
    assert max(calls) > 21

    first = list(calls)
    calls.clear()
    second = engine.run(streams.filters, streams.ranking)
    assert calls == first
    assert second.sequence_required is True


def test_sequence_selection_prefers_cached_files_first(storage: Storage) -> None:
    sets = tuple(
        make_beatmapset(
            index,
            rating=10.0 - index * 0.7,
            play_count=1_000_000 // index,
        )
        for index in range(1, 7)
    )
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        return SequenceAnalysis()

    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_sequences_limit=2),
        sequence_loader=loader,
        sequence_cache_probe=lambda beatmap_id: beatmap_id == 61,
    )
    result = engine.run(streams.filters, streams.ranking)
    assert calls[0] == (61, False)
    assert any(allow_download for _, allow_download in calls)
    assert result.sequence_cache_hits == 1
    assert result.sequence_downloads == 1


def test_local_sequence_rerank_skips_missing_cache_without_blocking(
    storage: Storage,
) -> None:
    storage.save_beatmapsets(
        [
            make_beatmapset(1, rating=10.0, play_count=1_000_000),
            make_beatmapset(2, rating=7.0, play_count=1_000),
        ]
    )
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        if beatmap_id == 21:
            return _sequence("stream")
        raise GameplayError("missing cache entry")

    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    engine = DiscoveryEngine(
        None,
        storage,
        settings(fetch_sequences_limit=2),
        sequence_loader=loader,
        sequence_cache_probe=lambda beatmap_id: beatmap_id == 21,
    )
    result = engine.rerank_stored(streams.filters, streams.ranking)
    assert calls == [(21, False)]
    assert [item.beatmapset.id for item in result.scored] == [2]
    assert result.sequence_analyzed == 1
    assert result.sequence_unanalyzed == 1
    assert result.sequences_complete is False


@pytest.mark.parametrize(
    "name",
    ("Mixed Aim", "All-rounder", "Speed", "Stamina", "Rhythm Control"),
)
def test_sequence_analysis_triggers_for_skill_weights(
    name: str, storage: Storage
) -> None:
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        return SequenceAnalysis()

    preset = presets.known_builtin_by_name(name)
    assert preset is not None
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_sequences_limit=1),
        sequence_loader=loader,
    )
    engine.run(preset.filters, preset.ranking)
    assert calls


def test_sequence_analysis_triggers_for_legacy_trait_targets(
    storage: Storage,
) -> None:
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        return SequenceAnalysis()

    ranking = RankingSettings()
    ranking.trait_targets = {"bursts": 1.0}
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_sequences_limit=1),
        sequence_loader=loader,
    )
    engine.run(Filters(), ranking)
    assert calls


def test_sequence_stats_distinguish_downloads_and_unanalyzed(
    storage: Storage,
) -> None:
    sets = tuple(make_beatmapset(index) for index in range(1, 4))

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        return SequenceAnalysis()

    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_sequences_limit=1),
        sequence_loader=loader,
    )
    result = engine.run(streams.filters, streams.ranking)
    assert result.sequence_required is True
    assert result.sequence_analyzed == 1
    assert result.sequence_unanalyzed == 2
    assert result.sequences_complete is False
    assert result.sequence_downloads == 1
    assert result.sequence_cache_hits == 0


def test_zero_sequence_limit_uses_bounded_cache_only(
    storage: Storage,
) -> None:
    sets = tuple(make_beatmapset(index) for index in range(1, 56))
    calls: list[tuple[int, bool]] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append((beatmap_id, allow_download))
        return SequenceAnalysis()

    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    api = StubApi(lambda query: SearchPage(beatmapsets=sets))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_sequences_limit=0),
        sequence_loader=loader,
        sequence_cache_probe=lambda beatmap_id: True,
    )
    result = engine.run(streams.filters, streams.ranking)
    assert len(calls) == 50
    assert all(not allow_download for _, allow_download in calls)
    assert result.sequence_analyzed == 50
    assert result.sequence_unanalyzed == 5
    assert result.sequence_downloads == 0
    assert result.sequence_cache_hits == 50
    assert result.sequences_complete is False
    assert any("50" in note and "кэш" in note.casefold() for note in result.notes)


def test_bursts_preset_does_not_spend_attribute_budget(
    storage: Storage,
) -> None:
    calls: list[int] = []

    def loader(beatmap_id: int, allow_download: bool) -> SequenceAnalysis:
        calls.append(beatmap_id)
        return SequenceAnalysis()

    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None
    api = StubApi(
        lambda query: SearchPage(beatmapsets=(make_beatmapset(1),))
    )
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_attributes_limit=1, fetch_sequences_limit=1),
        sequence_loader=loader,
    )
    engine.run(bursts.filters, bursts.ranking)
    assert calls
    assert api.attribute_calls == []


def test_sequence_only_preset_does_not_reserve_attribute_requests(
    storage: Storage,
) -> None:
    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None

    def responder(query: SearchQuery) -> SearchPage:
        return SearchPage(
            beatmapsets=(make_beatmapset(1),),
            cursor={"page": len(api.search_calls)},
        )

    api = StubApi(responder)
    engine = DiscoveryEngine(
        api,
        storage,
        settings(
            max_requests=2,
            max_pages_per_strategy=5,
            fetch_attributes_limit=1,
            fetch_sequences_limit=1,
        ),
        sequence_loader=lambda beatmap_id, allow_download: SequenceAnalysis(),
    )
    engine.run(bursts.filters, bursts.ranking)
    assert len(api.search_calls) == 2
    assert api.attribute_calls == []


def test_sequence_only_preset_preselects_attributes_by_generic_score(
    storage: Storage,
) -> None:
    streams = presets.builtin_by_name("Streams & Stamina")
    assert streams is not None
    low = _candidate(1, rating=5.0, play_count=100)
    high = _candidate(2, rating=10.0, play_count=1_000_000)
    storage.save_beatmapsets([low.beatmapset, high.beatmapset])
    api = StubApi(lambda query: SearchPage(beatmapsets=()))
    engine = DiscoveryEngine(
        api,
        storage,
        settings(fetch_attributes_limit=1),
    )
    ctx = RankingContext.from_user_data(streams.ranking, {}, {}, {})
    engine._enrich_attributes(
        [low, high],
        ctx,
        budget={"requests": 0},
        notes=[],
        errors=[],
    )
    assert api.attribute_calls == [21]


def test_relevance_gate_reports_not_analyzed_separately() -> None:
    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None
    excluded: dict[str, int] = {}
    exclusions: list[Exclusion] = []
    kept = _apply_relevance_gate(
        [_candidate(1)],
        bursts.ranking,
        excluded=excluded,
        exclusions=exclusions,
    )
    assert kept == []
    assert any("не анализировалось" in reason for reason in excluded)


def test_relevance_gate_reports_insufficient_data_separately() -> None:
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    excluded: dict[str, int] = {}
    exclusions: list[Exclusion] = []
    kept = _apply_relevance_gate(
        [_candidate(1)],
        farm.ranking,
        excluded=excluded,
        exclusions=exclusions,
    )
    assert kept == []
    assert any(
        "недостаточно данных" in reason or "не анализировалось" in reason
        for reason in excluded
    )


def test_relevance_gate_reports_below_threshold_separately() -> None:
    bursts = presets.builtin_by_name("Bursts")
    assert bursts is not None
    excluded: dict[str, int] = {}
    exclusions: list[Exclusion] = []
    kept = _apply_relevance_gate(
        [_candidate(1, sequence=SequenceAnalysis(analyzed_notes=10, total_notes=10))],
        bursts.ranking,
        excluded=excluded,
        exclusions=exclusions,
    )
    assert kept == []
    assert any("ниже порога" in reason for reason in excluded)


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


def test_enrich_attributes_batches_cached_reads(storage, monkeypatch):
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    low = _candidate(1)
    high = _candidate(2)
    storage.save_beatmapsets([low.beatmapset, high.beatmapset])
    for beatmap_id in (low.beatmap.id, high.beatmap.id):
        storage.save_attributes(
            beatmap_id,
            {
                "star_rating": 5.5,
                "aim_difficulty": 3.0,
                "speed_difficulty": 1.0,
                "slider_factor": 1.0,
                "max_combo": 500,
                "source": "test",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            },
        )
    calls = {"all": 0, "get": 0}
    original_all = storage.all_attributes
    original_get = storage.get_attributes

    def counted_all(ids):
        calls["all"] += 1
        return original_all(ids)

    def counted_get(beatmap_id):
        calls["get"] += 1
        return original_get(beatmap_id)

    monkeypatch.setattr(storage, "all_attributes", counted_all)
    monkeypatch.setattr(storage, "get_attributes", counted_get)
    api = StubApi(lambda query: SearchPage(beatmapsets=()))
    engine = DiscoveryEngine(api, storage, settings(fetch_attributes_limit=2))
    ctx = RankingContext.from_user_data(farm.ranking, {}, {}, {})
    budget = {"requests": 0}
    updated = engine._enrich_attributes(
        [low, high], ctx, budget=budget, notes=[], errors=[]
    )
    assert [item.beatmap.id for item in updated] == [
        low.beatmap.id,
        high.beatmap.id,
    ]
    assert all(item.attributes is not None for item in updated)
    assert api.attribute_calls == []
    assert calls == {"all": 1, "get": 0}
    assert budget["attributes_cached"] == 2
    assert budget.get("attributes_requested", 0) == 0


def test_enrich_attributes_preserves_uncached_api_order(storage):
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    low = _candidate(1)
    high = _candidate(2)
    storage.save_beatmapsets([low.beatmapset, high.beatmapset])
    api = StubApi(lambda query: SearchPage(beatmapsets=()))
    engine = DiscoveryEngine(api, storage, settings(fetch_attributes_limit=2))
    ctx = RankingContext.from_user_data(farm.ranking, {}, {}, {})
    budget = {"requests": 0}
    updated = engine._enrich_attributes(
        [low, high], ctx, budget=budget, notes=[], errors=[]
    )
    assert [item.attributes is not None for item in updated] == [True, True]
    assert api.attribute_calls == [low.beatmap.id, high.beatmap.id]
    assert budget["attributes_requested"] == 2
    assert budget["attributes_succeeded"] == 2
    assert budget.get("attributes_cached", 0) == 0

