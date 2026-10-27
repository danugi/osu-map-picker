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
from features import SOURCE_LABELS
from models import Candidate, RankingSettings, RelevanceResult, TargetMatch
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
                "не применяется (например, Mixed Aim или All-rounder)."
            ),
        )

    targets = tuple(
        _target_match(target_id, candidate, threshold) for target_id in target_ids
    )
    available = [item for item in targets if item.score is not None]
    matched = [item for item in available if item.matched]
    best = (
        max(available, key=lambda item: (item.score or 0.0, item.target_id))
        if available
        else None
    )
    if matched:
        parts = []
        for item in matched:
            suffix = (
                " (ограниченная поддержка)"
                if item.status == MATCHED_LIMITED
                else ""
            )
            parts.append(f"{item.label} {item.display_score}%{suffix}")
        reason = (
            f"Хотя бы один целевой тег выше порога {threshold:.0%}: "
            + ", ".join(parts)
            + "."
        )
    elif not available:
        sequence_targets = [
            item
            for item in targets
            if skill_uses_sequences(item.target_id)
        ]
        if sequence_targets and all(
            item.status == NOT_ANALYZED for item in sequence_targets
        ):
            reason = (
                "не анализировалось: для целевых тегов нужен .osu, но "
                "sequence-анализ для этой карты не выполнялся."
            )
        elif any(item.status == SOURCE_UNAVAILABLE for item in targets):
            reason = (
                "источник недоступен: .osu для целевых тегов отсутствует в "
                "кеше. Это не значит, что паттерна нет."
            )
        elif any(item.status == FAILED for item in targets):
            reason = (
                "анализ не удался: .osu был доступен, но разбор завершился "
                "ошибкой; целевые признаки остаются неизвестными."
            )
        elif any(item.status == PARTIAL for item in targets):
            reason = (
                "частичный анализ: доступна только часть данных для целевых "
                "тегов; совпадение не выдумывается."
            )
        else:
            reason = (
                "недостаточно данных: ни один из целевых тегов ("
                + ", ".join(item.label for item in targets)
                + ") не удалось оценить. Совпадение не выдумывается."
            )
    else:
        reason = (
            f"Ни один целевой тег не выше порога {threshold:.0%}: "
            + ", ".join(
                f"{item.label} {item.display_score}%" for item in targets
            )
            + "."
        )
    return RelevanceResult(
        targets=targets,
        threshold=threshold,
        passed=bool(matched),
        best_target=best,
        reason=reason,
    )
