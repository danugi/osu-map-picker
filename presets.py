"""Editable presets and safe JSON import/export.

Built-in presets are ordinary database rows: the user can edit, rename,
duplicate or delete them, and restore the defaults later. Switching a preset
never mutates another preset or touches ratings, history and favourites.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import catalog
import config
from filters import FilterConfigError, validate_filters
from models import (
    DiscoverySettings,
    Filters,
    ModelError,
    Preset,
    PresetConfig,
    RankingSettings,
)
from ranking import RankingConfigError, validate_ranking
from skillsets import SKILL_ALIASES


class PresetError(ValueError):
    """Invalid preset configuration or import payload."""


DISCOVERY_LIMITS = {
    "max_requests": (1, 60),
    "max_pages_per_strategy": (1, 10),
    "max_candidates": (10, 2000),
    "fetch_attributes_limit": (0, 50),
    "fetch_sequences_limit": (0, 50),
    "cache_ttl_minutes": (0, 1440),
}


def validate_preset_config(preset: PresetConfig) -> None:
    name = preset.name.strip()
    if not name:
        raise PresetError("Имя пресета не может быть пустым")
    if len(name) > 80:
        raise PresetError("Имя пресета длиннее 80 символов")
    if any(ord(ch) < 32 for ch in name):
        raise PresetError("Имя пресета содержит управляющие символы")
    if len(preset.description) > 500:
        raise PresetError("Описание пресета длиннее 500 символов")
    if preset.schema_version != config.SETTINGS_SCHEMA_VERSION:
        raise PresetError(
            f"Версия схемы пресета {preset.schema_version} не поддерживается"
        )
    if preset.catalog_id:
        node = catalog.PRESET_NODE_BY_ID.get(preset.catalog_id)
        if node is None:
            raise PresetError(
                f"catalog_id: неизвестный узел {preset.catalog_id}"
            )
        if (
            node.name != name
            or preset.parent_id != node.parent_id
            or preset.category_id != node.category_id
            or preset.display_order != node.order
        ):
            raise PresetError(
                "catalog_id: метаданные иерархии не совпадают с каталогом"
            )
    elif preset.parent_id is not None or preset.category_id != "custom":
        raise PresetError(
            "parent_id/category_id без catalog_id недопустимы"
        )
    try:
        validate_filters(preset.filters)
        validate_ranking(preset.ranking)
    except (FilterConfigError, RankingConfigError) as exc:
        raise PresetError(str(exc)) from exc
    for field_name, (low, high) in DISCOVERY_LIMITS.items():
        value = getattr(preset.discovery, field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PresetError(f"discovery.{field_name} должен быть целым числом")
        if not low <= value <= high:
            raise PresetError(
                f"discovery.{field_name} должен быть в диапазоне {low}..{high}"
            )
    title_queries = preset.discovery.title_queries
    if len(title_queries) > 6:
        raise PresetError("discovery.title_queries: допускается не больше 6 запросов")
    normalized: set[str] = set()
    for query in title_queries:
        text = query.strip()
        if not text or len(text) > 64:
            raise PresetError(
                "discovery.title_queries: запрос должен быть длиной 1..64"
            )
        if any(ord(ch) < 32 for ch in text):
            raise PresetError(
                "discovery.title_queries: управляющие символы недопустимы"
            )
        key = text.casefold()
        if key in normalized:
            raise PresetError("discovery.title_queries: запросы повторяются")
        normalized.add(key)


def _clone_filters(filters: Filters) -> Filters:
    return Filters.from_dict(filters.to_dict())


def _clone_ranking(ranking: RankingSettings) -> RankingSettings:
    return RankingSettings.from_dict(ranking.to_dict())


def _clone_discovery(discovery: DiscoverySettings) -> DiscoverySettings:
    return DiscoverySettings.from_dict(discovery.to_dict())


def default_config(name: str = "Custom", description: str = "") -> PresetConfig:
    return PresetConfig(
        name=name,
        description=description or "Пользовательская конфигурация.",
        filters=Filters(),
        ranking=RankingSettings(),
        discovery=DiscoverySettings(),
    )


def _skill_preset(
    name: str,
    description: str,
    skill_weights: dict[str, float],
    *,
    comfort_stars: float = 5.4,
    comfort_width: float = 0.9,
    ideal_length: int = 140,
    length_tolerance: int = 70,
    duration_max: int = 180,
    diversity: float = 0.45,
    mapping_weight: float = 1.6,
    statuses: list[str] | None = None,
) -> PresetConfig:
    """Shared builder: every skillset preset is one ordinary configuration."""

    cfg = default_config(name, description)
    cfg.ranking.trait_targets = {}
    cfg.ranking.skill_weights = dict(skill_weights)
    cfg.ranking.comfort_stars = comfort_stars
    cfg.ranking.comfort_width = comfort_width
    cfg.ranking.ideal_length = ideal_length
    cfg.ranking.length_tolerance = length_tolerance
    cfg.ranking.diversity = diversity
    cfg.ranking.factors["mapping_fit"].weight = mapping_weight
    cfg.filters.duration_max = duration_max
    if statuses is not None:
        cfg.filters.statuses = statuses
    return cfg


def _aim() -> PresetConfig:
    cfg = _skill_preset(
        "Aim",
        "Основной aim-профиль: классический aim как база, aim control и aim "
        "bursts как равные альтернативы, короткие карты. В тегах osu! нет "
        "отдельного skillset/aim — aim-навыки распределены между jumps, "
        "aim control и bursts, поэтому пресет покрывает их все. "
        "Цель — разнообразие, а не максимум звёзд.",
        {
            "aim_classic": 0.60,
            "aim_control": 0.20,
            "aim_bursts": 0.20,
        },
        comfort_stars=5.4,
        ideal_length=140,
        mapping_weight=1.3,
    )
    return cfg


def _jump_aim() -> PresetConfig:
    return _skill_preset(
        "Jump Aim",
        "Jump-aim: движения строятся на измеренной доле прыжков и ширине "
        "прыжков из .osu, а не только на высоком aim-атрибуте. Без "
        "sequence-анализа профиль не подтверждает jump-паттерн.",
        {
            "jump_aim": 1.0,
            "aim_classic": 0.20,
        },
        comfort_stars=5.5,
        comfort_width=0.9,
        ideal_length=150,
        length_tolerance=90,
        duration_max=240,
        diversity=0.45,
        mapping_weight=1.45,
    )


def _legacy_mixed_aim_skillset() -> PresetConfig:
    """Exact pre-v15 Mixed Aim body (the v14 skillset-engine era)."""

    cfg = _skill_preset(
        "Mixed Aim",
        "Основной баланс: интересный mixed aim, немного aimslop для стабильности "
        "и короткие карты. Цель — разнообразие, а не максимум звёзд. "
        "Classic Aim сочетается с control и bursts.",
        {
            "aim_classic": 0.60,
            "aim_control": 0.20,
            "aim_bursts": 0.20,
        },
        comfort_stars=5.4,
        ideal_length=140,
        mapping_weight=1.3,
    )
    return cfg


def _any_taste() -> PresetConfig:
    cfg = default_config(
        "Any Taste",
        "Широкий поиск хороших карт любого стиля и жанра. Aim-профиль не "
        "навязывается; качество оценивается по рейтингу, избранному и "
        "просмотрам сообщества, а не только по звёздам. Сложность и "
        "длительность остаются вашими фильтрами.",
    )
    cfg.ranking.trait_targets = {}
    cfg.ranking.factors["mapping_fit"].enabled = False
    cfg.ranking.factors["community"].weight = 0.9
    cfg.ranking.factors["novelty"].weight = 0.8
    cfg.ranking.factors["taste"].weight = 1.2
    cfg.ranking.factors["difficulty"].weight = 1.1
    cfg.ranking.diversity = 0.55
    cfg.ranking.comfort_stars = 5.3
    cfg.ranking.comfort_width = 1.0
    cfg.ranking.ideal_length = 140
    cfg.discovery.use_oldest = True
    cfg.discovery.use_rating = True
    cfg.discovery.use_popular = True
    cfg.discovery.expand_seeds = True
    return cfg


def _aim_farm() -> PresetConfig:
    cfg = default_config(
        "Aim Farm",
        "Практичный NM aim-фарм: только Ranked-карты, которые могут дать PP. "
        "Мягкое окно около 90 с (по умолчанию 60–120 с), aim-давление, "
        "комфортная звёздность и достаточно игровых объектов. Длительность не "
        "является единственным критерием; это farm-efficiency эвристика, а не "
        "расчёт PP. Отдельный поиск коротких версий дополняет общий.",
    )
    cfg.filters.stars_min = 5.0
    cfg.filters.stars_max = 6.9
    cfg.filters.duration_max = 150
    # PP is awarded by Ranked maps. Loved/Qualified can be good aim maps, but
    # presenting them as PP-eligible would be false.
    cfg.filters.statuses = ["ranked"]
    cfg.ranking.trait_targets = {}
    cfg.ranking.skill_weights = {"aim_classic": 1.0}
    cfg.ranking.comfort_stars = 5.8
    cfg.ranking.comfort_width = 0.8
    cfg.ranking.ideal_length = 95
    cfg.ranking.length_tolerance = 35
    cfg.ranking.diversity = 0.20
    cfg.ranking.farm_target_seconds = 90
    cfg.ranking.farm_tolerance_seconds = 30
    cfg.ranking.factors["mapping_fit"].weight = 1.3
    cfg.ranking.factors["farm_nm"].enabled = True
    cfg.ranking.factors["farm_nm"].weight = 1.8
    cfg.ranking.factors["difficulty"].weight = 1.1
    cfg.ranking.factors["length"].weight = 0.45
    cfg.ranking.factors["taste"].weight = 0.6
    cfg.ranking.factors["feedback"].weight = 1.2
    cfg.ranking.factors["music"].weight = 0.2
    cfg.ranking.factors["community"].weight = 0.30
    cfg.ranking.factors["novelty"].weight = 0.1
    cfg.ranking.factors["repeat_penalty"].weight = 0.6
    cfg.discovery.use_oldest = False
    cfg.discovery.use_rating = True
    cfg.discovery.use_popular = False
    cfg.discovery.expand_seeds = False
    cfg.discovery.max_requests = 56
    cfg.discovery.max_pages_per_strategy = 3
    cfg.discovery.fetch_attributes_limit = 40
    cfg.discovery.title_queries = [
        "Nightcore & Cut Ver",
        "Cut Ver",
        "Sped Up",
    ]
    return cfg


def _legacy_aim_farm_config() -> PresetConfig:
    """Exact old Aim Farm profile, before the NM/skillset rework."""

    cfg = default_config(
        "Aim Farm / PP",
        "Больше aimslop и farm-паттернов: сложнее, короче, меньше разнообразия. "
        "Всё ещё aim-ориентировано, не чистый BPM-фильтр.",
    )
    cfg.ranking.factors.pop("farm_nm", None)
    cfg.filters.stars_min = 5.0
    cfg.filters.stars_max = 6.9
    cfg.filters.duration_max = 150
    cfg.filters.statuses = ["ranked", "loved"]
    cfg.ranking.trait_targets = {"aim": 0.85, "speed": 0.55, "density": 0.55}
    cfg.ranking.comfort_stars = 6.0
    cfg.ranking.comfort_width = 0.7
    cfg.ranking.ideal_length = 115
    cfg.ranking.diversity = 0.30
    cfg.ranking.factors["mapping_fit"].weight = 1.7
    cfg.ranking.factors["community"].weight = 0.4
    cfg.discovery.use_oldest = False
    cfg.discovery.expand_seeds = False
    return cfg


def _legacy_aim_farm_pp_config() -> PresetConfig:
    """Historical farm profile with corrected PP eligibility.

    The old row remains available for users who selected it, but only Ranked
    maps can award PP. Other historical search/ranking choices are preserved.
    """

    cfg = _legacy_aim_farm_config()
    cfg.filters.statuses = ["ranked"]
    cfg.description = (
        "Исторический aim-farm профиль. Для PP оставлены только Ranked-карты; "
        "остальные настройки сохранены как в старой версии."
    )
    return cfg


def _legacy_aim_alt_config() -> PresetConfig:
    """Exact old Aim + Alt profile, before the skillset combination."""

    cfg = default_config(
        "Aim + Alt",
        "Aim вместе с bursts и более сложной работой пальцев. Bursts — "
        "прокси из плотности и speed-доли, без анализа паттернов.",
    )
    cfg.ranking.factors.pop("farm_nm", None)
    cfg.filters.stars_min = 4.5
    cfg.ranking.trait_targets = {"speed": 0.75, "density": 0.80, "bursts": 0.70}
    cfg.ranking.comfort_stars = 5.6
    cfg.ranking.diversity = 0.55
    return cfg


def _legacy_mixed_aim_config() -> PresetConfig:
    cfg = default_config(
        "Mixed Aim",
        "Основной баланс: интересный mixed aim, немного aimslop для стабильности "
        "и PP, короткие карты. Цель — разнообразие, а не максимум звёзд.",
    )
    cfg.ranking.trait_targets = {"aim": 0.72, "speed": 0.45, "density": 0.55}
    cfg.ranking.comfort_stars = 5.4
    cfg.ranking.ideal_length = 140
    return cfg


def _legacy_wide_aim_config() -> PresetConfig:
    cfg = default_config(
        "Wide Aim",
        "Прокси для широких прыжков: высокий aim-вклад при сравнительно низкой "
        "плотности. Реальная ширина прыжков требует локального анализа .osu.",
    )
    cfg.filters.stars_min = 4.5
    cfg.ranking.trait_targets = {"aim": 0.90, "density": 0.35}
    cfg.ranking.comfort_stars = 5.3
    cfg.ranking.diversity = 0.55
    return cfg


def _legacy_flow_aim_config() -> PresetConfig:
    cfg = default_config(
        "Flow Aim",
        "Прокси для связного aim: высокий aim-вклад вместе с высокой плотностью. "
        "Это не измерение flow-геометрии.",
    )
    cfg.filters.stars_min = 4.5
    cfg.ranking.trait_targets = {"aim": 0.80, "density": 0.75}
    cfg.ranking.comfort_stars = 5.5
    cfg.ranking.diversity = 0.50
    return cfg


def _legacy_aim_control_config() -> PresetConfig:
    cfg = default_config(
        "Aim Control",
        "Фокус на контроле траекторий и точности. Control и precision — "
        "прокси из aim-доли, звёздности и CS.",
    )
    cfg.filters.stars_min = 4.5
    cfg.ranking.trait_targets = {"control": 0.90, "aim": 0.70, "precision": 0.60}
    cfg.ranking.comfort_stars = 5.5
    cfg.ranking.comfort_width = 0.85
    return cfg


def _legacy_weird_aim_config() -> PresetConfig:
    cfg = default_config(
        "Weird Aim",
        "Нестандартные движения и экспериментальный маппинг. Геометрия углов "
        "пока не измеряется; пресет использует aim control и novelty как прокси.",
    )
    cfg.filters.stars_min = 4.0
    cfg.filters.stars_max = 6.5
    cfg.filters.statuses = ["ranked", "loved", "qualified", "pending"]
    cfg.ranking.trait_targets = {"control": 0.85, "aim": 0.65, "precision": 0.55}
    cfg.ranking.comfort_stars = 4.9
    cfg.ranking.comfort_width = 1.1
    cfg.ranking.diversity = 0.72
    cfg.ranking.factors["novelty"].weight = 1.2
    cfg.ranking.factors["mapping_fit"].weight = 1.2
    cfg.discovery.max_requests = 18
    cfg.discovery.max_pages_per_strategy = 3
    return cfg


def _normalized_config_dict(cfg: PresetConfig) -> dict[str, Any]:
    """Compare old configs that predate factors introduced later."""

    data = cfg.to_dict()
    factors = data.get("ranking", {}).get("factors", {})
    for key, default in config.DEFAULT_RANKING_FACTORS.items():
        factors.setdefault(key, copy.deepcopy(default))
    return data


def _matches_legacy(cfg: PresetConfig, legacy: PresetConfig) -> bool:
    return _normalized_config_dict(cfg) == _normalized_config_dict(legacy)


def preset_drift(stored: PresetConfig, builtin: PresetConfig) -> list[str]:
    """Human-readable search-affecting differences from the current built-in.

    Names and descriptions are metadata and intentionally do not count as
    behavioural drift.
    """

    changes: list[str] = []
    if stored.filters.statuses != builtin.filters.statuses:
        changes.append(
            "статусы: "
            + ", ".join(stored.filters.statuses)
            + " → "
            + ", ".join(builtin.filters.statuses)
        )
    if (
        stored.filters.stars_min,
        stored.filters.stars_max,
        stored.filters.star_mode,
    ) != (
        builtin.filters.stars_min,
        builtin.filters.stars_max,
        builtin.filters.star_mode,
    ):
        changes.append(
            "звёзды: "
            f"{stored.filters.stars_min}..{stored.filters.stars_max} → "
            f"{builtin.filters.stars_min}..{builtin.filters.stars_max}"
        )
    if (
        stored.filters.duration_max,
        stored.filters.duration_min,
    ) != (
        builtin.filters.duration_max,
        builtin.filters.duration_min,
    ):
        changes.append(
            "длительность: "
            f"{stored.filters.duration_min}..{stored.filters.duration_max} → "
            f"{builtin.filters.duration_min}..{builtin.filters.duration_max}"
        )
    if stored.ranking.skill_weights != builtin.ranking.skill_weights:
        changes.append("веса навыков отличаются")
    if stored.ranking.relevance_targets != builtin.ranking.relevance_targets:
        changes.append(
            "целевой гейт: "
            + ", ".join(stored.ranking.relevance_targets)
            + " → "
            + ", ".join(builtin.ranking.relevance_targets)
        )
    if stored.ranking.relevance_policy != builtin.ranking.relevance_policy:
        changes.append("политика целевого гейта отличается")
    stored_factors = {
        key: (value.weight, value.enabled)
        for key, value in stored.ranking.factors.items()
    }
    builtin_factors = {
        key: (value.weight, value.enabled)
        for key, value in builtin.ranking.factors.items()
    }
    if stored_factors != builtin_factors:
        changes.append("веса факторов ранжирования отличаются")
    discovery_pairs = (
        ("максимум запросов", "max_requests"),
        ("страниц на стратегию", "max_pages_per_strategy"),
        ("максимум кандидатов", "max_candidates"),
        ("attribute limit", "fetch_attributes_limit"),
        ("title-запросы", "title_queries"),
    )
    for label, field in discovery_pairs:
        if getattr(stored.discovery, field) != getattr(
            builtin.discovery, field
        ):
            changes.append(f"discovery: {label} отличаются")
    if stored.ranking.farm_target_seconds != builtin.ranking.farm_target_seconds:
        changes.append(
            "цель NM-фарма: "
            f"{stored.ranking.farm_target_seconds} → "
            f"{builtin.ranking.farm_target_seconds} с"
        )
    if (
        stored.ranking.farm_tolerance_seconds
        != builtin.ranking.farm_tolerance_seconds
    ):
        changes.append(
            "допуск NM-фарма: "
            f"{stored.ranking.farm_tolerance_seconds} → "
            f"{builtin.ranking.farm_tolerance_seconds} с"
        )
    return changes


def _legacy_aim_farm_matches(cfg: PresetConfig) -> bool:
    """Only an exact, unedited legacy profile is safe to upgrade."""

    return _matches_legacy(cfg, _legacy_aim_farm_config())


def _current_aim_farm_matches(cfg: PresetConfig) -> bool:
    """Compare the old-name farm profile with the current body.

    Name and description are ignored because that is exactly what version 6
    changes. Everything that affects search must still be untouched.
    """

    current = _normalized_config_dict(_aim_farm())
    stored = _normalized_config_dict(cfg)
    for payload in (current, stored):
        payload.pop("name", None)
        payload.pop("description", None)
    return current == stored


def _legacy_aim_alt_matches(cfg: PresetConfig) -> bool:
    return _matches_legacy(cfg, _legacy_aim_alt_config())


# Exact pre-v10 fingerprints are handled by _upgrade_v10_definitions; the
# proxy-era skill-profile upgrade loop was removed when Mixed Aim was renamed
# to Aim (v15): proxy-era rows stay as hidden legacy rows instead of being
# silently rewritten into a profile that is no longer offered.


def _speed() -> PresetConfig:
    cfg = _skill_preset(
        "Speed",
        "Скоростные карты: BPM, speed-давление, bursts и высокая плотность. "
        "Основной фокус — стабильная скорость, а не только высокий BPM.",
        {
            "speed_high_bpm": 1.0,
            "speed_bursts": 0.55,
            "speed_density": 0.5,
        },
        comfort_stars=5.4,
        diversity=0.35,
    )
    cfg.ranking.factors["community"].weight = 0.25
    return cfg


def _high_bpm() -> PresetConfig:
    return _skill_preset(
        "High BPM",
        "Высокий BPM без обязательного burst-рисунка. Основное доказательство "
        "— измеренный BPM карты; speed-атрибут остаётся дополнительным "
        "сигналом, если он доступен. Высокий BPM сам по себе не доказывает "
        "рисунок паттерна.",
        {
            "high_bpm": 1.0,
            "speed_density": 0.20,
        },
        comfort_stars=5.5,
        ideal_length=130,
        mapping_weight=1.4,
    )


def _speed_bursts() -> PresetConfig:
    return _skill_preset(
        "Speed Bursts",
        "Скоростные burst-серии 5–9 нот: доля, количество и ровность "
        "интервалов из .osu. Длинные stream-цепочки не считаются burst "
        "доказательством.",
        {
            "speed_bursts": 1.0,
            "burst_control": 0.35,
        },
        comfort_stars=5.4,
        ideal_length=145,
        length_tolerance=85,
        duration_max=240,
        diversity=0.4,
        mapping_weight=1.45,
    )


def _streams() -> PresetConfig:
    cfg = _skill_preset(
        "Streams",
        "Stream-карты в понимании тегов osu!: непрерывные потоки 10+ нот "
        "(skillset/streams) вместе с burst-сериями 5–9 нот (streams/bursts — "
        "на сайте bursts входят в группу streams). Оба вида доказательства "
        "берутся из локального .osu; без него гейт честно не пропускает "
        "карту. Выносливость — отдельный пресет Stamina.",
        {
            "stream_consistency": 1.0,
            "burst_control": 0.7,
            "stream_long": 0.6,
            "stream_burst": 0.45,
            "speed_bursts": 0.3,
        },
        comfort_stars=5.2,
        ideal_length=190,
        length_tolerance=110,
        duration_max=300,
        diversity=0.4,
    )
    cfg.ranking.factors["length"].weight = 0.5
    return cfg


def _burst_streams() -> PresetConfig:
    return _skill_preset(
        "Burst-oriented Streams",
        "Burst-ориентированная ветка единой Streams-категории: короткие "
        "серии 5–9 нот из .osu. Это не отдельный top-level Bursts-профиль и "
        "не Stamina; длинные цепочки сами по себе здесь не подтверждают "
        "burst-рисунок.",
        {
            "burst_control": 1.0,
            "speed_bursts": 0.55,
            "stream_burst": 0.30,
        },
        comfort_stars=5.2,
        ideal_length=160,
        length_tolerance=95,
        duration_max=270,
        diversity=0.45,
        mapping_weight=1.4,
    )


def _continuous_streams() -> PresetConfig:
    return _skill_preset(
        "Continuous Streams",
        "Непрерывные stream-цепочки 10+ нот: длина цепочки, доля потока и "
        "ровность интервалов. Burst-серии не заменяют длинную цепочку, а "
        "выносливость остаётся отдельным профилем Stamina.",
        {
            "stream_long": 1.0,
            "stream_consistency": 0.55,
        },
        comfort_stars=5.2,
        ideal_length=190,
        length_tolerance=105,
        duration_max=300,
        diversity=0.4,
        mapping_weight=1.4,
    )


def _legacy_streams_config() -> PresetConfig:
    """Exact pre-v16 Streams body (streams-only, before the merge)."""

    cfg = _skill_preset(
        "Streams",
        "Stream-карты: непрерывность и длина stream-участков, bursts. "
        "Stream-признаки требуют локального .osu; без него кандидат не "
        "получает выдуманный прокси и не проходит обязательный гейт.",
        {
            "stream_consistency": 1.0,
            "stream_long": 0.7,
            "stream_burst": 0.4,
        },
        comfort_stars=5.2,
        ideal_length=190,
        length_tolerance=110,
        duration_max=300,
        diversity=0.4,
    )
    cfg.ranking.factors["length"].weight = 0.5
    return cfg


def _stamina() -> PresetConfig:
    cfg = _skill_preset(
        "Stamina",
        "Выносливость как отдельный навык (streams/stamina на osu!): "
        "длительная stream-нагрузка, а не просто плотность или одна короткая "
        "серия. Основной гейт — stream_endurance по нескольким stream-цепочкам "
        "и длине карты; прокси stamina остаётся вспомогательным сигналом.",
        {
            "stream_endurance": 1.0,
            "stamina_finger": 0.55,
            "stream_long": 0.45,
            "speed_stamina": 0.30,
        },
        comfort_stars=5.2,
        ideal_length=220,
        length_tolerance=130,
        duration_max=360,
        diversity=0.4,
    )
    cfg.ranking.factors["length"].weight = 0.4
    return cfg


def _streams_stamina() -> PresetConfig:
    cfg = _skill_preset(
        "Streams & Stamina",
        "Потоки вместе с выносливостью: непрерывность и длина stream-участков, "
        "finger stamina и speed stamina. Stream-доказательство берётся из "
        "локального .osu; без него гейт честно не пропускает карту. Длина "
        "остаётся мягким предпочтением.",
        {
            "stream_consistency": 0.90,
            "stamina_finger": 0.65,
            "stream_long": 0.55,
            "speed_stamina": 0.35,
        },
        comfort_stars=5.2,
        ideal_length=200,
        length_tolerance=120,
        duration_max=360,
        diversity=0.45,
    )
    cfg.ranking.factors["length"].weight = 0.45
    return cfg


def _bursts() -> PresetConfig:
    cfg = _skill_preset(
        "Bursts",
        "Короткие серии 5–9 нот, найденные в .osu: burst control, скорость "
        "серий и их количество. Длинные stream-цепочки 10+ нот не считаются "
        "bursts и не поднимают этот профиль.",
        {
            "burst_control": 1.0,
            "speed_bursts": 0.65,
        },
        comfort_stars=5.2,
        ideal_length=150,
        length_tolerance=90,
        duration_max=240,
        diversity=0.4,
    )
    cfg.ranking.factors["length"].weight = 0.30
    return cfg


def _finger_control() -> PresetConfig:
    return _skill_preset(
        "Finger Control",
        "Контроль пальцев, alternation и speed control. Основа — прокси из "
        "плотности, скорости, OD и тегов; это не точный анализ tapping.",
        {
            "finger_control": 1.0,
            "alternating": 0.65,
            "speed_control": 0.45,
        },
        comfort_stars=5.0,
        diversity=0.4,
    )


def _alternating() -> PresetConfig:
    return _skill_preset(
        "Alternating",
        "Alternation-профиль: speed, плотность, низкий aim-вклад и тег alt. "
        "Прямого измерения распределения пальцев в .osu нет, поэтому это "
        "ограниченный прокси, а не доказанный hand-alternation.",
        {
            "alternating": 1.0,
            "finger_control": 0.35,
        },
        comfort_stars=5.1,
        diversity=0.45,
        mapping_weight=1.3,
    )


def _double_tap() -> PresetConfig:
    return _skill_preset(
        "Double-tap / Burst Control",
        "Короткие burst-группы как tapping-нагрузка: измеренная burst-доля, "
        "медианная длина и ровность. Профиль отличается от Alternating "
        "наличием реальных burst-цепочек из .osu.",
        {
            "double_tap_control": 1.0,
            "burst_control": 0.35,
        },
        comfort_stars=5.2,
        ideal_length=160,
        length_tolerance=90,
        duration_max=270,
        diversity=0.4,
        mapping_weight=1.4,
    )


def _accuracy() -> PresetConfig:
    return _skill_preset(
        "Accuracy",
        "Точность: высокий OD, CS и разреженные или плотные precision-паттерны. "
        "OD/CS измеряются напрямую, «accuracy-карта» остаётся эвристикой.",
        {
            "accuracy_focused": 1.0,
            "od_precision": 0.7,
            "low_density_precision": 0.4,
        },
        comfort_stars=4.9,
        diversity=0.35,
    )


def _reading() -> PresetConfig:
    return _skill_preset(
        "Reading",
        "Чтение: AR-челлендж, высокий или низкий AR и reading-теги. AR "
        "измеряется напрямую, но реальная читаемость паттернов не доказывается "
        "метаданными API; это ограничение показывается явно.",
        {
            "ar_challenge": 1.0,
            "high_ar": 0.6,
            "low_ar": 0.5,
            "reading_aim": 0.3,
        },
        comfort_stars=5.0,
        diversity=0.55,
    )


def _tech() -> PresetConfig:
    return _skill_preset(
        "Tech",
        "Техничные карты: слайдеры, SV-теги, смены ритма и смешанные паттерны. "
        "Tech — широкая категория, поэтому часть оценки ограничена.",
        {
            "tech": 1.0,
            "slider_heavy": 0.6,
            "rhythm_complexity": 0.5,
            "mixed_technical": 0.4,
        },
        comfort_stars=5.0,
        diversity=0.5,
    )


def _flashlight() -> PresetConfig:
    return _skill_preset(
        "Flashlight",
        "Flashlight — отдельный модификатор, а не универсальная сложность. "
        "Поддержка ограничена тегом flashlight и базовыми признаками; без "
        "тега FL-совместимость неизвестна.",
        {
            "fl_friendly": 1.0,
            "fl_reading": 0.5,
            "fl_aim": 0.4,
        },
        comfort_stars=4.8,
        diversity=0.45,
        mapping_weight=1.1,
    )


def _allrounder() -> PresetConfig:
    return _skill_preset(
        "All-rounder",
        "Сбалансированная тренировка без доминирования одного навыка: "
        "aim, speed, streams, finger control, accuracy и tech.",
        {
            "balanced": 1.0,
            "mixed_skills": 0.6,
            "difficulty_variety": 0.3,
        },
        comfort_stars=5.4,
        diversity=0.55,
    )



def _weird_aim() -> PresetConfig:
    cfg = default_config(
        "Weird Aim",
        "Нестандартные движения и экспериментальный маппинг. Геометрия углов "
        "пока не измеряется; пресет использует aim control и novelty как прокси.",
    )
    cfg.filters.stars_min = 4.0
    cfg.filters.stars_max = 6.5
    cfg.filters.statuses = ["ranked", "loved", "qualified", "pending"]
    cfg.ranking.skill_weights = {
        "tech": 0.45,
        "aim_control": 0.30,
        "unusual_spacing": 0.25,
    }
    cfg.ranking.comfort_stars = 4.9
    cfg.ranking.comfort_width = 1.1
    cfg.ranking.diversity = 0.72
    cfg.ranking.factors["novelty"].weight = 1.2
    cfg.ranking.factors["mapping_fit"].weight = 1.2
    cfg.discovery.max_requests = 18
    cfg.discovery.max_pages_per_strategy = 3
    return cfg


def _wide_aim() -> PresetConfig:
    cfg = _skill_preset(
        "Wide Aim",
        "Прокси для широких прыжков: высокий aim-вклад при сравнительно низкой "
        "плотности. Ширина прыжков пока не измеряется без локального .osu; "
        "оценка остаётся ограниченной.",
        {"aim_wide": 0.70, "aim_classic": 0.30},
        comfort_stars=5.3,
        diversity=0.55,
        mapping_weight=1.3,
    )
    cfg.filters.stars_min = 4.5
    return cfg


def _flow_aim() -> PresetConfig:
    cfg = _skill_preset(
        "Flow Aim",
        "Прокси для связного aim: Classic Aim и Flow Aim вместе. Это не "
        "измерение flow-геометрии; структура движений остаётся ограниченной.",
        {"aim_flow": 0.70, "aim_classic": 0.30},
        comfort_stars=5.5,
        diversity=0.50,
        mapping_weight=1.3,
    )
    cfg.filters.stars_min = 4.5
    return cfg


def _aim_control() -> PresetConfig:
    cfg = _skill_preset(
        "Aim Control",
        "Фокус на контроле траекторий и точности. Control и precision — "
        "прокси из aim-доли, звёздности и CS; это не анализ реальных паттернов.",
        {
            "aim_control": 0.60,
            "aim_precision": 0.25,
            "aim_classic": 0.15,
        },
        comfort_stars=5.5,
        comfort_width=0.85,
        mapping_weight=1.3,
    )
    cfg.filters.stars_min = 4.5
    return cfg


def _aim_alt() -> PresetConfig:
    cfg = _skill_preset(
        "Aim + Alt",
        "Комбинация Classic Aim и Finger Control/Alternating: aim-нагрузка "
        "вместе с контролем пальцев и alternation. Оба компонента — "
        "универсальные навыки, а не отдельная «alt»-эвристика.",
        {
            "aim_classic": 0.60,
            "finger_control": 0.25,
            "alternating": 0.15,
        },
        comfort_stars=5.6,
        diversity=0.55,
    )
    return cfg


def _aim_bursts() -> PresetConfig:
    return _skill_preset(
        "Aim + Bursts",
        "Aim-ориентированный профиль с короткими burst-всплесками: Classic Aim "
        "остаётся основой, Aim + Bursts и Speed bursts добавляют контроль "
        "ускорений. Это отдельный поиск по навыкам, а не по одному числу.",
        {
            "aim_classic": 0.55,
            "aim_bursts": 0.30,
            "speed_bursts": 0.15,
        },
        comfort_stars=5.6,
        ideal_length=140,
        mapping_weight=1.45,
    )


def _speed_aim() -> PresetConfig:
    return _skill_preset(
        "Speed + Aim",
        "Скорость с прицелом: Speed + Aim, High-BPM и классический aim. "
        "Требует difficulty attributes для обоих компонентов; если локальный "
        "каталог их не содержит, профиль честно не подтверждает совпадение.",
        {
            "speed_aim": 0.45,
            "speed_high_bpm": 0.20,
            "aim_classic": 0.20,
            "speed_density": 0.15,
        },
        comfort_stars=5.8,
        ideal_length=130,
        mapping_weight=1.55,
    )


def _speed_stamina() -> PresetConfig:
    return _skill_preset(
        "Speed + Stamina",
        "Длинные скоростные карты: High-BPM, Alternating stamina и Speed "
        "consistency. Опора на выносливость, а не на пиковую сложность.",
        {
            "speed_high_bpm": 0.35,
            "stamina_alt": 0.35,
            "speed_consistency": 0.20,
            "speed_finger": 0.10,
        },
        comfort_stars=5.6,
        comfort_width=1.0,
        ideal_length=220,
        length_tolerance=110,
        duration_max=300,
        diversity=0.35,
    )


def _low_ar_reading() -> PresetConfig:
    cfg = _skill_preset(
        "Low AR Reading",
        "Низкий AR и чтение паттернов. Пресет явно ограничивает AR сверху "
        "8.5: это осознанное ограничение профиля, а не автоматическое "
        "следствие выбора навыка.",
        {
            "low_ar": 0.55,
            "ar_challenge": 0.25,
            "reading_aim": 0.20,
        },
        comfort_stars=5.0,
        comfort_width=1.1,
        ideal_length=170,
        mapping_weight=1.35,
    )
    cfg.filters.ar_max = 8.5
    return cfg


def _high_ar_reading() -> PresetConfig:
    cfg = _skill_preset(
        "High AR Reading",
        "Высокий AR и быстрое чтение. Пресет явно требует AR не ниже 9.2; "
        "это ограничение профиля, а не скрытое правило выбора навыка.",
        {
            "high_ar": 0.50,
            "ar_challenge": 0.30,
            "reading_aim": 0.20,
        },
        comfort_stars=5.5,
        comfort_width=0.9,
        ideal_length=140,
        mapping_weight=1.35,
    )
    cfg.filters.ar_min = 9.2
    return cfg


def _precision() -> PresetConfig:
    return _skill_preset(
        "Precision",
        "Точность и контроль: Accuracy/OD precision и Precision Aim без "
        "обязательных модов. Пресет не сортирует карты по одному числу.",
        {
            "accuracy_focused": 0.40,
            "od_precision": 0.30,
            "aim_precision": 0.30,
        },
        comfort_stars=5.1,
        comfort_width=0.8,
        ideal_length=160,
        diversity=0.35,
    )


def _reading_aim() -> PresetConfig:
    return _skill_preset(
        "Reading + Aim",
        "Чтение вместе с aim: Reading + aim, Aim Control и AR challenge. "
        "Для карт, где сложность в чтении и нестандартном движении.",
        {
            "reading_aim": 0.55,
            "aim_control": 0.25,
            "ar_challenge": 0.20,
        },
        comfort_stars=5.3,
        comfort_width=1.0,
        ideal_length=170,
        mapping_weight=1.45,
    )


def _reading_streams() -> PresetConfig:
    return _skill_preset(
        "Reading + Streams",
        "Чтение потоков: Streams + reading, Stream consistency и смены "
        "плотности. Не требует DT и не подменяет чтение звёздами.",
        {
            "stream_reading": 0.55,
            "stream_consistency": 0.25,
            "density_changes": 0.20,
        },
        comfort_stars=5.4,
        comfort_width=1.0,
        ideal_length=190,
        mapping_weight=1.45,
    )


def _jump_streams() -> PresetConfig:
    return _skill_preset(
        "Jump Streams",
        "Jump streams: прыжки в потоке, скорость с aim и bursts. "
        "Профиль для паттернов, где поток нельзя оценить одной плотностью.",
        {
            "stream_jump": 0.55,
            "speed_aim": 0.20,
            "stream_burst": 0.15,
            "stamina_finger": 0.10,
        },
        comfort_stars=5.7,
        comfort_width=0.9,
        ideal_length=150,
        mapping_weight=1.55,
    )


def _slider_tech() -> PresetConfig:
    return _skill_preset(
        "Slider Tech",
        "Слайдеры и техничность: Slider Heavy, Tech, Angular Control и SV "
        "changes. Оценка остаётся эвристикой по доступным признакам.",
        {
            "slider_heavy": 0.40,
            "tech": 0.25,
            "angular_control": 0.20,
            "sv_changes": 0.15,
        },
        comfort_stars=5.3,
        comfort_width=1.0,
        ideal_length=170,
        mapping_weight=1.50,
    )


def _rhythm_control() -> PresetConfig:
    return _skill_preset(
        "Rhythm Control",
        "Ритмический контроль: Variable rhythm, Rhythm complexity, Stream "
        "control и Finger control. Нужны реальные изменения ритма, а не "
        "только высокий BPM.",
        {
            "variable_rhythm": 0.35,
            "rhythm_complexity": 0.25,
            "stream_control": 0.20,
            "finger_control": 0.20,
        },
        comfort_stars=5.2,
        comfort_width=1.0,
        ideal_length=170,
        mapping_weight=1.45,
    )


def _discovery() -> PresetConfig:
    cfg = default_config(
        "Discovery",
        "Открытие новых карт и мапперов: меньше опоры на популярность, больше "
        "новизны, старых и незнакомых карт. Фильтры сложности сохраняются.",
    )
    cfg.filters.statuses = ["ranked", "loved", "qualified", "pending"]
    cfg.ranking.trait_targets = {}
    cfg.ranking.factors["novelty"].weight = 1.8
    cfg.ranking.factors["community"].weight = 0.1
    cfg.ranking.factors["repeat_penalty"].weight = 1.0
    cfg.ranking.diversity = 0.80
    cfg.ranking.comfort_stars = 5.2
    cfg.ranking.comfort_width = 1.0
    cfg.discovery.use_oldest = True
    cfg.discovery.use_rating = False
    cfg.discovery.use_popular = True
    cfg.discovery.expand_seeds = True
    cfg.discovery.max_requests = 20
    cfg.discovery.max_pages_per_strategy = 3
    return cfg


def _custom() -> PresetConfig:
    return default_config(
        "Custom",
        "Полностью пользовательская конфигурация. Меняйте фильтры, веса и discovery.",
    )


# Mandatory target tags/features per specialized preset. Each value names
# exactly three skillsets. A map must be strictly above the relevance
# All-rounder, Any Taste and the generic discovery presets deliberately have
# no entry: they must not inherit the restrictions of specialized profiles.
# Aim (formerly Mixed Aim) keeps its classic/control/burst alternatives
# instead of collapsing to one pattern.
_RELEVANCE_TARGETS: dict[str, tuple[str, ...]] = {
    "Aim": ("aim_classic", "aim_control", "aim_bursts"),
    "Aim Farm": ("aim_classic",),
    "Speed": ("speed_high_bpm", "speed_bursts", "speed_density"),
    # osu! groups bursts under the streams tag group (streams/bursts is the
    # 5-9-note tier between doubles and continuous 10+ note streams), so the
    # curated Streams preset covers both evidence kinds.
    "Streams": ("stream_consistency", "stream_long", "burst_control"),
    "Stamina": ("stream_endurance", "stream_long"),
    "Finger Control": ("finger_control", "alternating", "speed_control"),
    "Reading": ("ar_challenge", "high_ar", "low_ar"),
    "Accuracy": (
        "accuracy_focused",
        "od_precision",
        "low_density_precision",
    ),
    "Tech": ("tech", "slider_heavy", "rhythm_complexity"),
    "Flashlight": ("fl_friendly", "fl_reading", "fl_aim"),
    # Legacy profiles keep their own target sets instead of inheriting the
    # current preset that happens to have a similar name.
    "Mixed Aim": ("aim_classic", "aim_control", "aim_bursts"),
    "Streams & Stamina": ("stream_consistency", "stream_long"),
    "Bursts": (
        "burst_control",
        "speed_bursts",
    ),
    "Jump Aim": ("jump_aim",),
    "Wide Aim": ("aim_wide",),
    "Flow Aim": ("aim_flow",),
    "Aim Control": ("aim_control",),
    "Aim + Alt": ("aim_classic", "finger_control", "alternating"),
    "Aim + Bursts": ("aim_bursts",),
    "High BPM": ("high_bpm",),
    "Speed Bursts": ("speed_bursts",),
    "Burst-oriented Streams": ("burst_control", "speed_bursts"),
    "Continuous Streams": ("stream_long", "stream_consistency"),
    "Alternating": ("alternating",),
    "Double-tap / Burst Control": ("double_tap_control",),
    "Speed + Aim": ("speed_aim",),
    "Slider Tech": ("slider_heavy",),
    "Rhythm Control": ("variable_rhythm",),
    "Speed + Stamina": (
        "speed_high_bpm",
        "stamina_alt",
        "speed_consistency",
    ),
    "Low AR Reading": ("low_ar", "ar_challenge", "reading_aim"),
    "High AR Reading": ("high_ar", "ar_challenge", "reading_aim"),
    "Precision": ("accuracy_focused", "od_precision", "aim_precision"),
    "Reading + Aim": ("reading_aim", "aim_control", "ar_challenge"),
    "Reading + Streams": (
        "stream_reading",
        "stream_consistency",
        "density_changes",
    ),
    "Jump Streams": (
        "stream_jump",
        "speed_aim",
        "stream_burst",
    ),
    "Weird Aim": ("tech", "aim_control", "unusual_spacing"),
}

# Preset-specific gate policies. The score threshold is still the existing
# strict > relevance_threshold; these rules only decide which targets are
# primary and which feature evidence must corroborate them.
_RELEVANCE_POLICIES: dict[str, dict[str, Any]] = {
    "Aim": {
        "primary_targets": ["aim_classic", "aim_control", "aim_bursts"],
    },
    "Jump Aim": {
        "primary_targets": ["jump_aim"],
    },
    "Wide Aim": {
        "primary_targets": ["aim_wide"],
        "require_features_any": ["wide_jump_share"],
        "feature_minimums": {"wide_jump_share": 0.0},
    },
    "Flow Aim": {
        "primary_targets": ["aim_flow"],
        "require_features_any": ["flow_share"],
        "feature_minimums": {"flow_share": 0.0},
    },
    "Aim Control": {
        "primary_targets": ["aim_control"],
        "require_features_any": ["angle_change_share"],
        "feature_minimums": {"angle_change_share": 0.0},
    },
    "Aim + Bursts": {
        "primary_targets": ["aim_bursts"],
        "require_features_any": ["bursts"],
        "feature_minimums": {"bursts": 0.0},
    },
    "Speed": {
        "primary_targets": [
            "speed_high_bpm",
            "speed_bursts",
            "speed_density",
        ],
        "require_features_any": ["speed_pressure", "bursts"],
    },
    "High BPM": {
        "primary_targets": ["high_bpm"],
    },
    "Speed Bursts": {
        "primary_targets": ["speed_bursts"],
        "require_features_any": ["bursts"],
        "feature_minimums": {"bursts": 0.0},
    },
    "Speed + Aim": {
        "primary_targets": ["speed_aim"],
    },
    "Streams & Stamina": {
        "primary_targets": ["stream_consistency", "stream_long"],
        "require_features_any": ["streams"],
    },
    "Stamina": {
        # Endurance must come from sustained stream structure; the generic
        # stamina proxy and map density alone are not a strict gate.
        "primary_targets": ["stream_endurance"],
    },
    "Streams": {
        "primary_targets": [
            "stream_consistency",
            "stream_long",
            "burst_control",
        ],
        # Measured evidence only: continuous streams or 5-9 note bursts must
        # come from the local .osu analysis, never from a mapper tag. These are
        # raw note shares, not target scores: one measured chain is evidence
        # even when it covers less than 55% of a long map. The primary target
        # must still clear the normal relevance threshold.
        "require_features_any": ["streams", "bursts"],
        "feature_minimums": {"streams": 0.0, "bursts": 0.0},
    },
    "Burst-oriented Streams": {
        "primary_targets": ["burst_control", "speed_bursts"],
        "require_features_any": ["bursts"],
        "feature_minimums": {"bursts": 0.0},
    },
    "Continuous Streams": {
        "primary_targets": ["stream_long"],
        "require_features_any": ["streams"],
        "feature_minimums": {"streams": 0.0},
    },
    "Bursts": {
        # Both targets require burst evidence and must exceed the relevance
        # threshold. The raw burst-note share is a fraction of the whole map,
        # not a 0..1 evidence score; requiring it above 0.55 would reject
        # legitimately burst-heavy maps (e.g. bursts=0.15) whose target score
        # is already strong.
        "primary_targets": [
            "burst_control",
            "speed_bursts",
        ],
    },
    "Finger Control": {
        "primary_targets": ["finger_control", "alternating", "speed_control"],
        # Measured evidence only: a mapper tag alone must not open the gate.
        # Tags still lift the skill scores via their weights.
        "require_features_any": ["density", "speed_pressure"],
    },
    "Alternating": {
        "primary_targets": ["alternating"],
    },
    "Double-tap / Burst Control": {
        "primary_targets": ["double_tap_control"],
        "require_features_any": ["bursts"],
        "feature_minimums": {"bursts": 0.0},
    },
    "Reading": {
        "primary_targets": ["ar_challenge"],
    },
    "Accuracy": {
        "primary_targets": ["accuracy_focused"],
        "require_features_any": ["low_density", "od"],
    },
    "Tech": {
        "primary_targets": ["tech", "slider_heavy", "rhythm_complexity"],
        # Measured evidence only: a mapper tag alone must not open the gate.
        "require_features_any": [
            "slider_ratio",
            "density",
        ],
    },
    "Slider Tech": {
        "primary_targets": ["slider_heavy"],
        "require_features_any": ["slider_ratio", "slider_share"],
        "feature_minimums": {"slider_ratio": 0.0, "slider_share": 0.0},
    },
    "Rhythm Control": {
        "primary_targets": ["variable_rhythm"],
        "require_features_any": ["rhythm_variability"],
        "feature_minimums": {"rhythm_variability": 0.0},
    },
    "Flashlight": {
        "primary_targets": ["fl_friendly"],
        "require_features_any": ["tag_fl"],
    },
    # Retired names keep their stored policies so an untouched legacy row
    # round-trips through known_builtin_by_name() unchanged.
    "Mixed Aim": {
        "primary_targets": ["aim_classic", "aim_control", "aim_bursts"],
    },
}

# Target sets shipped by v11. v12 migration uses these exact old values so an
# untouched built-in can be updated without touching edited/user copies.
_PRE_V12_TARGETS: dict[str, tuple[str, ...]] = {
    "Mixed Aim": (),
    "Speed": ("speed_high_bpm", "speed_bursts", "speed_density"),
    "Streams & Stamina": ("stream_consistency", "stream_long"),
    "Bursts": (
        "burst_control",
        "speed_bursts",
        "speed_short_bursts",
    ),
    "Finger Control": ("finger_control", "alternating", "speed_control"),
    "Reading": ("ar_challenge", "high_ar", "low_ar"),
    "Accuracy": (
        "accuracy_focused",
        "od_precision",
        "low_density_precision",
    ),
    "Tech": ("tech", "slider_heavy", "rhythm_complexity"),
    "Flashlight": ("fl_friendly", "fl_reading", "fl_aim"),
}

# The v12 Bursts policy required the raw burst-note proportion to exceed 0.55.
# Keep the exact old value for the v13 migration comparison.
_PRE_V13_BURSTS_POLICY: dict[str, Any] = {
    "primary_targets": [
        "burst_control",
        "speed_bursts",
        "speed_short_bursts",
    ],
    "require_features_any": ["bursts"],
}

_PRE_V17_STAMINA_SKILL_WEIGHTS = {
    "stamina_finger": 1.0,
    "stream_long": 0.55,
    "speed_stamina": 0.45,
}
_PRE_V17_STAMINA_TARGETS = (
    "stamina_finger",
    "stream_long",
    "speed_stamina",
)
_PRE_V17_STAMINA_DESCRIPTION = (
    "Выносливость как отдельный навык (streams/stamina на osu!): длинные "
    "и плотные карты, finger stamina и stream stamina. Длина — мягкое "
    "предпочтение; stream-часть требует локального .osu, остальное "
    "остаётся явным прокси."
)

# v17 definitions that become narrower/clearer active children in v18.
# Fingerprints let the migration update only untouched built-ins.
_PRE_V18_FINGERPRINTS = {
    "Streams": "f57851d985fb50cae47241690d9119f49252bf1513a792b2e4acc3a4f991d71a",
    "Wide Aim": "d1780ff3de70bfeaaf6ebd388bf773259bf8380b9ac0702435b0a28fb0f49aa1",
    "Flow Aim": "40b2aa8984a703aafbcdbc98f762320023d13f3e26e7d1cc9a3ce44891ccec30",
    "Aim Control": "ad4ed0d556db0982f3449d3c77ffbd07674d2eb37f9e1b32466152e45f1e84dd",
    "Aim + Bursts": "759d37b2266449113c525571db3a889ae5813a25315c560b2d03c3ccfed57d2d",
    "Speed + Aim": "e957e942f28ceefc19bc11725871832a669483cbf5c11ed9635826efedef1ab0",
    "Slider Tech": "8c51e90ab84b173c695eecc1a7b31fe054c6df09faaa6deadf01816cf95a255b",
    "Rhythm Control": "438bc0085bc779d9a54c972ca20540a49f1d8eea404ebf8e7b63865004ce8c36",
}
_PRE_V18_STREAMS_POLICY = {
    "primary_targets": [
        "stream_consistency",
        "stream_long",
        "burst_control",
    ],
    "require_features_any": ["streams", "bursts"],
}


def _apply_relevance_defaults(cfg: PresetConfig) -> PresetConfig:
    """Attach a preset's own targets and gate policy after its builder."""

    cfg.ranking.relevance_targets = list(_RELEVANCE_TARGETS.get(cfg.name, ()))
    cfg.ranking.relevance_policy = copy.deepcopy(
        _RELEVANCE_POLICIES.get(cfg.name, {})
    )
    return cfg


