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


def _mixed_aim() -> PresetConfig:
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
    cfg.discovery.max_requests = 18
    cfg.discovery.max_pages_per_strategy = 2
    cfg.discovery.fetch_attributes_limit = 8
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


_LEGACY_SKILL_PROFILES = (
    ("Mixed Aim", _legacy_mixed_aim_config),
    ("Wide Aim", _legacy_wide_aim_config),
    ("Flow Aim", _legacy_flow_aim_config),
    ("Aim Control", _legacy_aim_control_config),
    ("Weird Aim", _legacy_weird_aim_config),
)


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


def _streams() -> PresetConfig:
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
        "Выносливость: длинные и плотные карты, finger stamina и stream "
        "stamina. Длина — мягкое предпочтение; stream-часть требует "
        "локального .osu, остальное остаётся явным прокси.",
        {
            "stamina_finger": 1.0,
            "stream_long": 0.55,
            "speed_stamina": 0.45,
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
            "speed_short_bursts": 0.45,
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
        "Скорость с прицелом: High-BPM, Speed + aim и немного классического "
        "aim. Подходит для карт, где скорость неотделима от движения курсора.",
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
        "Ритмический контроль: Variable rhythm, Rhythm changes, Stream "
        "control и Finger control. Нужны реальные изменения ритма, а не "
        "только высокий BPM.",
        {
            "variable_rhythm": 0.35,
            "rhythm_changes": 0.25,
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
# threshold for at least one of them before it can be ranked. Mixed Aim,
# All-rounder, Any Taste and the generic discovery presets deliberately have
# no entry: they must not inherit the restrictions of specialized profiles.
_RELEVANCE_TARGETS: dict[str, tuple[str, ...]] = {
    "Aim Farm": ("aim_classic",),
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
    # Legacy profiles keep their own target sets instead of inheriting the
    # current preset that happens to have a similar name.
    "Streams": ("stream_consistency", "stream_long", "stream_burst"),
    "Stamina": ("stamina_finger", "stream_long", "speed_stamina"),
    "Wide Aim": ("aim_wide", "aim_classic"),
    "Flow Aim": ("aim_flow", "aim_classic"),
    "Aim Control": ("aim_control", "aim_precision", "aim_classic"),
    "Aim + Alt": ("aim_classic", "finger_control", "alternating"),
    "Aim + Bursts": ("aim_bursts", "speed_bursts"),
    "Speed + Aim": ("speed_aim", "speed_high_bpm", "aim_classic"),
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
    "Slider Tech": ("slider_heavy", "tech", "angular_control"),
    "Rhythm Control": (
        "variable_rhythm",
        "rhythm_changes",
        "finger_control",
    ),
    "Weird Aim": ("tech", "aim_control", "unusual_spacing"),
}


def _apply_relevance_defaults(cfg: PresetConfig) -> PresetConfig:
    """Attach a preset's own target tags after its builder has run.

    Keeping this outside the individual builders makes the exceptions
    explicit: Mixed Aim and All-rounder have no key in the mapping and
    therefore no gate.
    """

    cfg.ranking.relevance_targets = list(_RELEVANCE_TARGETS.get(cfg.name, ()))
    return cfg


# Curated built-ins: one clear profile per training goal.  The long tail of
# overlapping experiments from earlier versions stays available as legacy
# rows, but is no longer offered as a ready-made preset.
_BUILDERS = (
    _mixed_aim,
    _discovery,
    _allrounder,
    _aim_farm,
    _speed,
    _streams_stamina,
    _bursts,
    _finger_control,
    _reading,
    _accuracy,
    _tech,
    _flashlight,
)

_LEGACY_BUILDERS = (
    _any_taste,
    _streams,
    _stamina,
    _weird_aim,
    _wide_aim,
    _flow_aim,
    _aim_control,
    _aim_alt,
    _aim_bursts,
    _speed_aim,
    _speed_stamina,
    _low_ar_reading,
    _high_ar_reading,
    _precision,
    _reading_aim,
    _reading_streams,
    _jump_streams,
    _slider_tech,
    _rhythm_control,
    _custom,
    _legacy_aim_farm_pp_config,
)

BUILTIN_PRESETS_VERSION = 10
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
    # Fields introduced by v10 are absent from every old row. Strip them so
    # an old untouched definition keeps a stable fingerprint after parsing.
    data.get("ranking", {}).pop("farm_tolerance_seconds", None)
    data.get("discovery", {}).pop("title_queries", None)
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


def builtin_presets() -> list[PresetConfig]:
    """Fresh copies of the curated built-in presets."""
    return [
        copy.deepcopy(_apply_relevance_defaults(builder()))
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
    """Curated built-ins, user presets and the currently active legacy row.

    Hiding a legacy built-in never deletes or rewrites it.  Keeping the active
    one visible lets an existing installation continue from exactly where it
    was, while new users see only the curated catalog.
    """

    return [
        preset
        for preset in storage.list_presets()
        if not is_legacy_builtin(preset) or preset.id == include_id
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
        if _matches_legacy(preset.config, candidate):
            storage.update_preset(preset.id, modern)
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

    for name, legacy_factory in _LEGACY_SKILL_PROFILES:
        existing_profile = storage.get_preset_by_name(name)
        if (
            existing_profile is None
            or not existing_profile.is_builtin
            or not _matches_legacy(existing_profile.config, legacy_factory())
        ):
            continue
        upgraded_profile = builtin_by_name(name)
        if upgraded_profile is not None:
            storage.update_preset(existing_profile.id, upgraded_profile)

    if version < 10:
        _upgrade_v10_definitions(storage)

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
