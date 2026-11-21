from __future__ import annotations

from conftest import make_beatmapset

from features import derive_features
from feedback import (
    build_taste_profile,
    preference_value,
    skill_taste_similarity,
    taste_similarity,
    verdict_summary,
)
from models import Beatmapset
from skillsets import evaluate_skills


def sets() -> dict[int, Beatmapset]:
    return {
        1: make_beatmapset(
            1, artist="DJ TOTTO", creator="Nevo", genre_id=10, tags=("aim",)
        ),
        2: make_beatmapset(
            2, artist="BadArtist", creator="BadMapper", genre_id=5, tags=("pop",)
        ),
        3: make_beatmapset(
            3, artist="ThirdArtist", creator="ThirdMapper", genre_id=4, tags=("rock",)
        ),
    }


def test_music_dislike_affects_music_not_mapper() -> None:
    profile = build_taste_profile({2: ("music_dislike",)}, sets())
    assert profile.artist_affinity["badartist"] == -0.8
    assert "badmapper" not in profile.creator_affinity
    assert profile.genre_affinity[5] == -0.6


def test_mapping_dislike_affects_mapper_not_artist() -> None:
    profile = build_taste_profile({2: ("mapping_dislike",)}, sets())
    assert profile.creator_affinity["badmapper"] == -0.8
    assert "badartist" not in profile.artist_affinity
    assert 5 not in profile.genre_affinity


def test_too_hard_is_not_taste() -> None:
    profile = build_taste_profile({2: ("too_hard", "too_easy", "played")}, sets())
    assert profile.rated_sets == 0
    assert profile.confidence == "none"
    assert profile.artist_affinity == {}
    assert profile.creator_affinity == {}


def test_confidence_thresholds_and_no_false_conclusions() -> None:
    one = build_taste_profile({1: ("like",)}, sets())
    assert one.rated_sets == 1
    assert one.confidence == "low"
    assert not one.enough_data
    assert taste_similarity(sets()[1], one) is None

    three = build_taste_profile(
        {1: ("like",), 2: ("boring",), 3: ("like",)}, sets()
    )
    assert three.confidence == "medium"
    assert three.enough_data
    assert taste_similarity(sets()[1], three) is not None


def test_like_creates_positive_affinities() -> None:
    profile = build_taste_profile({1: ("like",)}, sets())
    assert profile.artist_affinity["dj totto"] == 1.0
    assert profile.creator_affinity["nevo"] == 0.7
    assert profile.genre_affinity[10] == 0.5
    assert profile.liked[0][0] == 1
    assert profile.disliked == ()


def test_boring_is_a_negative_taste_signal() -> None:
    profile = build_taste_profile({1: ("boring",)}, sets())
    assert profile.artist_affinity["dj totto"] < 0
    assert profile.creator_affinity["nevo"] < 0
    assert profile.disliked[0][0] == 1


def test_preference_value_uses_minimum() -> None:
    assert preference_value(("like",)) == 1.0
    assert preference_value(("like", "too_hard")) == 0.35
    assert preference_value(()) is None
    assert preference_value(("unknown",)) is None


def test_verdict_summary_in_russian() -> None:
    text = verdict_summary(("music_dislike", "like"))
    assert "Кайф" in text
    assert "Не нравится музыка" in text


def test_taste_similarity_matching_artist_above_neutral() -> None:
    profile = build_taste_profile(
        {1: ("like",), 2: ("boring",), 3: ("like",)}, sets()
    )
    same_artist = make_beatmapset(9, artist="DJ TOTTO", creator="Other")
    stranger = make_beatmapset(
        10, artist="Nobody", creator="Nobody", genre_id=1, tags=()
    )
    assert taste_similarity(same_artist, profile) > 0.5
    assert taste_similarity(stranger, profile) == 0.5


def test_skill_affinity_is_soft_and_requires_enough_ratings() -> None:
    one = build_taste_profile({1: ("like",)}, sets())
    assert not one.enough_data
    target = sets()[1]
    skills = evaluate_skills(
        derive_features(target.beatmaps[0], target)
    )
    assert skill_taste_similarity(skills, one) is None

    enough = build_taste_profile(
        {1: ("like",), 2: ("like",), 3: ("like",)}, sets()
    )
    assert enough.enough_data
    assert enough.skill_affinity
    similarity = skill_taste_similarity(skills, enough)
    assert similarity is not None
    assert 0.5 <= similarity <= 1.0


def test_music_dislike_does_not_create_skill_affinity() -> None:
    profile = build_taste_profile(
        {1: ("music_dislike",), 2: ("music_dislike",), 3: ("music_dislike",)},
        sets(),
    )
    assert profile.skill_affinity == {}
