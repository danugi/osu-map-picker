"""Mandatory target-tag gate for specialized presets.

This is deliberately separate from the Recommendation score. The gate runs
after strict filters and difficulty-attribute enrichment, but before ranking:

    strict filters -> attributes -> target gate -> ranking

A target is an existing skillset definition. Its score is derived only from
features with explicit provenance (API attributes, `.osu` sequence evidence,
beatmap counts/metadata, mapper tags or documented proxies). A target is
matched only when its score is strictly greater than the configured threshold,
so exactly 50% never passes. Streams and bursts are separate measured features:
a stream chain cannot satisfy a burst target and vice versa.

Missing data is not replaced by 0.5 or by a title word. If no target can be
evaluated, the candidate is excluded and the UI shows "недостаточно данных".
A sequence target that was never offered to the analyzer is reported separately
as "не анализировалось"; an analyzed map with zero chains is "ниже порога".
"""

from __future__ import annotations

import evidence
from features import FEATURE_LABELS, SOURCE_LABELS
from models import Candidate, RankingSettings, RelevanceResult, TargetMatch
from patterns import SEQUENCE_FEATURE_KEYS
from skillsets import SKILLS, evaluate_skills, skill_uses_sequences

NO_DATA = "недостаточно данных"
MATCHED = "подтверждено"
MATCHED_LIMITED = "выше порога (ограниченная поддержка)"
BELOW = "ниже порога"
NOT_ANALYZED = "не анализировалось"
SOURCE_UNAVAILABLE = "источник недоступен"
PARTIAL = "частичный анализ"
FAILED = "анализ не удался"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _source_text(sources: tuple[str, ...]) -> str:
    labels = [SOURCE_LABELS.get(source, source) for source in sources if source]
    if not labels:
        return NO_DATA
    return "; ".join(dict.fromkeys(labels))


def _target_feature_ids(
    skill_id: str,
    seen: set[str] | None = None,
) -> tuple[str, ...]:
    """Feature ids that can contribute to a skill, including components."""

    visited = seen if seen is not None else set()
    if skill_id in visited:
        return ()
    visited.add(skill_id)
    definition = SKILLS.get(skill_id)
    if definition is None:
        return ()
    features = list(definition.feature_names)
    for component in definition.component_names:
        features.extend(_target_feature_ids(component, visited))
    return tuple(dict.fromkeys(features))


def _uses_partial_sequence_evidence(
    target_id: str,
    candidate: Candidate,
    item: object,
) -> bool:
    """Whether a target's score actually drew on a partial .osu sequence.

    A skill may use sequence features without the current score using them at
    all (for example ``aim_control`` can score from attributes/CS while
    ``angle_change_share`` is absent).  Only an actually contributing partial
    sequence feature, or partial sequence provenance retained on the score,
    counts as incomplete.
    """

    sources = tuple(getattr(item, "sources", ()))
    if "osu_sequence_partial" in sources:
        return True
    for feature in _target_feature_ids(target_id):
        if feature not in SEQUENCE_FEATURE_KEYS:
            continue
        if candidate.traits.scores.get(feature) is None:
            continue
        status = candidate.traits.status(feature)
        source = candidate.traits.sources.get(feature, "")
        if status == evidence.PARTIAL or source == "osu_sequence_partial":
            return True
    return False


def _target_match(
    target_id: str,
    candidate: Candidate,
    threshold: float,
) -> TargetMatch:
    definition = SKILLS.get(target_id)
    label = definition.name if definition is not None else target_id
    skillsets = candidate.skillsets or evaluate_skills(candidate.traits)
    item = skillsets.get(target_id)
    if item is None or item.score is None:
        status_code = item.status if item is not None else evidence.NOT_ANALYZED
        status_labels = {
            evidence.NOT_ANALYZED: NOT_ANALYZED,
            evidence.SOURCE_UNAVAILABLE: SOURCE_UNAVAILABLE,
            evidence.PARTIAL: PARTIAL,
            evidence.FAILED: FAILED,
            evidence.PROXY: NO_DATA,
            evidence.MEASURED: NO_DATA,
            evidence.NOT_DETECTED: BELOW,
        }
        status = status_labels.get(status_code, NO_DATA)
        return TargetMatch(
            target_id=target_id,
            label=label,
            score=None,
            threshold=threshold,
            matched=False,
            status=status,
            source=status,
            confidence="none",
            support=definition.support if definition is not None else "limited",
            missing=item.missing if item is not None else (target_id,),
        )

    score = _clamp(float(item.score))
    incomplete = _uses_partial_sequence_evidence(target_id, candidate, item)
    if incomplete:
        # Keep the numeric score and provenance for diagnostics, but a value
        # from a truncated .osu pass cannot independently confirm a target.
        matched = False
        status = PARTIAL
    else:
        matched = score > threshold
        if matched:
            confirmed = item.support == "measured" and item.confidence in {
                "medium",
                "high",
            }
            status = MATCHED if confirmed else MATCHED_LIMITED
        else:
            status = BELOW
    return TargetMatch(
        target_id=target_id,
        label=label,
        score=score,
        threshold=threshold,
        matched=matched,
        status=status,
        source=_source_text(item.sources),
        confidence=item.confidence,
        support=item.support,
        evidence=item.evidence,
        missing=item.missing,
    )


