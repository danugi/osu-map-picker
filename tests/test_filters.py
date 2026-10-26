from __future__ import annotations

import pytest
from conftest import make_beatmap, make_beatmapset

from filters import (
    FilterConfigError,
    UserFilterData,
    apply_filters,
    validate_filters,
)
from models import Filters


def make_filters(**overrides) -> Filters:
    values = Filters().to_dict()
    values.update(overrides)
    return Filters.from_dict(values)


def test_default_profile_passes_short_ranked_map() -> None:
    decision = apply_filters(make_beatmapset(1), Filters(), target_stars=5.4)
    assert decision.passed
    assert decision.beatmap is not None
    assert decision.beatmap.difficulty_rating == 5.0


def test_star_minimum_excludes_with_reason() -> None:
    decision = apply_filters(make_beatmapset(1), make_filters(stars_min=5.5))
    assert not decision.passed
    assert decision.reason == "звёздность вне диапазона"


def test_star_maximum_excludes_with_reason() -> None:
    decision = apply_filters(make_beatmapset(1), make_filters(stars_max=4.5))
    assert not decision.passed
    assert decision.reason == "звёздность вне диапазона"


def test_off_mode_allows_unknown_stars() -> None:
    beatmapset = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, stars=None),)
    )
    decision = apply_filters(
        beatmapset,
        make_filters(star_mode="off", stars_min=None, stars_max=None),
    )
    assert decision.passed
    assert decision.beatmap is not None


def test_strict_mode_excludes_unknown_stars() -> None:
    beatmapset = make_beatmapset(1, beatmaps=(make_beatmap(11, 1, stars=None),))
    decision = apply_filters(beatmapset, Filters())
    assert not decision.passed
    assert decision.reason == "звёздность неизвестна"


def test_soft_mode_allows_outside_range_with_flag() -> None:
    beatmapset = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, stars=4.5),)
    )
    decision = apply_filters(
        beatmapset,
        make_filters(
            stars_min=5.0,
            stars_max=6.0,
            star_mode="soft",
            star_tolerance=0.6,
        ),
    )
    assert decision.passed
    assert decision.within_tolerance is False


def test_soft_mode_still_excludes_beyond_tolerance() -> None:
    beatmapset = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, stars=4.0),)
    )
    decision = apply_filters(
        beatmapset,
        make_filters(
            stars_min=5.0,
            stars_max=6.0,
            star_mode="soft",
            star_tolerance=0.6,
        ),
    )
    assert not decision.passed


def test_duration_uses_selected_field() -> None:
    beatmapset = make_beatmapset(
        1,
        beatmaps=(
            make_beatmap(
                11, 1, total_length=220, hit_length=170, stars=5.0
            ),
        ),
    )
    assert not apply_filters(beatmapset, Filters()).passed
    assert apply_filters(
        beatmapset, make_filters(duration_field="hit_length")
    ).passed


def test_duration_minimum() -> None:
    decision = apply_filters(make_beatmapset(1), make_filters(duration_min=130))
    assert not decision.passed
    assert decision.reason == "длительность вне диапазона"


def test_unknown_duration_is_excluded_when_bound_active() -> None:
    beatmapset = make_beatmapset(
        1,
        beatmaps=(make_beatmap(11, 1, total_length=None, hit_length=None),),
    )
    decision = apply_filters(beatmapset, Filters())
    assert not decision.passed
    assert decision.reason == "длительность неизвестна"


def test_status_filter() -> None:
    beatmapset = make_beatmapset(1, status="pending")
    decision = apply_filters(beatmapset, make_filters(statuses=["ranked"]))
    assert not decision.passed
    assert decision.reason == "статус: pending"
    assert apply_filters(
        beatmapset, make_filters(statuses=["ranked", "pending"])
    ).passed


def test_non_osu_difficulty_is_excluded() -> None:
    beatmapset = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, mode="taiko"),)
    )
    decision = apply_filters(beatmapset, Filters())
    assert not decision.passed
    assert "нет сложности режима" in (decision.reason or "")


def test_excluded_artist_and_mapper() -> None:
    beatmapset = make_beatmapset(1, artist="DJ TOTTO", creator="Nevo")
    assert not apply_filters(
        beatmapset, make_filters(exclude_artists=["totto"])
    ).passed
    assert not apply_filters(
        beatmapset, make_filters(exclude_mappers=["NEVO"])
    ).passed
    assert apply_filters(
        beatmapset, make_filters(include_artists=["dj"])
    ).passed


