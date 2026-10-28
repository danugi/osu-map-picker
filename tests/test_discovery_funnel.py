from __future__ import annotations

from conftest import make_beatmapset

import presets
from discovery import DiscoveryEngine, _cache_key, build_strategies
from models import (
    DiscoverySettings,
    Filters,
    RankingSettings,
    SearchPage,
    SearchQuery,
)
from osu_api import OsuServerError


def _settings(**overrides) -> DiscoverySettings:
    settings = DiscoverySettings(
        max_requests=12,
        max_pages_per_strategy=2,
        max_candidates=400,
        fetch_attributes_limit=0,
        fetch_sequences_limit=0,
        cache_ttl_minutes=30,
        use_oldest=False,
        use_rating=False,
        use_popular=False,
        expand_seeds=False,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class CursorApi:
    def __init__(self, *, per_page: int = 50, pages: int = 5) -> None:
        self.per_page = per_page
        self.pages = pages
        self.search_calls: list[SearchQuery] = []
        self.http_attempts = 0
        self.token_requests = 0
        self.requests_made = 0

    def get_beatmap_attributes(self, beatmap_id):
        from models import DifficultyAttributes

        return DifficultyAttributes(
            beatmap_id=beatmap_id,
            star_rating=5.0,
            aim_difficulty=4.0,
            speed_difficulty=1.0,
        )

    def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        self.requests_made += 1
        self.http_attempts += 1
        page = 0
        if query.cursor_string:
            page = int(query.cursor_string.rsplit("-", 1)[-1])
        start = page * self.per_page
        beatmapsets = tuple(
            make_beatmapset(
                start + index + 1,
                title=f"Page {page + 1} Map {index}",
            )
            for index in range(self.per_page)
        )
        cursor = (
            f"page-{page + 1}" if page + 1 < self.pages else None
        )
        return SearchPage(beatmapsets=beatmapsets, cursor_string=cursor)


class EmptyApi:
    def __init__(self) -> None:
        self.search_calls: list[SearchQuery] = []
        self.http_attempts = 0
        self.token_requests = 0
        self.requests_made = 0

    def get_beatmap_attributes(self, beatmap_id):
        from models import DifficultyAttributes

        return DifficultyAttributes(
            beatmap_id=beatmap_id,
            star_rating=5.0,
            aim_difficulty=4.0,
            speed_difficulty=1.0,
        )

    def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        self.requests_made += 1
        self.http_attempts += 1
        return SearchPage(beatmapsets=(), cursor_string=None)


def test_multiple_pages_return_distinct_ids_and_complete_funnel(storage):
    api = CursorApi(per_page=50, pages=5)
    engine = DiscoveryEngine(
        api,
        storage,
        _settings(max_pages_per_strategy=5, max_requests=10),
    )
    result = engine.run(
        Filters(statuses=["ranked"]),
        RankingSettings(),
        record=False,
    )
    assert result.raw_rows == 250
    assert result.total_fetched == 250
    assert result.unique_raw == 250
    assert result.passed_filters == 250
    assert result.count == 250
    assert result.complete is True
    assert result.stop_reason == "complete"
    assert result.strategy_runs[0].pages == 5
    assert result.strategy_runs[0].raw_rows == 250
    assert result.strategy_runs[0].cursor_advances == 4
    assert result.strategy_runs[0].exhausted is True
    assert len(result.scored) == 250
    assert len({item.beatmapset.id for item in result.scored}) == 250
    assert sum(run.raw_rows for run in result.strategy_runs) == result.raw_rows
    assert sum(run.pages for run in result.strategy_runs) == result.pages_attempted
    assert sum(result.filter_excluded.values()) == (
        result.unique_raw - result.passed_filters
    )


def test_api_pages_with_partial_strategy_overlap_are_deduped(storage):
    class Api(CursorApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            # Both ranked and loved strategies return a shared page plus
            # strategy-specific rows.
            if query.status == "ranked":
                beatmapsets = (make_beatmapset(1), make_beatmapset(2))
            else:
                beatmapsets = (make_beatmapset(2), make_beatmapset(3))
            return SearchPage(beatmapsets=beatmapsets, cursor_string=None)

    result = DiscoveryEngine(Api(), storage, _settings()).run(
        Filters(statuses=["ranked", "loved"]),
        RankingSettings(),
        record=False,
    )
    assert result.raw_rows == 4
    assert result.unique_raw == 3
    assert {item.beatmapset.id for item in result.scored} == {1, 2, 3}
    assert result.strategy_runs[0].raw_rows == 2
    assert result.strategy_runs[1].raw_rows == 2


def test_only_twenty_five_matching_candidates_is_reported_honestly(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            self.requests_made += 1
            self.http_attempts += 1
            return SearchPage(
                beatmapsets=tuple(
                    make_beatmapset(index) for index in range(1, 26)
                ),
                cursor_string=None,
            )

    result = DiscoveryEngine(Api(), storage, _settings()).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert result.raw_rows == 25
    assert result.unique_raw == 25
    assert result.count == 25
    assert result.complete is True
    assert result.strategy_runs[0].exhausted is True


def test_fresh_search_bypasses_cached_pages_but_warms_cache_again(storage):
    api = CursorApi(per_page=2, pages=1)
    settings = _settings(max_requests=10, max_pages_per_strategy=1)
    first = DiscoveryEngine(api, storage, settings).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert first.api_requests == 1
    before_calls = len(api.search_calls)
    fresh = DiscoveryEngine(api, storage, settings).run(
        Filters(statuses=["ranked"]),
        RankingSettings(),
        record=False,
        fresh=True,
    )
    assert len(api.search_calls) == before_calls + 1
    assert fresh.cache_hits == 0
    assert fresh.api_requests == 1
    cached = DiscoveryEngine(api, storage, settings).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert cached.api_requests == 0
    assert cached.cache_hits == 1


def test_cached_first_page_then_uncached_deeper_page(storage):
    api = CursorApi(per_page=2, pages=2)
    first = DiscoveryEngine(
        api,
        storage,
        _settings(max_pages_per_strategy=1),
    ).run(Filters(statuses=["ranked"]), RankingSettings(), record=False)
    assert first.api_requests == 1
    second = DiscoveryEngine(
        api,
        storage,
        _settings(max_pages_per_strategy=2),
    ).run(Filters(statuses=["ranked"]), RankingSettings(), record=False)
    assert second.cache_hits >= 1
    assert second.api_requests == 1
    assert second.unique_raw == 4


def test_cache_key_includes_query_and_cursor_but_not_plain_cursor_param():
    first = SearchQuery(query="Cut Ver")
    second = SearchQuery(query="Sped Up")
    third = SearchQuery(query="Cut Ver", cursor_string="next")
    assert _cache_key(first) != _cache_key(second)
    assert _cache_key(first) != _cache_key(third)
    assert "cursor_string" not in first.cache_params()
    assert first.to_params()["q"] == "Cut Ver"
    assert first.to_params()["nsfw"] == "hide"


def test_title_queries_are_planned_early_and_all_executed_with_budget(storage):
    settings = _settings(
        max_requests=4,
        max_pages_per_strategy=1,
        title_queries=["Cut Ver", "Nightcore & Cut Ver", "Sped Up"],
    )
    strategies = build_strategies(Filters(statuses=["ranked"]), settings)
    assert [item.name for item in strategies[:1]] == ["ranked:recent"]
    assert [item.query for item in strategies[1:4]] == [
        "Cut Ver",
        "Nightcore & Cut Ver",
        "Sped Up",
    ]
    api = EmptyApi()
    result = DiscoveryEngine(api, storage, settings).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    requested = [query.query for query in api.search_calls]
    assert "Cut Ver" in requested
    assert "Nightcore & Cut Ver" in requested
    assert "Sped Up" in requested
    assert result.strategy_runs[0].executed is True
    assert all(run.executed for run in result.strategy_runs if run.query)


def test_candidate_limit_is_explicit_and_not_a_hidden_display_cap(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            self.requests_made += 1
            self.http_attempts += 1
            return SearchPage(
                beatmapsets=tuple(
                    make_beatmapset(index) for index in range(1, 61)
                ),
                cursor_string=None,
            )

    result = DiscoveryEngine(
        Api(), storage, _settings(max_candidates=10)
    ).run(Filters(statuses=["ranked"]), RankingSettings(), record=False)
    assert result.raw_rows == 60
    assert result.unique_raw == 60
    assert result.passed_filters == 60
    assert result.count == 10
    assert result.candidate_limit_hit is True
    assert result.complete is False
    assert result.stop_reason == "candidate_limit"


def test_aim_farm_title_queries_use_different_outgoing_requests(storage):
    farm = presets.builtin_by_name("Aim Farm")
    assert farm is not None
    api = EmptyApi()
    DiscoveryEngine(api, storage, farm.discovery).run(
        farm.filters, farm.ranking, record=False
    )
    requested = {query.query for query in api.search_calls}
    assert {"Cut Ver", "Nightcore & Cut Ver", "Sped Up"} <= requested
    assert all(
        query.status == "ranked"
        for query in api.search_calls
        if query.query in {"Cut Ver", "Nightcore & Cut Ver", "Sped Up"}
    )


def test_empty_page_with_cursor_continues_to_the_next_page(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            self.requests_made += 1
            if query.cursor_string is None:
                return SearchPage(beatmapsets=(), cursor_string="next")
            return SearchPage(
                beatmapsets=(make_beatmapset(7),),
                cursor_string=None,
            )

    result = DiscoveryEngine(Api(), storage, _settings()).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert result.raw_rows == 1
    assert result.unique_raw == 1
    assert {item.beatmapset.id for item in result.scored} == {7}
    assert result.strategy_runs[0].pages == 2
    assert sum(result.relevance_excluded.values()) == (
        result.relevance_evaluated - result.passed_relevance
    )


def test_missing_cursor_exhausts_strategy_without_infinite_paging(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            self.requests_made += 1
            return SearchPage(
                beatmapsets=(make_beatmapset(1),),
                cursor_string=None,
            )

    result = DiscoveryEngine(Api(), storage, _settings()).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert result.complete is True
    assert result.strategy_runs[0].exhausted is True
    assert len(result.strategy_runs[0].params) > 0


def test_repeated_cursor_stops_with_an_explicit_reason(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            self.requests_made += 1
            return SearchPage(
                beatmapsets=(make_beatmapset(1),),
                cursor_string="same-cursor",
            )

    result = DiscoveryEngine(
        Api(), storage, _settings(max_pages_per_strategy=10)
    ).run(Filters(statuses=["ranked"]), RankingSettings(), record=False)
    assert result.strategy_runs[0].stop_reason == "repeated_cursor"
    assert result.strategy_runs[0].pages == 2
    assert not result.complete


def test_api_error_is_reported_in_the_funnel(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            self.requests_made += 1
            raise OsuServerError("osu! down", 503)

    result = DiscoveryEngine(Api(), storage, _settings()).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert result.count == 0
    assert result.complete is False
    assert result.stop_reason == "network_failure"
    assert result.strategy_runs[0].executed is True


def test_unexecuted_strategies_are_visible_in_the_funnel(storage):
    settings = _settings(
        max_requests=1,
        max_pages_per_strategy=1,
        title_queries=["Cut Ver", "Sped Up"],
    )
    result = DiscoveryEngine(EmptyApi(), storage, settings).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert len(result.strategy_runs) >= 3
    executed = [run for run in result.strategy_runs if run.executed]
    assert len(executed) == 1
    assert any(
        run.stop_reason == "not_attempted"
        for run in result.strategy_runs
        if not run.executed
    )
    assert result.complete is False
    assert result.stop_reason == "request_budget"


def test_page_error_marks_run_incomplete(storage):
    class Api(EmptyApi):
        def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
            self.search_calls.append(query)
            return SearchPage(
                beatmapsets=(make_beatmapset(1),),
                error="partial response",
            )

    result = DiscoveryEngine(Api(), storage, _settings()).run(
        Filters(statuses=["ranked"]), RankingSettings(), record=False
    )
    assert result.complete is False
    assert result.stop_reason == "api_error"
    assert result.strategy_runs[0].error == "partial response"


def test_title_queries_precede_secondary_status_strategies():
    settings = _settings(
        title_queries=["Cut Ver"],
        use_oldest=True,
        use_rating=True,
    )
    strategies = build_strategies(
        Filters(statuses=["ranked", "loved"]), settings
    )
    names = [item.name for item in strategies]
    assert names.index("title:Cut Ver") < names.index("loved:recent")
    assert names.index("title:Cut Ver") < names.index("ranked:oldest")
