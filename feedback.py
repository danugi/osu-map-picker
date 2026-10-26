"""User feedback vocabulary and taste-profile derivation.

Different negative verdicts are intentionally kept independent:

* ``music_dislike`` affects the artist/genre affinity, not the aim score.
* ``mapping_dislike`` affects the mapper/tag affinity, not the music score.
* ``too_hard`` / ``too_easy`` describe difficulty, not taste.
* ``played`` and ``hide`` are workflow signals, not taste.
"""

from __future__ import annotations

import math
import unicodedata

from features import derive_features
from models import Beatmapset, SkillScore, TasteProfile
from skillsets import evaluate_skills

VERDICT_LABELS = {
    "like": "Кайф",
    "fine": "Норм",
    "boring": "Скучно / однообразно",
    "music_dislike": "Не нравится музыка",
    "mapping_dislike": "Не нравится маппинг",
    "too_hard": "Слишком сложно",
    "too_easy": "Слишком легко",
    "played": "Уже играл",
    "hide": "Не хочу видеть снова",
}

VERDICT_HELP = {
    "like": "Сильный положительный сигнал для вкуса и этой карты.",
    "fine": "Умеренно положительный сигнал.",
    "boring": "Штраф за однообразие; влияет на маппинг и вкус.",
    "music_dislike": "Минус исполнителю и жанру, но не aim-фактору.",
    "mapping_dislike": "Минус мапперу и паттернам, но не музыке.",
    "too_hard": "Не считается «не нравится»; влияет на комфорт сложности.",
    "too_easy": "Не считается «не нравится»; влияет на комфорт сложности.",
    "played": "Скрывает карту только при включённом фильтре «уже играл».",
    "hide": "Исключает карту из выдачи (не влияет на вкус).",
}

VERDICT_ORDER = (
    "like",
    "fine",
    "boring",
    "music_dislike",
    "mapping_dislike",
    "too_hard",
    "too_easy",
    "played",
    "hide",
)

# Direct effect on this map's ranking factor (0..1).
VERDICT_PREFERENCE = {
    "like": 1.0,
    "fine": 0.65,
    "boring": 0.15,
    "music_dislike": 0.40,
    "mapping_dislike": 0.10,
    "too_hard": 0.35,
    "too_easy": 0.40,
    "played": 0.30,
    "hide": 0.0,
}

# Which profile dimensions a verdict teaches us. Difficulty verdicts teach
# nothing about taste, by design.
_TASTE_EFFECTS: dict[str, dict[str, float]] = {
    "like": {"artist": 1.0, "creator": 0.7, "genre": 0.5, "tags": 0.3},
    "fine": {"artist": 0.25, "creator": 0.2, "genre": 0.1, "tags": 0.05},
    "boring": {"artist": -0.4, "creator": -0.6, "genre": -0.2, "tags": -0.3},
    "music_dislike": {"artist": -0.8, "genre": -0.6},
    "mapping_dislike": {"creator": -0.8, "tags": -0.3},
    "too_hard": {},
    "too_easy": {},
    "played": {},
    "hide": {},
}

_TASTE_VERDICTS = {"like", "fine", "boring", "music_dislike", "mapping_dislike"}

# Skillset learning is deliberately soft and based on the top skills of the
# rated map. Music verdicts teach nothing about skills. Difficulty verdicts
# are handled by the comfort factor, not here.
_SKILL_TASTE_EFFECTS = {
    "like": 0.50,
    "fine": 0.20,
    "boring": -0.35,
    "mapping_dislike": -0.50,
    "music_dislike": 0.0,
}


def normalize_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def is_hidden(verdicts: tuple[str, ...] | list[str]) -> bool:
    return "hide" in verdicts


def is_played(verdicts: tuple[str, ...] | list[str]) -> bool:
    return "played" in verdicts