def test_genre_filters() -> None:
    beatmapset = make_beatmapset(1, genre_id=5)
    assert apply_filters(beatmapset, make_filters(include_genres=[5])).passed
    assert not apply_filters(
        beatmapset, make_filters(include_genres=[10])
    ).passed
    assert not apply_filters(
        beatmapset, make_filters(exclude_genres=[5])
    ).passed


def test_unknown_genre_with_include_filter_is_excluded() -> None:
    beatmapset = make_beatmapset(1, genre_id=None)
    decision = apply_filters(beatmapset, make_filters(include_genres=[5]))
    assert not decision.passed
    assert decision.reason == "жанр неизвестен"


def test_nsfw_filter() -> None:
    beatmapset = make_beatmapset(1, nsfw=True)
    assert not apply_filters(beatmapset, Filters()).passed
    assert apply_filters(beatmapset, make_filters(exclude_nsfw=False)).passed


def test_user_flags_hidden_seen_played_favorites() -> None:
    beatmapset = make_beatmapset(1)
    user = UserFilterData(hidden=frozenset({1}))
    assert not apply_filters(beatmapset, Filters(), user).passed
    user = UserFilterData(seen=frozenset({1}))
    assert not apply_filters(beatmapset, make_filters(hide_seen=True), user).passed
    user = UserFilterData(played=frozenset({1}))
    assert not apply_filters(
        beatmapset, make_filters(hide_played=True), user
    ).passed
    user = UserFilterData(favorites=frozenset({1}))
    assert apply_filters(
        beatmapset, make_filters(only_favorites=True), user
    ).passed
    assert not apply_filters(
        beatmapset, make_filters(only_favorites=True), UserFilterData()
    ).passed


def test_unknown_ar_passes_and_is_reported() -> None:
    beatmapset = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, ar=None),)
    )
    decision = apply_filters(beatmapset, make_filters(ar_min=9.0))
    assert decision.passed
    assert decision.unknown_fields == ("AR",)


def test_known_ar_out_of_range_fails() -> None:
    beatmapset = make_beatmapset(
        1, beatmaps=(make_beatmap(11, 1, ar=8.0),)
    )
    decision = apply_filters(beatmapset, make_filters(ar_min=9.0))
    assert not decision.passed
    assert decision.reason == "AR вне диапазона"


def test_target_stars_selects_closest_difficulty() -> None:
    beatmapset = make_beatmapset(
        1,
        beatmaps=(
            make_beatmap(11, 1, stars=4.2, version="Easy"),
            make_beatmap(12, 1, stars=6.5, version="Hard"),
        ),
    )
    decision = apply_filters(beatmapset, Filters(), target_stars=5.0)
    assert decision.passed
    assert decision.beatmap is not None
    assert decision.beatmap.difficulty_rating == 4.2


def test_without_target_picks_highest_passing_difficulty() -> None:
    beatmapset = make_beatmapset(
        1,
        beatmaps=(
            make_beatmap(11, 1, stars=4.2),
            make_beatmap(12, 1, stars=6.5),
        ),
    )
    decision = apply_filters(beatmapset, Filters())
    assert decision.beatmap is not None
    assert decision.beatmap.difficulty_rating == 6.5


def test_text_query_matches_unicode_title() -> None:
    beatmapset = make_beatmapset(1, title="Crystalia", artist="DJ TOTTO")
    assert apply_filters(
        beatmapset, make_filters(query="crysta")
    ).passed
    assert not apply_filters(
        beatmapset, make_filters(query="palette")
    ).passed


@pytest.mark.parametrize(
    "overrides",
    [
        {"stars_min": 7.0, "stars_max": 4.0},
        {"statuses": []},
        {"statuses": ["nonsense"]},
        {"modes": ["nonsense"]},
        {"star_mode": "maybe"},
        {"duration_field": "song_length"},
        {"star_tolerance": -1},
        {"ar_min": 12},
        {"include_genres": [999]},
        {"published_after": "not-a-date"},
        {"published_after": "2024-01-01", "published_before": "2023-01-01"},
        {"exclude_beatmapset_ids": [0]},
    ],
)
def test_invalid_filter_configuration(overrides: dict) -> None:
    with pytest.raises(FilterConfigError):
        validate_filters(make_filters(**overrides))


def test_filter_from_dict_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        Filters.from_dict({"stars_min": 4.0, "unknown_field": 1})
