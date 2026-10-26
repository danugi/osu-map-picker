from __future__ import annotations

import httpx
import pytest

import config
from models import SearchQuery
from osu_api import (
    OsuApiClient,
    OsuApiError,
    OsuAuthError,
    OsuForbiddenError,
    OsuNetworkError,
    OsuRateLimitError,
    OsuSchemaError,
    OsuServerError,
)


def token_response(token: str = "token-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": token, "expires_in": 3600, "token_type": "Bearer"},
    )


def search_payload(
    ids: list[int],
    *,
    cursor_string: str | None = None,
    cursor: dict | None = None,
) -> dict:
    sets = []
    for set_id in ids:
        sets.append(
            {
                "id": set_id,
                "artist": "DJ TOTTO",
                "artist_unicode": "DJ TOTTO",
                "title": "Crystalia",
                "title_unicode": "Crystalia",
                "creator": "Nevo",
                "status": "ranked",
                "bpm": 180.0,
                "favourite_count": 123,
                "play_count": 4567,
                "rating": 8.5,
                "genre": {"id": 10, "name": "Electronic"},
                "language": {"id": 3, "name": "Japanese"},
                "tags": "aim jumps electronic",
                "nsfw": False,
                "video": False,
                "storyboard": False,
                "preview_url": "https://example.invalid/preview.mp3",
                "covers": {"cover": "https://example.invalid/cover.jpg"},
                "ranked_date": "2021-05-05T00:00:00+00:00",
                "submitted_date": "2021-01-01T00:00:00+00:00",
                "last_updated": "2021-05-05T00:00:00+00:00",
                "beatmaps": [
                    {
                        "id": set_id * 10 + 1,
                        "beatmapset_id": set_id,
                        "mode": "osu",
                        "mode_int": 0,
                        "version": "Insane",
                        "difficulty_rating": 5.4,
                        "total_length": 150,
                        "hit_length": 140,
                        "bpm": 180.0,
                        "ar": 9.3,
                        "accuracy": 8.5,
                        "cs": 4.0,
                        "drain": 5.0,
                        "max_combo": 800,
                        "count_circles": 200,
                        "count_sliders": 80,
                        "count_spinners": 1,
                        "status": "ranked",
                        "url": f"https://osu.ppy.sh/b/{set_id * 10 + 1}",
                        "owners": [{"id": 1, "username": "Nevo"}],
                    }
                ],
            }
        )
    payload: dict = {"beatmapsets": sets, "total": len(sets)}
    if cursor_string:
        payload["cursor_string"] = cursor_string
    if cursor:
        payload["cursor"] = cursor
    return payload


def client_with(handler, *, max_retries: int = 2) -> tuple[OsuApiClient, list[float]]:
    sleeps: list[float] = []
    transport = httpx.MockTransport(handler)
    api = OsuApiClient(
        config.ApiConfig(
            client_id="123",
            client_secret="super-secret",
            timeout=5,
            min_interval=0.0,
            max_retries=max_retries,
        ),
        transport=transport,
        sleep=lambda seconds: sleeps.append(seconds),
    )
    return api, sleeps


def test_successful_search_and_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oauth/token":
            assert b"client_credentials" in request.content
            assert b"super-secret" in request.content
            return token_response()
        assert request.url.path == "/api/v2/beatmapsets/search"
        assert request.headers["Authorization"] == "Bearer token-1"
        return httpx.Response(200, json=search_payload([42]))

    api, _ = client_with(handler)
    page = api.search_beatmapsets(
        SearchQuery(mode="osu", status="ranked", sort="ranked_desc")
    )
    assert len(page.beatmapsets) == 1
    beatmapset = page.beatmapsets[0]
    assert beatmapset.id == 42
    assert beatmapset.title_unicode == "Crystalia"
    assert beatmapset.genre_id == 10
    assert beatmapset.beatmaps[0].ar == 9.3
    assert beatmapset.beatmaps[0].od == 8.5
    assert beatmapset.beatmaps[0].url == "https://osu.ppy.sh/b/421"

    search_request = [r for r in seen if r.url.path.endswith("/search")][0]
    assert search_request.url.params["m"] == "0"
    assert search_request.url.params["s"] == "ranked"
    assert search_request.url.params["sort"] == "ranked_desc"
    assert search_request.url.params["nsfw"] == "hide"


def test_token_is_reused_between_requests() -> None:
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return token_response()
        return httpx.Response(200, json=search_payload([1]))

    api, _ = client_with(handler)
    api.search_beatmapsets(SearchQuery())
    api.search_beatmapsets(SearchQuery(status="loved"))
    assert token_calls == 1


def test_expired_or_rejected_token_triggers_one_refresh() -> None:
    token_calls = 0
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, search_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return token_response(f"token-{token_calls}")
        search_calls += 1
        if search_calls == 1:
            return httpx.Response(401, json={"error": "invalid_token"})
        assert request.headers["Authorization"] == "Bearer token-2"
        return httpx.Response(200, json=search_payload([7]))

    api, _ = client_with(handler)
    page = api.search_beatmapsets(SearchQuery())
    assert [b.id for b in page.beatmapsets] == [7]
    assert token_calls == 2


