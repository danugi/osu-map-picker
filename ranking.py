"""Transparent heuristic ranking and result diversification.

The score is an internal ranking value (0..100), not a predicted
probability that the user will like a map. Every factor reports its value,
weight and explanation; missing data is marked as missing rather than
guessed. Strict filters are applied before ranking and are never softened
here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import config
from features import TRAITS, trait_similarity
from feedback import (
    VERDICT_LABELS,
    VERDICT_PREFERENCE,
    build_taste_profile,
    preference_value,
    skill_taste_similarity,
    taste_matches,
    taste_similarity,
)
from models import (
    GENRES,
    Beatmapset,
    Candidate,
    FactorConfig,
    FactorScore,
    RankingSettings,
    ScoredCandidate,
    TasteProfile,
)
from skillsets import (
    CONFIDENCE_LABELS,
    SUPPORT_LABELS,
    evaluate_skills,
    skill_fit,
    skill_id_valid,
)

FACTOR_ORDER = (
    "mapping_fit",
    "farm_nm",
    "difficulty",
    "taste",
    "feedback",
    "music",
    "length",
    "novelty",
    "community",
    "repeat_penalty",
)

FACTOR_LABELS = {
    "mapping_fit": "Соответствие навыкам",
    "farm_nm": "NM farm-потенциал (эвристика)",
    "difficulty": "Комфорт сложности",
    "taste": "Личный вкус",
    "feedback": "Ваша оценка карты",
    "music": "Музыка",
    "length": "Длительность",
    "novelty": "Новизна",
    "community": "Сигнал сообщества",
    "repeat_penalty": "Штраф за повтор",
}


class RankingConfigError(ValueError):
    """Raised for invalid ranking weights or settings."""


@dataclass
class RankingContext:
    settings: RankingSettings
    profile: TasteProfile
    feedback_by_set: dict[int, tuple[str, ...]]
    seen_counts: dict[int, int]
    recent_sets: tuple[Beatmapset, ...] = ()
    known_artists: frozenset[str] = frozenset()
    known_creators: frozenset[str] = frozenset()
    now: datetime = datetime.now(timezone.utc)

    @classmethod
    def from_user_data(
        cls,
        settings: RankingSettings,
        feedback_by_set: dict[int, tuple[str, ...]],
        sets_by_id: dict[int, Beatmapset],
        seen_counts: dict[int, int],
        recent_sets: tuple[Beatmapset, ...] = (),
    ) -> "RankingContext":
        profile = build_taste_profile(feedback_by_set, sets_by_id)
        known = [
            sets_by_id[set_id]
            for set_id, verdicts in feedback_by_set.items()
            if set_id in sets_by_id and "hide" not in verdicts
        ]
        known += list(recent_sets)
        try:
            now = datetime.now(timezone.utc)
        except Exception:  # pragma: no cover - clock should always work
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return cls(
            settings=settings,
            profile=profile,
            feedback_by_set=feedback_by_set,
            seen_counts=seen_counts,
            recent_sets=tuple(recent_sets[:40]),
            known_artists=frozenset(
                beatmapset.display_artist for beatmapset in known
            ),
            known_creators=frozenset(beatmapset.creator for beatmapset in known),
            now=now,
        )


def default_factor(key: str) -> FactorConfig:
    raw = config.DEFAULT_RANKING_FACTORS.get(key, {"weight": 0.0, "enabled": False})
    return FactorConfig(float(raw["weight"]), bool(raw["enabled"]))


def validate_ranking(settings: RankingSettings) -> None:
    if not settings.factors:
        raise RankingConfigError("нет ни одного фактора ранжирования")
    unknown = set(settings.factors) - set(config.DEFAULT_RANKING_FACTORS)
    if unknown:
        raise RankingConfigError(
            f"неизвестные факторы: {', '.join(sorted(unknown))}"
        )
    enabled = 0
    for key, factor in settings.factors.items():
        if not math.isfinite(factor.weight) or factor.weight < 0 or factor.weight > 5:
            raise RankingConfigError(f"вес фактора {key} должен быть 0..5")
        if factor.enabled and factor.weight > 0:
            enabled += 1
    if enabled == 0:
        raise RankingConfigError("включите хотя бы один фактор с весом больше нуля")
    if not 0 <= settings.diversity <= 1:
        raise RankingConfigError("diversity должен быть 0..1")
    if not 1 <= settings.max_per_artist <= 50:
        raise RankingConfigError("max_per_artist должен быть 1..50")
    if not 1 <= settings.max_per_mapper <= 50:
        raise RankingConfigError("max_per_mapper должен быть 1..50")
    if not 0 <= settings.comfort_stars <= 20:
        raise RankingConfigError("comfort_stars должен быть 0..20")
    if not 0.05 <= settings.comfort_width <= 10:
        raise RankingConfigError("comfort_width должен быть 0.05..10")
    if not 10 <= settings.ideal_length <= 3600:
        raise RankingConfigError("ideal_length должен быть 10..3600")
    if not 1 <= settings.length_tolerance <= 3600:
        raise RankingConfigError("length_tolerance должен быть 1..3600")
    unknown_genres = set(settings.preferred_genres) - set(GENRES)
    if unknown_genres:
        raise RankingConfigError(
            f"неизвестные жанры: {', '.join(str(g) for g in sorted(unknown_genres))}"
        )
    unknown_skills = {
        key for key in settings.skill_weights if not skill_id_valid(key)
    }
    if unknown_skills:
        raise RankingConfigError(
            f"неизвестные навыки: {', '.join(sorted(unknown_skills))}"
        )
    for key, value in settings.skill_weights.items():
        if not math.isfinite(value) or not 0 <= value <= 5:
            raise RankingConfigError(f"skill_weights.{key} должен быть 0..5")
    if settings.skill_weights and not any(
        value > 0 for value in settings.skill_weights.values()
    ):
        raise RankingConfigError(
            "skill_weights заданы, но все веса равны нулю; отключите навыки "
            "или задайте положительный вес"
        )

    targets = list(settings.relevance_targets)
    if len(targets) > 3:
        raise RankingConfigError(
            "relevance_targets: допускается не больше трёх целевых тегов"
        )
    if len(targets) != len(set(targets)):
        raise RankingConfigError("relevance_targets: целевые теги повторяются")
    unknown_targets = {key for key in targets if not skill_id_valid(key)}
    if unknown_targets:
        raise RankingConfigError(
            "неизвестные целевые теги: "
            + ", ".join(sorted(unknown_targets))
        )
    threshold = settings.relevance_threshold
    if (
        not math.isfinite(threshold)
        or threshold < 0.5
        or threshold > 1.0
    ):
        raise RankingConfigError(
            "relevance_threshold должен быть в диапазоне 0.5..1.0 "
            "(сравнение строгое, поэтому 50% не проходит)"
        )
    unknown_traits = set(settings.trait_targets) - set(TRAITS)
    if unknown_traits:
        raise RankingConfigError(
            f"неизвестные legacy-признаки: {', '.join(sorted(unknown_traits))}"
        )
    for key, value in settings.trait_targets.items():
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise RankingConfigError(f"trait_targets.{key} должен быть 0..1")
    if not 10 <= settings.farm_target_seconds <= 600:
        raise RankingConfigError("farm_target_seconds должен быть 10..600")


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(1.0, value))


def _factor(
    key: str, value: float | None, weight: float, explanation: str
) -> FactorScore:
    return FactorScore(
        key=key,
        label=FACTOR_LABELS.get(key, key),
        value=_finite(value),
        weight=weight,
        explanation=explanation,
    )


def _weight(settings: RankingSettings, key: str) -> tuple[float, bool]:
    factor = settings.factors.get(key)
    if factor is None:
        fallback = default_factor(key)
        return fallback.weight, fallback.enabled
    return factor.weight, factor.enabled


def _mapping_fit(candidate: Candidate, settings: RankingSettings) -> FactorScore:
    weight, _ = _weight(settings, "mapping_fit")
    if settings.skill_weights:
        skills = candidate.skillsets or evaluate_skills(candidate.traits)
        fit = skill_fit(skills, settings.skill_weights)
        if fit is None:
            selected = ", ".join(settings.skill_weights)
            return _factor(
                "mapping_fit",
                None,
                weight,
                f"Выбраны навыки ({selected}), но измеримых признаков для них "
                "не хватает. Значение не выдумывается.",
            )
        parts: list[str] = []
        for key, value in settings.skill_weights.items():
            if value <= 0:
                continue
            item = skills.get(key)
            if item is None or item.score is None:
                parts.append(f"{key}: нет данных")
                continue
            parts.append(
                f"{item.name}: {item.display_score}% "
                f"({CONFIDENCE_LABELS.get(item.confidence, item.confidence)}, "
                f"{SUPPORT_LABELS.get(item.support, item.support)})"
            )
        explanation = "Навыки: " + ", ".join(parts) + "."
        if fit.missing:
            explanation += (
                " Недоступно: "
                + ", ".join(fit.missing)
                + f"; покрытие весов {fit.coverage:.0%}, score снижен за "
                "неполное покрытие."
            )
        if fit.confidence == "low":
            explanation += " Низкая уверенность: часть признаков — прокси или теги."
        return _factor("mapping_fit", fit.score, weight, explanation)

    targets = settings.trait_targets
    if not targets:
        return _factor(
            "mapping_fit",
            None,
            weight,
            "Навыки не выбраны. Выберите один или несколько skillsets, чтобы "
            "включить этот фактор.",
        )
    similarity = trait_similarity(targets, candidate.traits)
    if similarity is None:
        return _factor(
            "mapping_fit",
            None,
            weight,
            "Нет измеримых признаков для legacy aim-профиля (difficulty "
            "attributes не загружены).",
        )
    parts = []
    for key, target in targets.items():
        value = candidate.traits.get(key)
        if value is not None:
            parts.append(f"{key} {round(value * 100)}% (цель {round(target * 100)}%)")
    explanation = "Legacy aim-профиль: " + ", ".join(parts)
    if candidate.traits.confidence == "low":
        explanation += ". Низкая уверенность (нет difficulty attributes)."
    return _factor("mapping_fit", similarity, weight, explanation)


def _farm_nm(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    """Approximate NM farm potential from map data.

    It combines short length, stars near the user's comfort point, aim
    pressure and moderate density. It never becomes the sole ordering key,
    never adds mods and never invents a performance estimate.
    """
    weight, _ = _weight(ctx.settings, "farm_nm")
    beatmap = candidate.beatmap
    duration = beatmap.total_length or beatmap.hit_length
    stars = beatmap.difficulty_rating
    if duration is None or stars is None:
        return _factor(
            "farm_nm",
            None,
            weight,
            "Неизвестна длительность или звёздность — NM farm-потенциал не "
            "оценивается. Это не расчёт производительности.",
        )
    aim_pressure = candidate.traits.get("aim_pressure")
    aim_share = candidate.traits.get("aim")
    if aim_pressure is not None and aim_share is not None:
        aim_component = 0.65 * aim_pressure + 0.35 * aim_share
    elif aim_pressure is not None:
        aim_component = aim_pressure
    elif aim_share is not None:
        aim_component = aim_share
    else:
        return _factor(
            "farm_nm",
            None,
            weight,
            "Нет aim-признаков (difficulty attributes или тега) — нельзя "
            "честно оценить NM farm-потенциал.",
        )

    target = ctx.settings.farm_target_seconds
    short_score = max(0.0, 1.0 - max(0, duration - target) / (target * 1.5))
    width = max(0.8, ctx.settings.comfort_width)
    star_fit = math.exp(
        -((stars - ctx.settings.comfort_stars) ** 2) / (2 * width**2)
    )
    density = candidate.traits.get("density")
    density_score = (
        max(0.0, 1.0 - abs(density - 0.55) / 0.55)
        if density is not None
        else None
    )

    parts: list[tuple[str, float, float]] = [
        ("короткая длительность", short_score, 0.32),
        ("звёзды около комфорта", star_fit, 0.24),
        ("aim-нагрузка", aim_component, 0.24),
    ]
    if density_score is not None:
        parts.append(("умеренная плотность", density_score, 0.14))
    total = sum(weight_part for _, _, weight_part in parts)
    value = sum(score * weight_part for _, score, weight_part in parts) / total
    details = (
        f"{duration}с при цели ≤{target}с; {stars:.2f}★ при комфорте "
        f"{ctx.settings.comfort_stars:.1f}★; aim {aim_component:.0%}"
    )
    if density is not None:
        details += f"; плотность {density:.0%}"
    explanation = (
        "NM-профиль без модов: " + details + ". Это эвристика практичности "
        "короткой aim-карты, а не расчёт производительности и не гарантия "
        "награды."
    )
    if candidate.traits.confidence == "low":
        explanation += " Признаки частично основаны на тегах/метаданных."
    return _factor("farm_nm", value, weight, explanation)



def _difficulty(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    settings = ctx.settings
    weight, _ = _weight(settings, "difficulty")
    stars = candidate.beatmap.difficulty_rating
    if stars is None:
        return _factor("difficulty", None, weight, "Звёздность неизвестна.")
    width = settings.comfort_width
    verdicts = ctx.feedback_by_set.get(candidate.beatmapset.id, ())
    shift = 0.0
    notes: list[str] = []
    if "too_hard" in verdicts:
        shift -= max(0.3, width * 0.8)
        notes.append("вы отметили «слишком сложно» — комфорт сдвинут вниз")
    if "too_easy" in verdicts:
        shift += max(0.3, width * 0.8)
        notes.append("вы отметили «слишком легко» — комфорт сдвинут вверх")
    effective = settings.comfort_stars + shift
    value = math.exp(-((stars - effective) ** 2) / (2 * width**2))
    explanation = (
        f"{stars:.2f}★ при комфортной точке {effective:.1f}★ "
        f"(ширина {width:.1f})."
    )
    if notes:
        explanation += " " + "; ".join(notes) + "."
    if not candidate.within_tolerance:
        explanation += " Карта вне строгого диапазона (мягкий режим звёзд)."
    return _factor("difficulty", value, weight, explanation)


def _taste(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "taste")
    profile = ctx.profile
    if not profile.enough_data:
        return _factor(
            "taste",
            None,
            weight,
            f"Недостаточно оценок ({profile.rated_sets}); нужен минимум 3 "
            "оценённые карты для фактора вкуса.",
        )
    similarity = taste_similarity(candidate.beatmapset, profile)
    skill_similarity = skill_taste_similarity(candidate.skillsets, profile)
    if skill_similarity is not None and similarity is not None:
        similarity = 0.7 * similarity + 0.3 * skill_similarity
    matches = taste_matches(candidate.beatmapset, profile)
    if matches:
        detail = ", ".join(
            f"{label} {affinity:+.2f}" for label, affinity in matches[:4]
        )
        explanation = f"Совпадения профиля: {detail}."
    else:
        explanation = "Совпадений с профилем нет; нейтральное значение."
    if skill_similarity is not None:
        explanation += (
            " Навыки оценённых карт также мягко учтены "
            f"({skill_similarity:.2f}); это не доказательство качества навыка."
        )
    return _factor("taste", similarity, weight, explanation)


def _feedback(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "feedback")
    verdicts = ctx.feedback_by_set.get(candidate.beatmapset.id, ())
    value = preference_value(verdicts)
    if value is None:
        return _factor("feedback", None, weight, "Вы ещё не оценивали эту карту.")
    labels = ", ".join(VERDICT_LABELS.get(v, v) for v in verdicts)
    details = ", ".join(
        f"{VERDICT_LABELS.get(v, v)}: {VERDICT_PREFERENCE[v]:.2f}"
        for v in verdicts
        if v in VERDICT_PREFERENCE
    )
    return _factor(
        "feedback",
        value,
        weight,
        f"Ваши оценки: {labels}. Значения: {details} (берётся минимум).",
    )


def _music(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "music")
    beatmapset = candidate.beatmapset
    genre = beatmapset.genre_id
    if ctx.settings.preferred_genres:
        if genre is None:
            return _factor("music", None, weight, "Жанр неизвестен.")
        if genre in ctx.settings.preferred_genres:
            return _factor(
                "music",
                1.0,
                weight,
                f"Жанр {GENRES.get(genre, genre)} входит в выбранные.",
            )
        return _factor(
            "music",
            0.15,
            weight,
            f"Жанр {GENRES.get(genre, genre)} не входит в выбранные.",
        )
    if ctx.profile.enough_data and ctx.profile.genre_affinity:
        if genre is None:
            return _factor("music", None, weight, "Жанр неизвестен.")
        affinity = ctx.profile.genre_affinity.get(genre, 0.0)
        return _factor(
            "music",
            0.5 + 0.5 * affinity,
            weight,
            f"Жанр {GENRES.get(genre, genre)}: вкус {affinity:+.2f}.",
        )
    return _factor(
        "music", None, weight, "Нет данных о музыкальных предпочтениях."
    )


def _length(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "length")
    duration = candidate.beatmap.total_length
    if duration is None:
        return _factor("length", None, weight, "Длительность неизвестна.")
    distance = abs(duration - ctx.settings.ideal_length)
    value = max(0.0, 1.0 - distance / ctx.settings.length_tolerance)
    return _factor(
        "length",
        value,
        weight,
        f"{duration}с при предпочитаемых {ctx.settings.ideal_length}с "
        f"(допуск ±{ctx.settings.length_tolerance}с).",
    )


def _novelty(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "novelty")
    beatmapset = candidate.beatmapset
    known = (
        beatmapset.creator in ctx.known_creators
        or beatmapset.display_artist in ctx.known_artists
    )
    exploration = 0.0 if known else 1.0
    freshness: float | None = None
    raw_date = beatmapset.ranked_date or beatmapset.submitted_date
    if raw_date:
        try:
            published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            years = max(0.0, (ctx.now - published).days / 365.25)
            freshness = 1.0 if years <= 1 else max(0.0, 1.0 - (years - 1) / 9)
        except ValueError:
            freshness = None
    if freshness is None:
        value = exploration
        explanation = (
            "Дата публикации неизвестна; новизна только по незнакомому "
            f"мапперу/исполнителю ({'новый' if exploration else 'знакомый'})."
        )
    else:
        value = 0.4 * freshness + 0.6 * exploration
        explanation = (
            f"Свежесть {freshness:.2f}, незнакомость {exploration:.0f} "
            "(маппер/исполнитель)."
        )
    return _factor("novelty", value, weight, explanation)


def _community(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "community")
    beatmapset = candidate.beatmapset
    values: list[tuple[float, float]] = []
    if beatmapset.rating is not None:
        values.append((max(0.0, min(1.0, beatmapset.rating / 10.0)), 0.5))
    if beatmapset.play_count is not None:
        plays = math.log10(max(0, beatmapset.play_count) + 1) / 6.5
        values.append((max(0.0, min(1.0, plays)), 0.3))
    if beatmapset.favourite_count is not None:
        favourites = math.log10(max(0, beatmapset.favourite_count) + 1) / 4.5
        values.append((max(0.0, min(1.0, favourites)), 0.2))
    if not values:
        return _factor("community", None, weight, "Нет данных сообщества.")
    total_weight = sum(w for _, w in values)
    value = sum(v * w for v, w in values) / total_weight
    detail = []
    if beatmapset.rating is not None:
        detail.append(f"рейтинг {beatmapset.rating:g}")
    if beatmapset.play_count is not None:
        detail.append(f"просмотры {beatmapset.play_count}")
    if beatmapset.favourite_count is not None:
        detail.append(f"в избранном {beatmapset.favourite_count}")
    return _factor(
        "community", value, weight, "Сигнал сообщества: " + ", ".join(detail) + "."
    )


def combine_similarity(values: list[float]) -> float:
    """Combine independent similarity signals (0..1) with probabilistic OR."""
    product = 1.0
    for value in values:
        product *= 1.0 - max(0.0, min(1.0, value))
    return 1.0 - product


def candidate_similarity(a: Candidate, b: Candidate) -> float:
    signals: list[float] = []
    sa, sb = a.beatmapset, b.beatmapset
    if sa.display_artist and sa.display_artist == sb.display_artist:
        signals.append(0.55)
    if sa.creator and sa.creator == sb.creator:
        signals.append(0.50)
    if sa.genre_id is not None and sa.genre_id == sb.genre_id:
        signals.append(0.22)
    bpm_a, bpm_b = a.beatmap.bpm or sa.bpm, b.beatmap.bpm or sb.bpm
    if bpm_a is not None and bpm_b is not None and abs(bpm_a - bpm_b) <= 10:
        signals.append(0.18)
    stars_a, stars_b = a.beatmap.difficulty_rating, b.beatmap.difficulty_rating
    if stars_a is not None and stars_b is not None and abs(stars_a - stars_b) <= 0.6:
        signals.append(0.15)
    common = set(a.traits.scores) & set(b.traits.scores)
    if len(common) >= 2:
        diff = sum(abs(a.traits.scores[k] - b.traits.scores[k]) for k in common) / len(common)
        signals.append(0.35 * max(0.0, 1.0 - diff))
    return combine_similarity(signals)


def set_similarity(a: Beatmapset, b: Beatmapset) -> float:
    signals: list[float] = []
    if a.display_artist and a.display_artist == b.display_artist:
        signals.append(0.55)
    if a.creator and a.creator == b.creator:
        signals.append(0.50)
    if a.genre_id is not None and a.genre_id == b.genre_id:
        signals.append(0.22)
    bpm_a, bpm_b = a.bpm, b.bpm
    if bpm_a is not None and bpm_b is not None and abs(bpm_a - bpm_b) <= 10:
        signals.append(0.18)
    return combine_similarity(signals)


def _repeat_penalty(candidate: Candidate, ctx: RankingContext) -> FactorScore:
    weight, _ = _weight(ctx.settings, "repeat_penalty")
    set_id = candidate.beatmapset.id
    seen = ctx.seen_counts.get(set_id, 0)
    direct = min(1.0, 0.55 + 0.15 * seen) if seen else 0.0
    recent_similarity = 0.0
    for recent in ctx.recent_sets:
        if recent.id == set_id:
            continue
        recent_similarity = max(recent_similarity, set_similarity(candidate.beatmapset, recent) * 0.9)
    value = max(direct, recent_similarity)
    if not seen and recent_similarity == 0:
        explanation = "Нет истории показов и похожих недавних карт."
    else:
        explanation = (
            f"Показов этой карты: {seen}. Похожесть на недавние: "
            f"{recent_similarity:.2f}. Это штраф, он вычитается из score."
        )
    return _factor("repeat_penalty", value, weight, explanation)


def evaluate_candidate(candidate: Candidate, ctx: RankingContext) -> ScoredCandidate:
    builders = {
        "mapping_fit": lambda: _mapping_fit(candidate, ctx.settings),
        "farm_nm": lambda: _farm_nm(candidate, ctx),
        "difficulty": lambda: _difficulty(candidate, ctx),
        "taste": lambda: _taste(candidate, ctx),
        "feedback": lambda: _feedback(candidate, ctx),
        "music": lambda: _music(candidate, ctx),
        "length": lambda: _length(candidate, ctx),
        "novelty": lambda: _novelty(candidate, ctx),
        "community": lambda: _community(candidate, ctx),
        "repeat_penalty": lambda: _repeat_penalty(candidate, ctx),
    }
    factors: list[FactorScore] = []
    numerator = 0.0
    denominator = 0.0
    for key in FACTOR_ORDER:
        weight, enabled = _weight(ctx.settings, key)
        if not enabled or weight <= 0:
            continue
        factor = builders[key]()
        factors.append(factor)
        if factor.value is None:
            continue
        if key == "repeat_penalty":
            numerator -= factor.weight * factor.value
        else:
            numerator += factor.weight * factor.value
        denominator += factor.weight

    if denominator <= 0:
        base = 0.5
    else:
        base = numerator / denominator
    if not math.isfinite(base):
        base = 0.5
    score = max(0.0, min(1.0, base))
    penalty = 0.0
    normalized: list[FactorScore] = []
    for factor in factors:
        contribution = 0.0
        if factor.value is not None and denominator > 0:
            contribution = factor.weight * factor.value / denominator
            if factor.key == "repeat_penalty":
                contribution = -contribution
                penalty = -contribution
        normalized.append(replace(factor, contribution=contribution))
    factors = normalized

    positive = sorted(
        (
            f
            for f in factors
            if f.key != "repeat_penalty" and f.value is not None and (f.value or 0) >= 0.55
        ),
        key=lambda f: -((f.value or 0) * f.weight),
    )
    reasons = [f"{f.label}: {f.explanation}" for f in positive[:3]]
    if not reasons:
        reasons = [
            "Нет ярко выраженных положительных сигналов; карта прошла строгие "
            "фильтры, но данных для сильных выводов мало."
        ]
    missing = [f.label for f in factors if f.value is None]
    if missing:
        reasons.append("Нет данных: " + ", ".join(missing) + ".")
    return ScoredCandidate(
        candidate=candidate,
        score=score,
        factors=tuple(factors),
        reasons=tuple(reasons),
        penalty=penalty,
    )


def rank_candidates(
    candidates: list[Candidate],
    ctx: RankingContext,
    limit: int | None = None,
    notes: list[str] | None = None,
) -> list[ScoredCandidate]:
    scored = [evaluate_candidate(candidate, ctx) for candidate in candidates]
    return diversify(scored, ctx.settings, limit=limit, notes=notes)


def diversify(
    scored: list[ScoredCandidate],
    settings: RankingSettings,
    limit: int | None = None,
    notes: list[str] | None = None,
) -> list[ScoredCandidate]:
    """Greedy diversity-aware selection.

    Returns all candidates in display order (up to ``limit``). Similar cards
    are penalised, not banned; artist/mapper caps are relaxed if they would
    leave the result list short. The relaxation is reported in Discovery notes.
    """
    if not scored:
        return []
    target = len(scored) if limit is None else max(0, min(limit, len(scored)))
    if target == 0:
        return []
    diversity = max(0.0, min(1.0, settings.diversity))
    artist_cap = settings.max_per_artist if diversity > 0 else 10_000
    mapper_cap = settings.max_per_mapper if diversity > 0 else 10_000

    remaining = sorted(
        scored, key=lambda s: (-s.score, s.beatmapset.id, s.beatmap.id)
    )
    selected: list[ScoredCandidate] = []
    caps_relaxed = False
    while remaining and len(selected) < target:
        artist_counts: dict[str, int] = {}
        mapper_counts: dict[str, int] = {}
        for item in selected:
            artist = item.beatmapset.display_artist
            mapper = item.beatmapset.creator
            artist_counts[artist] = artist_counts.get(artist, 0) + 1
            mapper_counts[mapper] = mapper_counts.get(mapper, 0) + 1

        best: ScoredCandidate | None = None
        best_value = -1e9
        best_index = -1
        for index, item in enumerate(remaining):
            artist = item.beatmapset.display_artist
            mapper = item.beatmapset.creator
            if artist_counts.get(artist, 0) >= artist_cap:
                continue
            if mapper_counts.get(mapper, 0) >= mapper_cap:
                continue
            similarity = 0.0
            for chosen in selected:
                similarity = max(
                    similarity, candidate_similarity(item.candidate, chosen.candidate)
                )
            value = item.score - diversity * similarity
            if not math.isfinite(value):
                value = item.score
            if value > best_value + 1e-12 or (
                abs(value - best_value) <= 1e-12
                and best is not None
                and item.beatmapset.id < best.beatmapset.id
            ):
                best_value = value
                best = item
                best_index = index
        if best is None:
            if caps_relaxed:
                break
            caps_relaxed = True
            artist_cap = mapper_cap = 10_000
            if notes is not None:
                notes.append(
                    "Лимиты разнообразия (исполнитель/маппер) ослаблены: "
                    "подходящих карт меньше, чем ограничений."
                )
            continue
        remaining.pop(best_index)
        selected.append(
            replace(best, diversified_score=max(0.0, min(1.0, best_value)))
        )
    return selected


def explain(scored: ScoredCandidate) -> list[str]:
    lines = [f"Внутренняя оценка: {scored.display_score}/100 (не вероятность)."]
    for factor in scored.factors:
        if factor.value is None:
            lines.append(f"• {factor.label}: нет данных — {factor.explanation}")
        else:
            lines.append(
                f"• {factor.label} (вес {factor.weight:g}, значение "
                f"{factor.value:.2f}): {factor.explanation}"
            )
    return lines