def _legacy_reason(
    targets: tuple[TargetMatch, ...],
    threshold: float,
    available: list[TargetMatch],
    matched: list[TargetMatch],
) -> str:
    if matched:
        parts = []
        for item in matched:
            suffix = (
                " (ограниченная поддержка)"
                if item.status == MATCHED_LIMITED
                else ""
            )
            parts.append(f"{item.label} {item.display_score}%{suffix}")
        return (
            f"Хотя бы один целевой тег выше порога {threshold:.0%}: "
            + ", ".join(parts)
            + "."
        )
    partial = [item for item in available if item.status == PARTIAL]
    if partial:
        return (
            "частичный анализ: целевой тег имеет значение "
            + ", ".join(
                f"{item.label} {item.display_score}%" for item in partial
            )
            + ", но анализ покрыл только часть карты. Частичное значение не "
            "подтверждает соответствие."
        )
    if not available:
        sequence_targets = [
            item for item in targets if skill_uses_sequences(item.target_id)
        ]
        if sequence_targets and all(
            item.status == NOT_ANALYZED for item in sequence_targets
        ):
            return (
                "не анализировалось: для целевых тегов нужен .osu, но "
                "sequence-анализ для этой карты не выполнялся."
            )
        if any(item.status == SOURCE_UNAVAILABLE for item in targets):
            return (
                "источник недоступен: .osu для целевых тегов отсутствует в "
                "кеше. Это не значит, что паттерна нет."
            )
        if any(item.status == FAILED for item in targets):
            return (
                "анализ не удался: .osu был доступен, но разбор завершился "
                "ошибкой; целевые признаки остаются неизвестными."
            )
        if any(item.status == PARTIAL for item in targets):
            return (
                "частичный анализ: доступна только часть данных для целевых "
                "тегов; совпадение не выдумывается."
            )
        return (
            "недостаточно данных: ни один из целевых тегов ("
            + ", ".join(item.label for item in targets)
            + ") не удалось оценить. Совпадение не выдумывается."
        )
    return (
        f"Ни один целевой тег не выше порога {threshold:.0%}: "
        + ", ".join(f"{item.label} {item.display_score}%" for item in targets)
        + "."
    )


def _policy(settings: RankingSettings) -> tuple[list[str], int, list[str]]:
    policy = settings.relevance_policy or {}
    targets = [str(item) for item in settings.relevance_targets if str(item)]
    primary = [
        str(item)
        for item in policy.get("primary_targets", targets)
        if str(item)
    ]
    return (
        primary,
        int(policy.get("min_primary", 1)),
        [
            str(item)
            for item in policy.get("require_features_any", [])
            if str(item)
        ],
    )


def _feature_state(
    candidate: Candidate,
    feature_ids: list[str],
    threshold: float,
) -> tuple[bool, str]:
    if not feature_ids:
        return True, ""
    present: list[str] = []
    matched: list[str] = []
    partial: list[str] = []
    for feature in feature_ids:
        value = candidate.traits.scores.get(feature)
        status = candidate.traits.status(feature)
        source = candidate.traits.sources.get(feature, "")
        label = FEATURE_LABELS.get(feature, feature)
        incomplete = status == evidence.PARTIAL or source == "osu_sequence_partial"
        if incomplete:
            suffix = f" {round(value * 100)}%" if value is not None else ""
            if source:
                suffix += f" ({SOURCE_LABELS.get(source, source)})"
            partial.append(label + suffix)
            continue
        if value is None:
            continue
        present.append(f"{label} {round(value * 100)}%")
        if value > threshold:
            matched.append(label)
    if matched:
        return True, "обязательные признаки: " + ", ".join(matched)
    if partial:
        return False, (
            "частичный анализ обязательных признаков: " + ", ".join(partial)
        )
    if present:
        return False, (
            "обязательные признаки ниже порога: " + ", ".join(present)
        )
    return False, (
        "недостаточно данных по обязательным признакам: "
        + ", ".join(FEATURE_LABELS.get(feature, feature) for feature in feature_ids)
    )