# Curated built-ins: one clear profile per training goal.  The long tail of
# overlapping experiments from earlier versions stays available as legacy
# rows, but is no longer offered as a ready-made preset.
_BUILDERS = (
    _discovery,
    _allrounder,
    _aim_farm,
    _aim,
    _jump_aim,
    _flow_aim,
    _wide_aim,
    _aim_control,
    _aim_bursts,
    _speed,
    _high_bpm,
    _speed_bursts,
    _speed_aim,
    _streams,
    _burst_streams,
    _continuous_streams,
    _stamina,
    _finger_control,
    _alternating,
    _double_tap,
    _tech,
    _slider_tech,
    _rhythm_control,
)

_LEGACY_BUILDERS = (
    _any_taste,
    _legacy_mixed_aim_skillset,
    _streams_stamina,
    _bursts,
    _weird_aim,
    _aim_alt,
    _speed_stamina,
    _low_ar_reading,
    _high_ar_reading,
    _precision,
    _reading_aim,
    _reading_streams,
    _jump_streams,
    _custom,
    _legacy_aim_farm_pp_config,
    # Retired standalone active presets. Their stored rows remain readable as
    # legacy data, but they are no longer seeded, offered or reset as active
    # built-ins.
    _reading,
    _accuracy,
    _flashlight,
)

