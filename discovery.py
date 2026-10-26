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

import config
from feedback import VERDICT_PREFERENCE, build_taste_profile
from filters import UserFilterData, apply_filters, validate_filters
from features import derive_features
from gameplay import GameplayError
from models import (
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
    now_iso,
    search_page_from_dict,
    search_page_to_dict,
)
from patterns import SEQUENCE_FEATURE_KEYS, load_analysis
from osu_api import (
    OsuApiClient,
    OsuApiError,
    OsuAuthError,
    OsuForbiddenError,
    OsuRateLimitError,
)
from ranking import RankingContext, evaluate_candidate, rank_candidates, validate_ranking
from relevance import evaluate_relevance
from skillsets import SKILLS, evaluate_skills
from storage import Storage, StorageError

logger = logging.getLogger("osu_picker.discovery")

MAX_NOTES = 12
MAX_EXCLUSION_SAMPLES = 80


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

    def add(name: str, status: str, sort: str, query: str | None) -> None:
        key = (name, status, sort, query)
        if key not in {(s.name, s.status, s.sort, s.query) for s in strategies}:
            strategies.append(SearchStrategy(name, status, sort, query))

    for status in statuses:
        add(f"{status}:recent", status, "ranked_desc", base_query)
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


def _skill_uses_sequences(skill_id: str, seen: set[str] | None = None) -> bool:
    """Whether a skill (directly or through a component) needs .osu evidence."""

    visited = seen if seen is not None else set()
    if skill_id in visited:
        return False
    visited.add(skill_id)
    definition = SKILLS.get(skill_id)
    if definition is None:
        return False
    if set(definition.feature_names) & SEQUENCE_FEATURE_KEYS:
        return True
    return any(
        _skill_uses_sequences(component, visited)
        for component in definition.component_names
    )


def _needs_sequence_analysis(settings: RankingSettings) -> bool:
    return any(
        _skill_uses_sequences(target)
        for target in settings.relevance_targets
    )


