"""Strict filters.

A strict filter excludes a card. Soft preferences live in ``ranking``.
Missing values never silently pass a strict filter: if a bound is active and
the field is unknown, the card is excluded with a named reason. The single
exception is AR/OD/CS/HP, which are not guaranteed to be present in search
results; those checks are applied only when the value exists and the gap is
reported as ``unknown_fields`` / in Diagnostics.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

from models import (
    DURATION_FIELDS,
    GENRES,
    LANGUAGES,
    MODES,
    STAR_MODES,
    STATUSES,
    Beatmap,
    Beatmapset,
    Filters,
)

UNKNOWN = "неизвестно"


class FilterConfigError(ValueError):
    """Raised for an invalid filter combination (min > max, bad status, ...)."""


@dataclass(frozen=True)
class UserFilterData:
    seen: frozenset[int] = field(default_factory=frozenset)
    played: frozenset[int] = field(default_factory=frozenset)
    favorites: frozenset[int] = field(default_factory=frozenset)
    hidden: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class FilterDecision:
    passed: bool
    reason: str | None = None
    beatmap: Beatmap | None = None
    within_tolerance: bool = True
    unknown_fields: tuple[str, ...] = ()


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


# --------------------------------------------------------------------------
# Text query grammar.
#
# The raw query is forwarded to the API unchanged, and the API understands
# its own operator syntax: quoted phrases, ``-negatives``, ``artist=...``
# and numeric comparisons like ``stars<5``. The old local check required
# the raw string to appear as one contiguous substring, so every operator
# query returned API hits that were then all discarded locally (silent zero
# results). The local check now parses the same grammar; anything it cannot
# evaluate is left to the API instead of being required as literal text.
# --------------------------------------------------------------------------

_TEXT_FIELDS = ("artist", "creator", "mapper", "title")
_DATE_FIELDS = ("from", "to")
_DATE_NAME = r"(ranked|submitted|created)"
_DATE_TEXT = r"(\d{4}-\d{2}(?:-\d{2})?)"
_DATE_OP = re.compile(
    rf"^{_DATE_NAME}(<=|>=|<|>|=){_DATE_TEXT}$"
)
_NUMERIC_FIELDS = ("stars", "ar", "od", "cs", "hp", "bpm", "length")
_NUMERIC_OP = re.compile(
    r"^(stars|ar|od|cs|hp|bpm|length)(<=|>=|<|>|=)(\d+(?:\.\d+)?)$"
)
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")


@dataclass(frozen=True)
class ParsedQuery:
    """Structured pieces of a search query, mirroring osu! web semantics."""

    phrases: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    negations: tuple[str, ...] = ()
    fields: tuple[tuple[str, str], ...] = ()
    negated_fields: tuple[tuple[str, str], ...] = ()
    numbers: tuple[tuple[str, str, float], ...] = ()
    dates: tuple[tuple[str, str], ...] = ()
    date_conditions: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class _Token:
    text: str
    negated: bool = False


def _tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    buf: list[str] = []
    in_quotes = False
    for ch in query:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch.isspace() and not in_quotes:
            if buf:
                tokens.append(_Token("".join(buf)))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append(_Token("".join(buf)))
    result: list[_Token] = []
    for token in tokens:
        if token.text.startswith("-") and len(token.text) > 1:
            result.append(_Token(token.text[1:], negated=True))
        else:
            result.append(token)
    return result


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


@lru_cache(maxsize=256)
def parse_query(query: str) -> ParsedQuery:
    """Parse ``q`` grammar: phrases, negatives, fields, numeric comparisons."""
    phrases: list[str] = []
    terms: list[str] = []
    negations: list[str] = []
    fields: list[tuple[str, str]] = []
    negated_fields: list[tuple[str, str]] = []
    numbers: list[tuple[str, str, float]] = []
    dates: list[tuple[str, str]] = []
    date_conditions: list[tuple[str, str, str]] = []

    for token in _tokenize(query):
        raw = token.text
        if raw.startswith('"'):
            # Covers both "phrases" and -"negated phrases" (the dash was
            # stripped during tokenization).
            phrase = _strip_quotes(raw)
            if not phrase:
                continue
            (negations if token.negated else phrases).append(phrase.casefold())
            continue
        date_match = _DATE_OP.match(raw)
        if date_match and not token.negated:
            date_conditions.append(
                (date_match.group(1), date_match.group(2), date_match.group(3))
            )
            continue
        op_match = _NUMERIC_OP.match(raw)
        if op_match and not token.negated:
            numbers.append((op_match.group(1), op_match.group(2), float(op_match.group(3))))
            continue
        if '=' in raw:
            left, _, right = raw.partition('=')
            key = left.lower().strip()
            value = _strip_quotes(right).strip()
            if not value:
                terms.append(_norm(raw))
                continue
            if key in _TEXT_FIELDS:
                canonical = "creator" if key == "mapper" else key
                if token.negated:
                    negated_fields.append((canonical, _norm(value)))
                else:
                    fields.append((canonical, _norm(value)))
            elif key in _DATE_FIELDS and _DATE_VALUE.match(value):
                dates.append((key, value))
            elif key in _NUMERIC_FIELDS and _NUMERIC_OP.match(f"{key}={value}"):
                numbers.append((key, "=", float(value)))
            # Unknown operators (played=, ranked=, ...) are evaluated by the
            # API; the local check must not require them as literal text.
            continue
        word = _strip_quotes(raw)
        if not word:
            continue
        if any(ch in word for ch in "<>="):
            # Unrecognized operator syntax (played>10, ranked<2020, ...) is
            # evaluated by the API; the local check must not require it as
            # literal text and must not fail every local match.
            continue
        (negations if token.negated else terms).append(_norm(word))
    return ParsedQuery(
        phrases=tuple(phrases),
        terms=tuple(terms),
        negations=tuple(negations),
        fields=tuple(fields),
        negated_fields=tuple(negated_fields),
        numbers=tuple(numbers),
        dates=tuple(dates),
        date_conditions=tuple(date_conditions),
    )


def _normalize_query_date(value: str, operator: str) -> str:
    """Expand a YYYY-MM operand to the inclusive bound implied by operator."""

    if len(value) != 7:
        return value
    year, month = (int(part) for part in value.split("-"))
    if operator in {">=", "<"}:
        return f"{year:04d}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def _query_text_reason(
    parsed: ParsedQuery,
    beatmapset: Beatmapset,
    *,
    published_date_field: str = "ranked",
) -> str | None:
    """Set-level query conditions (text and dates). None means it passes."""
    # Fields are joined with a character that cannot occur in the data, so a
    # phrase can never match across two different fields (e.g. the end of the
    # title plus the beginning of the artist).
    title_hay = _norm(f"{beatmapset.display_title}\n{beatmapset.title}")
    artist_hay = _norm(f"{beatmapset.display_artist}\n{beatmapset.artist}")
    creator_hay = _norm(beatmapset.creator)
    tags_hay = _norm("\n".join(beatmapset.tags))
    haystack = "\n".join((title_hay, artist_hay, creator_hay, tags_hay))

    field_hays = {
        "artist": artist_hay,
        "title": title_hay,
        "creator": creator_hay,
    }
    for phrase in parsed.phrases:
        if phrase not in haystack:
            return "не совпадает с текстовым запросом"
    for term in parsed.terms:
        if term not in haystack:
            return "не совпадает с текстовым запросом"
    for negation in parsed.negations:
        if negation in haystack:
            return f"совпадает с исключаемым «{negation}»"
    for key, value in parsed.fields:
        if value not in field_hays.get(key, ""):
            return "не совпадает с текстовым запросом"
    for key, value in parsed.negated_fields:
        if value in field_hays.get(key, ""):
            return f"совпадает с исключаемым «{key}={value}»"
    for key, value in parsed.dates:
        actual = _published_date(beatmapset, published_date_field)
        if actual is None:
            return "дата публикации неизвестна"
        bound = _normalize_query_date(
            value, ">=" if key == "from" else "<="
        )
        if key == "from" and actual < bound:
            return "опубликована раньше даты запроса"
        if key == "to" and actual > bound:
            return "опубликована позже даты запроса"
    for field_name, operator, value in parsed.date_conditions:
        value = _normalize_query_date(value, operator)
        actual = _published_date(beatmapset, field_name)
        if actual is None:
            label = (
                "сабмита"
                if field_name in {"submitted", "created"}
                else "рейтинга"
            )
            return f"дата {label} неизвестна"
        if operator == ">=" and actual < value:
            return "дата запроса не выполнена"
        if operator == ">" and actual <= value:
            return "дата запроса не выполнена"
        if operator == "<=" and actual > value:
            return "дата запроса не выполнена"
        if operator == "<" and actual >= value:
            return "дата запроса не выполнена"
        if operator == "=" and actual != value:
            return "дата запроса не выполнена"
    return None


def _check_query_numeric(
    beatmap: Beatmap,
    parsed: ParsedQuery,
    filters: Filters,
) -> tuple[list[str], list[str]]:
    """Per-difficulty numeric query conditions (stars/ar/bpm/...).

    Mirrors the strict-filter rules: stars/BPM/length are always present in
    search results, a missing value fails; AR/OD/CS/HP are optional, so a
    missing value is reported as unknown instead of excluding the card.
    """
    failures: list[str] = []
    unknown: list[str] = []
    for field, op, value in parsed.numbers:
        expr = f"{field}{op}{value:g}"
        reason = f"запрос не выполнен ({expr})"
        if field == "stars":
            actual = beatmap.difficulty_rating
            missing = actual is None
        elif field == "length":
            actual = beatmap.duration(filters.duration_field)
            missing = actual is None
        elif field in ("ar", "od", "cs", "hp"):
            actual = getattr(beatmap, field)
            if actual is None:
                unknown.append(field.upper())
                continue
            missing = False
        else:
            actual = beatmap.bpm
            missing = actual is None
        if missing:
            failures.append(reason)
            continue
        assert actual is not None
        if op == "<" and not actual < value:
            failures.append(reason)
        elif op == "<=" and not actual <= value:
            failures.append(reason)
        elif op == ">" and not actual > value:
            failures.append(reason)
        elif op == ">=" and not actual >= value:
            failures.append(reason)
        elif op == "=" and not actual == value:
            failures.append(reason)
    return failures, unknown


def _check_range(
    low: float | None,
    high: float | None,
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1000.0,
) -> None:
    for value, label in ((low, "min"), (high, "max")):
        if value is None:
            continue
        if value < minimum or value > maximum:
            raise FilterConfigError(
                f"{name}.{label} должен быть в диапазоне {minimum:g}..{maximum:g}"
            )
    if low is not None and high is not None and low > high:
        raise FilterConfigError(f"{name}: минимум больше максимума")


def _check_date(value: str | None, name: str) -> None:
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise FilterConfigError(f"{name}: ожидалась дата YYYY-MM-DD") from exc


def validate_filters(filters: Filters) -> None:
    if not filters.modes:
        raise FilterConfigError("выберите хотя бы один режим")
    unknown_modes = set(filters.modes) - set(MODES)
    if unknown_modes:
        raise FilterConfigError(f"неизвестные режимы: {', '.join(sorted(unknown_modes))}")
    if not filters.statuses:
        raise FilterConfigError("выберите хотя бы один статус карт")
    unknown_statuses = set(filters.statuses) - set(STATUSES)
    if unknown_statuses:
        raise FilterConfigError(
            f"неизвестные статусы: {', '.join(sorted(unknown_statuses))}"
        )
    if filters.star_mode not in STAR_MODES:
        raise FilterConfigError(f"star_mode должен быть одним из {STAR_MODES}")
    if filters.duration_field not in DURATION_FIELDS:
        raise FilterConfigError(
            f"duration_field должен быть одним из {DURATION_FIELDS}"
        )
    if filters.star_tolerance < 0 or filters.star_tolerance > 3:
        raise FilterConfigError("star_tolerance должен быть в диапазоне 0..3")

    _check_range(filters.stars_min, filters.stars_max, "stars", minimum=0, maximum=20)
    _check_range(
        filters.duration_min,
        filters.duration_max,
        "duration",
        minimum=0,
        maximum=3600,
    )
    _check_range(filters.bpm_min, filters.bpm_max, "bpm", minimum=0, maximum=1000)
    for name in ("ar", "od", "cs", "hp"):
        _check_range(
            getattr(filters, f"{name}_min"),
            getattr(filters, f"{name}_max"),
            name,
            minimum=0,
            maximum=11,
        )
    if filters.published_date_field not in {"ranked", "submitted"}:
        raise FilterConfigError(
            "published_date_field должен быть ranked или submitted"
        )
    _check_date(filters.published_after, "published_after")
    _check_date(filters.published_before, "published_before")
    if (
        filters.published_after
        and filters.published_before
        and filters.published_after > filters.published_before
    ):
        raise FilterConfigError("published_after позже published_before")
    unknown_genres = (set(filters.include_genres) | set(filters.exclude_genres)) - set(
        GENRES
    )
    if unknown_genres:
        raise FilterConfigError(
            f"неизвестные жанры: {', '.join(str(g) for g in sorted(unknown_genres))}"
        )
    unknown_languages = (
        set(filters.include_languages) | set(filters.exclude_languages)
    ) - set(LANGUAGES)
    if unknown_languages:
        raise FilterConfigError(
            f"неизвестные языки: {', '.join(str(g) for g in sorted(unknown_languages))}"
        )
    for set_id in filters.exclude_beatmapset_ids:
        if set_id <= 0:
            raise FilterConfigError("ID beatmapset должен быть положительным")
    if len(filters.query) > 200:
        raise FilterConfigError("поисковый запрос длиннее 200 символов")


def _matches_any(value: str, needles: list[str]) -> bool:
    haystack = _norm(value)
    return any(_norm(needle) in haystack for needle in needles if _norm(needle))


def _published_date(
    beatmapset: Beatmapset,
    field_name: str = "ranked",
) -> str | None:
    raw = (
        beatmapset.submitted_date
        if field_name in {"submitted", "created"}
        else beatmapset.ranked_date
    )
    if not raw:
        return None
    return raw[:10] if len(raw) >= 10 else raw


def api_date_expression(filters: "Filters") -> str | None:
    """Build the osu! search date clause for the selected date meaning.

    The website/API grammar compares date fields inclusively with ``>=`` and
    exclusively with ``<``. The UI's upper bound is inclusive, so it becomes
    ``field<next_day``.
    """

    # osu!'s advanced-search grammar names the submission timestamp
    # ``created`` (mapped to Elasticsearch submit_date). It also accepts
    # ``submitted`` as an alias; emit the canonical key.
    field_name = (
        "created" if filters.published_date_field == "submitted" else "ranked"
    )
    parts: list[str] = []
    if filters.published_after:
        parts.append(f"{field_name}>={filters.published_after}")
    if filters.published_before:
        try:
            next_day = date.fromisoformat(filters.published_before) + timedelta(days=1)
        except ValueError:
            return None
        parts.append(f"{field_name}<{next_day.isoformat()}")
    return " ".join(parts) or None


def _star_range(filters: Filters) -> tuple[float | None, float | None]:
    if filters.star_mode == "off":
        return None, None
    if filters.star_mode == "soft":
        low = (
            filters.stars_min - filters.star_tolerance
            if filters.stars_min is not None
            else None
        )
        high = (
            filters.stars_max + filters.star_tolerance
            if filters.stars_max is not None
            else None
        )
        return low, high
    return filters.stars_min, filters.stars_max


def _check_beatmap(
    beatmap: Beatmap,
    filters: Filters,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    unknown: list[str] = []

    low, high = _star_range(filters)
    stars = beatmap.difficulty_rating
    if low is not None or high is not None:
        if stars is None:
            failures.append("звёздность неизвестна")
        else:
            if low is not None and stars < low:
                failures.append("звёздность вне диапазона")
            if high is not None and stars > high:
                failures.append("звёздность вне диапазона")

    if filters.duration_min is not None or filters.duration_max is not None:
        duration = beatmap.duration(filters.duration_field)
        if duration is None:
            failures.append("длительность неизвестна")
        else:
            if filters.duration_min is not None and duration < filters.duration_min:
                failures.append("длительность вне диапазона")
            if filters.duration_max is not None and duration > filters.duration_max:
                failures.append("длительность вне диапазона")

    if filters.bpm_min is not None or filters.bpm_max is not None:
        bpm = beatmap.bpm
        if bpm is None:
            failures.append("BPM неизвестен")
        else:
            if filters.bpm_min is not None and bpm < filters.bpm_min:
                failures.append("BPM вне диапазона")
            if filters.bpm_max is not None and bpm > filters.bpm_max:
                failures.append("BPM вне диапазона")

    for name in ("ar", "od", "cs", "hp"):
        low_value = getattr(filters, f"{name}_min")
        high_value = getattr(filters, f"{name}_max")
        if low_value is None and high_value is None:
            continue
        value = getattr(beatmap, name)
        if value is None:
            # Search results do not always include these fields. We do not
            # guess them: the check is skipped and the gap is reported.
            unknown.append(name.upper())
            continue
        if low_value is not None and value < low_value:
            failures.append(f"{name.upper()} вне диапазона")
        if high_value is not None and value > high_value:
            failures.append(f"{name.upper()} вне диапазона")

    return failures, unknown


def _within_star_range(beatmap: Beatmap, filters: Filters) -> bool:
    if filters.star_mode == "off":
        return True
    stars = beatmap.difficulty_rating
    if stars is None:
        return False
    low = filters.stars_min
    high = filters.stars_max
    if low is not None and stars < low:
        return False
    if high is not None and stars > high:
        return False
    return True


def apply_filters(
    beatmapset: Beatmapset,
    filters: Filters,
    user: UserFilterData | None = None,
    target_stars: float | None = None,
) -> FilterDecision:
    """Apply strict filters and choose one representative difficulty."""
    user = user or UserFilterData()

    if beatmapset.id in filters.exclude_beatmapset_ids:
        return FilterDecision(False, "в списке исключённых вами")
    if beatmapset.id in user.hidden:
        return FilterDecision(False, "скрыто оценкой «не хочу видеть»")
    if filters.only_favorites and beatmapset.id not in user.favorites:
        return FilterDecision(False, "нет в избранном")
    if filters.hide_favorites and beatmapset.id in user.favorites:
        return FilterDecision(False, "уже в избранном")
    if filters.hide_seen and beatmapset.id in user.seen:
        return FilterDecision(False, "уже показывалась")
    if filters.hide_played and beatmapset.id in user.played:
        return FilterDecision(False, "уже играли")
    if beatmapset.status not in filters.statuses:
        return FilterDecision(False, f"статус: {beatmapset.status}")
    if filters.exclude_nsfw and beatmapset.nsfw:
        return FilterDecision(False, "NSFW исключено")

    artist = beatmapset.display_artist
    if filters.include_artists and not _matches_any(artist, filters.include_artists):
        return FilterDecision(False, "исполнитель не входит в избранные")
    if filters.exclude_artists and _matches_any(artist, filters.exclude_artists):
        return FilterDecision(False, "исполнитель исключён")
    if filters.include_mappers and not _matches_any(
        beatmapset.creator, filters.include_mappers
    ):
        return FilterDecision(False, "маппер не входит в избранные")
    if filters.exclude_mappers and _matches_any(
        beatmapset.creator, filters.exclude_mappers
    ):
        return FilterDecision(False, "маппер исключён")

    if filters.include_genres:
        if beatmapset.genre_id is None:
            return FilterDecision(False, "жанр неизвестен")
        if beatmapset.genre_id not in filters.include_genres:
            return FilterDecision(False, "жанр не входит в избранные")
    if filters.exclude_genres and beatmapset.genre_id in filters.exclude_genres:
        return FilterDecision(False, "жанр исключён")

    if filters.include_languages:
        if beatmapset.language_id is None:
            return FilterDecision(False, "язык неизвестен")
        if beatmapset.language_id not in filters.include_languages:
            return FilterDecision(False, "язык не входит в избранные")
    if filters.exclude_languages and beatmapset.language_id in filters.exclude_languages:
        return FilterDecision(False, "язык исключён")

    parsed_query = parse_query(filters.query) if filters.query else None
    if parsed_query is not None:
        text_reason = _query_text_reason(
            parsed_query,
            beatmapset,
            published_date_field=filters.published_date_field,
        )
        if text_reason:
            return FilterDecision(False, text_reason)

    if filters.published_after or filters.published_before:
        date_label = (
            "сабмита"
            if filters.published_date_field == "submitted"
            else "рейтинга"
        )
        published = _published_date(beatmapset, filters.published_date_field)
        if published is None:
            return FilterDecision(False, f"дата {date_label} неизвестна")
        if filters.published_after and published < filters.published_after:
            return FilterDecision(False, f"дата {date_label} раньше выбранной")
        if filters.published_before and published > filters.published_before:
            return FilterDecision(False, f"дата {date_label} позже выбранной")

    difficulties = [b for b in beatmapset.beatmaps if b.mode in filters.modes]
    if not difficulties:
        return FilterDecision(
            False, f"нет сложности режима {', '.join(filters.modes)}"
        )

    checks = [
        (beatmap, *_check_beatmap(beatmap, filters)) for beatmap in difficulties
    ]
    if parsed_query is not None and parsed_query.numbers:
        merged: list[tuple[Beatmap, list[str], list[str]]] = []
        for beatmap, failures, unknown in checks:
            query_failures, query_unknown = _check_query_numeric(
                beatmap, parsed_query, filters
            )
            # Query failures come first so diagnostics name the query
            # condition that disqualified the difficulty.
            merged.append(
                (beatmap, query_failures + failures, unknown + query_unknown)
            )
        checks = merged
    evaluated = [item for item in checks if not item[1]]
    if evaluated:
        def _key(item: tuple[Beatmap, list[str], list[str]]) -> tuple[float, float]:
            beatmap = item[0]
            stars = beatmap.difficulty_rating
            if stars is None:
                return (1_000.0, -float(beatmap.id))
            if target_stars is None:
                return (-stars, -float(beatmap.id))
            return (abs(stars - target_stars), -stars)

        chosen, _, unknown = sorted(evaluated, key=_key)[0]
        return FilterDecision(
            True,
            None,
            chosen,
            within_tolerance=_within_star_range(chosen, filters),
            unknown_fields=tuple(sorted(set(unknown))),
        )

    # No difficulty passes. Report the reason from the difficulty that failed
    # the fewest checks, which keeps diagnostics useful.
    best = sorted(checks, key=lambda item: (len(item[1]), -float(item[0].id)))[0]
    reason = best[1][0] if best[1] else "нет подходящей сложности"
    return FilterDecision(False, reason)


def active_filter_summary(filters: Filters) -> list[str]:
    """Short human-readable list for the Search page."""
    lines: list[str] = []
    mode_names = {"osu": "osu!standard", "taiko": "taiko", "catch": "catch", "mania": "mania"}
    lines.append("Режим: " + ", ".join(mode_names.get(m, m) for m in filters.modes))
    lines.append(
        "Статусы: " + ", ".join(filters.statuses) if filters.statuses else "Статусы: —"
    )
    star_text = "★ без ограничения"
    if filters.star_mode != "off" and (filters.stars_min is not None or filters.stars_max is not None):
        left = f"{filters.stars_min:.1f}" if filters.stars_min is not None else "—"
        right = f"{filters.stars_max:.1f}" if filters.stars_max is not None else "—"
        star_text = f"★ {left}..{right}"
        if filters.star_mode == "soft":
            star_text += f" (мягко, ±{filters.star_tolerance:.1f})"
    lines.append(star_text)
    if filters.duration_max is not None or filters.duration_min is not None:
        left = f"{filters.duration_min:g}с" if filters.duration_min is not None else "0с"
        right = (
            f"{filters.duration_max:g}с" if filters.duration_max is not None else "∞"
        )
        field_label = (
            "total_length" if filters.duration_field == "total_length" else "hit_length"
        )
        lines.append(f"Длительность ({field_label}): {left}..{right}")
    if filters.bpm_min is not None or filters.bpm_max is not None:
        lines.append(
            f"BPM: {filters.bpm_min or '—'}..{filters.bpm_max or '—'}"
        )
    if filters.published_after or filters.published_before:
        label = "сабмита" if filters.published_date_field == "submitted" else "рейтинга"
        lines.append(
            f"Дата {label}: "
            f"{filters.published_after or '—'}..{filters.published_before or '—'}"
        )
    if filters.include_genres:
        lines.append(
            "Жанры: "
            + ", ".join(GENRES.get(g, str(g)) for g in filters.include_genres)
        )
    if filters.include_languages:
        lines.append(
            "Языки: "
            + ", ".join(LANGUAGES.get(l, str(l)) for l in filters.include_languages)
        )
    if filters.exclude_artists:
        lines.append("Исключены исполнители: " + ", ".join(filters.exclude_artists))
    if filters.exclude_mappers:
        lines.append("Исключены мапперы: " + ", ".join(filters.exclude_mappers))
    if filters.query:
        lines.append(f"Текст: {filters.query}")
    if filters.hide_seen:
        lines.append("Скрывать уже показанные")
    if filters.hide_played:
        lines.append("Скрывать уже сыгранные")
    return lines