# Exact v10 Aim Farm fingerprint. v10 already had title queries and the
# current gate, but used 18 requests / 2 pages / 8 attributes.
_PRE_V11_FINGERPRINTS = {
    "Aim Farm": (
        "d5ac64627bafe12fa1bc759079bf5884be3146fe2baf62532a8c98e970e84e6b"
    ),
}

BUILTIN_PRESETS_VERSION = 18
_V18_NEW_NAMES = frozenset(
    {
        "Jump Aim",
        "Flow Aim",
        "Wide Aim",
        "Aim Control",
        "Aim + Bursts",
        "High BPM",
        "Speed Bursts",
        "Speed + Aim",
        "Burst-oriented Streams",
        "Continuous Streams",
        "Alternating",
        "Double-tap / Burst Control",
        "Slider Tech",
        "Rhythm Control",
    }
)
_V2_NEW_NAMES = frozenset(
    {
        "Speed",
        "Finger Control",
        "Accuracy",
        "Reading",
        "Tech",
        "Flashlight",
        "All-rounder",
    }
)
_V5_NEW_NAMES = frozenset({"Streams & Stamina"})
_V6_NEW_NAMES = frozenset({"Aim Farm"})
_V9_NEW_NAMES = frozenset({"Bursts"})
_V15_NEW_NAMES = frozenset({"Aim"})
_V16_NEW_NAMES = frozenset({"Streams", "Stamina"})