def preference_value(verdicts: tuple[str, ...] | list[str]) -> float | None:
    known = [VERDICT_PREFERENCE[v] for v in verdicts if v in VERDICT_PREFERENCE]
    if not known:
        return None
    return min(known)


def verdict_summary(verdicts: tuple[str, ...] | list[str]) -> str:
    return ", ".join(VERDICT_LABELS.get(v, v) for v in VERDICT_ORDER if v in verdicts)


def _clamp_affinity(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(-1.0, min(1.0, value))


def build_taste_profile(
    feedback_by_set: dict[int, tuple[str, ...]],
    sets_by_id: dict[int, Beatmapset],
) -> TasteProfile:
    artist: dict[str, float] = {}
    creator: dict[str, float] = {}
    genre: dict[int, float] = {}
    tag: dict[str, float] = {}
    skill_raw: dict[str, float] = {}
    skill_counts: dict[str, int] = {}
    liked: list[tuple[int, str, str]] = []
    disliked: list[tuple[int, str, str]] = []
    rated_sets = 0

    for set_id, verdicts in feedback_by_set.items():
        beatmapset = sets_by_id.get(set_id)
        if beatmapset is None:
            continue
        taste_verdicts = [v for v in verdicts if v in _TASTE_VERDICTS]
        if not taste_verdicts:
            continue
        rated_sets += 1
        artist_key = normalize_key(beatmapset.display_artist)
        creator_key = normalize_key(beatmapset.creator)
        for verdict in taste_verdicts:
            effects = _TASTE_EFFECTS.get(verdict, {})
            if "artist" in effects and artist_key:
                artist[artist_key] = artist.get(artist_key, 0.0) + effects["artist"]
            if "creator" in effects and creator_key:
                creator[creator_key] = creator.get(creator_key, 0.0) + effects["creator"]
            if "genre" in effects and beatmapset.genre_id:
                genre[beatmapset.genre_id] = (
                    genre.get(beatmapset.genre_id, 0.0) + effects["genre"]
                )
            if "tags" in effects:
                for name in beatmapset.tags:
                    key = normalize_key(name)
                    if key:
                        tag[key] = tag.get(key, 0.0) + effects["tags"]

        beatmap = next(
            (item for item in beatmapset.beatmaps if item.mode == "osu"), None
        )
        if beatmap is not None:
            skills = evaluate_skills(derive_features(beatmap, beatmapset))
            important = sorted(
                (
                    item
                    for item in skills.values()
                    if item.score is not None and item.score >= 0.55
                ),
                key=lambda item: (-(item.score or 0.0), item.skill_id),
            )[:2]
            for verdict in taste_verdicts:
                effect = _SKILL_TASTE_EFFECTS.get(verdict, 0.0)
                if effect == 0.0:
                    continue
                for skill in important:
                    skill_raw[skill.skill_id] = (
                        skill_raw.get(skill.skill_id, 0.0)
                        + effect * (skill.score or 0.0)
                    )
                    skill_counts[skill.skill_id] = (
                        skill_counts.get(skill.skill_id, 0) + 1
                    )

        entry = (beatmapset.id, beatmapset.display_artist, beatmapset.display_title)
        if "like" in verdicts:
            liked.append(entry)
        if {"boring", "music_dislike", "mapping_dislike"} & set(verdicts):
            disliked.append(entry)

    if rated_sets == 0:
        confidence = "none"
    elif rated_sets < 3:
        confidence = "low"
    elif rated_sets < 10:
        confidence = "medium"
    else:
        confidence = "high"

    skill_affinity = {
        key: _clamp_affinity(value / max(1, skill_counts.get(key, 1)))
        for key, value in skill_raw.items()
    }

    notes: list[str]
    if confidence == "none":
        notes = [
            "Оценок вкуса ещё нет. Рекомендации используют только фильтры, "
            "выбранные навыки и слабые сигналы сообщества."
        ]
    elif confidence == "low":
        notes = [
            f"Оценено карт: {rated_sets}. Этого мало для уверенных выводов — "
            "фактор вкуса не применяется, пока не наберётся 3 карты."
        ]
    else:
        notes = [f"Оценено карт: {rated_sets}. Профиль строится по вашим оценкам."]
    if skill_affinity:
        notes.append(
            "Предпочтения по навыкам учитываются мягко и только при ≥3 "
            "содержательных оценках; одна карта не делает выводов."
        )
    notes.append("«Уже играл» и «Не хочу видеть» не считаются оценкой вкуса.")

    return TasteProfile(
        rated_sets=rated_sets,
        liked=tuple(liked[:20]),
        disliked=tuple(disliked[:20]),
        artist_affinity={
            key: _clamp_affinity(value) for key, value in artist.items()
        },
        creator_affinity={
            key: _clamp_affinity(value) for key, value in creator.items()
        },
        genre_affinity={key: _clamp_affinity(v) for key, v in genre.items()},
        tag_affinity={key: _clamp_affinity(value) for key, value in tag.items()},
        skill_affinity=skill_affinity,
        skill_counts=dict(skill_counts),
        confidence=confidence,
        notes=tuple(notes),
        ignored_verdicts=("played", "hide", "too_hard", "too_easy"),
    )


def taste_matches(
    beatmapset: Beatmapset, profile: TasteProfile
) -> list[tuple[str, float]]:
    """Return ``(label, affinity)`` for dimensions this map matches."""
    matches: list[tuple[str, float]] = []
    artist = profile.artist_affinity.get(normalize_key(beatmapset.display_artist))
    if artist is not None:
        matches.append((f"исполнитель {beatmapset.display_artist}", artist))
    creator = profile.creator_affinity.get(normalize_key(beatmapset.creator))
    if creator is not None:
        matches.append((f"маппер {beatmapset.creator}", creator))
    if beatmapset.genre_id is not None:
        genre = profile.genre_affinity.get(beatmapset.genre_id)
        if genre is not None:
            matches.append((f"жанр {beatmapset.genre_name or beatmapset.genre_id}", genre))
    best_tag: tuple[str, float] | None = None
    for name in beatmapset.tags:
        affinity = profile.tag_affinity.get(normalize_key(name))
        if affinity is None:
            continue
        if best_tag is None or abs(affinity) > abs(best_tag[1]):
            best_tag = (name, affinity)
    if best_tag:
        matches.append((f"тег {best_tag[0]}", best_tag[1]))
    return matches


def taste_similarity(beatmapset: Beatmapset, profile: TasteProfile) -> float | None:
    if not profile.enough_data:
        return None
    weights = {"исполнитель": 0.5, "маппер": 0.35, "жанр": 0.1, "тег": 0.05}
    matches = taste_matches(beatmapset, profile)
    if not matches:
        return 0.5
    total = 0.0
    weight_sum = 0.0
    for label, affinity in matches:
        for prefix, weight in weights.items():
            if label.startswith(prefix):
                total += weight * (0.5 + 0.5 * affinity)
                weight_sum += weight
                break
    if weight_sum == 0:
        return 0.5
    return max(0.0, min(1.0, total / weight_sum))


def skill_taste_similarity(
    skillsets: dict[str, SkillScore], profile: TasteProfile
) -> float | None:
    """Soft skillset preference from feedback; neutral when unmatched.

    This is intentionally not a claim that a disliked skillset is objectively
    bad or easy. It only reflects the user's stated experience.
    """
    if not profile.enough_data or not profile.skill_affinity or not skillsets:
        return None
    weighted = 0.0
    total = 0.0
    for skill in skillsets.values():
        if skill.score is None or skill.score < 0.45:
            continue
        affinity = profile.skill_affinity.get(skill.skill_id)
        if affinity is None:
            continue
        weighted += skill.score * (0.5 + 0.5 * affinity)
        total += skill.score
    if total <= 0:
        return 0.5
    return max(0.0, min(1.0, weighted / total))
