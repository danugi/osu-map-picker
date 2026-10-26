"""Shared pytest fixtures and small builders for tests.

No test touches the network: API tests use ``httpx.MockTransport`` and
discovery tests use a stub client. User data lives in a temporary SQLite file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import presets
from models import Beatmap, Beatmapset
from storage import Storage


def make_beatmap(
    beatmap_id: int,
    beatmapset_id: int,
    *,
    stars: float | None = 5.0,
    mode: str = "osu",
    version: str = "Insane",
    total_length: int | None = 120,
    hit_length: int | None = 110,
    bpm: float | None = 180.0,
    ar: float | None = 9.0,
    od: float | None = 8.0,
    cs: float | None = 4.0,
    hp: float | None = 5.0,
    max_combo: int | None = 900,
    count_circles: int | None = 200,
    count_sliders: int | None = 100,
    count_spinners: int | None = 1,
    status: str | None = "ranked",
    owners: tuple[str, ...] = ("Mapper",),
) -> Beatmap:
    return Beatmap(
        id=beatmap_id,
        beatmapset_id=beatmapset_id,
        mode=mode,
        version=version,
        difficulty_rating=stars,
        total_length=total_length,
        hit_length=hit_length,
        bpm=bpm,
        ar=ar,
        od=od,
        cs=cs,
        hp=hp,
        max_combo=max_combo,
        count_circles=count_circles,
        count_sliders=count_sliders,
        count_spinners=count_spinners,
        status=status,
        url=f"https://osu.ppy.sh/b/{beatmap_id}",
        owners=owners,
    )


def make_beatmapset(
    beatmapset_id: int,
    *,
    artist: str = "Artist",
    artist_unicode: str = "",
    title: str = "Title",
    title_unicode: str = "",
    creator: str = "Mapper",
    status: str = "ranked",
    bpm: float | None = 180.0,
    genre_id: int | None = 10,
    language_id: int | None = 3,
    tags: tuple[str, ...] = (),
    nsfw: bool = False,
    favourite_count: int = 120,
    play_count: int = 12_000,
    rating: float | None = 8.4,
    ranked_date: str | None = "2021-05-05T00:00:00+00:00",
    submitted_date: str | None = "2021-01-01T00:00:00+00:00",
    fetched_at: str = "2026-01-01T00:00:00+00:00",
    beatmaps: tuple[Beatmap, ...] | None = None,
) -> Beatmapset:
    if beatmaps is None:
        beatmaps = (make_beatmap(beatmapset_id * 10 + 1, beatmapset_id),)
    return Beatmapset(
        id=beatmapset_id,
        artist=artist,
        artist_unicode=artist_unicode,
        title=title,
        title_unicode=title_unicode,
        creator=creator,
        status=status,
        bpm=bpm,
        favourite_count=favourite_count,
        play_count=play_count,
        rating=rating,
        genre_id=genre_id,
        language_id=language_id,
        tags=tags,
        nsfw=nsfw,
        video=False,
        storyboard=False,
        preview_url=f"https://example.invalid/{beatmapset_id}.mp3",
        cover_url=f"https://example.invalid/{beatmapset_id}.jpg",
        ranked_date=ranked_date,
        submitted_date=submitted_date,
        last_updated=ranked_date,
        beatmaps=beatmaps,
        source="osu_api",
        fetched_at=fetched_at,
    )


@pytest.fixture()
def storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "osu_picker_test.db")
    presets.ensure_builtin_presets(store)
    return store


@pytest.fixture()
def empty_storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "empty.db")