def evaluate_relevance(
    candidate: Candidate,
    settings: RankingSettings,
) -> RelevanceResult:
    """Check a candidate against the active preset's mandatory targets."""

    target_ids = tuple(
        dict.fromkeys(str(item) for item in settings.relevance_targets if str(item))
    )
    threshold = float(settings.relevance_threshold)
    if not target_ids:
        return RelevanceResult(
            targets=(),
            threshold=threshold,
            passed=True,
            reason=(
                "Обязательные целевые теги не заданы: гейт соответствия "
                "не применяется (например, Discovery или All-rounder)."
            ),
        )

    targets = tuple(
        _target_match(target_id, candidate, threshold) for target_id in target_ids
    )
    available = [item for item in targets if item.score is not None]
    matched = [item for item in available if item.matched]
    if not settings.relevance_policy:
        best_legacy = (
            max(available, key=lambda item: (item.score or 0.0, item.target_id))
            if available
            else None
        )
        return RelevanceResult(
            targets=targets,
            threshold=threshold,
            passed=bool(matched),
            best_target=best_legacy,
            reason=_legacy_reason(targets, threshold, available, matched),
        )

    primary_ids, min_primary, feature_ids = _policy(settings)
    primary_set = set(primary_ids)
    primary = [item for item in targets if item.target_id in primary_set]
    primary_available = [item for item in primary if item.score is not None]
    primary_matched = [item for item in primary_available if item.matched]
    feature_pass, feature_reason = _feature_state(
        candidate, feature_ids, threshold
    )
    passed = len(primary_matched) >= min_primary and feature_pass
    if primary_matched:
        best = max(
            primary_matched,
            key=lambda item: (item.score or 0.0, item.target_id),
        )
    elif available:
        best = max(
            available, key=lambda item: (item.score or 0.0, item.target_id)
        )
    else:
        best = None

    if passed:
        parts = []
        for item in primary_matched[:min_primary]:
            suffix = (
                " (ограниченная поддержка)"
                if item.status == MATCHED_LIMITED
                else ""
            )
            parts.append(f"{item.label} {item.display_score}%{suffix}")
        reason = (
            f"Основные целевые теги выше порога {threshold:.0%}: "
            + ", ".join(parts)
            + (f". {feature_reason}." if feature_reason else ".")
        )
    elif primary_matched and not feature_pass:
        reason = (
            "Основной целевой тег пройден, но профиль не подтверждён: "
            f"{feature_reason}."
        )
    elif any(item.status == PARTIAL for item in primary):
        partial_primary = [
            item for item in primary if item.status == PARTIAL
        ]
        reason = (
            "частичный анализ: основные целевые теги имеют значения "
            + ", ".join(
                f"{item.label} {item.display_score}%"
                for item in partial_primary
            )
            + ", но анализ покрыл только часть карты. Частичное значение не "
            "подтверждает соответствие."
        )
        secondary_matches = [
            item for item in matched if item.target_id not in primary_set
        ]
        if secondary_matches:
            reason += (
                " Вторичные совпадения не заменяют основной профиль: "
                + ", ".join(item.label for item in secondary_matches)
                + "."
            )
        if feature_reason:
            reason += " " + feature_reason.capitalize() + "."
    elif not primary_available:
        sequence_targets = [
            item for item in primary if skill_uses_sequences(item.target_id)
        ]
        if sequence_targets and all(
            item.status == NOT_ANALYZED for item in sequence_targets
        ):
            reason = (
                "не анализировалось: для основных целевых тегов нужен .osu, "
                "но sequence-анализ для этой карты не выполнялся."
            )
        elif any(item.status == SOURCE_UNAVAILABLE for item in primary):
            reason = (
                "источник недоступен: .osu для основных целевых тегов "
                "отсутствует в кеше. Это не значит, что паттерна нет."
            )
        elif any(item.status == FAILED for item in primary):
            reason = (
                "анализ не удался: .osu был доступен, но разбор завершился "
                "ошибкой; основные целевые признаки остаются неизвестными."
            )
        elif any(item.status == PARTIAL for item in primary):
            reason = (
                "частичный анализ: доступна только часть данных для основных "
                "целевых тегов; совпадение не выдумывается."
            )
        else:
            reason = (
                "недостаточно данных: ни один из основных целевых тегов ("
                + ", ".join(item.label for item in primary)
                + ") не удалось оценить. Совпадение не выдумывается."
            )
        if feature_reason:
            reason += " " + feature_reason.capitalize() + "."
    else:
        primary_scores = ", ".join(
            f"{item.label} {item.display_score}%" for item in primary
        )
        secondary_matches = [
            item for item in matched if item.target_id not in primary_set
        ]
        reason = (
            f"Ни один основной целевой тег не выше порога {threshold:.0%}: "
            + primary_scores
            + "."
        )
        if secondary_matches:
            reason += (
                " Вторичные совпадения не заменяют основной профиль: "
                + ", ".join(item.label for item in secondary_matches)
                + "."
            )
        if feature_reason and not feature_pass:
            reason += " " + feature_reason.capitalize() + "."
    return RelevanceResult(
        targets=targets,
        threshold=threshold,
        passed=passed,
        best_target=best,
        reason=reason,
    )