# Canonical fingerprints of the pre-v10 seeded configurations. The v10
# migration only replaces rows that are still byte-for-byte equivalent to a
# known untouched built-in; edited built-ins and user presets are preserved.
_PRE_V10_FINGERPRINTS: dict[str, str] = {
    "Accuracy": "f33337a20da993cac58a88f0b4e6d28e1553275081d85f71713b187bf7826fc5",
    "Aim + Alt": "7565142c59812413ec7923647ac8c41ab3c66964730ee41e317c1a25dacaccca",
    "Aim + Bursts": "f49beaf8324293cad1e1de1d6b84daf76f5de7a1948b8b279c9d1da505bccf9f",
    "Aim Control": "ad4ed0d556db0982f3449d3c77ffbd07674d2eb37f9e1b32466152e45f1e84dd",
    "Aim Farm": "845e2f74fefd117ededb30d471b3a94cf8ae632a435edf08819c2b6fdbb9e319",
    "Aim Farm / PP": "a6339d0b9f91c844d882113c74081138f736d5cb73285c8d3bd94b0645c2aa21",
    "All-rounder": "2596f777bdafb760e1af5555a7596a13a115a1871b611b0342560909f553f26d",
    "Any Taste": "505b4c0a242e55e8b5885c88277b63d60059dc0d033a83a4cc3afc88ad7f9c8a",
    "Bursts": "3780d3f93823364d046550ab2b9bf06225fa97f312772b7951a97e27c1697756",
    "Custom": "24b506680dfc90bba989f374fdddf6e2b2955dc3ad7d76be3dd245bcd60bf8f2",
    "Discovery": "4a436f52361d6b94891686ac90e3eb60b6b134c860ad967331db821a88942edb",
    "Finger Control": "dc0b4bcfe2d9bbff9da4b9bdebf702e6430ad3c5fece871305dd69012bec74c6",
    "Flashlight": "b37828f3a2888da767de7ca3e14f2d345564964e949ac1323f5e9cc5cc3e6a79",
    "Flow Aim": "6e0bcedf14b122fd85ecd31f8562ebbfad2a80b6c55afc191c6cdf057f80b026",
    "High AR Reading": "1af3443f795d3282b83e6b69da8af84c524965b5becb4ba331b18e2318ab17af",
    "Jump Streams": "581b5c7563cb5ea9fb07f2ce7ce8f4c6119ad8c725be6a090f5322da857b93ae",
    "Low AR Reading": "d2718421f445826d1cc3c0e74835d04aac755b4f4d64c6637b6317ef8e9efae8",
    "Mixed Aim": "006283ad29fd617a28d1c11fcffd9d79430f597513672a352d465278fb4f13ad",
    "Precision": "08b144b846783ea6da008e960734ecb3c7730ec6274275ef44183fcb62dc921e",
    "Reading": "d073136b0599ab5e20f18e5f472f4a746a2caa24aaa8ab5cb56dd88f558861a2",
    "Reading + Aim": "bc7d41cdfafc18ca16916bba81d432c8d5da8588fbb06779008c149f1bea68de",
    "Reading + Streams": "137d4a7a1ee51eee7ba898dcd48d77c5634f69ba0b97fbedba67820c57f1862d",
    "Rhythm Control": "8dc18e519f0fd9bbeefde94ed38261f197c5afd5d46313cc6d48aac54bb98b6c",
    "Slider Tech": "8c51e90ab84b173c695eecc1a7b31fe054c6df09faaa6deadf01816cf95a255b",
    "Speed": "98915be19f8d8b5a436772bbf56a5829afc8e7d630bc709e2f6bb6bf542fba57",
    "Speed + Aim": "e957e942f28ceefc19bc11725871832a669483cbf5c11ed9635826efedef1ab0",
    "Speed + Stamina": "4cd9881df3649a1243bb541ea90d4f086f7cd35257aaa06f9c0737a915782756",
    "Stamina": "1d05ab568933f883a901051a0787a8296ccaf673d59b3323e477786cf82f136f",
    "Streams": "98bcea74c342daed3257ab6f422170b985bc816a95e466b34ebe4a43f404012b",
    "Streams & Stamina": "99beae62a0aace7ddf6cb2f5937ead26937b8991081b6f99aee551a22016e98e",
    "Tech": "038f5db26d1648a562e4ffaabd51433aae3e438cd6bb4c1a5ec461fa410fd799",
    "Weird Aim": "b3873cea35b5160a082192c21949e544ff30675ce90b35ab4e131039117a574f",
    "Wide Aim": "c5ef189575ee6ce0bcf473b08c97dd9af39b7b41a1c69a52269c35b12e9a6ea6",
}

