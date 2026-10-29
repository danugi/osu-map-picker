"""Candidate discovery through multiple bounded API search strategies.

The engine never relies on a single search. It combines status/sort
strategies, optional oldest/rating/popularity passes, and seed expansion from
the user's own positive feedback. Every API page is cached in SQLite, results
are deduplicated by beatmapset id, and the request budget is explicit. When
the budget or page limit truncates a strategy, the result is marked
incomplete instead of pretending the search was exhaustive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

import analysis as analysis_store
import config
import evidence
import patterns
from feedback import VERDICT_PREFERENCE, build_taste_profile
from filters import UserFilterData, apply_filters, validate_filters
from features import derive_features
from models import (
    AnalysisEvidence,
    Beatmapset,
    Candidate,
    DifficultyAttributes,
    DiscoveryResult,
    DiscoverySettings,
    Exclusion,
    Filters,
    RankingSettings,
    ScoredCandidate,
    SearchPage,
    SearchQuery,
    SequenceAnalysis,
    StrategyRunStats,
    now_iso,
    search_page_from_dict,
    search_page_to_dict,
)
from osu_api import (
    OsuApiClient,
    OsuApiError,
    OsuAuthError,
    OsuForbiddenError,
    OsuNetworkError,
    OsuRateLimitError,
    OsuServerError,
)
from ranking import RankingContext, evaluate_candidate, rank_candidates, validate_ranking
from relevance import (
    BELOW,
    FAILED,
    NO_DATA,
    NOT_ANALYZED,
    PARTIAL,
    SOURCE_UNAVAILABLE,
    evaluate_relevance,
)
from skillsets import SKILLS, evaluate_skills, skill_uses_sequences
from storage import Storage, StorageError

logger = logging.getLogger("osu_picker.discovery")

MAX_NOTES = 12
MAX_EXCLUSION_SAMPLES = 80
# Cached-only analysis is deliberately bounded even when the user sets the
# .osu download limit to zero.
MAX_CACHED_SEQUENCE_ANALYSIS = 50
SEQUENCE_STRATA = 5
_ATTRIBUTE_FEATURE_KEYS = {
    "aim",
    "speed",
    "control",
    "aim_pressure",
    "speed_pressure",
    "slider_factor",
    "stamina",
    "low_aim",
}


@dataclass(frozen=True, slots=True)
class SequenceStats:
    required: bool = False
    analyzed: int = 0
    unanalyzed: int = 0
    downloads: int = 0
    cache_hits: int = 0
    failed: int = 0
    partial: int = 0
    source_unavailable: int = 0
    not_analyzed: int = 0
    limit: int = 0
    cached_only: bool = False

    @property
    def complete(self) -> bool:
        return (
            not self.required
            or (self.unanalyzed == 0 and self.partial == 0)
        )


@dataclass(frozen=True)
class SearchStrategy:
    """One named API search pass."""

    name: str
    status: str
    sort: str
    query: str | None = None


def _status_priority(statuses: list[str]) -> str:
    for preferred in ("ranked", "loved", "qualified", "pending", "wip", "graveyard"):
        if preferred in statuses:
            return preferred
    return statuses[0]


def build_strategies(
    filters: Filters,
    settings: DiscoverySettings,
    seeds: Iterable[str] = (),
) -> list[SearchStrategy]:
    """Build a bounded, de-duplicated strategy list."""
    statuses = list(dict.fromkeys(filters.statuses))
    primary = _status_priority(statuses)
    base_query = filters.query.strip() or None
    strategies: list[SearchStrategy] = []
    seen_keys: set[tuple[str, str, str | None]] = set()

    def add(name: str, status: str, sort: str, query: str | None) -> None:
        # Two differently named strategies with the same request would spend
        # the budget twice and return the same candidates.
        key = (status, sort, query)
        if key in seen_keys:
            return
        seen_keys.add(key)
        strategies.append(SearchStrategy(name, status, sort, query))

    for index, status in enumerate(statuses):
        add(f"{status}:recent", status, "ranked_desc", base_query)
        # High-value preset-owned title searches run immediately after the
        # primary status, before secondary statuses and optional oldest/rating/
        # favourites directions. A tight page budget must not silently skip
        # them because of strategy ordering.
        if index == 0 and settings.title_queries and not base_query:
            for query in list(dict.fromkeys(
                text.strip() for text in settings.title_queries if text.strip()
            ))[:6]:
                add(f"title:{query}", primary, "ranked_desc", query)
    if settings.use_oldest:
        add(f"{primary}:oldest", primary, "ranked_asc", base_query)
    if settings.use_rating:
        add(f"{primary}:rating", primary, "rating_desc", base_query)
    if settings.use_popular:
        add(f"{primary}:favourites", primary, "favourites_desc", base_query)
    if settings.expand_seeds and not base_query:
        for seed in list(dict.fromkeys(s.strip() for s in seeds if s.strip()))[:3]:
            add(f"seed:{seed}", primary, "ranked_desc", seed)
    return strategies


def collect_seeds(
    storage: Storage,
    sets_by_id: dict[int, Beatmapset],
    feedback_by_set: dict[int, tuple[str, ...]],
    favorites: frozenset[int],
) -> list[str]:
    """Artists/mappers from positive feedback, falling back to favourites."""
    scores: dict[str, float] = {}
    for set_id, verdicts in feedback_by_set.items():
        beatmapset = sets_by_id.get(set_id)
        if beatmapset is None:
            continue
        positive = [
            VERDICT_PREFERENCE.get(v, 0.0)
            for v in verdicts
            if v in ("like", "fine")
        ]
        if not positive:
            continue
        weight = max(positive)
        if beatmapset.display_artist:
            key = beatmapset.display_artist
            scores[key] = scores.get(key, 0.0) + weight
        if beatmapset.creator:
            key = beatmapset.creator
            scores[key] = scores.get(key, 0.0) + weight * 0.8
    if not scores:
        for set_id in favorites:
            beatmapset = sets_by_id.get(set_id)
            if beatmapset is None:
                continue
            if beatmapset.display_artist:
                scores[beatmapset.display_artist] = 1.0
            if beatmapset.creator:
                scores[beatmapset.creator] = 0.8
    return [
        name
        for name, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def _cache_key(query: SearchQuery) -> str:
    payload = {
        "params": query.cache_params(),
        "cursor": query.cursor,
        "cursor_string": query.cursor_string,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "search:" + hashlib.sha256(raw).hexdigest()


def _attributes_from_row(row: dict[str, Any]) -> DifficultyAttributes:
    return DifficultyAttributes(
        beatmap_id=int(row["beatmap_id"]),
        star_rating=row.get("star_rating"),
        aim_difficulty=row.get("aim_difficulty"),
        speed_difficulty=row.get("speed_difficulty"),
        slider_factor=row.get("slider_factor"),
        max_combo=row.get("max_combo"),
        source=row.get("source") or "osu_api_attributes",
        fetched_at=row.get("fetched_at") or now_iso(),
    )


def _apply_relevance_gate(
    candidates: list[Candidate],
    settings: RankingSettings,
    *,
    excluded: dict[str, int],
    exclusions: list[Exclusion],
) -> list[Candidate]:
    """Apply the preset's mandatory target gate before ranking.

    A high Recommendation score cannot be calculated before this point, so it
    cannot compensate for a failed target. Candidates that do not pass are
    still reported with a detailed reason instead of silently disappearing.
    """

    if not settings.relevance_targets:
        return candidates
    kept: list[Candidate] = []
    for candidate in candidates:
        check = evaluate_relevance(candidate, settings)
        checked = replace(candidate, relevance=check)
        if check.passed:
            kept.append(checked)
            continue
        statuses = {target.status for target in check.targets}
        if BELOW in statuses:
            reason = "соответствие целевым тегам ниже порога"
        elif SOURCE_UNAVAILABLE in statuses:
            reason = "нет .osu для целевых тегов (источник недоступен)"
        elif FAILED in statuses:
            reason = "ошибка анализа .osu для целевых тегов"
        elif PARTIAL in statuses:
            reason = "частичный анализ .osu для целевых тегов"
        elif NO_DATA in statuses:
            reason = "недостаточно данных для целевых тегов"
        elif NOT_ANALYZED in statuses:
            reason = "не анализировалось (нужен .osu или attributes)"
        else:
            reason = "соответствие целевым тегам ниже порога"
        excluded[reason] = excluded.get(reason, 0) + 1
        if len(exclusions) < MAX_EXCLUSION_SAMPLES:
            exclusions.append(
                Exclusion(
                    beatmapset_id=candidate.beatmapset.id,
                    title=candidate.beatmapset.display_title,
                    reason=check.reason or check.exclusion_reason,
                    stage="relevance",
                )
            )
    return kept


def _needs_sequence_analysis(settings: RankingSettings) -> bool:
    """Whether any target, ranking skill or legacy trait needs .osu data."""

    if any(
        skill_uses_sequences(target)
        for target in settings.relevance_targets
    ):
        return True
    if any(
        weight > 0 and skill_uses_sequences(skill_id)
        for skill_id, weight in settings.skill_weights.items()
    ):
        return True
    return any(
        weight > 0 and trait in {"streams", "bursts"}
        for trait, weight in settings.trait_targets.items()
    )


def _skill_uses_attributes(
    skill_id: str, seen: set[str] | None = None
) -> bool:
    """Whether a skill (directly or by component) can use API attributes."""

    visited = seen if seen is not None else set()
    if skill_id in visited:
        return False
    visited.add(skill_id)
    definition = SKILLS.get(skill_id)
    if definition is None:
        return False
    if set(definition.feature_names) & _ATTRIBUTE_FEATURE_KEYS:
        return True
    return any(
        _skill_uses_attributes(component, visited)
        for component in definition.component_names
    )


def _needs_attribute_enrichment(settings: RankingSettings) -> bool:
    """Keep the attribute budget only where it can affect a gate or ranking."""

    if not settings.relevance_targets:
        return True
    if any(
        not skill_uses_sequences(target)
        for target in settings.relevance_targets
    ):
        return True
    if any(
        weight > 0 and _skill_uses_attributes(skill_id)
        for skill_id, weight in settings.skill_weights.items()
    ):
        return True
    return any(
        weight > 0 and trait in {"aim", "speed", "control"}
        for trait, weight in settings.trait_targets.items()
    )


class DiscoveryEngine:
    def __init__(
        self,
        api: OsuApiClient | None,
        storage: Storage,
        settings: DiscoverySettings | None = None,
        *,
        sequence_loader: Callable[[int, bool], SequenceAnalysis] | None = None,
        sequence_cache_probe: Callable[[int], bool] | None = None,
        deadline_seconds: float | None = None,
    ) -> None:
        self.api = api
        self.storage = storage
        self.settings = settings or DiscoverySettings()
        self.sequence_loader = sequence_loader
        self.sequence_cache_probe = sequence_cache_probe
        # Checked between network operations, never inside an in-flight HTTP
        # request. Zero means an immediate deadline; the default is fixed in
        # config so this is an additional hard bound, not a preset limit that
        # can be raised to compensate for empty results.
        self.deadline_seconds = (
            float(config.DEFAULT_DISCOVERY_DEADLINE_SECONDS)
            if deadline_seconds is None
            else max(0.0, float(deadline_seconds))
        )

    # -- page fetching -----------------------------------------------------
    def _fetch_page(
        self,
        query: SearchQuery,
        *,
        budget: dict[str, Any],
        errors: list[str],
        notes: list[str],
        cache_hits: list[int],
        deadline_at: float | None = None,
        fresh: bool = False,
    ) -> SearchPage | None:
        budget["pages"] = int(budget.get("pages", 0)) + 1
        key = _cache_key(query)
        cached = None if fresh else self.storage.cache_get(key)
        if cached is not None:
            try:
                page = search_page_from_dict(cached)
                cache_hits[0] += 1
                return page
            except Exception as exc:  # corrupted cache payload
                logger.warning("повреждённый кэш поиска: %s", exc)
                notes.append("Повреждённая запись кэша пропущена.")
        if budget["requests"] >= self.settings.max_requests:
            return None
        if deadline_at is not None and time.perf_counter() >= deadline_at:
            return None
        # Count the attempt before the network operation. A timeout, server
        # error or other failure consumes the same budget as a success, so a
        # persistent failure cannot fan out across every search strategy.
        budget["requests"] += 1
        try:
            page = self.api.search_beatmapsets(query)
        except OsuAuthError as exc:
            budget["failed"] = int(budget.get("failed", 0)) + 1
            errors.append(exc.user_message())
            raise
        except OsuRateLimitError as exc:
            budget["failed"] = int(budget.get("failed", 0)) + 1
            errors.append(exc.user_message())
            budget["fatal"] = "rate_limit"
            return None
        except OsuForbiddenError as exc:
            budget["failed"] = int(budget.get("failed", 0)) + 1
            errors.append(
                f"{query.status}/{query.sort}: {exc.user_message()}"
            )
            budget["fatal"] = "forbidden"
            return None
        except (OsuNetworkError, OsuServerError) as exc:
            budget["failed"] = int(budget.get("failed", 0)) + 1
            errors.append(
                f"{query.status}/{query.sort}: {exc.user_message()}"
            )
            # The client already exhausted its finite retries for this
            # operation; trying every remaining strategy would only repeat the
            # same timeout/server cost.
            budget["fatal"] = "network_failure"
            return None
        except OsuApiError as exc:
            budget["failed"] = int(budget.get("failed", 0)) + 1
            errors.append(
                f"{query.status}/{query.sort}: {exc.user_message()}"
            )
            return None
        budget["succeeded"] = int(budget.get("succeeded", 0)) + 1
        try:
            self.storage.cache_set(
                key,
                search_page_to_dict(page),
                self.settings.cache_ttl_minutes,
            )
        except StorageError as exc:
            notes.append(f"Не удалось закэшировать страницу: {exc}")
        return page

    @staticmethod
    def _next_query(page: SearchPage, query: SearchQuery) -> SearchQuery | None:
        if page.cursor_string:
            return replace(query, cursor=None, cursor_string=page.cursor_string)
        if page.cursor:
            return replace(query, cursor=page.cursor, cursor_string=None)
        return None

    # -- attributes --------------------------------------------------------
    def _enrich_attributes(
        self,
        candidates: list[Candidate],
        ctx: RankingContext,
        *,
        budget: dict[str, Any],
        notes: list[str],
        errors: list[str],
        deadline_at: float | None = None,
    ) -> list[Candidate]:
        limit = self.settings.fetch_attributes_limit
        if limit <= 0 or not candidates:
            return candidates
        if not _needs_attribute_enrichment(ctx.settings):
            return candidates
        if budget.get("fatal"):
            return candidates
        if deadline_at is not None and time.perf_counter() >= deadline_at:
            return candidates
        # Preselect without computing the Recommendation score. For
        # specialized presets the target gate decides which cards deserve
        # attributes; a popular but irrelevant card must not consume the
        # attribute budget before the gate runs.
        if ctx.settings.relevance_targets:
            pre = [
                (evaluate_relevance(candidate, ctx.settings), candidate)
                for candidate in candidates
            ]
            if any(
                check.best_target is not None
                and check.best_target.display_score is not None
                for check, _ in pre
            ):
                pre.sort(
                    key=lambda item: (
                        -(item[0].best_target.display_score or 0)
                        if item[0].best_target is not None
                        else 0,
                        item[1].beatmapset.id,
                    )
                )
                top_ids = [
                    candidate.beatmap.id for _, candidate in pre[:limit]
                ]
            else:
                # Every target is sequence-only, so its pre-sequence score is
                # unknown. Use a generic pre-score rather than falling back to
                # the lowest beatmap ids.
                ranked = [
                    evaluate_candidate(candidate, ctx)
                    for candidate in candidates
                ]
                ranked.sort(
                    key=lambda item: (-item.score, item.beatmapset.id)
                )
                top_ids = [
                    item.candidate.beatmap.id for item in ranked[:limit]
                ]
        else:
            ranked = [
                evaluate_candidate(candidate, ctx) for candidate in candidates
            ]
            ranked.sort(key=lambda item: (-item.score, item.beatmapset.id))
            top_ids = [item.candidate.beatmap.id for item in ranked[:limit]]
        budget["attributes_candidates"] = int(
            budget.get("attributes_candidates", 0)
        ) + len(top_ids)
        by_beatmap_id = {
            candidate.beatmap.id: candidate for candidate in candidates
        }
        # One bounded query covers every selected candidate. A concurrent
        # search may fill an individual cache entry after this snapshot; a miss
        # is rechecked below before spending an API request, preserving request
        # budget/order semantics without N cache connections.
        cached_attributes = self.storage.all_attributes(top_ids)
        updated: dict[int, Candidate] = {}
        forbidden = False
        for beatmap_id in top_ids:
            candidate = by_beatmap_id[beatmap_id]
            row = cached_attributes.get(beatmap_id)
            if row is None:
                row = self.storage.get_attributes(beatmap_id)
            attributes: DifficultyAttributes | None = None
            if row is not None:
                attributes = _attributes_from_row(row)
                budget["attributes_cached"] = int(
                    budget.get("attributes_cached", 0)
                ) + 1
            elif not forbidden:
                if budget["requests"] >= self.settings.max_requests:
                    budget["attribute_limit"] = True
                    budget["attributes_stop"] = "request_budget"
                    notes.append(
                        "Бюджет запросов исчерпан до загрузки difficulty "
                        "attributes; часть карт оценена по косвенным признакам."
                    )
                    break
                if deadline_at is not None and time.perf_counter() >= deadline_at:
                    budget["attribute_limit"] = True
                    budget["attributes_stop"] = "deadline"
                    break
                # Attributes share the same hard counter as search pages.
                budget["requests"] += 1
                budget["attributes_requested"] = int(
                    budget.get("attributes_requested", 0)
                ) + 1
                try:
                    attributes = self.api.get_beatmap_attributes(beatmap_id)
                except OsuForbiddenError:
                    budget["failed"] = int(budget.get("failed", 0)) + 1
                    budget["attribute_limit"] = True
                    budget["attributes_stop"] = "forbidden"
                    forbidden = True
                    notes.append(
                        "Difficulty attributes недоступны (HTTP 403). "
                        "Aim-профиль использует только плотность, CS и теги."
                    )
                except (OsuNetworkError, OsuServerError) as exc:
                    budget["failed"] = int(budget.get("failed", 0)) + 1
                    budget["fatal"] = "network_failure"
                    errors.append(
                        f"attributes {beatmap_id}: {exc.user_message()}"
                    )
                    break
                except OsuRateLimitError as exc:
                    budget["failed"] = int(budget.get("failed", 0)) + 1
                    budget["fatal"] = "rate_limit"
                    errors.append(
                        f"attributes {beatmap_id}: {exc.user_message()}"
                    )
                    break
                except OsuApiError as exc:
                    budget["failed"] = int(budget.get("failed", 0)) + 1
                    errors.append(
                        f"attributes {beatmap_id}: {exc.user_message()}"
                    )
                else:
                    budget["succeeded"] = int(budget.get("succeeded", 0)) + 1
                    budget["attributes_succeeded"] = int(
                        budget.get("attributes_succeeded", 0)
                    ) + 1
                    self.storage.save_attributes(
                        beatmap_id,
                        {
                            "star_rating": attributes.star_rating,
                            "aim_difficulty": attributes.aim_difficulty,
                            "speed_difficulty": attributes.speed_difficulty,
                            "slider_factor": attributes.slider_factor,
                            "max_combo": attributes.max_combo,
                            "source": attributes.source,
                            "fetched_at": attributes.fetched_at,
                        },
                    )
            if attributes is not None:
                updated[beatmap_id] = replace(
                    candidate,
                    attributes=attributes,
                )
        if not updated:
            return candidates

        result: list[Candidate] = []
        for candidate in candidates:
            enriched = updated.get(candidate.beatmap.id)
            if enriched is None:
                result.append(candidate)
            else:
                traits = derive_features(
                    enriched.beatmap,
                    enriched.beatmapset,
                    enriched.attributes,
                )
                result.append(
                    replace(
                        enriched,
                        traits=traits,
                        skillsets=evaluate_skills(traits),
                    )
                )
        return result

    # -- sequence analysis -------------------------------------------------
    def _load_analysis(
        self, beatmap_id: int, allow_download: bool
    ) -> AnalysisEvidence:
        if self.sequence_loader is not None:
            sequence = self.sequence_loader(beatmap_id, allow_download)
            return AnalysisEvidence(
                beatmap_id=int(beatmap_id),
                status=(
                    evidence.PARTIAL
                    if getattr(sequence, "truncated", False)
                    else evidence.MEASURED
                ),
                source="download" if allow_download else "cache",
                sequence=sequence,
                scope=(
                    f"{sequence.analyzed_notes}/{sequence.total_notes} objects"
                ),
                analyzer_version=getattr(
                    sequence, "analyzer_version", 0
                ),
            )
        return analysis_store.analyze_beatmap(
            self.storage,
            beatmap_id,
            allow_download=allow_download,
        )

    def _sequence_is_cached(self, beatmap_id: int) -> bool:
        if self.sequence_cache_probe is not None:
            return bool(self.sequence_cache_probe(beatmap_id))
        return (
            self.storage.path.parent
            / "gameplay_cache"
            / f"{beatmap_id}.osu"
        ).exists()

    @staticmethod
    def _stable_sequence_key(candidate: Candidate) -> str:
        raw = f"{candidate.beatmapset.id}:{candidate.beatmap.id}"
        return hashlib.blake2b(
            raw.encode("utf-8"), digest_size=8
        ).hexdigest()

    def _fair_sequence_order(
        self, candidates: list[Candidate], ctx: RankingContext
    ) -> list[Candidate]:
        """Deterministic stratified order, not a pure general-score shortlist."""

        scored = [
            (evaluate_candidate(candidate, ctx).score, candidate)
            for candidate in candidates
        ]
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].beatmapset.id,
                item[1].beatmap.id,
            )
        )
        ranked = [candidate for _, candidate in scored]
        total = len(ranked)
        if total == 0:
            return []
        strata_count = min(SEQUENCE_STRATA, total)
        strata = [
            ranked[
                (index * total)
                // strata_count : ((index + 1) * total)
                // strata_count
            ]
            for index in range(strata_count)
        ]
        for stratum in strata:
            stratum.sort(key=self._stable_sequence_key)
        # Interleave score bands from the extremes inward, so even a small
        # budget sees more than the top of the general Recommendation score.
        band_order: list[int] = []
        left, right = 0, strata_count - 1
        while left <= right:
            band_order.append(left)
            left += 1
            if left <= right:
                band_order.append(right)
                right -= 1
        ordered = [strata[index] for index in band_order]
        result: list[Candidate] = []
        for slot in range(max(len(stratum) for stratum in ordered)):
            for stratum in ordered:
                if slot < len(stratum):
                    result.append(stratum[slot])
        return result

    @staticmethod
    def _with_analysis(
        candidate: Candidate, analysis: AnalysisEvidence
    ) -> Candidate:
        sequence = analysis.sequence if analysis.usable else None
        traits = derive_features(
            candidate.beatmap,
            candidate.beatmapset,
            candidate.attributes,
            sequence=sequence,
            analysis_status=analysis.status,
            analysis_scope=analysis.scope,
            analyzer_version=analysis.analyzer_version,
            attributes_status=(
                evidence.MEASURED
                if candidate.attributes is not None
                else evidence.NOT_ANALYZED
            ),
        )
        return replace(
            candidate,
            sequence=sequence,
            analysis=analysis,
            traits=traits,
            skillsets=evaluate_skills(traits),
        )

    def _enrich_sequences(
        self,
        candidates: list[Candidate],
        ctx: RankingContext,
        *,
        allow_download: bool,
        notes: list[str],
        deadline_at: float | None = None,
    ) -> tuple[list[Candidate], SequenceStats]:
        limit = int(self.settings.fetch_sequences_limit)
        required = _needs_sequence_analysis(ctx.settings)
        if not candidates:
            return candidates, SequenceStats(required=required)
        cached_only = limit <= 0 or not allow_download
        analysis_limit = (
            MAX_CACHED_SEQUENCE_ANALYSIS if limit <= 0 else limit
        )
        evidence_by_id = analysis_store.bulk_peek(
            self.storage, (item.beatmap.id for item in candidates)
        )
        if not required:
            # Attach the cheap status even when no selected skill needs
            # sequence data, so cards do not invent "no streams".
            return (
                [
                    self._with_analysis(
                        candidate,
                        evidence_by_id.get(
                            candidate.beatmap.id,
                            AnalysisEvidence(
                                beatmap_id=candidate.beatmap.id,
                                status=evidence.NOT_ANALYZED,
                            ),
                        ),
                    )
                    for candidate in candidates
                ],
                SequenceStats(required=False),
            )

        fair = self._fair_sequence_order(candidates, ctx)
        updated: dict[int, Candidate] = {}
        attempted: set[int] = set()
        downloads = 0
        cache_hits = 0
        failed = 0

        def store(candidate: Candidate, item: AnalysisEvidence) -> None:
            nonlocal downloads, cache_hits
            updated[candidate.beatmap.id] = self._with_analysis(
                candidate, item
            )
            evidence_by_id[candidate.beatmap.id] = item
            if item.source == "download":
                downloads += 1
            else:
                cache_hits += 1

        def attempt(candidate: Candidate, allow: bool) -> None:
            nonlocal failed
            tried = False
            try:
                item = self._load_analysis(candidate.beatmap.id, allow)
                tried = True
            except Exception as exc:  # noqa: BLE001 - one map must not stop
                logger.warning(
                    "sequence analysis failed for %s: %s",
                    candidate.beatmap.id,
                    exc,
                )
                item = AnalysisEvidence(
                    beatmap_id=candidate.beatmap.id,
                    status=evidence.FAILED,
                    source="download" if allow else "cache",
                    scope=type(exc).__name__,
                    analyzer_version=patterns.ANALYZER_VERSION,
                )
            if item.usable:
                store(candidate, item)
            elif item.status == evidence.FAILED:
                failed += 1
                if len(notes) < MAX_NOTES:
                    notes.append(
                        f".osu {candidate.beatmap.id}: "
                        + (item.scope or "анализ не удался")
                    )
            if not tried and len(notes) < MAX_NOTES:
                notes.append(f".osu {candidate.beatmap.id}: файл недоступен.")

        # Existing database/cache evidence goes first, then bounded downloads.
        for candidate in fair:
            if len(updated) >= analysis_limit:
                break
            if deadline_at is not None and time.perf_counter() >= deadline_at:
                break
            beatmap_id = candidate.beatmap.id
            item = evidence_by_id.get(beatmap_id)
            if item is not None and item.usable:
                attempted.add(beatmap_id)
                store(candidate, item)
                continue
            if self._sequence_is_cached(beatmap_id):
                attempted.add(beatmap_id)
                attempt(candidate, False)
        if not cached_only:
            for candidate in fair:
                if len(updated) >= analysis_limit:
                    break
                if deadline_at is not None and time.perf_counter() >= deadline_at:
                    break
                beatmap_id = candidate.beatmap.id
                if beatmap_id in attempted or beatmap_id in updated:
                    continue
                attempted.add(beatmap_id)
                attempt(candidate, True)

        if deadline_at is not None and time.perf_counter() >= deadline_at:
            notes.append(
                "Sequence-анализ остановлен по дедлайну; часть карт осталась "
                "без анализа."
            )

        result: list[Candidate] = []
        analyzed = partial = not_analyzed = unavailable = failed_status = 0
        for candidate in candidates:
            item = evidence_by_id.get(candidate.beatmap.id)
            if item is None:
                item = AnalysisEvidence(
                    beatmap_id=candidate.beatmap.id,
                    status=evidence.NOT_ANALYZED,
                )
            if item.status == evidence.MEASURED:
                analyzed += 1
            elif item.status == evidence.PARTIAL:
                partial += 1
            elif item.status == evidence.SOURCE_UNAVAILABLE:
                unavailable += 1
            elif item.status == evidence.FAILED:
                failed_status += 1
            else:
                not_analyzed += 1
            enriched = updated.get(candidate.beatmap.id)
            result.append(
                enriched
                if enriched is not None
                else self._with_analysis(candidate, item)
            )
        unanalyzed = len(candidates) - analyzed - partial
        if limit <= 0:
            notes.append(
                "Лимит .osu = 0: новые файлы не скачиваются; используется "
                "только локальный кэш (не более 50 карт)."
            )
        if analyzed or partial:
            notes.append(
                f"Sequence-анализ .osu: {analyzed + partial} карт "
                f"({partial} частично); streams, bursts и геометрия считаются "
                "только по проанализированным объектам."
            )
        if failed:
            notes.append(
                f"Sequence-анализ: ошибка разбора для {failed} карт; их "
                "признаки остаются неизвестными, а не нулевыми."
            )
        if unavailable:
            notes.append(
                f"Нет .osu в кеше для {unavailable} карт: это "
                "«источник недоступен», а не «паттернов нет»."
            )
        if unanalyzed:
            cap = "0 (только кэш, до 50)" if limit <= 0 else str(analysis_limit)
            notes.append(
                f"Sequence-анализ: покрыто {analyzed + partial} из "
                f"{len(candidates)}; {unanalyzed} без анализа "
                f"(лимит {cap})."
            )
        stats = SequenceStats(
            required=True,
            analyzed=analyzed + partial,
            unanalyzed=unanalyzed,
            downloads=downloads,
            cache_hits=cache_hits,
            failed=failed_status,
            partial=partial,
            source_unavailable=unavailable,
            not_analyzed=not_analyzed,
            limit=limit,
            cached_only=cached_only,
        )
        return result, stats

    # -- main entry point --------------------------------------------------
    def run(
        self,
        filters: Filters,
        ranking_settings: RankingSettings,
        *,
        record: bool = True,
        strategy_offset: int = 0,
        fresh: bool = False,
    ) -> DiscoveryResult:
        validate_filters(filters)
        validate_ranking(ranking_settings)
        if self.api is None:
            raise ValueError("Для нового API-поиска нужен osu! API client.")
        started_perf = time.perf_counter()
        started = now_iso()
        http_before = int(getattr(self.api, "http_attempts", 0))
        oauth_before = int(getattr(self.api, "token_requests", 0))
        deadline_at = started_perf + self.deadline_seconds
        errors: list[str] = []
        notes: list[str] = []
        cache_hits = [0]
        budget: dict[str, Any] = {
            "requests": 0,
            "succeeded": 0,
            "failed": 0,
            "pages": 0,
            "fatal": "",
            "attributes_candidates": 0,
            "attributes_requested": 0,
            "attributes_succeeded": 0,
            "attributes_cached": 0,
            "attribute_limit": False,
            "attributes_stop": "",
        }
        # Do not let broad pagination consume the whole budget before the
        # optional difficulty attributes are requested. This keeps the first
        # search bounded without silently disabling measurable skillsets.
        resolved_requests = max(1, int(self.settings.max_requests))
        attribute_budget = (
            max(0, int(self.settings.fetch_attributes_limit))
            if _needs_attribute_enrichment(ranking_settings)
            else 0
        )
        attribute_reserve = min(
            attribute_budget,
            max(0, resolved_requests - 1),
        )
        page_budget = max(1, resolved_requests - attribute_reserve)
        excluded: dict[str, int] = {}
        exclusions: list[Exclusion] = []
        strategies_used: list[str] = []
        total_fetched = 0
        complete = True
        stop_reason = ""

        def mark_incomplete(reason: str, *, override: bool = False) -> None:
            nonlocal complete, stop_reason
            complete = False
            if not budget.get("fatal"):
                stop_reason = reason if override else (stop_reason or reason)

        def deadline_hit() -> bool:
            nonlocal complete
            if time.perf_counter() < deadline_at:
                return False
            complete = False
            if not budget.get("fatal"):
                budget["fatal"] = "deadline"
                notes.append(
                    f"Дедлайн discovery {self.deadline_seconds:g} с достигнут: "
                    "новые сетевые операции остановлены; выдача может быть "
                    "неполной. Уже начатый запрос мог завершиться после "
                    "дедлайна."
                )
            return True

        feedback_by_set = self.storage.all_feedback()
        favorites = self.storage.favorite_ids()
        seen_counts = self.storage.seen_counts()
        hidden = frozenset(
            set_id for set_id, verdicts in feedback_by_set.items() if "hide" in verdicts
        )
        played = frozenset(
            set_id
            for set_id, verdicts in feedback_by_set.items()
            if "played" in verdicts
        )
        feedback_sets = self.storage.get_beatmapsets(feedback_by_set.keys())
        favorite_sets = self.storage.get_beatmapsets(favorites)
        seeds = collect_seeds(
            self.storage,
            {**favorite_sets, **feedback_sets},
            feedback_by_set,
            favorites,
        )

        strategies = build_strategies(filters, self.settings, seeds)
        if strategies and strategy_offset:
            offset = int(strategy_offset) % len(strategies)
            if offset:
                strategies = strategies[offset:] + strategies[:offset]

        def base_query(strategy: SearchStrategy) -> SearchQuery:
            return SearchQuery(
                mode=filters.modes[0],
                status=strategy.status,
                sort=strategy.sort,
                query=strategy.query,
                nsfw=not filters.exclude_nsfw,
            )

        strategy_stats: dict[str, dict[str, Any]] = {
            strategy.name: {
                "name": strategy.name,
                "status": strategy.status,
                "sort": strategy.sort,
                "query": strategy.query,
                "params": base_query(strategy).to_params(),
                "executed": False,
                "pages": 0,
                "api_requests": 0,
                "cache_hits": 0,
                "raw_rows": 0,
                "raw_response_rows": 0,
                "cursor_advances": 0,
                "last_has_more": False,
                "exhausted": False,
                "stop_reason": "",
                "error": "",
            }
            for strategy in strategies
        }
        known: dict[int, Beatmapset] = {}
        sources: dict[int, list[str]] = {}

        for strategy in strategies:
            if budget.get("fatal") or deadline_hit():
                break
            if budget["requests"] >= page_budget:
                mark_incomplete("request_budget", override=True)
                notes.append(
                    f"Бюджет страниц ({page_budget} запросов из "
                    f"{resolved_requests}) исчерпан; выдача может быть неполной. "
                    "Остальные запросы зарезервированы под difficulty attributes."
                )
                break
            strategies_used.append(strategy.name)
            stats = strategy_stats[strategy.name]
            stats["executed"] = True
            stats["params"] = base_query(strategy).to_params()
            query = base_query(strategy)
            seen_cursors: set[str] = set()
            pages = 0
            while True:
                if budget.get("fatal") or deadline_hit():
                    break
                if budget["requests"] >= page_budget:
                    mark_incomplete("request_budget", override=True)
                    stats["stop_reason"] = "request_budget"
                    notes.append(
                        f"Бюджет страниц исчерпан в стратегии {strategy.name}."
                    )
                    break
                requests_before = int(budget["requests"])
                cache_before = cache_hits[0]
                page = self._fetch_page(
                    query,
                    budget=budget,
                    errors=errors,
                    notes=notes,
                    cache_hits=cache_hits,
                    deadline_at=deadline_at,
                    fresh=fresh,
                )
                stats["api_requests"] += int(budget["requests"]) - requests_before
                stats["cache_hits"] += cache_hits[0] - cache_before
                if page is None:
                    if budget.get("fatal"):
                        mark_incomplete(str(budget["fatal"]))
                    elif budget["requests"] >= page_budget:
                        mark_incomplete("request_budget", override=True)
                        stats["stop_reason"] = "request_budget"
                    else:
                        mark_incomplete("request_failed")
                        stats["stop_reason"] = "request_failed"
                    break
                if page.error:
                    mark_incomplete("api_error")
                    stats["stop_reason"] = stats["stop_reason"] or "api_error"
                    stats["error"] = str(page.error)
                    notes.append(
                        f"{strategy.name}: API сообщил «{page.error}»"
                    )
                for warning in page.warnings:
                    if len(notes) < MAX_NOTES:
                        notes.append(f"{strategy.name}: {warning}")
                total_fetched += len(page.beatmapsets)
                stats["pages"] += 1
                stats["raw_rows"] += len(page.beatmapsets)
                stats["raw_response_rows"] += int(page.raw_count) or len(page.beatmapsets)
                stats["last_has_more"] = bool(page.has_more)
                for beatmapset in page.beatmapsets:
                    if beatmapset.id not in known:
                        known[beatmapset.id] = beatmapset
                        sources[beatmapset.id] = []
                    if strategy.name not in sources[beatmapset.id]:
                        sources[beatmapset.id].append(strategy.name)
                pages += 1
                if pages >= self.settings.max_pages_per_strategy:
                    if page.has_more:
                        mark_incomplete("page_limit")
                        stats["stop_reason"] = "page_limit"
                        notes.append(
                            f"{strategy.name}: остановлено на "
                            f"{self.settings.max_pages_per_strategy} страницах."
                        )
                    else:
                        stats["exhausted"] = True
                    break
                next_query = self._next_query(page, query)
                if next_query is None:
                    stats["exhausted"] = True
                    break
                cursor_token = json.dumps(
                    {
                        "cursor": next_query.cursor,
                        "cursor_string": next_query.cursor_string,
                    },
                    sort_keys=True,
                )
                if cursor_token in seen_cursors:
                    mark_incomplete("repeated_cursor")
                    stats["stop_reason"] = "repeated_cursor"
                    notes.append(
                        f"{strategy.name}: API вернул повторяющийся курсор; "
                        "пагинация остановлена."
                    )
                    break
                seen_cursors.add(cursor_token)
                stats["cursor_advances"] += 1
                stats["last_has_more"] = True
                query = next_query
                if not page.beatmapsets and not page.has_more:
                    stats["exhausted"] = True
                    break

        for name, stats in strategy_stats.items():
            if not stats["executed"] and not stats["stop_reason"]:
                stats["stop_reason"] = "not_attempted"
        strategy_runs = tuple(
            StrategyRunStats(**stats) for stats in strategy_stats.values()
        )

        if known:
            try:
                self.storage.save_beatmapsets(known.values())
            except StorageError as exc:
                errors.append(f"Не удалось сохранить карты в SQLite: {exc}")
        fetch_elapsed = time.perf_counter() - started_perf
        filter_started = time.perf_counter()

        # Strict filters.
        user = UserFilterData(
            seen=frozenset(seen_counts),
            played=played,
            favorites=favorites,
            hidden=hidden,
        )
        candidates: list[Candidate] = []
        unknown_field_counts: dict[str, int] = {}
        for set_id, beatmapset in known.items():
            decision = apply_filters(
                beatmapset,
                filters,
                user,
                target_stars=ranking_settings.comfort_stars,
            )
            if decision.passed and decision.beatmap is not None:
                for field in decision.unknown_fields:
                    unknown_field_counts[field] = (
                        unknown_field_counts.get(field, 0) + 1
                    )
                traits = derive_features(decision.beatmap, beatmapset)
                candidates.append(
                    Candidate(
                        beatmapset=beatmapset,
                        beatmap=decision.beatmap,
                        sources=tuple(sources.get(set_id, ())),
                        traits=traits,
                        skillsets=evaluate_skills(traits),
                        within_tolerance=decision.within_tolerance,
                        unknown_fields=decision.unknown_fields,
                    )
                )
            else:
                reason = decision.reason or "не прошла фильтры"
                excluded[reason] = excluded.get(reason, 0) + 1
                if len(exclusions) < MAX_EXCLUSION_SAMPLES:
                    exclusions.append(
                        Exclusion(
                            beatmapset_id=set_id,
                            title=beatmapset.display_title,
                            reason=reason,
                        )
                    )

        if unknown_field_counts:
            detail = ", ".join(
                f"{field}: {count}" for field, count in sorted(unknown_field_counts.items())
            )
            notes.append(
                "AR/OD/CS/HP-фильтры не применены к части карт, потому что API "
                f"не вернул значения ({detail}). Карты не исключены вслепую."
            )

        filter_elapsed = time.perf_counter() - filter_started

        # Ranking context and optional attribute enrichment.
        recent = self.storage.recent_sets(40)
        ctx = RankingContext.from_user_data(
            ranking_settings,
            feedback_by_set,
            {**feedback_sets, **known},
            seen_counts,
            recent_sets=recent,
        )
        attributes_started = time.perf_counter()
        candidates = self._enrich_attributes(
            candidates,
            ctx,
            budget=budget,
            notes=notes,
            errors=errors,
            deadline_at=deadline_at,
        )
        attributes_elapsed = time.perf_counter() - attributes_started
        if budget.get("attribute_limit"):
            mark_incomplete(str(budget.get("attributes_stop") or "attribute_limit"))
        sequences_started = time.perf_counter()
        candidates, sequence_stats = self._enrich_sequences(
            candidates,
            ctx,
            # A fatal network/deadline stop must not trigger more .osu
            # downloads; cached files can still be analyzed locally.
            allow_download=not bool(budget.get("fatal")),
            notes=notes,
            deadline_at=deadline_at,
        )
        sequences_elapsed = time.perf_counter() - sequences_started
        strict_passed = len(candidates)
        filter_excluded = dict(excluded)
        relevance_evaluated = len(candidates)
        excluded_before_relevance = dict(excluded)
        candidates = _apply_relevance_gate(
            candidates,
            ranking_settings,
            excluded=excluded,
            exclusions=exclusions,
        )
        relevance_passed = len(candidates)
        relevance_excluded = {
            reason: count - excluded_before_relevance.get(reason, 0)
            for reason, count in excluded.items()
            if count > excluded_before_relevance.get(reason, 0)
        }
        if ranking_settings.relevance_targets and strict_passed != relevance_passed:
            notes.append(
                "Целевые теги: исключено "
                f"{strict_passed - relevance_passed} из {strict_passed} карт, "
                f"прошедших строгие фильтры (порог "
                f"{ranking_settings.relevance_threshold:.0%}, нужно превышение "
                "хотя бы по одному из трёх тегов)."
            )
        rank_started = time.perf_counter()
        # Rebuild the context in case attributes changed nothing about user
        # data; it is cheap and keeps the code path uniform.
        diversification_notes: list[str] = []
        candidate_limit = max(1, int(self.settings.max_candidates))
        truncated = len(candidates) > candidate_limit
        scored = rank_candidates(
            candidates,
            ctx,
            limit=candidate_limit if truncated else None,
            notes=diversification_notes,
        )
        if truncated:
            mark_incomplete("candidate_limit")
            notes.append(
                f"Показано не более {candidate_limit} кандидатов из "
                f"{len(candidates)}; выдача ограничена настройкой "
                "«Максимум кандидатов»."
            )
        notes.extend(diversification_notes[:3])
        # A request chain may have crossed the deadline even when it started
        # before it; reflect that in the final completeness flag.
        deadline_hit()
        rank_elapsed = time.perf_counter() - rank_started
        total_elapsed = time.perf_counter() - started_perf

        if not scored:
            if not known and errors:
                notes.append(
                    "API не вернул ни одной карты из-за ошибок; это не означает, "
                    "что подходящих карт не существует."
                )
            elif (
                strict_passed
                and ranking_settings.relevance_targets
                and relevance_passed == 0
            ):
                detail = ", ".join(
                    f"{reason} ({count})"
                    for reason, count in sorted(
                        excluded.items(), key=lambda item: (-item[1], item[0])
                    )[:4]
                )
                notes.append(
                    f"Строгие фильтры прошли {strict_passed} карт, но "
                    "обязательный гейт целевых тегов не пропустил ни одну"
                    + (f": {detail}." if detail else ".")
                )
            elif strict_passed:
                notes.append(
                    f"Строгие фильтры прошли {strict_passed} карт, но "
                    "ранжирование не вернуло ни одной карты."
                )
            elif known:
                top_reasons = sorted(
                    excluded.items(), key=lambda item: (-item[1], item[0])
                )[:4]
                detail = ", ".join(f"{reason} ({count})" for reason, count in top_reasons)
                notes.append(
                    f"Строгие фильтры исключили все {len(known)} уникальных карт"
                    + (f": {detail}." if detail else ".")
                )
        if total_fetched > len(known):
            notes.append(
                f"Дедупликация: {total_fetched} строк API → {len(known)} "
                "уникальных beatmapsets."
            )
        if budget["failed"]:
            complete = False
            if not budget.get("fatal"):
                stop_reason = stop_reason or "request_failed"
            notes.append(
                "Сетевые запросы: успешно "
                f"{budget.get('succeeded', 0)}, ошибок {budget['failed']}; "
                "результат неполный."
            )
        if budget.get("fatal"):
            stop_reason = str(budget["fatal"])
        elif complete:
            stop_reason = "complete"
        elif not stop_reason:
            stop_reason = "limits"
        if not complete and not any("неполн" in note for note in notes):
            notes.append(
                "Выдача может быть неполной: "
                f"discovery остановлен ({stop_reason})."
            )

        finished = now_iso()
        result = DiscoveryResult(
            scored=tuple(scored),
            total_fetched=total_fetched,
            raw_rows=total_fetched,
            raw_response_rows=sum(
                stats["raw_response_rows"] for stats in strategy_stats.values()
            ),
            unique_raw=len(known),
            passed_filters=strict_passed,
            passed_relevance=relevance_passed,
            filter_excluded=dict(
                sorted(
                    filter_excluded.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            relevance_evaluated=relevance_evaluated,
            relevance_excluded=dict(
                sorted(
                    relevance_excluded.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            attributes_candidates=int(
                budget.get("attributes_candidates", 0)
            ),
            attributes_requested=int(
                budget.get("attributes_requested", 0)
            ),
            attributes_succeeded=int(
                budget.get("attributes_succeeded", 0)
            ),
            attributes_cached=int(budget.get("attributes_cached", 0)),
            candidate_limit_hit=bool(truncated),
            strategy_runs=strategy_runs,
            excluded=dict(
                sorted(excluded.items(), key=lambda item: (-item[1], item[0]))
            ),
            exclusions=tuple(exclusions),
            errors=tuple(dict.fromkeys(errors)),
            notes=tuple(dict.fromkeys(notes))[:MAX_NOTES],
            complete=complete,
            api_requests=budget["requests"],
            requests_succeeded=int(budget.get("succeeded", 0)),
            requests_failed=int(budget.get("failed", 0)),
            pages_attempted=int(budget.get("pages", 0)),
            http_attempts=max(
                0, int(getattr(self.api, "http_attempts", 0)) - http_before
            ),
            oauth_requests=max(
                0,
                int(getattr(self.api, "token_requests", 0)) - oauth_before,
            ),
            cache_hits=cache_hits[0],
            stop_reason=stop_reason,
            deadline_seconds=self.deadline_seconds,
            strategies=tuple(strategies_used),
            started_at=started,
            finished_at=finished,
            filters_used=filters,
            ranking_used=ranking_settings,
            timings={
                "fetch": round(fetch_elapsed, 3),
                "filter": round(filter_elapsed, 3),
                "attributes": round(attributes_elapsed, 3),
                "sequences": round(sequences_elapsed, 3),
                "rank": round(rank_elapsed, 3),
                "total": round(total_elapsed, 3),
            },
            sequence_required=sequence_stats.required,
            sequences_complete=sequence_stats.complete,
            sequence_analyzed=sequence_stats.analyzed,
            sequence_unanalyzed=sequence_stats.unanalyzed,
            sequence_downloads=sequence_stats.downloads,
            sequence_cache_hits=sequence_stats.cache_hits,
            sequence_partial=sequence_stats.partial,
            sequence_source_unavailable=sequence_stats.source_unavailable,
            sequence_not_analyzed=sequence_stats.not_analyzed,
            sequence_failed=sequence_stats.failed,
        )
        if record:
            try:
                self.storage.record_search_run(result)
            except StorageError as exc:
                logger.warning("не удалось записать историю поиска: %s", exc)
        return result

    def rerank_stored(
        self,
        filters: Filters,
        ranking_settings: RankingSettings,
        *,
        limit: int | None = None,
        all_candidates: bool = False,
    ) -> DiscoveryResult:
        """Re-rank stored beatmapsets without making an API request.

        This is deliberately an explicit, bounded local pass. The result is
        marked ``origin="local_cache"`` and never claims to be an exhaustive
        osu! search. The caller decides whether the local pool is large enough
        or whether a real discovery run should extend it.

        ``all_candidates=True`` keeps every candidate that passed the strict
        filters and relevance gate. The randomizer uses that broad pool instead
        of the display cap, so lower-ranked maps remain reachable.
        """

        validate_filters(filters)
        validate_ranking(ranking_settings)
        started_perf = time.perf_counter()
        started = now_iso()
        feedback_by_set = self.storage.all_feedback()
        favorites = self.storage.favorite_ids()
        seen_counts = self.storage.seen_counts()
        hidden = frozenset(
            set_id for set_id, verdicts in feedback_by_set.items() if "hide" in verdicts
        )
        played = frozenset(
            set_id
            for set_id, verdicts in feedback_by_set.items()
            if "played" in verdicts
        )
        feedback_sets = self.storage.get_beatmapsets(feedback_by_set.keys())
        limit_sets = max(1000, int(self.settings.max_candidates) * 4)
        # A deterministic newest-first slice would permanently starve older
        # stored sets once the catalog exceeds the bound. Use a bounded,
        # evenly spaced sample across the whole catalog instead.
        stored = self.storage.list_beatmapsets(
            limit=limit_sets,
            balanced=True,
        )
        total_stored = self.storage.beatmapset_count()
        filter_started = time.perf_counter()
        if not stored:
            return DiscoveryResult(
                notes=(
                    "Локальный кеш пуст: сохранённых карт для переранжирования нет.",
                ),
                complete=False,
                origin="local_cache",
                started_at=started,
                finished_at=now_iso(),
                filters_used=filters,
                ranking_used=ranking_settings,
                timings={
                    "filter": 0.0,
                    "rank": 0.0,
                    "total": round(time.perf_counter() - started_perf, 3),
                },
            )

        # Only attributes belonging to the sampled sets can be used below.
        attributes = self.storage.all_attributes(
            beatmap.id for item in stored for beatmap in item.beatmaps
        )
        user = UserFilterData(
            seen=frozenset(seen_counts),
            played=played,
            favorites=favorites,
            hidden=hidden,
        )
        candidates: list[Candidate] = []
        excluded: dict[str, int] = {}
        exclusions: list[Exclusion] = []
        unknown_field_counts: dict[str, int] = {}
        for beatmapset in stored:
            decision = apply_filters(
                beatmapset,
                filters,
                user,
                target_stars=ranking_settings.comfort_stars,
            )
            if not decision.passed or decision.beatmap is None:
                reason = decision.reason or "не прошла фильтры"
                excluded[reason] = excluded.get(reason, 0) + 1
                if len(exclusions) < MAX_EXCLUSION_SAMPLES:
                    exclusions.append(
                        Exclusion(
                            beatmapset_id=beatmapset.id,
                            title=beatmapset.display_title,
                            reason=reason,
                        )
                    )
                continue
            for field in decision.unknown_fields:
                unknown_field_counts[field] = (
                    unknown_field_counts.get(field, 0) + 1
                )
            row = attributes.get(decision.beatmap.id)
            attrs = _attributes_from_row(row) if row is not None else None
            traits = derive_features(decision.beatmap, beatmapset, attrs)
            candidates.append(
                Candidate(
                    beatmapset=beatmapset,
                    beatmap=decision.beatmap,
                    sources=("local-cache",),
                    attributes=attrs,
                    traits=traits,
                    skillsets=evaluate_skills(traits),
                    within_tolerance=decision.within_tolerance,
                    unknown_fields=decision.unknown_fields,
                )
            )

        filter_elapsed = time.perf_counter() - filter_started
        recent = self.storage.recent_sets(40)
        ctx = RankingContext.from_user_data(
            ranking_settings,
            feedback_by_set,
            {**feedback_sets, **{item.id: item for item in stored}},
            seen_counts,
            recent_sets=recent,
        )
        sequence_notes: list[str] = []
        sequences_started = time.perf_counter()
        candidates, sequence_stats = self._enrich_sequences(
            candidates,
            ctx,
            allow_download=False,
            notes=sequence_notes,
        )
        sequences_elapsed = time.perf_counter() - sequences_started
        strict_passed = len(candidates)
        filter_excluded = dict(excluded)
        relevance_evaluated = len(candidates)
        excluded_before_relevance = dict(excluded)
        candidates = _apply_relevance_gate(
            candidates,
            ranking_settings,
            excluded=excluded,
            exclusions=exclusions,
        )
        relevance_passed = len(candidates)
        relevance_excluded = {
            reason: count - excluded_before_relevance.get(reason, 0)
            for reason, count in excluded.items()
            if count > excluded_before_relevance.get(reason, 0)
        }
        notes = [
            f"Локальный кеш SQLite: {total_stored} сохранённых карт; для "
            f"переранжирования рассмотрено {len(stored)} (равномерная выборка "
            f"по id, без сети). {strict_passed} прошли строгие фильтры, "
            f"{relevance_passed} прошли целевые теги. Это повторное "
            "ранжирование без нового API-поиска."
        ]
        notes.extend(sequence_notes)
        if strict_passed != relevance_passed:
            notes.append(
                "Обязательный гейт целевых тегов исключил "
                f"{strict_passed - relevance_passed} карт (порог "
                f"{ranking_settings.relevance_threshold:.0%})."
            )
        if unknown_field_counts:
            detail = ", ".join(
                f"{field}: {count}"
                for field, count in sorted(unknown_field_counts.items())
            )
            notes.append(
                "AR/OD/CS/HP-фильтры не применены к части карт: API не вернул "
                f"значения ({detail})."
            )
        candidate_limit = max(1, int(self.settings.max_candidates))
        if all_candidates:
            cap = max(1, len(candidates))
        elif limit is None:
            cap = candidate_limit
        else:
            cap = max(1, int(limit))
        truncated = len(candidates) > cap
        if truncated:
            notes.append(
                f"Локальный пул ограничен {cap} кандидатами из {len(candidates)} "
                "прошедших фильтры."
            )
        elif all_candidates and candidates:
            notes.append(
                f"Пул случайной подборки: все {len(candidates)} кандидатов, "
                "прошедших фильтры и гейт; обрезки до топ-N нет, разнообразие "
                "учитывается при взвешенной выборке."
            )
        rank_started = time.perf_counter()
        if all_candidates:
            # Random sampling needs every eligible candidate, not the greedy
            # display ordering. Ranking still computes each score (including
            # taste, skillsets and repeat penalty); diversity is applied later
            # by the weighted sampler instead of an O(n^2) display pass.
            scored = [
                evaluate_candidate(candidate, ctx)
                for candidate in candidates
            ]
            scored.sort(
                key=lambda item: (
                    -item.score,
                    item.beatmapset.id,
                    item.beatmap.id,
                )
            )
        else:
            scored = rank_candidates(
                candidates,
                ctx,
                limit=cap if truncated else None,
            )
        rank_elapsed = time.perf_counter() - rank_started
        return DiscoveryResult(
            scored=tuple(scored),
            total_fetched=len(stored),
            raw_rows=len(stored),
            unique_raw=len(stored),
            passed_filters=strict_passed,
            passed_relevance=relevance_passed,
            filter_excluded=dict(
                sorted(
                    filter_excluded.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            relevance_evaluated=relevance_evaluated,
            relevance_excluded=dict(
                sorted(
                    relevance_excluded.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            candidate_limit_hit=bool(truncated),
            excluded=dict(
                sorted(excluded.items(), key=lambda item: (-item[1], item[0]))
            ),
            exclusions=tuple(exclusions),
            notes=tuple(notes[:MAX_NOTES]),
            complete=False,
            api_requests=0,
            cache_hits=len(stored),
            strategies=("local-cache",),
            started_at=started,
            finished_at=now_iso(),
            filters_used=filters,
            ranking_used=ranking_settings,
            origin="local_cache",
            timings={
                "filter": round(filter_elapsed, 3),
                "sequences": round(sequences_elapsed, 3),
                "rank": round(rank_elapsed, 3),
                "total": round(time.perf_counter() - started_perf, 3),
            },
            sequence_required=sequence_stats.required,
            sequences_complete=sequence_stats.complete,
            sequence_analyzed=sequence_stats.analyzed,
            sequence_unanalyzed=sequence_stats.unanalyzed,
            sequence_downloads=sequence_stats.downloads,
            sequence_cache_hits=sequence_stats.cache_hits,
            sequence_partial=sequence_stats.partial,
            sequence_source_unavailable=sequence_stats.source_unavailable,
            sequence_not_analyzed=sequence_stats.not_analyzed,
            sequence_failed=sequence_stats.failed,
        )


def preview_candidates(
    beatmapset: Beatmapset,
    filters: Filters,
    ranking_settings: RankingSettings,
    user: UserFilterData | None = None,
) -> list[ScoredCandidate]:
    """Evaluate a single stored beatmapset (used by tests and diagnostics)."""
    decision = apply_filters(
        beatmapset,
        filters,
        user,
        target_stars=ranking_settings.comfort_stars,
    )
    if not decision.passed or decision.beatmap is None:
        return []
    candidate = Candidate(
        beatmapset=beatmapset,
        beatmap=decision.beatmap,
        traits=derive_features(decision.beatmap, beatmapset),
        within_tolerance=decision.within_tolerance,
        unknown_fields=decision.unknown_fields,
    )
    candidate = replace(
        candidate, skillsets=evaluate_skills(candidate.traits)
    )
    check = evaluate_relevance(candidate, ranking_settings)
    if not check.passed:
        return []
    candidate = replace(candidate, relevance=check)
    ctx = RankingContext.from_user_data(
        ranking_settings, {}, {beatmapset.id: beatmapset}, {}
    )
    return [evaluate_candidate(candidate, ctx)]
