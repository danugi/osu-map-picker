"""Strict filters.

A strict filter excludes a card. Soft preferences live in ``ranking``.
Missing values never silently pass a strict filter: if a bound is active and
the field is unknown, the card is excluded with a named reason. The single
exception is AR/OD/CS/HP, which are not guaranteed to be present in search
results; those checks are applied only when the value exists and the gap is
reported as ``unknown_fields`` / in Diagnostics.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date

from models import (
    DURATION_FIELDS,
    GENRES,
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
    for set_id in filters.exclude_beatmapset_ids:
        if set_id <= 0:
            raise FilterConfigError("ID beatmapset должен быть положительным")
    if len(filters.query) > 200:
        raise FilterConfigError("поисковый запрос длиннее 200 символов")


def _matches_any(value: str, needles: list[str]) -> bool:
    haystack = _norm(value)
    return any(_norm(needle) in haystack for needle in needles if _norm(needle))


def _published_date(beatmapset: Beatmapset) -> str | None:
    raw = beatmapset.ranked_date or beatmapset.submitted_date
    if not raw:
        return None
    if len(raw) >= 10:
        return raw[:10]
    return raw


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

    if filters.published_after or filters.published_before:
        published = _published_date(beatmapset)
        if published is None:
            return FilterDecision(False, "дата публикации неизвестна")
        if filters.published_after and published < filters.published_after:
            return FilterDecision(False, "опубликована раньше выбранной даты")
        if filters.published_before and published > filters.published_before:
            return FilterDecision(False, "опубликована позже выбранной даты")

    if filters.query:
        haystack = " ".join(
            [
                beatmapset.display_title,
                beatmapset.title,
                beatmapset.display_artist,
                beatmapset.artist,
                beatmapset.creator,
                " ".join(beatmapset.tags),
            ]
        )
        if _norm(filters.query) not in _norm(haystack):
            return FilterDecision(False, "не совпадает с текстовым запросом")

    difficulties = [b for b in beatmapset.beatmaps if b.mode in filters.modes]
    if not difficulties:
        return FilterDecision(
            False, f"нет сложности режима {', '.join(filters.modes)}"
        )

    checks = [
        (beatmap, *_check_beatmap(beatmap, filters)) for beatmap in difficulties
    ]
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
        lines.append(
            f"Дата: {filters.published_after or '—'}..{filters.published_before or '—'}"
        )
    if filters.include_genres:
        lines.append(
            "Жанры: "
            + ", ".join(GENRES.get(g, str(g)) for g in filters.include_genres)
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