def _canonical_config_value(value: Any) -> Any:
    """Make SQLite JSON round-trips (150 vs 150.0) hash-comparable."""

    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {
            key: _canonical_config_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]
    return value


def _preset_fingerprint(cfg: PresetConfig) -> str:
    data = _canonical_config_value(_normalized_config_dict(cfg))
    # Hierarchy metadata was added later and is not search-affecting; strip it
    # so old built-in fingerprints remain stable.
    for key in ("catalog_id", "parent_id", "category_id", "display_order"):
        data.pop(key, None)
    # Fields introduced by v10 are absent from every old row. Strip them so
    # an old untouched definition keeps a stable fingerprint after parsing.
    data.get("ranking", {}).pop("farm_tolerance_seconds", None)
    data.get("ranking", {}).pop("relevance_policy", None)
    data.get("discovery", {}).pop("title_queries", None)
    # Language filters were added after v13. Old rows (and every canonical
    # pre-v14 fingerprint) implicitly mean "no language filter", so drop the
    # fields while they still hold the default empty value.
    filters = data.get("filters", {})
    for key in ("include_languages", "exclude_languages"):
        if not filters.get(key):
            filters.pop(key, None)
    if filters.get("published_date_field") == "ranked":
        filters.pop("published_date_field", None)
    raw = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _upgrade_v10_definitions(storage: Any) -> int:
    changed = 0
    for preset in storage.list_presets():
        if not preset.is_builtin:
            continue
        expected = _PRE_V10_FINGERPRINTS.get(preset.name)
        if expected is None or _preset_fingerprint(preset.config) != expected:
            continue
        modern = builtin_by_name(preset.name) or known_builtin_by_name(
            preset.name
        )
        if modern is None or _matches_legacy(preset.config, modern):
            continue
        storage.update_preset(preset.id, modern)
        changed += 1
    return changed