class DiscoveryEngine:
    def __init__(
        self,
        api: OsuApiClient | None,
        storage: Storage,
        settings: DiscoverySettings | None = None,
        *,
        sequence_loader: Callable[[int, bool], SequenceAnalysis] | None = None,
    ) -> None:
        self.api = api
        self.storage = storage
        self.settings = settings or DiscoverySettings()
        self.sequence_loader = sequence_loader

    # -- page fetching -----------------------------------------------------
    def _fetch_page(
        self,
        query: SearchQuery,
        *,
        budget: dict[str, int],
        errors: list[str],
        notes: list[str],
        cache_hits: list[int],
    ) -> SearchPage | None:
        key = _cache_key(query)
        cached = self.storage.cache_get(key)
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
        try:
            page = self.api.search_beatmapsets(query)
        except OsuAuthError as exc:
            errors.append(exc.user_message())
            raise
        except OsuRateLimitError as exc:
            errors.append(exc.user_message())
            budget["requests"] = self.settings.max_requests
            return None
        except OsuApiError as exc:
            errors.append(
                f"{query.status}/{query.sort}: {exc.user_message()}"
            )
            return None
        budget["requests"] += 1
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
        budget: dict[str, int],
        notes: list[str],
        errors: list[str],
    ) -> list[Candidate]:
        limit = self.settings.fetch_attributes_limit
        if limit <= 0 or not candidates:
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
            pre.sort(
                key=lambda item: (
                    -(item[0].best_target.display_score or 0)
                    if item[0].best_target is not None
                    else 0,
                    item[1].beatmapset.id,
                )
            )
            top_ids = [candidate.beatmap.id for _, candidate in pre[:limit]]
        else:
            ranked = [
                evaluate_candidate(candidate, ctx) for candidate in candidates
            ]
            ranked.sort(key=lambda item: (-item.score, item.beatmapset.id))
            top_ids = [item.candidate.beatmap.id for item in ranked[:limit]]
        by_beatmap_id = {
            candidate.beatmap.id: candidate for candidate in candidates
        }
        updated: dict[int, Candidate] = {}
        forbidden = False
        for beatmap_id in top_ids:
            candidate = by_beatmap_id[beatmap_id]
            row = self.storage.get_attributes(beatmap_id)
            attributes: DifficultyAttributes | None = None
            if row is not None:
                attributes = _attributes_from_row(row)
            elif not forbidden:
                if budget["requests"] >= self.settings.max_requests:
                    notes.append(
                        "Бюджет запросов исчерпан до загрузки difficulty "
                        "attributes; часть карт оценена по косвенным признакам."
                    )
                    break
                try:
                    attributes = self.api.get_beatmap_attributes(beatmap_id)
                    budget["requests"] += 1
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
                except OsuForbiddenError:
                    forbidden = True
                    notes.append(
                        "Difficulty attributes недоступны (HTTP 403). "
                        "Aim-профиль использует только плотность, CS и теги."
                    )
                except OsuApiError as exc:
                    errors.append(f"attributes {beatmap_id}: {exc.user_message()}")
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
    def _load_sequence(
        self, beatmap_id: int, allow_download: bool
    ) -> SequenceAnalysis:
        if self.sequence_loader is not None:
            return self.sequence_loader(beatmap_id, allow_download)
        return load_analysis(
            beatmap_id,
            self.storage.path.parent / "gameplay_cache",
            allow_download=allow_download,
        )

    def _enrich_sequences(
        self,
        candidates: list[Candidate],
        ctx: RankingContext,
        *,
        allow_download: bool,
        notes: list[str],
    ) -> list[Candidate]:
        limit = int(self.settings.fetch_sequences_limit)
        if (
            limit <= 0
            or not candidates
            or not _needs_sequence_analysis(ctx.settings)
        ):
            return candidates

        # The budget is spent on candidates that already look promising. This
        # is only a selection heuristic; the mandatory target gate runs later
        # and still sees no evidence for the unanalysed cards.
        ranked = [
            evaluate_candidate(candidate, ctx) for candidate in candidates
        ]
        ranked.sort(key=lambda item: (-item.score, item.beatmapset.id))
        selected = ranked[:limit]
        by_beatmap_id = {
            item.candidate.beatmap.id: item.candidate for item in selected
        }
        updated: dict[int, Candidate] = {}
        analysed = 0
        failed = 0
        missing_cache = 0
        for beatmap_id, candidate in by_beatmap_id.items():
            try:
                analysis = self._load_sequence(beatmap_id, allow_download)
            except GameplayError as exc:
                if allow_download:
                    failed += 1
                    if len(notes) < MAX_NOTES:
                        notes.append(f".osu {beatmap_id}: {exc}")
                else:
                    missing_cache += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one map must not stop search
                failed += 1
                logger.warning("sequence analysis failed for %s: %s", beatmap_id, exc)
                if len(notes) < MAX_NOTES:
                    notes.append(
                        f".osu {beatmap_id}: анализ последовательностей не удался."
                    )
                continue
            traits = derive_features(
                candidate.beatmap,
                candidate.beatmapset,
                candidate.attributes,
                sequence=analysis,
            )
            updated[beatmap_id] = replace(
                candidate,
                sequence=analysis,
                traits=traits,
                skillsets=evaluate_skills(traits),
            )
            analysed += 1

        if analysed:
            notes.append(
                f"Sequence-анализ .osu: {analysed} карт; streams и bursts "
                "считаются раздельно по непрерывным цепочкам."
            )
        if failed:
            notes.append(
                f"Sequence-анализ: не удалось прочитать .osu для {failed} карт; "
                "их stream/burst-признаки остаются неизвестными."
            )
        if missing_cache:
            notes.append(
                "Локальное переранжирование не скачивает .osu: для "
                f"{missing_cache} карт файла нет в кеше."
            )
        if len(candidates) > len(selected):
            notes.append(
                f"Sequence-анализ ограничен {len(selected)} из "
                f"{len(candidates)} кандидатов (лимит "
                f"{limit}); у остальных stream/burst-признаки неизвестны."
            )
        if not updated:
            return candidates

        result: list[Candidate] = []
        for candidate in candidates:
            enriched = updated.get(candidate.beatmap.id)
            result.append(enriched if enriched is not None else candidate)
        return result

    # -- main entry point --------------------------------------------------
    def run(
        self,
        filters: Filters,
        ranking_settings: RankingSettings,
        *,
        record: bool = True,
        strategy_offset: int = 0,
    ) -> DiscoveryResult:
        validate_filters(filters)
        validate_ranking(ranking_settings)
        if self.api is None:
            raise ValueError("Для нового API-поиска нужен osu! API client.")
        started_perf = time.perf_counter()
        started = now_iso()
        errors: list[str] = []
        notes: list[str] = []
        cache_hits = [0]
        budget = {"requests": 0}
        # Do not let broad pagination consume the whole budget before the
        # optional difficulty attributes are requested. This keeps the first
        # search bounded without silently disabling measurable skillsets.
        resolved_requests = max(1, int(self.settings.max_requests))
        attribute_reserve = min(
            max(0, int(self.settings.fetch_attributes_limit)),
            max(0, resolved_requests - 1),
        )
        page_budget = max(1, resolved_requests - attribute_reserve)
        excluded: dict[str, int] = {}
        exclusions: list[Exclusion] = []
        strategies_used: list[str] = []
        total_fetched = 0
        complete = True

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
        known: dict[int, Beatmapset] = {}
        sources: dict[int, list[str]] = {}

        for strategy in strategies:
            if budget["requests"] >= page_budget:
                complete = False
                notes.append(
                    f"Бюджет страниц ({page_budget} запросов из "
                    f"{resolved_requests}) исчерпан; выдача может быть неполной. "
                    "Остальные запросы зарезервированы под difficulty attributes."
                )
                break
            strategies_used.append(strategy.name)
            query = SearchQuery(
                mode=filters.modes[0],
                status=strategy.status,
                sort=strategy.sort,
                query=strategy.query,
                nsfw=not filters.exclude_nsfw,
            )
            seen_cursors: set[str] = set()
            pages = 0
            while True:
                if budget["requests"] >= page_budget:
                    complete = False
                    notes.append(
                        f"Бюджет страниц исчерпан в стратегии {strategy.name}."
                    )
                    break
                page = self._fetch_page(
                    query,
                    budget=budget,
                    errors=errors,
                    notes=notes,
                    cache_hits=cache_hits,
                )
                if page is None:
                    complete = False
                    break
                if page.error:
                    notes.append(
                        f"{strategy.name}: API сообщил «{page.error}»"
                    )
                for warning in page.warnings:
                    if len(notes) < MAX_NOTES:
                        notes.append(f"{strategy.name}: {warning}")
                total_fetched += len(page.beatmapsets)
                for beatmapset in page.beatmapsets:
                    if beatmapset.id not in known:
                        known[beatmapset.id] = beatmapset
                        sources[beatmapset.id] = []
                    if strategy.name not in sources[beatmapset.id]:
                        sources[beatmapset.id].append(strategy.name)
                pages += 1
                if pages >= self.settings.max_pages_per_strategy:
                    if page.has_more:
                        complete = False
                        notes.append(
                            f"{strategy.name}: остановлено на "
                            f"{self.settings.max_pages_per_strategy} страницах."
                        )
                    break
                next_query = self._next_query(page, query)
                if next_query is None:
                    break
                cursor_token = json.dumps(
                    {
                        "cursor": next_query.cursor,
                        "cursor_string": next_query.cursor_string,
                    },
                    sort_keys=True,
                )
                if cursor_token in seen_cursors:
                    notes.append(
                        f"{strategy.name}: API вернул повторяющийся курсор; "
                        "пагинация остановлена."
                    )
                    break
                seen_cursors.add(cursor_token)
                query = next_query
                if not page.beatmapsets and not page.has_more:
                    break

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
        )
        attributes_elapsed = time.perf_counter() - attributes_started
        sequences_started = time.perf_counter()
        candidates = self._enrich_sequences(
            candidates,
            ctx,
            allow_download=True,
            notes=notes,
        )
        sequences_elapsed = time.perf_counter() - sequences_started
        strict_passed = len(candidates)
        candidates = _apply_relevance_gate(
            candidates,
            ranking_settings,
            excluded=excluded,
            exclusions=exclusions,
        )
        relevance_passed = len(candidates)
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
            complete = False
            notes.append(
                f"Показано не более {candidate_limit} кандидатов из "
                f"{len(candidates)}; выдача ограничена настройкой "
                "«Максимум кандидатов»."
            )
        notes.extend(diversification_notes[:3])
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
        if not complete and not any("неполн" in note for note in notes):
            notes.append("Выдача может быть неполной: поиск остановлен по лимитам.")

        finished = now_iso()
        result = DiscoveryResult(
            scored=tuple(scored),
            total_fetched=len(known),
            passed_filters=strict_passed,
            passed_relevance=relevance_passed,
            excluded=dict(
                sorted(excluded.items(), key=lambda item: (-item[1], item[0]))
            ),
            exclusions=tuple(exclusions),
            errors=tuple(dict.fromkeys(errors)),
            notes=tuple(dict.fromkeys(notes))[:MAX_NOTES],
            complete=complete,
            api_requests=budget["requests"],
            cache_hits=cache_hits[0],
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
    ) -> DiscoveryResult:
        """Re-rank stored beatmapsets without making an API request.

        This is deliberately an explicit, bounded local pass. The result is
        marked ``origin="local_cache"`` and never claims to be an exhaustive
        osu! search. The caller decides whether the local pool is large enough
        or whether a real discovery run should extend it.
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
        stored = self.storage.list_beatmapsets(limit=limit_sets)
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

        attributes = self.storage.all_attributes()
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
        candidates = self._enrich_sequences(
            candidates,
            ctx,
            allow_download=False,
            notes=sequence_notes,
        )
        sequences_elapsed = time.perf_counter() - sequences_started
        strict_passed = len(candidates)
        candidates = _apply_relevance_gate(
            candidates,
            ranking_settings,
            excluded=excluded,
            exclusions=exclusions,
        )
        relevance_passed = len(candidates)
        notes = [
            f"Локальный кеш: {len(stored)} сохранённых карт, "
            f"{strict_passed} прошли строгие фильтры, "
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
        cap = candidate_limit if limit is None else max(1, int(limit))
        truncated = len(candidates) > cap
        if truncated:
            notes.append(
                f"Локальный пул ограничен {cap} кандидатами из {len(candidates)} "
                "прошедших фильтры."
            )
        rank_started = time.perf_counter()
        scored = rank_candidates(candidates, ctx, limit=cap if truncated else None)
        rank_elapsed = time.perf_counter() - rank_started
        return DiscoveryResult(
            scored=tuple(scored),
            total_fetched=len(stored),
            passed_filters=strict_passed,
            passed_relevance=relevance_passed,
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