def test_repeated_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(401, json={"error": "invalid_token"})

    api, _ = client_with(handler)
    with pytest.raises(OsuAuthError):
        api.search_beatmapsets(SearchQuery())


def test_bad_client_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        return httpx.Response(400, json={"error": "invalid_client"})

    api, _ = client_with(handler)
    with pytest.raises(OsuAuthError) as info:
        api.search_beatmapsets(SearchQuery())
    assert ".env" in info.value.user_message()


def test_403_raises_forbidden_without_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/oauth/token":
            return token_response()
        calls += 1
        return httpx.Response(403, json={"error": "access denied"})

    api, _ = client_with(handler)
    with pytest.raises(OsuForbiddenError) as info:
        api.search_beatmapsets(SearchQuery())
    assert info.value.status_code == 403
    assert calls == 1


def test_429_is_retried_with_retry_after() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/oauth/token":
            return token_response()
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=search_payload([9]))

    api, sleeps = client_with(handler)
    page = api.search_beatmapsets(SearchQuery())
    assert [b.id for b in page.beatmapsets] == [9]
    assert sleeps == [1.0]


def test_429_exhausts_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(429, headers={"Retry-After": "0.5"})

    api, _ = client_with(handler, max_retries=1)
    with pytest.raises(OsuRateLimitError):
        api.search_beatmapsets(SearchQuery())


def test_5xx_retried_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/oauth/token":
            return token_response()
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=search_payload([3]))

    api, sleeps = client_with(handler)
    page = api.search_beatmapsets(SearchQuery())
    assert page.beatmapsets[0].id == 3
    assert len(sleeps) == 2


def test_5xx_exhausts_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(500)

    api, _ = client_with(handler, max_retries=0)
    with pytest.raises(OsuServerError):
        api.search_beatmapsets(SearchQuery())


def test_network_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        raise httpx.ConnectError("no network", request=request)

    api, _ = client_with(handler, max_retries=0)
    with pytest.raises(OsuNetworkError):
        api.search_beatmapsets(SearchQuery())


def test_missing_beatmapsets_field_is_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(200, json={"error": "something"})

    api, _ = client_with(handler)
    with pytest.raises(OsuSchemaError):
        api.search_beatmapsets(SearchQuery())


def test_empty_response_is_valid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(200, json={"beatmapsets": []})

    api, _ = client_with(handler)
    page = api.search_beatmapsets(SearchQuery())
    assert page.beatmapsets == ()
    assert page.total is None


def test_malformed_item_is_skipped_with_warning() -> None:
    payload = search_payload([5])
    payload["beatmapsets"].insert(0, {"title": "no id here"})
    payload["beatmapsets"][1]["beatmaps"].insert(0, {"version": "broken"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        return httpx.Response(200, json=payload)

    api, _ = client_with(handler)
    page = api.search_beatmapsets(SearchQuery())
    assert [b.id for b in page.beatmapsets] == [5]
    assert len(page.warnings) == 1


def test_pagination_uses_cursor_string() -> None:
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        seen_params.append(dict(request.url.params))
        if len(seen_params) == 1:
            return httpx.Response(
                200, json=search_payload([1], cursor_string="next-page")
            )
        return httpx.Response(200, json=search_payload([2]))

    api, _ = client_with(handler)
    first = api.search_beatmapsets(SearchQuery())
    assert first.cursor_string == "next-page"
    second = api.search_beatmapsets(SearchQuery(cursor_string=first.cursor_string))
    assert second.beatmapsets[0].id == 2
    assert seen_params[1]["cursor_string"] == "next-page"


def test_cursor_object_is_forwarded() -> None:
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json=search_payload([1]))

    api, _ = client_with(handler)
    api.search_beatmapsets(SearchQuery(cursor={"page": 2}))
    assert seen_params[0]["cursor[page]"] == "2"


def test_attributes_flat_and_nested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return token_response()
        assert request.url.path == "/api/v2/beatmaps/123/attributes"
        return httpx.Response(
            200,
            json={
                "attributes": {
                    "star_rating": 6.1,
                    "aim_difficulty": 3.0,
                    "speed_difficulty": 1.0,
                    "slider_factor": 1.2,
                    "max_combo": 1000,
                }
            },
        )

    api, _ = client_with(handler)
    attributes = api.get_beatmap_attributes(123)
    assert attributes.star_rating == 6.1
    assert attributes.aim_difficulty == 3.0
    assert abs((attributes.aim_share or 0) - 0.75) < 1e-9
    assert attributes.max_combo == 1000


def test_check_connection_does_not_expose_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response("very-secret-token")

    api, _ = client_with(handler)
    result = api.check_connection()
    assert result["ok"] is True
    assert "very-secret-token" not in str(result)
    assert result["token"]["present"] is True


def test_auth_error_body_is_not_leaked_in_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client_secret"})

    api, _ = client_with(handler)
    with pytest.raises(OsuAuthError) as info:
        api.search_beatmapsets(SearchQuery())
    assert "super-secret" not in str(info.value)