_BUILTIN_NAMES = frozenset(builder().name for builder in _BUILDERS)
LEGACY_BUILTIN_NAMES = frozenset(
    builder().name for builder in _LEGACY_BUILDERS
) - _BUILTIN_NAMES


def _with_catalog_metadata(cfg: PresetConfig) -> PresetConfig:
    node = catalog.PRESET_NODE_BY_NAME.get(cfg.name)
    if node is None:
        cfg.catalog_id = ""
        cfg.parent_id = None
        cfg.category_id = "custom"
        cfg.display_order = 0
    else:
        cfg.catalog_id = node.id
        cfg.parent_id = node.parent_id
        cfg.category_id = node.category_id
        cfg.display_order = node.order
    return cfg


def builtin_presets() -> list[PresetConfig]:
    """Fresh copies of the curated built-in presets with hierarchy metadata."""
    return [
        _with_catalog_metadata(
            copy.deepcopy(_apply_relevance_defaults(builder()))
        )
        for builder in _BUILDERS
    ]


def legacy_builtin_presets() -> list[PresetConfig]:
    """Old built-ins kept for migration and explicit restoration."""
    return [
        copy.deepcopy(_apply_relevance_defaults(builder()))
        for builder in _LEGACY_BUILDERS
    ]


def is_legacy_builtin(preset: Preset) -> bool:
    return bool(preset.is_builtin and preset.name in LEGACY_BUILTIN_NAMES)


def visible_presets(storage: Any, *, include_id: int | None = None) -> list[Preset]:
    """Curated active built-ins and user presets.

    Legacy/retired rows remain in SQLite so old links and user data resolve,
    but they are deliberately absent from every active selection surface.
    ``include_id`` is retained as a compatibility argument for callers that
    used to re-show the active row; it no longer overrides retirement.
    """

    del include_id
    return [
        preset
        for preset in storage.list_presets()
        if not is_legacy_builtin(preset)
    ]


def legacy_presets(storage: Any) -> list[Preset]:
    return [preset for preset in storage.list_presets() if is_legacy_builtin(preset)]


def builtin_by_name(name: str) -> PresetConfig | None:
    for preset in builtin_presets():
        if preset.name.casefold() == name.casefold():
            return preset
    return None


