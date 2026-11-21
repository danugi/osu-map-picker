"""Reuse the normal local reranker for the Library's preset mode.

The Library must not invent a second interpretation of preset hard filters,
target skills or ranking. This module runs the existing ``rerank_stored``
pipeline with a catalog-sized local sample and exposes its real result to the
Library query layer.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from discovery import DiscoveryEngine
from models import DiscoveryResult, PresetConfig, ScoredCandidate
from storage import Storage


@dataclass(frozen=True, slots=True)
class LibraryPresetResult:
    result: DiscoveryResult
    set_order: tuple[int, ...]
    beatmaps: tuple[tuple[int, int], ...]
    missing_evidence: int
    below_threshold: int
    target_gate: bool

    @property
    def beatmap_by_set(self) -> dict[int, int]:
        return dict(self.beatmaps)

    @property
    def scored_by_beatmap(self) -> dict[int, ScoredCandidate]:
        return {
            item.beatmap.id: item
            for item in self.result.scored
        }

    @property
    def limitation(self) -> str:
        if not self.target_gate:
            return (
                "У активного пресета нет обязательного гейта целевых навыков: "
                "применены его hard filters и обычное локальное ранжирование."
            )
        parts = [
            f"прошли hard filters: {self.result.passed_filters}",
            f"прошли целевой гейт: {self.result.passed_relevance}",
        ]
        if self.missing_evidence:
            parts.append(
                f"без достаточных данных: {self.missing_evidence} — они не "
                "считаются неподходящими, но не получают выдуманный score"
            )
        if self.below_threshold:
            parts.append(f"ниже порога: {self.below_threshold}")
        return "; ".join(parts) + "."


def preset_cache_signature(
    store: Storage, cfg: PresetConfig
) -> tuple[Any, ...]:
    """Cheap invalidation key for local preset evaluation.

    Includes the preset JSON, catalog size and the analysis revision. The
    cache is session-local; no persistent writes are made.
    """

    config_digest = hashlib.sha256(
        json.dumps(
            cfg.to_dict(), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    with store.connection() as conn:
        catalog = conn.execute(
            "SELECT COUNT(*) AS sets, "
            "(SELECT COUNT(*) FROM beatmaps) AS beatmaps, "
            "(SELECT COALESCE(MAX(updated_at), '') FROM beatmaps) AS updated, "
            "(SELECT COUNT(*) FROM beatmap_analysis) AS analyses, "
            "(SELECT COALESCE(MAX(analyzed_at), '') FROM beatmap_analysis) "
            "AS analyzed_at, "
            "(SELECT COUNT(*) FROM feedback) AS feedback_count, "
            "(SELECT COALESCE(MAX(updated_at), '') FROM feedback) "
            "AS feedback_updated, "
            "(SELECT COUNT(*) FROM favorites) AS favorite_count, "
            "(SELECT COUNT(*) FROM history) AS history_count, "
            "(SELECT COALESCE(MAX(shown_at), '') FROM history) "
            "AS history_updated"
        ).fetchone()
    return (
        config_digest,
        int(catalog["sets"] or 0),
        int(catalog["beatmaps"] or 0),
        str(catalog["updated"] or ""),
        int(catalog["analyses"] or 0),
        str(catalog["analyzed_at"] or ""),
        int(catalog["feedback_count"] or 0),
        str(catalog["feedback_updated"] or ""),
        int(catalog["favorite_count"] or 0),
        int(catalog["history_count"] or 0),
        str(catalog["history_updated"] or ""),
    )


def evaluate_preset(
    store: Storage, cfg: PresetConfig
) -> LibraryPresetResult:
    """Evaluate the active preset over the whole local catalog, offline."""

    settings = copy.deepcopy(cfg.discovery)
    catalog_size = max(1, store.beatmapset_count())
    # rerank_stored uses max(1000, max_candidates * 4) as its sample bound.
    # Raise it so Library preset mode sees the whole local catalog instead of
    # inheriting the discovery display cap.
    settings.max_candidates = max(
        int(settings.max_candidates), catalog_size, 1000
    )
    engine = DiscoveryEngine(None, store, settings)
    result = engine.rerank_stored(
        cfg.filters,
        cfg.ranking,
        all_candidates=True,
    )
    scored = list(result.scored)
    set_order = tuple(item.beatmapset.id for item in scored)
    beatmaps = tuple(
        (item.beatmapset.id, item.beatmap.id) for item in scored
    )
    missing = 0
    below = 0
    for reason, count in result.excluded.items():
        text = str(reason).casefold()
        if any(
            marker in text
            for marker in (
                "не анализировалось",
                "недостаточно данных",
                "источник недоступен",
                "частичный",
                "ошибка анализа",
            )
        ):
            missing += int(count)
        elif "ниже порога" in text:
            below += int(count)
    return LibraryPresetResult(
        result=result,
        set_order=set_order,
        beatmaps=beatmaps,
        missing_evidence=missing,
        below_threshold=below,
        target_gate=bool(cfg.ranking.relevance_targets),
    )
