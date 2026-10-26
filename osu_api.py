"""Isolated client for the official osu! API v2.

Everything that depends on osu! API quirks lives in this module:

* OAuth 2.0 client-credentials token handling (cached in memory only);
* rate limiting (osu! asks for <= 60 requests/minute);
* retries with backoff for 429 / 5xx / network errors;
* defensive parsing of API payloads (missing fields are not invented).

Reference: https://osu.ppy.sh/docs/ . Note that the beatmapset search
endpoint is documented as ``TODO`` there; see ``SearchQuery`` and the
README section "API uncertainty".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

import httpx

import config
from models import (
    Beatmap,
    Beatmapset,
    DifficultyAttributes,
    ModelError,
    SearchPage,
    SearchQuery,
    now_iso,
)

logger = logging.getLogger("osu_picker.api")

_RANK_STATUS_BY_INT = {
    -2: "graveyard",
    -1: "wip",
    0: "pending",
    1: "ranked",
    2: "approved",
    3: "qualified",
    4: "loved",
}
_MODE_BY_INT = {0: "osu", 1: "taiko", 2: "catch", 3: "mania"}
_MODE_BY_NAME = {name: name for name in ("osu", "taiko", "catch", "mania")}


class OsuApiError(Exception):
    """Base class for every error raised by the API client."""

    def __init__(self, message: str, status_code: int | None = None, hint: str = ""):
        self.message = message
        self.status_code = status_code
        self.hint = hint
        super().__init__(message)

    def user_message(self) -> str:
        text = self.message
        if self.hint:
            text += f" ({self.hint})"
        return text


class OsuAuthError(OsuApiError):
    """Token could not be obtained or was rejected."""


class OsuForbiddenError(OsuApiError):
    """HTTP 403 - the token is valid but not allowed to use this route."""


class OsuNotFoundError(OsuApiError):
    """HTTP 404 - the requested object does not exist."""


class OsuRateLimitError(OsuApiError):
    """HTTP 429 after all retries."""


class OsuServerError(OsuApiError):
    """HTTP 5xx after all retries."""


class OsuNetworkError(OsuApiError):
    """Network failure after all retries."""


class OsuSchemaError(OsuApiError):
    """The response did not have the shape we can safely work with."""


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _status_from(value: Any, fallback: Any = None) -> str:
    if isinstance(value, str) and value:
        return value
    as_int = _opt_int(value)
    if as_int is not None and as_int in _RANK_STATUS_BY_INT:
        return _RANK_STATUS_BY_INT[as_int]
    as_int = _opt_int(fallback)
    if as_int is not None and as_int in _RANK_STATUS_BY_INT:
        return _RANK_STATUS_BY_INT[as_int]
    return "unknown"


def _mode_from(value: Any, fallback: Any = None) -> str:
    if isinstance(value, str) and value in _MODE_BY_NAME:
        return value
    if isinstance(value, str) and value.isdigit():
        return _MODE_BY_INT.get(int(value), "osu")
    as_int = _opt_int(value)
    if as_int is not None and as_int in _MODE_BY_INT:
        return _MODE_BY_INT[as_int]
    as_int = _opt_int(fallback)
    if as_int is not None and as_int in _MODE_BY_INT:
        return _MODE_BY_INT[as_int]
    return "osu"


def _tags_from(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split() if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return ()


def _nested_id(value: Any) -> int | None:
    if isinstance(value, dict):
        return _opt_int(value.get("id"))
    return _opt_int(value)


def _cover_url(covers: Any) -> str:
    if isinstance(covers, dict):
        for key in ("cover", "card", "list", "slimcover"):
            url = covers.get(key)
            if isinstance(url, str) and url:
                return url
    if isinstance(covers, str):
        return covers
    return ""


def parse_beatmap(payload: Any, beatmapset_id: int) -> Beatmap:
    if not isinstance(payload, dict):
        raise OsuSchemaError("beatmap: ожидался объект")
    beatmap_id = _opt_int(payload.get("id"))
    if beatmap_id is None:
        raise OsuSchemaError("beatmap: отсутствует id")
    owners_raw = payload.get("owners")
    owners: list[str] = []
    if isinstance(owners_raw, list):
        for owner in owners_raw:
            if isinstance(owner, dict) and owner.get("username"):
                owners.append(str(owner["username"]))
            elif isinstance(owner, str):
                owners.append(owner)
    if not owners and isinstance(payload.get("user"), dict):
        username = payload["user"].get("username")
        if username:
            owners.append(str(username))
    return Beatmap(
        id=beatmap_id,
        beatmapset_id=beatmapset_id,
        mode=_mode_from(payload.get("mode"), payload.get("mode_int")),
        version=_opt_str(payload.get("version")),
        difficulty_rating=_opt_float(payload.get("difficulty_rating")),
        total_length=_opt_int(payload.get("total_length")),
        hit_length=_opt_int(payload.get("hit_length")),
        bpm=_opt_float(payload.get("bpm")),
        ar=_opt_float(payload.get("ar")),
        od=_opt_float(payload.get("accuracy")),
        cs=_opt_float(payload.get("cs")),
        hp=_opt_float(payload.get("drain")),
        max_combo=_opt_int(payload.get("max_combo")),
        count_circles=_opt_int(payload.get("count_circles")),
        count_sliders=_opt_int(payload.get("count_sliders")),
        count_spinners=_opt_int(payload.get("count_spinners")),
        status=_status_from(payload.get("status"), payload.get("ranked")),
        url=_opt_str(payload.get("url")) or None,
        owners=tuple(owners),
    )


def parse_beatmapset(payload: Any) -> Beatmapset:
    """Parse a Beatmapset object. Raises OsuSchemaError if unusable."""
    if not isinstance(payload, dict):
        raise OsuSchemaError("beatmapset: ожидался объект")
    set_id = _opt_int(payload.get("id"))
    if set_id is None:
        raise OsuSchemaError("beatmapset: отсутствует id")
    beatmaps: list[Beatmap] = []
    raw_beatmaps = payload.get("beatmaps")
    if isinstance(raw_beatmaps, list):
        for item in raw_beatmaps:
            try:
                beatmaps.append(parse_beatmap(item, set_id))
            except OsuSchemaError as exc:
                logger.debug("skipping malformed beatmap in set %s: %s", set_id, exc)
    genre_id = _nested_id(payload.get("genre")) or _opt_int(payload.get("genre_id"))
    language_id = _nested_id(payload.get("language")) or _opt_int(
        payload.get("language_id")
    )
    return Beatmapset(
        id=set_id,
        artist=_opt_str(payload.get("artist")),
        artist_unicode=_opt_str(payload.get("artist_unicode")),
        title=_opt_str(payload.get("title")),
        title_unicode=_opt_str(payload.get("title_unicode")),
        creator=_opt_str(payload.get("creator")),
        status=_status_from(payload.get("status"), payload.get("ranked")),
        bpm=_opt_float(payload.get("bpm")),
        favourite_count=_opt_int(payload.get("favourite_count")),
        play_count=_opt_int(payload.get("play_count")),
        rating=_opt_float(payload.get("rating")),
        genre_id=genre_id,
        language_id=language_id,
        tags=_tags_from(payload.get("tags")),
        nsfw=bool(payload.get("nsfw")),
        video=bool(payload.get("video")),
        storyboard=bool(payload.get("storyboard")),
        preview_url=_opt_str(payload.get("preview_url")),
        cover_url=_cover_url(payload.get("covers")),
        ranked_date=_opt_str(payload.get("ranked_date")) or None,
        submitted_date=_opt_str(payload.get("submitted_date")) or None,
        last_updated=_opt_str(payload.get("last_updated")) or None,
        beatmaps=tuple(beatmaps),
        source="osu_api",
        fetched_at=now_iso(),
    )


def parse_search_page(payload: Any) -> SearchPage:
    if not isinstance(payload, dict):
        raise OsuSchemaError("search: ожидался объект с полем beatmapsets")
    raw = payload.get("beatmapsets")
    if raw is None:
        raise OsuSchemaError("search: в ответе нет поля beatmapsets")
    if not isinstance(raw, list):
        raise OsuSchemaError("search: поле beatmapsets должно быть массивом")
    parsed: list[Beatmapset] = []
    warnings: list[str] = []
    for index, item in enumerate(raw):
        try:
            parsed.append(parse_beatmapset(item))
        except OsuSchemaError as exc:
            warnings.append(f"пропущен элемент #{index}: {exc}")
    cursor = payload.get("cursor")
    cursor_string = payload.get("cursor_string")
    total = _opt_int(payload.get("total"))
    recommended = _opt_float(payload.get("recommended_difficulty"))
    error = payload.get("error")
    return SearchPage(
        beatmapsets=tuple(parsed),
        cursor=cursor if isinstance(cursor, dict) else None,
        cursor_string=cursor_string if isinstance(cursor_string, str) else None,
        total=total,
        recommended_difficulty=recommended,
        error=str(error) if error else None,
        warnings=tuple(warnings),
        raw_count=len(raw),
    )


class OsuApiClient:
    """HTTP client with token caching, rate limiting and retries."""

    def __init__(
        self,
        api_config: config.ApiConfig,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        api_config.validate()
        self._config = api_config
        self._sleep = sleep
        self._monotonic = monotonic
        self._client = httpx.Client(transport=transport, timeout=api_config.timeout)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._next_request_at: float = 0.0
        self.last_error: str | None = None
        self.requests_made: int = 0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OsuApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def configured(self) -> bool:
        return self._config.configured

    def has_token(self) -> bool:
        return bool(self._token) and self._monotonic() < self._token_expires_at

    def token_debug_info(self) -> dict[str, Any]:
        """Safe summary of the token state (never exposes the token)."""
        if not self._token:
            return {"present": False}
        seconds_left = max(0.0, self._token_expires_at - self._monotonic())
        return {"present": True, "expires_in_seconds": int(seconds_left)}

    # -- token -------------------------------------------------------------
    def _get_token(self) -> str:
        if self.has_token():
            return self._token  # type: ignore[return-value]
        payload = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": "client_credentials",
            "scope": "public",
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        url = self._config.token_url
        try:
            response = self._client.post(url, data=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise OsuNetworkError(
                f"Не удалось запросить OAuth-токен: {exc.__class__.__name__}",
                hint="проверьте подключение к интернету",
            ) from exc
        self.requests_made += 1
        if response.status_code in (400, 401):
            raise OsuAuthError(
                "osu! отклонил Client ID/Client Secret",
                status_code=response.status_code,
                hint="проверьте OSU_CLIENT_ID и OSU_CLIENT_SECRET в .env",
            )
        if response.status_code >= 400:
            raise OsuAuthError(
                f"Не удалось авторизоваться: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise OsuAuthError("Ответ авторизации не является JSON") from exc
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise OsuAuthError("В ответе авторизации нет access_token")
        expires_in = _opt_int(data.get("expires_in")) or 3600
        ttl = max(30, expires_in - 60)
        self._token = token
        self._token_expires_at = self._monotonic() + ttl
        logger.info("OAuth client-credentials token получен (ttl ~%ss)", ttl)
        return token

    def invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    # -- low level requests ------------------------------------------------
    def _wait_for_slot(self) -> None:
        now = self._monotonic()
        if now < self._next_request_at:
            self._sleep(self._next_request_at - now)
        self._next_request_at = self._monotonic() + self._config.min_interval

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(0.5, min(30.0, float(header)))
            except ValueError:
                pass
        return min(20.0, 1.5 * (2 ** attempt))

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else self._config.api_base + path
        attempts = self._config.max_retries + 1
        last_exc: Exception | None = None
        token_retry_used = False
        for attempt in range(attempts):
            token = self._get_token()
            self._wait_for_slot()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                response = self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("%s %s: network error %s", method, path, exc.__class__.__name__)
                if attempt < attempts - 1:
                    self._sleep(min(20.0, 1.5 * (2 ** attempt)))
                    continue
                break
            self.requests_made += 1

            if response.status_code == 401:
                self.invalidate_token()
                self.last_error = "HTTP 401: токен отклонён"
                if not token_retry_used:
                    token_retry_used = True
                    logger.info("токен отклонён, повторная авторизация")
                    continue
                raise OsuAuthError(
                    "osu! отклонил токен после повторной авторизации",
                    status_code=401,
                    hint="проверьте OSU_CLIENT_ID и OSU_CLIENT_SECRET",
                )
            if response.status_code == 403:
                message = self._error_message(response)
                self.last_error = f"HTTP 403: {message}"
                raise OsuForbiddenError(
                    f"Доступ запрещён (HTTP 403): {message}",
                    status_code=403,
                    hint="роут может требовать пользовательский токен",
                )
            if response.status_code == 404:
                message = self._error_message(response)
                self.last_error = f"HTTP 404: {message}"
                raise OsuNotFoundError(f"Запись не найдена (HTTP 404): {message}", 404)
            if response.status_code == 429:
                wait = self._retry_after_seconds(response, attempt)
                self.last_error = "HTTP 429: превышен лимит запросов"
                logger.warning("rate limited, пауза %.1fs", wait)
                if attempt < attempts - 1:
                    self._sleep(wait)
                    continue
                raise OsuRateLimitError(
                    "osu! ограничил частоту запросов (HTTP 429)",
                    status_code=429,
                    hint="увеличьте OSU_MIN_INTERVAL_SECONDS или повторите позже",
                )
            if response.status_code >= 500:
                self.last_error = f"HTTP {response.status_code}: ошибка сервера osu!"
                logger.warning("сервер osu! вернул %s", response.status_code)
                if attempt < attempts - 1:
                    self._sleep(min(20.0, 1.5 * (2 ** attempt)))
                    continue
                raise OsuServerError(
                    f"osu! вернул ошибку сервера (HTTP {response.status_code})",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                message = self._error_message(response)
                self.last_error = f"HTTP {response.status_code}: {message}"
                raise OsuApiError(
                    f"Ошибка запроса (HTTP {response.status_code}): {message}",
                    status_code=response.status_code,
                )

            self.last_error = None
            try:
                return response.json()
            except ValueError as exc:
                raise OsuSchemaError("Ответ osu! API не является JSON") from exc

        if isinstance(last_exc, httpx.HTTPError):
            raise OsuNetworkError(
                f"Сетевая ошибка при обращении к osu! API: {last_exc.__class__.__name__}",
                hint="проверьте подключение к интернету",
            ) from last_exc
        raise OsuApiError("Не удалось выполнить запрос к osu! API")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            text = response.text[:200]
            return text or "без описания"
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, str) and error:
                return error[:200]
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:200]
        return "без описания"

    # -- public endpoints --------------------------------------------------
    def search_beatmapsets(self, query: SearchQuery) -> SearchPage:
        payload = self._request("GET", "/beatmapsets/search", params=query.to_params())
        return parse_search_page(payload)

    def get_beatmapset(self, beatmapset_id: int) -> Beatmapset:
        payload = self._request("GET", f"/beatmapsets/{int(beatmapset_id)}")
        if not isinstance(payload, dict):
            raise OsuSchemaError("beatmapset: ожидался объект")
        return parse_beatmapset(payload)

    def get_beatmap_attributes(
        self, beatmap_id: int, mods: Iterable[str] = ()
    ) -> DifficultyAttributes:
        body = {"mods": list(mods), "ruleset": "osu"}
        payload = self._request(
            "POST", f"/beatmaps/{int(beatmap_id)}/attributes", json_body=body
        )
        attributes: Any = payload
        if isinstance(payload, dict) and "attributes" in payload:
            attributes = payload["attributes"]
        if not isinstance(attributes, dict):
            raise OsuSchemaError("attributes: ожидался объект attributes")
        star = _opt_float(attributes.get("star_rating"))
        # The osu! ruleset response is normally flat; some deployments nest it
        # under "osu". Support both without inventing values.
        aim = _opt_float(attributes.get("aim_difficulty"))
        speed = _opt_float(attributes.get("speed_difficulty"))
        slider = _opt_float(attributes.get("slider_factor"))
        osu_block = attributes.get("osu")
        if isinstance(osu_block, dict):
            aim = _opt_float(osu_block.get("aim_difficulty")) or aim
            speed = _opt_float(osu_block.get("speed_difficulty")) or speed
            slider = _opt_float(osu_block.get("slider_factor")) or slider
        return DifficultyAttributes(
            beatmap_id=int(beatmap_id),
            star_rating=star,
            aim_difficulty=aim,
            speed_difficulty=speed,
            slider_factor=slider,
            max_combo=_opt_int(attributes.get("max_combo")),
        )

    def check_connection(self) -> dict[str, Any]:
        """Acquire a token and report safe diagnostics (no secrets)."""
        try:
            self._get_token()
        except OsuApiError as exc:
            return {
                "ok": False,
                "message": exc.user_message(),
                "status_code": exc.status_code,
            }
        info = self.token_debug_info()
        return {
            "ok": True,
            "message": "OAuth-токен получен, API доступен",
            "token": info,
        }


def try_parse_beatmapset(payload: Any) -> tuple[Beatmapset | None, str | None]:
    """Parse without raising; returns ``(beatmapset, error)`` for discovery."""
    try:
        return parse_beatmapset(payload), None
    except ModelError as exc:  # pragma: no cover - ModelError from models only
        return None, str(exc)
    except OsuSchemaError as exc:
        return None, str(exc)