def known_builtin_by_name(name: str) -> PresetConfig | None:
    """Canonical or legacy built-in, used only for an explicit reset."""
    for preset in (*builtin_presets(), *legacy_builtin_presets()):
        if preset.name.casefold() == name.casefold():
            return preset
    return None


def _upgrade_search_budget(storage: Any) -> int:
    """Reduce the old default request budget without touching user edits.

    Version 6 presets used 20 requests, 2 pages per strategy and 8 difficulty
    attributes. That made the first API search spend roughly 22 seconds just in
    rate-limited requests. Only exact old defaults are upgraded; custom values
    remain untouched.
    """

    changed = 0
    for preset in storage.list_presets():
        discovery = preset.config.discovery
        if (
            int(discovery.max_requests) != 20
            or int(discovery.max_pages_per_strategy) != 2
            or int(discovery.fetch_attributes_limit) != 8
        ):
            continue
        upgraded = copy.deepcopy(preset.config)
        upgraded.discovery.max_requests = config.DEFAULT_MAX_REQUESTS
        upgraded.discovery.max_pages_per_strategy = (
            config.DEFAULT_MAX_PAGES_PER_STRATEGY
        )
        upgraded.discovery.fetch_attributes_limit = (
            config.DEFAULT_FETCH_ATTRIBUTES_LIMIT
        )
        storage.update_preset(preset.id, upgraded)
        changed += 1
    return changed


def _upgrade_relevance_targets(storage: Any) -> int:
    """Add preset-owned target gates to untouched v7 built-ins only.

    A user-edited built-in is not rewritten. Newly seeded presets get the
    current mapping automatically through ``builtin_presets()``.
    """

    changed = 0
    for preset in storage.list_presets():
        if not preset.is_builtin:
            continue
        modern = builtin_by_name(preset.name)
        if modern is None or not modern.ranking.relevance_targets:
            continue
        before_gate = copy.deepcopy(modern)
        before_gate.ranking.relevance_targets = []
        before_gate.ranking.relevance_policy = {}
        if _matches_legacy(preset.config, before_gate):
            storage.update_preset(preset.id, modern)
            changed += 1
    return changed


def _upgrade_sequence_targets(storage: Any) -> int:
    """Move untouched built-ins from proxy targets to sequence evidence.

    Only the target list is allowed to differ from the current definition. If
    the user changed filters, weights or discovery settings, the preset stays
    exactly as they left it.
    """

    changed = 0
    for preset in storage.list_presets():
        if not preset.is_builtin:
            continue
        modern = builtin_by_name(preset.name)
        if modern is None:
            continue
        candidate = copy.deepcopy(modern)
        candidate.ranking.relevance_targets = list(
            preset.config.ranking.relevance_targets
        )
        candidate.ranking.relevance_policy = {}
        if _matches_legacy(preset.config, candidate):
            storage.update_preset(preset.id, modern)
            changed += 1
    return changed


def _upgrade_v11_definitions(storage: Any) -> None:
    """Update untouched v10 Aim Farm discovery limits to the audited defaults."""

    existing = storage.get_preset_by_name("Aim Farm")
    if (
        existing is None
        or not existing.is_builtin
        or _preset_fingerprint(existing.config)
        != _PRE_V11_FINGERPRINTS["Aim Farm"]
    ):
        return
    current = builtin_by_name("Aim Farm")
    if current is not None:
        storage.update_preset(existing.id, current)


def _upgrade_v12_definitions(storage: Any) -> int:
    """Add preset-specific relevance policies to untouched v11 built-ins."""

    changed = 0
    for preset in storage.list_presets():
        if not preset.is_builtin:
            continue
        old_targets = _PRE_V12_TARGETS.get(preset.name)
        if old_targets is None:
            continue
        modern = builtin_by_name(preset.name)
        if modern is None:
            continue
        before = copy.deepcopy(modern)
        before.ranking.relevance_targets = list(old_targets)
        before.ranking.relevance_policy = {}
        if not _matches_legacy(preset.config, before):
            continue
        storage.update_preset(preset.id, modern)
        changed += 1
    return changed


# require_features_any values shipped before v14. The v14 migration swaps
# them for measured-only lists when a stored preset still carries the old
# canonical value; user-edited policies are left alone.
_PRE_V14_REQUIRE_FEATURES: dict[str, tuple[str, ...]] = {
    "Tech": ("tag_tech", "tag_rhythm", "tag_slider", "slider_ratio"),
    "Finger Control": ("tag_finger", "tag_alt"),
    "Accuracy": ("tag_precision", "low_density"),
}

_MEASURED_REQUIRE_FEATURES: dict[str, tuple[str, ...]] = {
    "Tech": ("slider_ratio", "density"),
    "Finger Control": ("density", "speed_pressure"),
    "Accuracy": ("low_density", "od"),
}


def _upgrade_v13_definitions(storage: Any) -> int:
    """Remove the redundant raw-burst feature requirement from untouched v12."""

    preset = storage.get_preset_by_name("Bursts")
    if preset is None or not preset.is_builtin:
        return 0
    # Bursts was demoted to a legacy row by v16; the legacy builder carries
    # the exact same body, so the cleanup stays meaningful for old databases.
    modern = known_builtin_by_name("Bursts")
    if modern is None:
        return 0
    before = copy.deepcopy(modern)
    before.ranking.relevance_policy = copy.deepcopy(_PRE_V13_BURSTS_POLICY)
    if not _matches_legacy(preset.config, before):
        return 0
    storage.update_preset(preset.id, modern)
    return 1


def _upgrade_v14_definitions(storage: Any) -> int:
    """Rewrite retired near-duplicate skillset ids to their canonical survivor.

    Unlike the earlier definition upgrades this touches user presets and
    edited built-ins too: a stored weight or gate target pointing at a
    retired id would otherwise silently weight nothing.
    """

    changed = 0
    for preset in storage.list_presets():
        ranking = preset.config.ranking
        weights = ranking.skill_weights
        new_weights: dict[str, float] = {}
        for key, value in weights.items():
            resolved = SKILL_ALIASES.get(str(key), str(key))
            value = float(value)
            if resolved in new_weights:
                new_weights[resolved] = max(new_weights[resolved], value)
            else:
                new_weights[resolved] = value
        targets = list(
            dict.fromkeys(
                SKILL_ALIASES.get(str(t), str(t)) for t in ranking.relevance_targets
            )
        )
        policy = copy.deepcopy(ranking.relevance_policy)
        primary = policy.get("primary_targets")
        if isinstance(primary, list):
            policy["primary_targets"] = list(
                dict.fromkeys(
                    SKILL_ALIASES.get(str(t), str(t)) for t in primary
                )
            )
        # Policy upgrade: tag-only gate evidence -> measured-only lists, but
        # only when the stored policy still carries the pre-v14 canonical
        # value (a user-edited policy is preserved).
        old_require = _PRE_V14_REQUIRE_FEATURES.get(preset.config.name)
        if old_require is not None:
            current = policy.get("require_features_any")
            if isinstance(current, list) and [
                str(item) for item in current
            ] == list(old_require):
                policy["require_features_any"] = list(
                    _MEASURED_REQUIRE_FEATURES[preset.config.name]
                )
        if (
            new_weights != weights
            or targets != ranking.relevance_targets
            or policy != ranking.relevance_policy
        ):
            ranking.skill_weights = new_weights
            ranking.relevance_targets = targets
            ranking.relevance_policy = policy
            storage.update_preset(preset.id, preset.config)
            changed += 1
    return changed


def _upgrade_v15_definitions(storage: Any) -> int:
    """Rename untouched Mixed Aim rows to Aim (v15 taxonomy rename).

    The profile itself is unchanged; only the name and description differ,
    mirroring the osu! tag model where aim skills have no dedicated
    skillset/aim tag. A user-edited Mixed Aim row is left alone and stays
    as a hidden legacy row.
    """

    changed = 0
    mixed_row = storage.get_preset_by_name("Mixed Aim")
    if mixed_row is None or not mixed_row.is_builtin:
        return 0
    if storage.get_preset_by_name("Aim") is not None:
        return 0
    before = _apply_relevance_defaults(_legacy_mixed_aim_skillset())
    if not _matches_legacy(mixed_row.config, before):
        return 0
    upgraded = builtin_by_name("Aim")
    if upgraded is not None:
        storage.update_preset(mixed_row.id, upgraded)
        changed += 1
    return changed


def _upgrade_v16_definitions(storage: Any) -> int:
    """Adopt the osu! streams tag model: Streams absorbs Bursts.

    osu! user tags put bursts inside the streams group (streams/bursts, the
    5-9-note tier), so the curated Streams preset now targets both continuous
    streams and bursts, and Stamina is offered standalone again. Untouched
    legacy Streams rows are rewritten to the merged body; Streams & Stamina
    and Bursts stay as hidden legacy rows for whoever still uses them.
    """

    changed = 0
    streams_row = storage.get_preset_by_name("Streams")
    if streams_row is not None and streams_row.is_builtin:
        modern = builtin_by_name("Streams")
        if modern is not None and not _matches_legacy(
            streams_row.config, modern
        ):
            # Stored rows come in two shapes: pre-v10 seeds without gate
            # targets, and rows rewritten by the v10 fingerprint migration
            # with the old legacy target list attached.
            plain = _legacy_streams_config()
            with_targets = _legacy_streams_config()
            with_targets.ranking.relevance_targets = [
                "stream_consistency",
                "stream_long",
                "stream_burst",
            ]
            if _matches_legacy(
                streams_row.config, plain
            ) or _matches_legacy(streams_row.config, with_targets):
                storage.update_preset(streams_row.id, modern)
                changed += 1
    stamina_row = storage.get_preset_by_name("Stamina")
    stamina_modern = builtin_by_name("Stamina")
    # A promoted legacy row must not silently lose its gate. Compare against
    # the pre-v16 shape, not the current builder (which v17 changes again).
    stamina_v16 = copy.deepcopy(_stamina())
    stamina_v16.description = _PRE_V17_STAMINA_DESCRIPTION
    stamina_v16.ranking.skill_weights = dict(_PRE_V17_STAMINA_SKILL_WEIGHTS)
    stamina_v16.ranking.relevance_targets = []
    stamina_v16.ranking.relevance_policy = {}
    if (
        stamina_row is not None
        and stamina_row.is_builtin
        and stamina_modern is not None
        and not _matches_legacy(stamina_row.config, stamina_modern)
        and _matches_legacy(stamina_row.config, stamina_v16)
    ):
        storage.update_preset(stamina_row.id, stamina_modern)
        changed += 1
    return changed


def _upgrade_v17_definitions(storage: Any) -> int:
    """Make untouched Stamina rows require sustained stream endurance."""

    stamina_row = storage.get_preset_by_name("Stamina")
    modern = builtin_by_name("Stamina")
    if (
        stamina_row is None
        or not stamina_row.is_builtin
        or modern is None
    ):
        return 0
    before = copy.deepcopy(modern)
    before.description = _PRE_V17_STAMINA_DESCRIPTION
    before.ranking.skill_weights = dict(_PRE_V17_STAMINA_SKILL_WEIGHTS)
    before.ranking.relevance_targets = list(_PRE_V17_STAMINA_TARGETS)
    before.ranking.relevance_policy = {}
    if not _matches_legacy(stamina_row.config, before):
        return 0
    storage.update_preset(stamina_row.id, modern)
    return 1


def _apply_catalog_metadata(storage: Any) -> int:
    """Attach stable hierarchy metadata without touching search settings."""

    changed = 0
    active = {
        preset.name.casefold(): preset for preset in builtin_presets()
    }
    for row in storage.list_presets():
        if not row.is_builtin:
            continue
        desired = active.get(row.name.casefold())
        if desired is None:
            continue
        current = row.config
        if (
            current.catalog_id == desired.catalog_id
            and current.parent_id == desired.parent_id
            and current.category_id == desired.category_id
            and current.display_order == desired.display_order
        ):
            continue
        updated = PresetConfig.from_dict(current.to_dict())
        updated.catalog_id = desired.catalog_id
        updated.parent_id = desired.parent_id
        updated.category_id = desired.category_id
        updated.display_order = desired.display_order
        storage.update_preset(row.id, updated)
        changed += 1
    return changed


def _upgrade_v18_definitions(storage: Any) -> int:
    """Promote the hierarchical catalog and refresh untouched v17 definitions."""

    changed = 0
    for name, fingerprint in _PRE_V18_FINGERPRINTS.items():
        row = storage.get_preset_by_name(name)
        modern = builtin_by_name(name)
        if row is None or not row.is_builtin or modern is None:
            continue
        if _preset_fingerprint(row.config) != fingerprint:
            continue
        if name == "Streams":
            if row.config.ranking.relevance_policy != _PRE_V18_STREAMS_POLICY:
                continue
        storage.update_preset(row.id, modern)
        changed += 1
    return changed


def ensure_builtin_presets(storage: Any) -> int:
    """Seed built-ins and add newer profiles without destroying user edits.

    Existing databases keep their edited presets. Exact, unedited legacy
    profiles are upgraded to the shared skillset engine; anything the user
    changed is left alone. Deleted built-ins are not re-created on ordinary
    startup.
    """
    if storage.get_preference("builtins_seeded") != "1":
        created = 0
        for preset in builtin_presets():
            if storage.get_preset_by_name(preset.name) is None:
                storage.create_preset(preset, is_builtin=True)
                created += 1
        storage.set_preference("builtins_seeded", "1")
        storage.set_preference("builtins_version", str(BUILTIN_PRESETS_VERSION))
        return created

    try:
        version = int(storage.get_preference("builtins_version") or "1")
    except ValueError:
        version = 1
    if version >= BUILTIN_PRESETS_VERSION:
        _apply_catalog_metadata(storage)
        return 0

    created = 0
    new_names: set[str] = set()
    if version < 7:
        _upgrade_search_budget(storage)
    if version < 8:
        _upgrade_relevance_targets(storage)
    if version < 9:
        _upgrade_sequence_targets(storage)
    if version < 3:
        new_names.update(_V2_NEW_NAMES)
    if version < 5:
        new_names.update(_V5_NEW_NAMES)
    if version < 6:
        new_names.update(_V6_NEW_NAMES)
        # Older databases stored the exact same NM farm profile under a
        # misleading "... / PP" name. Rename the untouched built-in in place;
        # a user-edited copy is left alone and stays hidden as a legacy row.
        old_farm = storage.get_preset_by_name("Aim Farm / PP")
        new_farm = storage.get_preset_by_name("Aim Farm")
        if (
            old_farm is not None
            and old_farm.is_builtin
            and new_farm is None
            and (
                _current_aim_farm_matches(old_farm.config)
                or _legacy_aim_farm_matches(old_farm.config)
            )
        ):
            upgraded = builtin_by_name("Aim Farm")
            if upgraded is not None:
                storage.update_preset(old_farm.id, upgraded)
    if version < 9:
        new_names.update(_V9_NEW_NAMES)
    if version < 15:
        # The rename must run before the create loop so the renamed row
        # satisfies the "Aim" lookup and no duplicate row is seeded.
        _upgrade_v15_definitions(storage)
        new_names.update(_V15_NEW_NAMES)
    if version < 16:
        _upgrade_v16_definitions(storage)
        new_names.update(_V16_NEW_NAMES)
    if version < 17:
        _upgrade_v17_definitions(storage)
    if version < 18:
        _upgrade_v18_definitions(storage)
        new_names.update(_V18_NEW_NAMES)

    for preset in builtin_presets():
        if preset.name not in new_names:
            continue
        if storage.get_preset_by_name(preset.name) is None:
            storage.create_preset(preset, is_builtin=True)
            created += 1

    existing_alt = storage.get_preset_by_name("Aim + Alt")
    if (
        existing_alt is not None
        and existing_alt.is_builtin
        and _legacy_aim_alt_matches(existing_alt.config)
    ):
        upgraded_alt = builtin_by_name("Aim + Alt")
        if upgraded_alt is not None:
            storage.update_preset(existing_alt.id, upgraded_alt)

    if version < 10:
        _upgrade_v10_definitions(storage)
    if version < 11:
        _upgrade_v11_definitions(storage)
    if version < 12:
        _upgrade_v12_definitions(storage)
    if version < 13:
        _upgrade_v13_definitions(storage)
    if version < 14:
        _upgrade_v14_definitions(storage)

    _apply_catalog_metadata(storage)
    storage.set_preference("builtins_version", str(BUILTIN_PRESETS_VERSION))
    return created


def restore_builtin_presets(storage: Any) -> dict[str, int]:
    """Restore/update all built-in presets without touching other data."""
    created = 0
    updated = 0
    for preset in builtin_presets():
        existing = storage.get_preset_by_name(preset.name)
        if existing is None:
            storage.create_preset(preset, is_builtin=True)
            created += 1
        else:
            storage.update_preset(existing.id, preset)
            updated += 1
    return {"created": created, "updated": updated}


def reset_preset_to_default(storage: Any, preset_id: int) -> Preset:
    current = storage.get_preset(preset_id)
    if current is None:
        raise PresetError("Пресет не найден")
    default = known_builtin_by_name(current.name)
    if default is None:
        default = default_config(current.name, current.config.description)
        default.name = current.name
    return storage.update_preset(preset_id, default)


def duplicate_preset(storage: Any, preset_id: int, new_name: str | None = None) -> Preset:
    source = storage.get_preset(preset_id)
    if source is None:
        raise PresetError("Пресет не найден")
    name = (new_name or f"{source.name} copy").strip()
    if storage.get_preset_by_name(name) is not None:
        raise PresetError(f"Пресет с именем «{name}» уже существует")
    config_copy = PresetConfig.from_dict(source.config.to_dict())
    config_copy.name = name
    config_copy.description = source.config.description
    _with_catalog_metadata(config_copy)
    validate_preset_config(config_copy)
    return storage.create_preset(config_copy, is_builtin=False)


def create_preset(
    storage: Any, name: str, base_preset_id: int | None = None
) -> Preset:
    name = name.strip()
    if not name:
        raise PresetError("Имя пресета не может быть пустым")
    if storage.get_preset_by_name(name) is not None:
        raise PresetError(f"Пресет с именем «{name}» уже существует")
    if base_preset_id is not None:
        base = storage.get_preset(base_preset_id)
        if base is None:
            raise PresetError("Базовый пресет не найден")
        cfg = PresetConfig.from_dict(base.config.to_dict())
        cfg.name = name
        cfg.schema_version = config.SETTINGS_SCHEMA_VERSION
        _with_catalog_metadata(cfg)
    else:
        cfg = default_config(name)
    validate_preset_config(cfg)
    return storage.create_preset(cfg, is_builtin=False)


def rename_preset(storage: Any, preset_id: int, new_name: str) -> Preset:
    current = storage.get_preset(preset_id)
    if current is None:
        raise PresetError("Пресет не найден")
    name = new_name.strip()
    existing = storage.get_preset_by_name(name)
    if existing is not None and existing.id != preset_id:
        raise PresetError(f"Пресет с именем «{name}» уже существует")
    cfg = PresetConfig.from_dict(current.config.to_dict())
    cfg.name = name
    _with_catalog_metadata(cfg)
    validate_preset_config(cfg)
    return storage.update_preset(preset_id, cfg)


# --------------------------------------------------------------------------
# JSON import / export
# --------------------------------------------------------------------------


def export_preset(preset: Preset) -> dict[str, Any]:
    return {
        "schema_version": config.SETTINGS_SCHEMA_VERSION,
        "exported_at": preset.updated_at or preset.created_at,
        "preset": preset.config.to_dict(),
    }


def export_presets_json(presets: list[Preset]) -> str:
    payload = {
        "schema_version": config.SETTINGS_SCHEMA_VERSION,
        "app": config.APP_NAME,
        "presets": [
            {
                "name": preset.name,
                "is_builtin": preset.is_builtin,
                "config": preset.config.to_dict(),
            }
            for preset in presets
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def import_presets_json(text: str) -> list[PresetConfig]:
    """Validate an import completely before the caller writes anything."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PresetError(f"Некорректный JSON: {exc}") from exc
    if isinstance(data, dict) and "preset" in data:
        version = data.get("schema_version", config.SETTINGS_SCHEMA_VERSION)
        if version != config.SETTINGS_SCHEMA_VERSION:
            raise PresetError(f"Версия схемы {version!r} не поддерживается")
        items = [data]
    elif isinstance(data, dict) and "presets" in data:
        version = data.get("schema_version", config.SETTINGS_SCHEMA_VERSION)
        if version != config.SETTINGS_SCHEMA_VERSION:
            raise PresetError(f"Версия схемы {version!r} не поддерживается")
        items = data["presets"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "name" in data:
        items = [data]
    else:
        raise PresetError(
            "Ожидался объект пресета, массив или {schema_version, presets}"
        )
    if not isinstance(items, list) or not items:
        raise PresetError("В файле нет пресетов")

    result: list[PresetConfig] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PresetError(f"presets[{index}]: ожидался объект")
        if "config" in item:
            raw = item["config"]
        elif "preset" in item:
            raw = item["preset"]
        else:
            raw = item
        try:
            cfg = PresetConfig.from_dict(raw, f"presets[{index}]")
        except ModelError as exc:
            raise PresetError(str(exc)) from exc
        validate_preset_config(cfg)
        result.append(cfg)
    return result
