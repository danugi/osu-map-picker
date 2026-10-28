"""Streamlit UI for the Personal osu! Map Picker.

The app is deliberately thin: discovery, filtering, ranking and persistence
live in testable modules. The UI never invents beatmapsets, never shows OAuth
secrets, and never calls the osu! API just because Streamlit re-ran the script.
A network search happens only after the explicit "Искать карты" click.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import random
import secrets
import tempfile
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st

import analysis_worker
import appearance
import catalog
import config
import library
import library_preset
import evidence
import gameplay
import pattern_filters
import presets
import randomizer
import ui_theme
from discovery import DiscoveryEngine
from features import (
    TRAIT_DESCRIPTIONS,
    TRAIT_LABELS,
    TRAITS,
)
from feedback import (
    VERDICT_HELP,
    VERDICT_LABELS,
    VERDICT_ORDER,
    build_taste_profile,
    verdict_summary,
)
from filters import FilterConfigError, active_filter_summary, validate_filters
from models import (
    DURATION_FIELDS,
    GENRES,
    MODES,
    STATUSES,
    DiscoveryResult,
    FactorConfig,
    FactorScore,
    PresetConfig,
    ScoredCandidate,
)
from osu_api import OsuApiClient, OsuApiError
from presets import PresetError
from ranking import FACTOR_LABELS, RankingConfigError, explain, validate_ranking
from relevance import (
    BELOW,
    FAILED,
    MATCHED,
    MATCHED_LIMITED,
    NO_DATA,
    NOT_ANALYZED,
    PARTIAL,
    SOURCE_UNAVAILABLE,
)
from skillsets import (
    CONFIDENCE_LABELS,
    SKILLS,
    SKILL_IDS,
    SUPPORT_LABELS,
    skill_fit,
    skill_label,
)
from storage import Storage, StorageError, UserDataImportError

logger = logging.getLogger("osu_picker.app")

APP_TITLE = "Personal osu! Map Picker"
CFG_EDITOR_PREFIX = "cfgskill"
TARGET_STATUS_LABELS = {
    MATCHED: "подтверждено",
    MATCHED_LIMITED: "выше порога (ограниченная поддержка)",
    BELOW: "ниже порога",
    NO_DATA: "недостаточно данных",
    NOT_ANALYZED: "не анализировалось",
    SOURCE_UNAVAILABLE: "источник недоступен",
    PARTIAL: "частичный анализ",
    FAILED: "анализ не удался",
}
PATTERN_LABELS = dict(pattern_filters.PATTERN_RU)
ANALYSIS_SOURCE_LABELS = {
    "download": "скачано",
    "cache": "локальный кэш",
    "db": "SQLite",
    "local": "локальный файл",
}
QUEUE_STATUS_LABELS = {
    "pending": "в очереди",
    "processing": "в работе",
    "done": "готово",
    "failed": "ошибка",
    "unavailable": "источник недоступен",
}
NAV_ITEMS = (
    "search",
    "library",
    "analysis",
    "favorites",
    "history",
    "settings",
)
NAV_LABELS = {
    "search": "Обзор",
    "library": "Библиотека",
    "analysis": "Анализ",
    "favorites": "Избранное",
    "history": "История",
    "settings": "Настройки",
}
NAV_GROUPS = (("Приложение", NAV_ITEMS),)
SETTINGS_SECTIONS = (
    "presets",
    "skillsets",
    "filters",
    "ranking",
    "feedback",
    "appearance",
    "diagnostics",
)
SETTINGS_LABELS = {
    "presets": "📦 Пресеты",
    "skillsets": "🎯 Скиллсеты",
    "filters": "🧰 Фильтры",
    "ranking": "⚖️ Ранжирование",
    "feedback": "⭐ Оценки",
    "appearance": "🎨 Оформление",
    "diagnostics": "🩺 Диагностика",
}
NAV_ALIASES = {
    "🔎 Поиск / Discovery": "search",
    "Библиотека": "library",
    "🎯 Навыки": "settings",
    "🧰 Фильтры": "settings",
    "⚖️ Ранжирование": "settings",
    "📦 Пресеты": "settings",
    "🎧 Вкус": "settings",
    "🕘 История и избранное": "history",
    "🎨 Оформление": "settings",
    "🩺 Диагностика": "settings",
    "presets": "settings",
    "skillsets": "settings",
    "filters": "settings",
    "ranking": "settings",
    "appearance": "settings",
    "diagnostics": "settings",
}
NAV_SETTINGS_SECTION = {
    "presets": "presets",
    "skillsets": "skillsets",
    "filters": "filters",
    "ranking": "ranking",
    "appearance": "appearance",
    "diagnostics": "diagnostics",
    "settings": "feedback",
    "📦 Пресеты": "presets",
    "🎯 Навыки": "skillsets",
    "🧰 Фильтры": "filters",
    "⚖️ Ранжирование": "ranking",
    "🎨 Оформление": "appearance",
    "🩺 Диагностика": "diagnostics",
    "🎧 Вкус": "feedback",
}
MODE_LABELS = {
    "osu": "osu!standard",
    "taiko": "osu!taiko",
    "catch": "osu!catch",
    "mania": "osu!mania",
}
STATUS_RU = {
    "ranked": "Ranked",
    "loved": "Loved",
    "qualified": "Qualified",
    "pending": "Pending",
    "wip": "WIP",
    "graveyard": "Graveyard",
}
STAR_MODE_LABELS = {
    "strict": "Строго: вне диапазона — не показывать",
    "soft": "Мягко: небольшой допуск, карта помечается",
    "off": "Не ограничивать по звёздам",
}
DURATION_LABELS = {
    "total_length": "Полная длина (total_length)",
    "hit_length": "Игровая длина (hit_length)",
}
TRAIT_HELP = {
    "aim": "Доля aim-сложности из официальных difficulty attributes.",
    "speed": "Доля speed-сложности из официальных difficulty attributes.",
    "control": "Прокси: aim-доля, взвешенная звёздностью. Не геометрия.",
    "precision": "Прокси по CS: меньшие круги — выше точность.",
    "density": "Объектов в секунду по счётчикам карты.",
    "bursts": "Прокси из плотности и speed-доли; не анализ паттерна.",
}


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def load_storage(path: str) -> Storage:
    store = Storage(Path(path))
    presets.ensure_builtin_presets(store)
    return store


def _gameplay_cache_dir(store: Storage) -> Path:
    return Path(store.path).parent / "gameplay_cache"


def _render_appearance(store: Storage) -> appearance.AppearanceSettings:
    settings = appearance.load_appearance(store)
    background = appearance.background_data_uri(store, settings)
    st.markdown(
        ui_theme.build_theme_css(settings, background), unsafe_allow_html=True
    )
    return settings


def _editor_epoch() -> int:
    return int(st.session_state.get("_editor_epoch", 0))


def _ck(name: str) -> str:
    """Version a config-editor widget key.

    Streamlit can keep a widget's previous frontend value when a session key
    is deleted. A new generation suffix makes the next preset start with fresh
    widgets initialized from its own saved configuration instead of receiving
    stale values from the previous profile.
    """
    return f"{name}__e{_editor_epoch()}"


def _bump_editor_epoch() -> None:
    st.session_state["_editor_epoch"] = _editor_epoch() + 1


def _clear_editor_state() -> None:
    prefixes = (
        "flt_",
        "rnk_",
        "dsc_",
        "trait_",
        "qflt_",
        "cfgskill_",
        "qskill_",
        "rnk_skill_",
        "skill_",
        "status_new_",
        "preset_new_",
        "preset_create_",
        "preset_delete_",
    )
    for key in list(st.session_state.keys()):
        if key.startswith(prefixes):
            del st.session_state[key]
    st.session_state.pop("preset_base", None)


def _clear_appearance_state() -> None:
    for key in list(st.session_state.keys()):
        if key.startswith("appearance_"):
            del st.session_state[key]


def _appearance_widget_key(name: str) -> str:
    """Give appearance widgets a new identity after apply/remove/reset.

    Streamlit may keep the previous frontend value for a widget when its
    session key is deleted after an action. A version suffix forces a fresh
    widget initialized from the saved settings, so the controls and the
    applied theme cannot disagree.
    """
    version = int(st.session_state.get("_appearance_editor_version", 0))
    suffix = "" if version == 0 else f"__v{version}"
    return f"appearance_{name}{suffix}"


def _bump_appearance_editor() -> None:
    st.session_state["_appearance_editor_version"] = (
        int(st.session_state.get("_appearance_editor_version", 0)) + 1
    )


def _active_config(store: Storage) -> PresetConfig:
    """Return the one configuration used by every editor and by the engine."""
    cfg = st.session_state.get("active_config")
    if isinstance(cfg, PresetConfig):
        return cfg
    preset_id = int(st.session_state["active_preset_id"])
    cfg = _load_active(store, preset_id)
    st.session_state["active_config"] = cfg
    return cfg


def _active_preset_name(store: Storage) -> str:
    preset = store.get_preset(int(st.session_state["active_preset_id"]))
    return preset.name if preset is not None else "—"


def _config_is_dirty(store: Storage, cfg: PresetConfig) -> bool:
    preset = store.get_preset(int(st.session_state["active_preset_id"]))
    return preset is not None and preset.config.to_dict() != cfg.to_dict()


def _render_dirty_bar(store: Storage, cfg: PresetConfig) -> None:
    if not _config_is_dirty(store, cfg):
        return
    st.warning(
        "Настройки изменены, но ещё не записаны в активный пресет. Поиск "
        "использует эти значения; переключение пресета без сохранения "
        "потеряет изменения."
    )
    cols = st.columns([1.0, 1.2, 3.0])
    if cols[0].button(
        "Сохранить изменения",
        key="status_save_active",
        type="primary",
        use_container_width=True,
    ):
        if _save_active(store):
            st.rerun()
    if cols[1].button(
        "Вернуть настройки пресета",
        key="status_discard_active",
        use_container_width=True,
    ):
        _discard_active(store)


def _clear_result_display() -> None:
    for key in (
        "result",
        "result_config",
        "result_visible",
        "result_shuffle_seed",
        "result_pick_id",
        "result_display_order",
        "result_sort_mode",
        "result_new_ids",
        "result_only_new",
        "result_expand_notice",
        "preview_beatmap_id",
        "random_meta",
    ):
        st.session_state.pop(key, None)


def _clear_random_state() -> None:
    st.session_state["random_round"] = 0
    st.session_state["random_last_ids"] = []
    st.session_state["random_seen_ids"] = []
    st.session_state.pop("random_scope_key", None)
    st.session_state["random_meta"] = None
    st.session_state["random_notice"] = None


def _random_scope_key(cfg: PresetConfig) -> str:
    """Identify the random session so a preset/filter change starts fresh."""

    return json.dumps(
        {
            "preset": cfg.name,
            "filters": cfg.filters.to_dict(),
            # A skillset/weights/relevance/diversity change changes the scores
            # and the sampler, so the no-repeat session must start fresh too.
            "ranking": cfg.ranking.to_dict(),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def _clear_result_state() -> None:
    _clear_result_display()
    _clear_random_state()


def _apply_preset(store: Storage, preset_id: int, *, keep_result: bool = False) -> None:
    """Load a whole preset into the session before editors are rendered."""
    new_config = _load_active(store, preset_id)
    st.session_state["active_preset_id"] = preset_id
    st.session_state["active_config"] = new_config
    st.session_state.pop("_pending_preset_id", None)
    if not keep_result:
        _clear_result_state()
    st.session_state["_reset_editors"] = True
    store.set_preference("selected_preset", str(preset_id))


def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def _parse_ids(text: str) -> list[int]:
    result: list[int] = []
    for part in _split_csv(text):
        value = int(part)
        if value <= 0:
            raise ValueError(f"ID должен быть положительным: {part}")
        result.append(value)
    return list(dict.fromkeys(result))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _choose_preset(store: Storage) -> int:
    presets_list = store.list_presets()
    if not presets_list:
        presets.ensure_builtin_presets(store)
        presets_list = store.list_presets()
    preference = store.get_preference("selected_preset")
    if preference:
        try:
            pid = int(preference)
        except ValueError:
            pid = -1
        if any(item.id == pid for item in presets_list):
            return pid
    for item in presets_list:
        if item.name == "Mixed Aim":
            return item.id
    return presets_list[0].id


def _load_active(store: Storage, preset_id: int) -> PresetConfig:
    item = store.get_preset(preset_id)
    if item is None:
        raise StorageError("Пресет не найден")
    return PresetConfig.from_dict(item.config.to_dict())


def _activate(store: Storage, preset_id: int) -> None:
    """Switch preset from a button (runs before widgets on the next rerun)."""
    _apply_preset(store, preset_id)
    st.rerun()


def _flash(message: str) -> None:
    st.session_state["_flash"] = message


def _save_active(store: Storage) -> bool:
    cfg: PresetConfig = st.session_state["active_config"]
    pid = st.session_state["active_preset_id"]
    try:
        presets.validate_preset_config(cfg)
        saved = store.update_preset(pid, cfg)
    except (PresetError, StorageError, RankingConfigError, FilterConfigError) as exc:
        st.session_state["_save_error"] = str(exc)
        return False
    st.session_state["active_config"] = PresetConfig.from_dict(saved.config.to_dict())
    _flash("Изменения сохранены в пресете.")
    return True


def _discard_active(store: Storage) -> None:
    _apply_preset(store, int(st.session_state["active_preset_id"]))
    st.rerun()


def _get_api(api_config: config.ApiConfig) -> OsuApiClient:
    key = (
        api_config.client_id,
        hashlib.sha256(api_config.client_secret.encode("utf-8")).hexdigest(),
        api_config.timeout,
        api_config.min_interval,
        api_config.max_retries,
    )
    existing = st.session_state.get("_api_client")
    if existing is not None and st.session_state.get("_api_key") == key:
        return existing
    if isinstance(existing, OsuApiClient):
        existing.close()
    client = OsuApiClient(api_config)
    st.session_state["_api_client"] = client
    st.session_state["_api_key"] = key
    return client


# ---------------------------------------------------------------------------
# Sidebar and shared header
# ---------------------------------------------------------------------------


def _normalise_nav() -> str:
    raw = str(st.session_state.get("nav", NAV_ITEMS[0]))
    if raw in NAV_ITEMS:
        page = raw
    else:
        page = NAV_ALIASES.get(raw, NAV_ITEMS[0])
        section = NAV_SETTINGS_SECTION.get(raw)
        if section is not None:
            st.session_state["settings_section"] = section
    st.session_state["nav"] = page
    return page


def _sidebar(store: Storage) -> str:
    st.sidebar.markdown(
        "<div class='rail-heading'>Пресет</div>"
        "<div class='rail-caption'>Фильтры, навыки и discovery загружаются "
        "одним профилем.</div>",
        unsafe_allow_html=True,
    )

    all_presets = store.list_presets()
    if not all_presets:
        st.sidebar.error("Нет пресетов. Пересоздайте встроенные на странице «Пресеты».")
        return NAV_ITEMS[0]

    active_id = st.session_state["active_preset_id"]
    if not any(item.id == active_id for item in all_presets):
        active_id = _choose_preset(store)
        _apply_preset(store, active_id)
    catalog_presets = presets.visible_presets(store, include_id=active_id)
    if not catalog_presets:
        catalog_presets = all_presets

    pending_id = st.session_state.get("_pending_preset_id")
    if pending_id is not None:
        # The selector must keep showing the preset whose settings are active
        # until the user explicitly confirms the switch.
        st.session_state["preset_selector"] = active_id
    elif st.session_state.get("preset_selector") != active_id:
        st.session_state["preset_selector"] = active_id

    names = {item.id: item.name for item in all_presets}
    for item in catalog_presets:
        item_name = item.name
        if presets.is_legacy_builtin(item):
            names[item.id] = f"{item_name} · legacy"

    def on_preset_change() -> None:
        pid = int(st.session_state["preset_selector"])
        if pid == active_id:
            return
        try:
            cfg = _active_config(store)
        except StorageError as exc:
            st.session_state["_save_error"] = str(exc)
            return
        if _config_is_dirty(store, cfg):
            st.session_state["_pending_preset_id"] = pid
            st.session_state["preset_selector"] = active_id
            return
        try:
            _apply_preset(store, pid)
        except StorageError as exc:
            st.session_state["_save_error"] = str(exc)
            st.session_state["preset_selector"] = active_id

    st.sidebar.selectbox(
        "Активный пресет",
        options=[item.id for item in catalog_presets],
        format_func=lambda pid: names.get(pid, str(pid)),
        key="preset_selector",
        on_change=on_preset_change,
        help="Каталог разделён на Explore, Farm, Train и Custom. Пресет "
        "загружает все настройки целиком: фильтры, навыки, веса, ранжирование "
        "и discovery.",
    )
    active_item = next(
        (item for item in all_presets if item.id == active_id), None
    )
    if active_item is not None:
        group = catalog.preset_group(
            active_item.name, is_builtin=active_item.is_builtin
        )
        role = "пользовательский" if not active_item.is_builtin else (
            "legacy" if presets.is_legacy_builtin(active_item) else "встроенный"
        )
        st.sidebar.caption(f"{group.icon} {group.label} · {role}")
    st.sidebar.caption(
        "Ручные изменения сохраняются отдельно от пресета."
    )

    pending_id = st.session_state.get("_pending_preset_id")
    if pending_id is not None:
        pending_name = names.get(int(pending_id), str(pending_id))
        st.sidebar.warning(
            f"В «{_active_preset_name(store)}» есть несохранённые изменения. "
            f"Переключиться на «{pending_name}»?"
        )
        pending_cols = st.sidebar.columns(2)
        if pending_cols[0].button(
            "Сохранить и перейти", key="pending_save_switch"
        ):
            if _save_active(store):
                _apply_preset(store, int(pending_id))
                st.session_state.pop("_pending_preset_id", None)
                st.rerun()
        if pending_cols[1].button(
            "Перейти без сохранения", key="pending_discard_switch"
        ):
            _apply_preset(store, int(pending_id))
            st.session_state.pop("_pending_preset_id", None)
            st.rerun()
        if st.sidebar.button("Остаться в текущем", key="pending_stay"):
            st.session_state.pop("_pending_preset_id", None)
            st.rerun()

    if st.session_state.get("_save_error"):
        st.sidebar.error(st.session_state.pop("_save_error"))

    st.sidebar.divider()
    page = _normalise_nav()
    try:
        worker = analysis_worker.worker_status(store)
    except Exception:  # noqa: BLE001 - sidebar must never block browsing
        worker = {}
    queue = worker.get("queue", {})
    worker_state = (
        "пауза"
        if worker.get("paused")
        else ("работает" if worker.get("thread_alive") else "ожидает")
    )
    st.sidebar.caption(
        f"Анализ: {worker.get('measured', 0)} + "
        f"{worker.get('partial', 0)} част. из "
        f"{worker.get('beatmaps', 0)} сложностей · "
        f"очередь {queue.get('pending', 0) + queue.get('processing', 0)} · "
        f"{worker_state}"
    )
    try:
        api_config = config.load_api_config()
        configured = api_config.configured
    except ValueError:
        configured = False
    st.sidebar.caption(
        "API: " + ("настроен" if configured else "не настроен (.env)")
    )
    st.sidebar.caption(f"БД: {store.path.name}")
    return page


def _site_header(store: Storage) -> str:
    page = _normalise_nav()
    try:
        counts = library.counts(store)
    except StorageError:
        counts = {
            "total": 0,
            "analyzed": 0,
            "partial": 0,
            "pending": 0,
            "failed": 0,
            "unavailable": 0,
        }
    try:
        configured = bool(config.load_api_config().configured)
    except ValueError:
        configured = False
    analyzed = counts.get("analyzed", 0)
    partial = counts.get("partial", 0)
    total = counts.get("total", 0)
    mode_text = "API онлайн" if configured else "только локально"
    total_text = f"{total:,}".replace(",", " ")
    analyzed_text = f"{analyzed + partial:,}".replace(",", " ")
    st.markdown(
        "<div class='site-header'>"
        "<div class='site-brand'>"
        "<span class='site-mark'>osu!</span>"
        "<span class='site-brand-copy'>"
        "<span class='site-brand-kicker'>beatmap discovery</span>"
        "<span class='site-brand-name'>Map Picker</span>"
        "</span></div>"
        "<div class='site-header-meta'>"
        f"<span class='site-stat'><strong>{total_text}</strong> "
        f"{_ru_plural(total, 'сложность', 'сложности', 'сложностей')}</span>"
        f"<span class='site-stat'><strong>{analyzed_text}</strong> "
        "с анализом</span>"
        f"<span class='site-pill {'online' if configured else 'local'}'>"
        f"{mode_text}</span>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    with st.container(key="site_nav"):
        columns = st.columns([1.15, 1.15, 1.0, 1.0, 1.0, 1.0])
        for index, page_id in enumerate(NAV_ITEMS):
            if columns[index].button(
                NAV_LABELS[page_id],
                key=f"nav_{page_id}",
                type="primary" if page == page_id else "secondary",
                use_container_width=True,
            ):
                st.session_state["nav"] = page_id
                st.rerun()
    return page


def _page_intro(kicker: str, title: str, subtitle: str) -> None:
    st.markdown(
        f"<div class='page-kicker'>{html.escape(kicker)}</div>",
        unsafe_allow_html=True,
    )
    st.header(title)
    st.caption(subtitle)


# ---------------------------------------------------------------------------
# Filters editor
# ---------------------------------------------------------------------------


def _range_inputs(
    label: str,
    low: float | None,
    high: float | None,
    key: str,
    *,
    default_low: float,
    default_high: float,
    minimum: float,
    maximum: float,
    step: float,
    help_text: str = "",
) -> tuple[float | None, float | None]:
    enabled = st.checkbox(
        f"Ограничить: {label}",
        value=low is not None or high is not None,
        key=f"{key}_on",
        help=help_text,
    )
    if not enabled:
        return None, None
    cols = st.columns(2)
    lo = cols[0].number_input(
        f"{label}: минимум",
        min_value=minimum,
        max_value=maximum,
        value=float(low if low is not None else default_low),
        step=step,
        key=f"{key}_low",
    )
    hi = cols[1].number_input(
        f"{label}: максимум",
        min_value=minimum,
        max_value=maximum,
        value=float(high if high is not None else default_high),
        step=step,
        key=f"{key}_high",
    )
    return float(lo), float(hi)


PINNED_SKILLS_KEY = "pinned_skills_v1"


def _pinned_skills(store: Storage | None) -> list[str]:
    if store is None:
        return []
    try:
        raw = json.loads(store.get_preference(PINNED_SKILLS_KEY) or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item in SKILLS]


def _save_pinned_skills(store: Storage | None, values: list[str]) -> None:
    if store is None:
        return
    store.set_preference(
        PINNED_SKILLS_KEY,
        json.dumps(list(dict.fromkeys(values)), ensure_ascii=False),
    )


def _skill_row(
    skill_id: str,
    *,
    prefix: str,
    selected: set[str],
    pinned: set[str],
    store: Storage | None,
    detailed: bool,
) -> None:
    definition = SKILLS[skill_id]
    category = catalog.category_for_skill(skill_id)
    subcategory = catalog.subcategory_for_skill(skill_id)
    cols = st.columns([0.08, 0.76, 0.16])
    checked = cols[0].checkbox(
        f"Выбрать {definition.name}",
        value=skill_id in selected,
        key=_ck(f"{prefix}_pick_{skill_id}"),
        label_visibility="collapsed",
    )
    if not checked:
        st.session_state.pop(_ck(f"{prefix}_w_{skill_id}"), None)
    with cols[1]:
        support = SUPPORT_LABELS.get(definition.support, definition.support)
        st.markdown(f"**{definition.name}** · <span class='osu-config-line'>{support}</span>", unsafe_allow_html=True)
        if detailed:
            st.caption(definition.description)
            if definition.limitations:
                st.caption("Ограничения: " + " ".join(definition.limitations))
        elif subcategory is not None:
            st.caption(
                f"{category.icon} {category.label} / {subcategory.label} · "
                f"{support}"
            )
    with cols[2]:
        is_pinned = skill_id in pinned
        if st.button(
            "★" if is_pinned else "☆",
            key=_ck(f"{prefix}_pin_{skill_id}"),
            help="Убрать из закреплённых" if is_pinned else "Закрепить навык",
            use_container_width=True,
        ):
            new_pinned = [item for item in pinned if item != skill_id]
            if not is_pinned:
                new_pinned.append(skill_id)
            _save_pinned_skills(store, new_pinned)
            st.rerun()


def _remove_skills(prefix: str, ids: tuple[str, ...]) -> None:
    for skill_id in ids:
        st.session_state.pop(_ck(f"{prefix}_pick_{skill_id}"), None)
        st.session_state.pop(_ck(f"{prefix}_w_{skill_id}"), None)
    st.rerun()


def _skillset_editor(
    cfg: PresetConfig,
    prefix: str,
    store: Storage | None = None,
    *,
    detailed: bool = False,
) -> None:
    """Hierarchical multi-skillset selector shared by every page.

    The hierarchy is presentation metadata.  It reads and writes the same
    ``cfg.ranking.skill_weights`` dictionary as before, so switching a preset
    still restores all skills and weights and manual edits still mark the
    configuration dirty.
    """

    ranking = cfg.ranking
    current = {
        key: float(value)
        for key, value in ranking.skill_weights.items()
        if key in SKILLS and float(value) > 0
    }
    pinned = _pinned_skills(store)
    pinned_set = set(pinned)
    selected_set = set(current)

    st.caption(
        "Скиллсеты влияют только на ранжирование подходящих карт. Они не "
        "ослабляют и не заменяют жёсткие фильтры сложности и длительности."
    )
    if selected_set:
        chips = []
        for skill_id in SKILL_IDS:
            if skill_id not in selected_set:
                continue
            definition = SKILLS[skill_id]
            chips.append(
                "<span class='osu-chip accent'>"
                + html.escape(definition.name)
                + "</span>"
            )
        st.markdown(
            f"**Выбрано навыков: {len(selected_set)}** "
            "— они остаются видимыми даже при свёрнутых категориях.",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='osu-hero-meta'>" + "".join(chips) + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.info(
            "Навыки не выбраны: фактор соответствия навыкам не участвует. "
            "Фильтры, вкус и остальные факторы продолжают работать."
        )

    if pinned:
        with st.expander(
            f"★ Закреплённые навыки · {len(pinned)}", expanded=False
        ):
            st.caption(
                "Закрепление хранится отдельно от пресета и не меняет веса."
            )
            pin_cols = st.columns(min(3, max(1, len(pinned))))
            for index, skill_id in enumerate(pinned):
                with pin_cols[index % len(pin_cols)]:
                    if st.button(
                        f"☆ {SKILLS[skill_id].name}",
                        key=_ck(f"{prefix}_open_pin_{skill_id}"),
                        help="Показать навык в поиске. Крестик в строке снимает закрепление.",
                    ):
                        st.session_state[_ck(f"{prefix}_search")] = SKILLS[skill_id].name
                        st.rerun()

    search_cols = st.columns([3.2, 1.0])
    query = search_cols[0].text_input(
        "Поиск навыка",
        key=_ck(f"{prefix}_search"),
        placeholder="Например: low AR, streams, stamina, finger…",
        help="Ищет по названию, описанию, категории и подкатегории.",
    )
    if search_cols[1].button(
        "Сбросить поиск",
        key=_ck(f"{prefix}_clear_search"),
        disabled=not bool(query),
        use_container_width=True,
    ):
        st.session_state[_ck(f"{prefix}_search")] = ""
        st.rerun()

    if query.strip():
        matches = catalog.search_skills(query)
        st.caption(
            f"Поиск по каталогу: {len(matches)} из {len(SKILL_IDS)} навыков. "
            "Категории временно скрыты, выбранные навыки не потеряны."
        )
        if not matches:
            st.warning(
                "Совпадений нет. Попробуйте название навыка, «AR», «stream» "
                "или категорию «Reading»."
            )
        for skill_id in matches:
            _skill_row(
                skill_id,
                prefix=prefix,
                selected=selected_set,
                pinned=pinned_set,
                store=store,
                detailed=detailed,
            )
    else:
        st.caption(
            "Категории раскрываются по нажатию. Внутри — подкатегории; "
            "счётчик показывает выбранные навыки, а не все доступные."
        )
        for category in catalog.SKILL_CATEGORIES:
            count = catalog.selected_count(category, current)
            selected_subcategories = sum(
                1
                for subcategory in category.subcategories
                if any(skill_id in current for skill_id in subcategory.skills)
            )
            with st.expander(
                f"{category.icon} {category.label} · навыков {count} · "
                f"подкатегорий {selected_subcategories}",
                expanded=count > 0,
            ):
                st.caption(category.description)
                tabs = st.tabs(
                    [subcategory.label for subcategory in category.subcategories]
                )
                for tab, subcategory in zip(tabs, category.subcategories):
                    with tab:
                        st.caption(subcategory.description)
                        for skill_id in subcategory.skills:
                            _skill_row(
                                skill_id,
                                prefix=prefix,
                                selected=selected_set,
                                pinned=pinned_set,
                                store=store,
                                detailed=detailed,
                            )
                        sub_count = sum(
                            1
                            for skill_id in subcategory.skills
                            if current.get(skill_id, 0) > 0
                        )
                        if st.button(
                            f"Снять навыки подкатегории · {sub_count}",
                            key=_ck(f"{prefix}_clear_sub_{category.id}_{subcategory.id}"),
                            disabled=sub_count == 0,
                        ):
                            _remove_skills(prefix, subcategory.skills)
                if st.button(
                    f"Снять всю категорию «{category.label}» · {count}",
                    key=_ck(f"{prefix}_clear_cat_{category.id}"),
                    disabled=count == 0,
                ):
                    _remove_skills(prefix, category.skill_ids)

    # Every rendered checkbox has now updated session_state. Build one shared
    # dictionary in the catalog order; zero weights stay as explicit toggles.
    new_weights: dict[str, float] = {}
    for category in catalog.SKILL_CATEGORIES:
        for skill_id in category.skill_ids:
            checked = bool(
                st.session_state.get(
                    _ck(f"{prefix}_pick_{skill_id}"), skill_id in current
                )
            )
            if checked:
                new_weights[skill_id] = float(current.get(skill_id, 1.0))
    for skill_id in SKILL_IDS:
        if skill_id not in new_weights:
            st.session_state.pop(_ck(f"{prefix}_w_{skill_id}"), None)
    ranking.skill_weights = new_weights

    if new_weights:
        with st.expander(
            f"Веса выбранных навыков · {len(new_weights)}", expanded=True
        ):
            st.caption(
                "Веса относительные: больший вес сильнее влияет на порядок, "
                "но не расширяет выдачу. 0 временно отключает навык, сохраняя "
                "его в выборе. Нулевые веса не проходят валидацию поиска."
            )
            ordered = [
                skill_id
                for category in catalog.SKILL_CATEGORIES
                for skill_id in category.skill_ids
                if skill_id in new_weights
            ]
            for start in range(0, len(ordered), 3):
                cols = st.columns(3)
                for col, skill_id in zip(cols, ordered[start : start + 3]):
                    item = SKILLS[skill_id]
                    with col:
                        new_weights[skill_id] = float(
                            st.number_input(
                                f"{item.name}: вес",
                                min_value=0.0,
                                max_value=5.0,
                                value=float(new_weights[skill_id]),
                                step=0.05,
                                key=_ck(f"{prefix}_w_{skill_id}"),
                                help=(
                                    f"{item.description} Поддержка: "
                                    f"{SUPPORT_LABELS.get(item.support, item.support)}."
                                ),
                            )
                        )
            ranking.skill_weights = new_weights
            positive = sum(value for value in new_weights.values() if value > 0)
            if positive > 0:
                shares = ", ".join(
                    f"{SKILLS[skill_id].name} {value / positive:.0%}"
                    for skill_id, value in new_weights.items()
                    if value > 0
                )
                st.caption(
                    "Нормализованные доли: " + shares + ". Сортировка по "
                    "соответствию использует эти же веса; строгие фильтры "
                    "остаются строгими."
                )
                mapping = ranking.factors.get("mapping_fit")
                if mapping is None or not mapping.enabled or mapping.weight <= 0:
                    st.warning(
                        "Навыки выбраны, но фактор «Соответствие навыкам» "
                        "выключен или имеет нулевой вес — сейчас они не влияют "
                        "на score."
                    )
                    if st.button(
                        "Включить фактор навыков",
                        key=_ck(f"{prefix}_enable_mapping"),
                        help="Не меняет жёсткие фильтры; включает только мягкий "
                        "фактор ранжирования.",
                    ):
                        if mapping is None:
                            mapping = ranking.factors.setdefault(
                                "mapping_fit",
                                FactorConfig(weight=1.3, enabled=True),
                            )
                        mapping.enabled = True
                        mapping.weight = max(float(mapping.weight), 1.3)
                        st.session_state["_reset_editors"] = True
                        st.rerun()
            else:
                st.warning(
                    "Все веса навыков равны нулю: навыки не влияют на рейтинг. "
                    "Поставьте хотя бы один вес > 0, иначе поиск отклонит "
                    "конфигурацию."
                )


def page_skillsets(store: Storage, cfg: PresetConfig) -> None:
    st.header("🎯 Скиллсеты")
    st.caption(
        "Один универсальный движок: каждый навык — типизированная функция от "
        "измеримых признаков, а не случайный тег. Иерархия только организует "
        "каталог; выбранные навыки и веса остаются одной конфигурацией."
    )
    _skillset_editor(cfg, CFG_EDITOR_PREFIX, store, detailed=True)
    st.info(
        "Смена навыков не меняет фильтры сложности, длины, статусов и ваши "
        "оценки. Это отдельная цель ранжирования. Пресет может явно задать "
        "фильтр (например, Low AR Reading), но выбор категории сам по себе "
        "не добавляет скрытых ограничений."
    )

    st.subheader("Что означает поддержка")
    st.markdown(
        "- **Измеряемая основа** — значения берутся из официальных "
        "difficulty attributes, счётчиков объектов, BPM, AR/OD/CS/HP или "
        "разброса сложностей сета.\n"
        "- **Прокси-оценка** — используется обоснованный производный признак, "
        "но это не прямое измерение категории.\n"
        "- **Ограниченная поддержка** — без тегов или локального анализа "
        "`.osu` часть навыка может быть неизвестна. Приложение не подставляет "
        "фиктивные значения."
    )
    st.subheader("Что означает уверенность")
    st.markdown(
        "- **Средняя** — есть официальные difficulty attributes и/или прямые "
        "метаданные карты; рисунок паттерна всё равно может быть неизвестен.\n"
        "- **Низкая** — оценка опирается на теги, производные прокси или "
        "неполные метаданные. Это подсказка, а не измерение.\n"
        "- **Нет данных** — для обязательных признаков навыка не хватает; "
        "процент не показывается. Уверенность относится к конкретной карте, "
        "а не к навыку вообще."
    )


def page_filters(cfg: PresetConfig) -> None:
    st.header("🧰 Фильтры")
    st.caption(
        "Жёсткие фильтры исключают карту. Мягкий режим звёзд только расширяет "
        "диапазон и помечает карту; остальные фильтры не ослабляются."
    )
    filters = cfg.filters

    top = st.columns([1.1, 1.9])
    with top[0]:
        mode = st.selectbox(
            "Режим",
            list(MODES),
            index=list(MODES).index(filters.modes[0]) if filters.modes and filters.modes[0] in MODES else 0,
            format_func=lambda value: MODE_LABELS.get(value, value),
            key=_ck("flt_mode"),
        )
        filters.modes = [mode]
        st.caption(
            "Признаки навыков и комфорт спроектированы для osu!standard; "
            "другие режимы не получают фиктивных skillset-оценок."
        )
    with top[1]:
        statuses = st.multiselect(
            "Статусы",
            options=list(STATUSES),
            default=[item for item in filters.statuses if item in STATUSES],
            format_func=lambda value: STATUS_RU.get(value, value),
            key=_ck("flt_statuses"),
            help="Пустой список недопустим: поиску нужен хотя бы один статус.",
        )
        filters.statuses = statuses

    st.subheader("Сложность и длительность")
    star_cols = st.columns([1.4, 1, 1, 1])
    star_mode = star_cols[0].selectbox(
        "Режим звёзд",
        options=["strict", "soft", "off"],
        index=["strict", "soft", "off"].index(filters.star_mode)
        if filters.star_mode in ("strict", "soft", "off")
        else 0,
        format_func=lambda value: STAR_MODE_LABELS[value],
        key=_ck("flt_star_mode"),
    )
    filters.star_mode = star_mode
    stars_min_input = star_cols[1].number_input(
        "★ минимум",
        min_value=0.0,
        max_value=12.0,
        value=float(filters.stars_min if filters.stars_min is not None else 4.0),
        step=0.1,
        key=_ck("flt_stars_min"),
        disabled=star_mode == "off",
    )
    stars_max_input = star_cols[2].number_input(
        "★ максимум",
        min_value=0.0,
        max_value=12.0,
        value=float(filters.stars_max if filters.stars_max is not None else 6.9),
        step=0.1,
        key=_ck("flt_stars_max"),
        disabled=star_mode == "off",
    )
    if star_mode == "off":
        filters.stars_min = None
        filters.stars_max = None
    else:
        filters.stars_min = float(stars_min_input)
        filters.stars_max = float(stars_max_input)
    filters.star_tolerance = star_cols[3].number_input(
        "Допуск soft, ★",
        min_value=0.0,
        max_value=2.0,
        value=float(filters.star_tolerance),
        step=0.05,
        key=_ck("flt_tolerance"),
        disabled=star_mode != "soft",
    )
    st.caption(
        "★ — difficulty_rating выбранной сложности. Если API не вернул ★, "
        "строгий режим исключает карту, а не угадывает значение."
    )

    duration_fields = st.columns([1.3, 1, 1])
    duration_field = duration_fields[0].selectbox(
        "Какая длина считается длительностью",
        options=list(DURATION_FIELDS),
        index=list(DURATION_FIELDS).index(filters.duration_field)
        if filters.duration_field in DURATION_FIELDS
        else 0,
        format_func=lambda value: DURATION_LABELS[value],
        key=_ck("flt_duration_field"),
    )
    filters.duration_field = duration_field
    use_duration = duration_fields[1].checkbox(
        "Ограничить длительность",
        value=filters.duration_min is not None or filters.duration_max is not None,
        key=_ck("flt_duration_on"),
    )
    if use_duration:
        filters.duration_min = duration_fields[2].number_input(
            "Минимум, сек",
            min_value=0,
            max_value=7200,
            value=int(filters.duration_min or 0),
            step=5,
            key=_ck("flt_duration_min"),
        )
    else:
        filters.duration_min = None
    if use_duration:
        filters.duration_max = st.number_input(
            "Максимум, сек",
            min_value=1,
            max_value=7200,
            value=int(filters.duration_max or 180),
            step=5,
            key=_ck("flt_duration_max"),
        )
    else:
        filters.duration_max = None

    with st.expander("BPM, AR, OD, CS, HP", expanded=False):
        st.caption(
            "Если API не вернул AR/OD/CS/HP, фильтр по этому полю не "
            "применяется вслепую; карта остаётся, а диагностика сообщает "
            "о неизвестном значении."
        )
        filters.bpm_min, filters.bpm_max = _range_inputs(
            "BPM", filters.bpm_min, filters.bpm_max, "flt_bpm",
            default_low=120, default_high=240, minimum=0, maximum=1000,
            step=1.0,
        )
        filters.ar_min, filters.ar_max = _range_inputs(
            "AR", filters.ar_min, filters.ar_max, "flt_ar",
            default_low=7, default_high=10, minimum=0, maximum=11,
            step=0.1,
        )
        filters.od_min, filters.od_max = _range_inputs(
            "OD", filters.od_min, filters.od_max, "flt_od",
            default_low=6, default_high=10, minimum=0, maximum=11,
            step=0.1,
        )
        filters.cs_min, filters.cs_max = _range_inputs(
            "CS", filters.cs_min, filters.cs_max, "flt_cs",
            default_low=3, default_high=6, minimum=0, maximum=11,
            step=0.1,
        )
        filters.hp_min, filters.hp_max = _range_inputs(
            "HP", filters.hp_min, filters.hp_max, "flt_hp",
            default_low=3, default_high=8, minimum=0, maximum=11,
            step=0.1,
        )

    with st.expander("Даты публикации", expanded=False):
        after_on = st.checkbox(
            "Не раньше даты",
            value=bool(filters.published_after),
            key=_ck("flt_after_on"),
        )
        if after_on:
            value = st.date_input(
                "Опубликована не раньше",
                value=_parse_date(filters.published_after) or date(2015, 1, 1),
                key=_ck("flt_after_date"),
            )
            filters.published_after = value.isoformat()
        else:
            filters.published_after = None
        before_on = st.checkbox(
            "Не позже даты",
            value=bool(filters.published_before),
            key=_ck("flt_before_on"),
        )
        if before_on:
            value = st.date_input(
                "Опубликована не позже",
                value=_parse_date(filters.published_before) or date.today(),
                key=_ck("flt_before_date"),
            )
            filters.published_before = value.isoformat()
        else:
            filters.published_before = None

    st.subheader("Текст, артисты, мапперы, жанры")
    filters.query = st.text_input(
        "Текстовый запрос",
        value=filters.query,
        key=_ck("flt_query"),
        placeholder="необязательно: название, артист, маппер или тег",
        help="Передаётся в API как q и дополнительно проверяется локально.",
    )
    artist_cols = st.columns(2)
    include_artists = artist_cols[0].text_input(
        "Включить артистов (через запятую)",
        value=", ".join(filters.include_artists),
        key=_ck("flt_include_artists"),
    )
    exclude_artists = artist_cols[1].text_input(
        "Исключить артистов",
        value=", ".join(filters.exclude_artists),
        key=_ck("flt_exclude_artists"),
    )
    filters.include_artists = _split_csv(include_artists)
    filters.exclude_artists = _split_csv(exclude_artists)

    mapper_cols = st.columns(2)
    include_mappers = mapper_cols[0].text_input(
        "Включить мапперов",
        value=", ".join(filters.include_mappers),
        key=_ck("flt_include_mappers"),
    )
    exclude_mappers = mapper_cols[1].text_input(
        "Исключить мапперов",
        value=", ".join(filters.exclude_mappers),
        key=_ck("flt_exclude_mappers"),
    )
    filters.include_mappers = _split_csv(include_mappers)
    filters.exclude_mappers = _split_csv(exclude_mappers)

    genre_options = sorted(GENRES)
    genre_cols = st.columns(2)
    filters.include_genres = genre_cols[0].multiselect(
        "Жанры: только эти",
        options=genre_options,
        default=[g for g in filters.include_genres if g in GENRES],
        format_func=lambda value: GENRES.get(value, str(value)),
        key=_ck("flt_include_genres"),
    )
    filters.exclude_genres = genre_cols[1].multiselect(
        "Жанры: исключить",
        options=genre_options,
        default=[g for g in filters.exclude_genres if g in GENRES],
        format_func=lambda value: GENRES.get(value, str(value)),
        key=_ck("flt_exclude_genres"),
    )

    st.subheader("Скрытие и исключения")
    flags = st.columns(3)
    filters.exclude_nsfw = flags[0].checkbox(
        "Скрывать NSFW", value=filters.exclude_nsfw, key=_ck("flt_nsfw")
    )
    filters.hide_seen = flags[1].checkbox(
        "Скрывать уже показанные", value=filters.hide_seen, key=_ck("flt_hide_seen")
    )
    filters.hide_played = flags[2].checkbox(
        "Скрывать «уже играл»", value=filters.hide_played, key=_ck("flt_hide_played")
    )
    flags2 = st.columns(2)
    filters.hide_favorites = flags2[0].checkbox(
        "Скрывать избранное", value=filters.hide_favorites, key=_ck("flt_hide_favorites")
    )
    filters.only_favorites = flags2[1].checkbox(
        "Только избранное", value=filters.only_favorites, key=_ck("flt_only_favorites")
    )
    raw_ids = st.text_input(
        "Исключить beatmapset ID (через запятую)",
        value=", ".join(str(value) for value in filters.exclude_beatmapset_ids),
        key=_ck("flt_exclude_ids"),
    )
    try:
        filters.exclude_beatmapset_ids = _parse_ids(raw_ids)
    except ValueError as exc:
        st.error(f"ID не принят: {exc}")

    try:
        validate_filters(filters)
    except FilterConfigError as exc:
        st.error(f"Проверьте фильтры: {exc}")
    else:
        st.success("Конфигурация фильтров корректна.")


# ---------------------------------------------------------------------------
# Ranking editor
# ---------------------------------------------------------------------------


def page_ranking(store: Storage, cfg: PresetConfig) -> None:
    st.header("⚖️ Ранжирование и разнообразие")
    st.caption(
        "Внутренний score 0–100 — это порядок внутри вашей выдачи, не "
        "вероятность. Веса относительные; объяснение видно у каждой карты."
    )
    ranking = cfg.ranking

    st.subheader("Навыки и их приоритет")
    _skillset_editor(cfg, CFG_EDITOR_PREFIX, store)
    st.caption(
        "Веса навыков нормализуются между собой. Фактор «Соответствие "
        "навыкам» использует общий движок; при отсутствии навыков он "
        "показывает понятное состояние «нет данных», а не подставляет 0.5."
    )

    st.subheader("Обязательное соответствие пресету")
    targets = tuple(ranking.relevance_targets)
    if targets:
        labels = ", ".join(
            SKILLS[target].name if target in SKILLS else target
            for target in targets
        )
        st.caption(
            "Карта попадёт в выдачу, только если **хотя бы один** из целевых "
            f"тегов выше порога: {labels}. Строгие фильтры сложности, "
            "длительности и статуса применяются раньше и не ослабляются."
        )
    else:
        st.caption(
            "У активного пресета нет обязательных целевых тегов. Гейт не "
            "применяется, поэтому Mixed Aim и All-rounder не наследуют "
            "ограничения специализированных профилей."
        )
    ranking.relevance_threshold = float(
        st.slider(
            "Порог соответствия целевому тегу",
            min_value=0.5,
            max_value=1.0,
            value=float(ranking.relevance_threshold),
            step=0.01,
            key=_ck("rnk_relevance_threshold"),
            disabled=not bool(targets),
            help="Сравнение строгое: ровно 50% не проходит. По умолчанию 55%. "
            "Порог не заменяет строгие фильтры и не повышает общий score.",
        )
    )

    st.subheader("Факторы")
    keys = list(ranking.factors)
    for start in range(0, len(keys), 3):
        cols = st.columns(3)
        for col, key in zip(cols, keys[start : start + 3]):
            factor = ranking.factors[key]
            with col:
                enabled = st.checkbox(
                    FACTOR_LABELS.get(key, key),
                    value=factor.enabled,
                    key=_ck(f"rnk_{key}_on"),
                    help=config.FACTOR_DESCRIPTIONS.get(key, ""),
                )
                weight = st.slider(
                    "Вес",
                    min_value=0.0,
                    max_value=5.0,
                    value=float(factor.weight),
                    step=0.05,
                    key=_ck(f"rnk_{key}_weight"),
                    disabled=not enabled,
                )
                ranking.factors[key] = FactorConfig(float(weight), enabled)

    st.subheader("Разнообразие")
    div_cols = st.columns(3)
    ranking.diversity = div_cols[0].slider(
        "Сила диверсификации",
        min_value=0.0,
        max_value=1.0,
        value=float(ranking.diversity),
        step=0.05,
        key=_ck("rnk_diversity"),
        help="0 — чистый score; 1 — сильнее раздвигать похожие карты.",
    )
    ranking.max_per_artist = int(
        div_cols[1].number_input(
            "Максимум на артиста",
            min_value=1,
            max_value=20,
            value=int(ranking.max_per_artist),
            step=1,
            key=_ck("rnk_max_artist"),
        )
    )
    ranking.max_per_mapper = int(
        div_cols[2].number_input(
            "Максимум на маппера",
            min_value=1,
            max_value=20,
            value=int(ranking.max_per_mapper),
            step=1,
            key=_ck("rnk_max_mapper"),
        )
    )
    st.caption(
        "Лимиты — мягкие: если карт мало, выдача ослабляет их и пишет об этом. "
        "Похожие карты не исчезают полностью, а получают штраф."
    )

    st.subheader("Комфорт и длина")
    comfort = st.columns(4)
    ranking.comfort_stars = comfort[0].number_input(
        "Комфортная ★",
        min_value=0.0,
        max_value=12.0,
        value=float(ranking.comfort_stars),
        step=0.1,
        key=_ck("rnk_comfort_stars"),
    )
    ranking.comfort_width = comfort[1].number_input(
        "Ширина комфорта, ★",
        min_value=0.05,
        max_value=5.0,
        value=float(ranking.comfort_width),
        step=0.05,
        key=_ck("rnk_comfort_width"),
    )
    ranking.ideal_length = int(
        comfort[2].number_input(
            "Идеальная длина, сек",
            min_value=10,
            max_value=3600,
            value=int(ranking.ideal_length),
            step=5,
            key=_ck("rnk_ideal_length"),
        )
    )
    ranking.length_tolerance = int(
        comfort[3].number_input(
            "Допуск длины, сек",
            min_value=1,
            max_value=3600,
            value=int(ranking.length_tolerance),
            step=5,
            key=_ck("rnk_length_tolerance"),
        )
    )
    if ranking.factors.get("farm_nm") and ranking.factors["farm_nm"].enabled:
        ranking.farm_target_seconds = int(
            st.number_input(
                "Цель NM-фарма, сек",
                min_value=10,
                max_value=600,
                value=int(ranking.farm_target_seconds),
                step=5,
                key=_ck("rnk_farm_target"),
                help="Мягкая цель фактора NM-фарма. Жёсткий максимум длины "
                "задаётся в фильтрах; это разные вещи.",
            )
        )

    st.subheader("Музыкальные предпочтения")
    ranking.preferred_genres = st.multiselect(
        "Желаемые жанры (не жёсткий фильтр)",
        options=sorted(GENRES),
        default=[g for g in ranking.preferred_genres if g in GENRES],
        format_func=lambda value: GENRES.get(value, str(value)),
        key=_ck("rnk_preferred_genres"),
        help="Пусто = любой жанр. Это мягкий сигнал для фактора «Музыка».",
    )

    with st.expander("Legacy aim-профиль (совместимость старых пресетов)", expanded=False):
        st.caption(
            "Это старый механизм Mixed Aim / Wide Aim и подобных пресетов. "
            "Новые пресеты используют универсальные навыки выше. Геометрия "
            "wide/flow/углов здесь не угадывается."
        )
        targets: dict[str, float] = {}
        for start in range(0, len(TRAITS), 3):
            cols = st.columns(3)
            for col, key in zip(cols, TRAITS[start : start + 3]):
                with col:
                    enabled = st.checkbox(
                        TRAIT_LABELS[key],
                        value=key in ranking.trait_targets,
                        key=_ck(f"trait_{key}_on"),
                        help=TRAIT_HELP.get(key, TRAIT_DESCRIPTIONS.get(key, "")),
                    )
                    value = st.slider(
                        "Цель, %",
                        min_value=0,
                        max_value=100,
                        value=int(round(ranking.trait_targets.get(key, 0.5) * 100)),
                        step=5,
                        key=_ck(f"trait_{key}_value"),
                        disabled=not enabled,
                    )
                    if enabled:
                        targets[key] = value / 100.0
        ranking.trait_targets = targets

        if st.button(
            "Отключить legacy aim-профиль (режим «любой стиль»)",
            key=_ck("rnk_disable_aim"),
            help="Очищает цели и выключает фактор соответствия. "
            "Универсальные навыки не затрагиваются.",
        ):
            cfg.ranking.trait_targets = {}
            if not cfg.ranking.skill_weights:
                cfg.ranking.factors["mapping_fit"].enabled = False
            st.session_state["_reset_editors"] = True
            st.rerun()

    try:
        validate_ranking(ranking)
    except RankingConfigError as exc:
        st.error(f"Проверьте ранжирование: {exc}")
    else:
        st.info(
            "Веса не обязаны суммироваться в 1: score нормируется по включённым "
            "факторам. Отключённый фактор не участвует."
        )


# ---------------------------------------------------------------------------
# Search / results
# ---------------------------------------------------------------------------


def _render_target_gate(scored: ScoredCandidate) -> None:
    """Show why this map passed the preset's mandatory target gate."""

    check = scored.candidate.relevance
    if check is None or not check.targets:
        st.caption(
            "Обязательные целевые теги не заданы: у этого пресета нет "
            "дополнительного гейта (например, Mixed Aim или All-rounder)."
        )
        return
    st.markdown("**Целевые теги пресета**")
    rows = []
    for target in check.targets:
        score = (
            f"{target.display_score}%"
            if target.display_score is not None
            else "недостаточно данных"
        )
        rows.append(
            {
                "Целевой тег": target.label,
                "Соответствие": score,
                "Порог": f">{check.threshold:.0%}",
                "Статус": TARGET_STATUS_LABELS.get(
                    target.status, target.status
                ),
                "Источник": target.source,
                "Уверенность": CONFIDENCE_LABELS.get(
                    target.confidence, target.confidence
                ),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")
    for target in check.targets:
        if target.evidence:
            st.caption(
                f"{target.label}: " + "; ".join(target.evidence[:4]) + "."
            )
        if target.missing:
            st.caption(
                f"{target.label}: нет данных по признакам "
                + ", ".join(target.missing[:5])
                + "."
            )
    st.caption(check.reason)


def _render_factor_table(scored: ScoredCandidate) -> None:
    rows = []
    for factor in scored.factors:
        contribution = getattr(factor, "contribution", 0.0)
        rows.append(
            {
                "Фактор": factor.label,
                "Значение": (
                    "нет данных"
                    if factor.value is None
                    else f"{round(factor.value, 3):g}"
                ),
                "Вес": factor.weight,
                "Вклад": (
                    "не участвует"
                    if factor.value is None
                    else f"{contribution:+.3f}"
                ),
                "Почему": factor.explanation,
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")
    known = [
        factor
        for factor in scored.factors
        if factor.value is not None
        and getattr(factor, "contribution", 0.0) != 0.0
    ]
    if known:
        strongest = max(known, key=lambda factor: factor.contribution)
        weakest = min(known, key=lambda factor: factor.contribution)
        st.caption(
            f"Наибольший вклад: {strongest.label} "
            f"({strongest.contribution:+.3f}); наименьший: {weakest.label} "
            f"({weakest.contribution:+.3f})."
        )
    st.caption(
        "Итог = сумма вкладов известных факторов; вклад = вес × значение / "
        "сумма весов известных факторов. Неизвестные факторы исключены из "
        "знаменателя и не заменены нулём. Итог ограничен диапазоном 0–100; "
        "это эвристический порядок, а не вероятность того, что карта "
        "понравится."
    )
    st.caption(
        f"Evidence coverage: {round(scored.coverage * 100)}% веса включённых "
        "факторов имеют значение. Низкое покрытие означает, что оценка "
        "опирается на меньшую часть заявленных целей."
    )


def _feedback_form(store: Storage, beatmapset_id: int) -> None:
    current = store.get_feedback(beatmapset_id)
    with st.form(f"feedback_form_{beatmapset_id}"):
        selected = st.multiselect(
            "Ваша оценка (можно несколько)",
            options=list(VERDICT_ORDER),
            default=list(current),
            format_func=lambda value: VERDICT_LABELS.get(value, value),
            key=f"feedback_select_{beatmapset_id}",
            help="Разные минусы разделены: музыка, маппинг, сложность, «уже играл».",
        )
        submitted = st.form_submit_button("Сохранить оценку")
    if submitted:
        try:
            store.set_feedback(beatmapset_id, selected)
            st.success("Оценка сохранена. Она повлияет на следующие поиски.")
        except StorageError as exc:
            st.error(f"Оценка не сохранена: {exc}")


def _card_skill_lines(
    scored: ScoredCandidate, weights: dict[str, float] | None = None
) -> list[str]:
    skills = scored.candidate.skillsets
    selected = [
        (key, weight)
        for key, weight in (weights or {}).items()
        if weight > 0 and key in SKILLS
    ]
    if selected:
        entries = [(key, skills.get(key)) for key, _ in selected]
    else:
        entries = [
            (item.skill_id, item)
            for item in sorted(
                (item for item in skills.values() if item.score is not None),
                key=lambda item: (-(item.score or 0.0), item.skill_id),
            )[:2]
        ]
    lines: list[str] = []
    for key, item in entries:
        name = SKILLS[key].name if key in SKILLS else key
        if item is None or item.score is None:
            status = (
                item.status
                if item is not None
                else scored.candidate.traits.status(key)
            )
            lines.append(f"{name}: {evidence.short_label(status)}")
            continue
        if item.status == evidence.NOT_DETECTED:
            lines.append(f"{name}: не найдено (анализ выполнялся)")
            continue
        suffix = evidence.short_label(item.status)
        lines.append(
            f"{name}: {item.display_score}%"
            + (f" ({suffix})" if suffix else "")
        )
    return lines


def _format_seconds(value: int | float | None) -> str:
    if value is None:
        return "?"
    seconds = max(0, int(value))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _ru_plural(value: int, one: str, few: str, many: str) -> str:
    number = abs(int(value))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def _match_factor(scored: ScoredCandidate) -> FactorScore | None:
    for factor in scored.factors:
        if factor.key == "mapping_fit":
            return factor
    return None


def _pattern_summary(scored: ScoredCandidate) -> str:
    candidate = scored.candidate
    sequence = candidate.sequence
    if sequence is not None:
        if sequence.labels:
            labels = [
                PATTERN_LABELS.get(label, label) for label in sequence.labels[:5]
            ]
            return "Паттерны .osu: " + ", ".join(labels) + "."
        return (
            "Паттерны .osu: проанализировано, выраженных паттернов по "
            "заданным критериям не найдено (это не отсутствие данных)."
        )
    analysis_evidence = candidate.analysis
    status = (
        analysis_evidence.status
        if analysis_evidence is not None
        else candidate.traits.analysis_status
    )
    return "Паттерны .osu: " + evidence.explanation(status)


def _render_card(
    store: Storage,
    scored: ScoredCandidate,
    position: int,
    skill_weights: dict[str, float] | None = None,
) -> None:
    beatmapset = scored.beatmapset
    beatmap = scored.beatmap
    url = config.BEATMAPSET_URL.format(id=beatmapset.id)
    bm_url = beatmap.url or url
    stars = beatmap.difficulty_rating
    stars_text = f"{stars:.2f}★" if stars is not None else "★ ?"
    bpm = beatmap.bpm if beatmap.bpm is not None else beatmapset.bpm
    bpm_text = f"{bpm:.0f}" if bpm is not None else "?"
    length = _format_seconds(beatmap.total_length or beatmap.hit_length)
    od = f"{beatmap.od:g}" if beatmap.od is not None else "?"
    ar = f"{beatmap.ar:g}" if beatmap.ar is not None else "?"
    status = STATUS_RU.get(beatmapset.status, beatmapset.status)
    genre = beatmapset.genre_name or "жанр ?"
    cover = html.escape(beatmapset.cover_url, quote=True)
    fallback = html.escape(
        (beatmapset.display_title or "osu!")[:2].upper(), quote=False
    )
    title = html.escape(beatmapset.display_title, quote=False)
    artist = html.escape(beatmapset.display_artist, quote=False)
    creator = html.escape(beatmapset.creator or "неизвестно", quote=False)
    version = html.escape(beatmap.version or "unknown", quote=False)
    safe_url = html.escape(url, quote=True)
    cover_html = (
        "<div class='osu-cover'>"
        + (f"<img src='{cover}' alt='cover' loading='lazy' "
           "onerror=\"this.style.display='none'\">" if cover else "")
        + f"<div class='osu-cover-fallback'>{fallback}</div>"
        + f"<span class='osu-cover-badge'>★ {html.escape(stars_text)}</span>"
        + f"<a class='osu-cover-link' href='{safe_url}' target='_blank' "
        "rel='noopener noreferrer' aria-label='Открыть beatmapset'></a>"
        + "</div>"
    )
    header_html = (
        f"<div class='osu-card-head'><div>"
        f"<div class='osu-card-title'><a href='{safe_url}' target='_blank' "
        f"rel='noopener noreferrer'>{title}</a></div>"
        f"<div class='osu-card-sub'>{artist} · маппер: {creator}</div>"
        f"<div class='osu-hero-meta'>"
        f"<span class='osu-chip'>{html.escape(status)}</span>"
        f"<span class='osu-chip muted'>{html.escape(genre)}</span>"
        f"<span class='osu-chip accent'>{html.escape(version)}</span>"
        f"</div></div>"
        f"<div class='osu-score'><div class='num'>{scored.display_score}</div>"
        f"<div class='lbl'>Рекомендация</div>"
        f"<div class='lbl'>покрытие {round(scored.coverage * 100)}%</div>"
        f"</div></div>"
        f"<div class='osu-card-stats'>"
        f"<div class='osu-stat'><div class='k'>Сложность</div><div class='v'>{html.escape(stars_text)}</div></div>"
        f"<div class='osu-stat'><div class='k'>BPM</div><div class='v'>{html.escape(bpm_text)}</div></div>"
        f"<div class='osu-stat'><div class='k'>Длина</div><div class='v'>{html.escape(length)}</div></div>"
        f"<div class='osu-stat'><div class='k'>AR / OD</div><div class='v'>{html.escape(ar)} / {html.escape(od)}</div></div>"
        f"</div>"
    )
    with st.container(border=True):
        st.markdown(cover_html, unsafe_allow_html=True)
        st.markdown(header_html, unsafe_allow_html=True)
        match = _match_factor(scored)
        match_text = (
            "недостаточно данных"
            if match is None or match.value is None
            else f"{round(match.value * 100)}%"
        )
        st.caption(
            f"Соответствие: {match_text} · покрытие доказательств: "
            f"{round(scored.coverage * 100)}% · рекомендация: "
            f"{scored.display_score}/100 (эвристический порядок)."
        )
        st.caption(_pattern_summary(scored))
        analysis_evidence = scored.candidate.analysis
        if (
            analysis_evidence is not None
            and analysis_evidence.status == evidence.PARTIAL
        ):
            st.caption(
                "Частичный анализ .osu: "
                + (analysis_evidence.scope or "покрыта только часть карты")
                + ". Найденное относится к этой части."
            )
        if not scored.candidate.within_tolerance:
            st.warning("Карта вне строгого диапазона звёзд: допущена мягким режимом.")
        unknown = scored.candidate.unknown_fields
        if unknown:
            st.caption(
                "API не вернул поля: " + ", ".join(unknown) + ". Они не угадывались."
            )
        skill_lines = _card_skill_lines(scored, skill_weights)
        if skill_lines:
            st.caption("Цели пресета: " + " · ".join(skill_lines))

        with st.expander("Почему эта карта здесь", expanded=False):
            st.caption(
                f"CS {beatmap.cs if beatmap.cs is not None else '?'} · "
                f"HP {beatmap.hp if beatmap.hp is not None else '?'} · "
                f"diff {version} · BPM {bpm_text} · длина {length}"
            )
            _render_target_gate(scored)
            traits = scored.candidate.traits
            if traits.notes:
                st.markdown("**Данные и ограничения**")
                for note in traits.notes:
                    st.caption("• " + note)
            st.markdown("**Факторы рекомендации**")
            for line in explain(scored):
                st.markdown(f"- {line}")
            _render_factor_table(scored)
            if (
                scored.diversified_score is not None
                and abs(scored.diversified_score - scored.score) > 0.005
            ):
                st.caption(
                    "Порядок «Рекомендация» учитывает разнообразие: базовая "
                    f"оценка {scored.display_score}/100, позиция со штрафом за "
                    f"сходство с уже показанными — "
                    f"{int(round(scored.diversified_score * 100))}/100. Это не "
                    "изменение строгих фильтров."
                )
            st.markdown("**Состояние анализа .osu**")
            analysis_evidence = scored.candidate.analysis
            if analysis_evidence is None:
                st.caption(
                    "Статус анализа неизвестен; sequence-признаки не "
                    "интерпретируются как отсутствие паттерна."
                )
            else:
                st.caption(
                    f"{evidence.label(analysis_evidence.status)}"
                    f" · источник: {analysis_evidence.source or '—'}"
                    f" · анализатор v{analysis_evidence.analyzer_version or '—'}"
                    + (
                        f" · {analysis_evidence.scope}"
                        if analysis_evidence.scope
                        else ""
                    )
                )
                st.caption(evidence.explanation(analysis_evidence.status))
                sequence = scored.candidate.sequence
                if sequence is not None:
                    if sequence.sections:
                        section_bits = []
                        for section in sequence.sections[:3]:
                            labels = ", ".join(
                                PATTERN_LABELS.get(label, label)
                                for label in section.labels
                            ) or "без выраженного паттерна"
                            section_bits.append(
                                f"{_format_seconds(section.start_ms / 1000)}–"
                                f"{_format_seconds(section.end_ms / 1000)}: "
                                f"{labels}"
                            )
                        st.caption("Секции: " + " · ".join(section_bits) + ".")
                    else:
                        st.caption(
                            "Секционное описание недоступно: карта слишком "
                            "короткая или объектов недостаточно."
                        )
            st.markdown("**Детали навыков**")
            if skill_weights:
                relevant = [
                    scored.candidate.skillsets[key]
                    for key, weight in skill_weights.items()
                    if weight > 0 and key in scored.candidate.skillsets
                ]
            else:
                relevant = [
                    item
                    for item in scored.candidate.skillsets.values()
                    if item.score is not None
                ]
                relevant.sort(
                    key=lambda item: (-(item.score or 0.0), item.skill_id)
                )
            if not relevant:
                st.caption(
                    "Для выбранных навыков не хватает измеримых признаков. "
                    "Значения не выдумывались."
                )
            for item in relevant[:6]:
                status = evidence.label(item.status)
                if item.score is None:
                    st.markdown(
                        f"- **{item.name}: {status}** · "
                        f"покрытие признаков: {item.coverage:.0%}"
                    )
                else:
                    st.markdown(
                        f"- **{item.name}: {item.display_score}%** · "
                        f"{status} · "
                        f"уверенность: {CONFIDENCE_LABELS.get(item.confidence, item.confidence)} · "
                        f"поддержка: {SUPPORT_LABELS.get(item.support, item.support)} · "
                        f"покрытие: {item.coverage:.0%}"
                    )
                if item.evidence:
                    st.caption("Признаки: " + "; ".join(item.evidence[:4]) + ".")
                if item.missing:
                    st.caption(
                        "Недоступные признаки: "
                        + ", ".join(item.missing[:6])
                        + "."
                    )
                if item.limitations:
                    st.caption("Ограничения: " + " ".join(item.limitations[:3]))

        actions = st.columns([1.1, 1.0, 1.0])
        favorite = store.is_favorite(beatmapset.id)
        with actions[0]:
            label = "★ Убрать из избранного" if favorite else "☆ В избранное"
            if st.button(label, key=f"favorite_{beatmapset.id}", use_container_width=True):
                try:
                    store.toggle_favorite(beatmapset.id)
                    st.rerun()
                except StorageError as exc:
                    st.error(f"Избранное не изменено: {exc}")
        with actions[1]:
            st.link_button(
                "Открыть difficulty",
                bm_url,
                use_container_width=True,
            )
        with actions[2]:
            if st.button(
                "▶ Превью",
                key=f"preview_{beatmap.id}",
                use_container_width=True,
                help="Показать реальное игровое поле из .osu этой difficulty.",
            ):
                st.session_state["preview_beatmap_id"] = beatmap.id
                st.rerun()
        if st.session_state.get("preview_beatmap_id") == beatmap.id:
            _render_gameplay_preview(store, scored)
        _feedback_form(store, beatmapset.id)


def _local_result(
    store: Storage,
    cfg: PresetConfig,
    *,
    all_candidates: bool = False,
) -> DiscoveryResult:
    engine = DiscoveryEngine(None, store, copy.deepcopy(cfg.discovery))
    return engine.rerank_stored(
        cfg.filters,
        cfg.ranking,
        all_candidates=all_candidates,
    )


def _remember_result(
    store: Storage,
    cfg: PresetConfig,
    result: DiscoveryResult,
    *,
    surface: str = "search",
) -> None:
    _clear_result_display()
    st.session_state["result"] = result
    st.session_state["result_config"] = cfg.to_dict()
    st.session_state["result_visible"] = 15
    st.session_state["result_shuffle_seed"] = secrets.randbits(64)
    st.session_state["result_pick_id"] = None
    st.session_state["result_display_order"] = None
    if result.scored:
        try:
            store.record_history(
                [
                    (item.beatmapset.id, item.score, index + 1)
                    for index, item in enumerate(result.scored[:40])
                ],
                surface=surface,
            )
        except StorageError as exc:
            st.warning(f"История показа не записана: {exc}")
        try:
            analysis_worker.prioritize_beatmaps(
                store,
                [item.beatmap.id for item in result.scored[:50]],
                priority=-5,
                reason="active_result",
            )
        except Exception:  # noqa: BLE001 - queue nudge must not break browsing
            logger.warning("analysis priority nudged failed")


def _render_gameplay_preview(
    store: Storage,
    item: ScoredCandidate,
) -> None:
    beatmap_id = item.beatmap.id
    head = st.columns([3.0, 1.0])
    head[0].subheader("▶ Превью геймплея")
    if head[1].button(
        "Закрыть превью",
        key="close_gameplay_preview",
        use_container_width=True,
    ):
        st.session_state.pop("preview_beatmap_id", None)
        st.rerun()
    st.caption(
        f"{item.beatmapset.display_title} · [{item.beatmap.version}] · "
        f"{item.beatmap.difficulty_rating or '?'}★. Используется .osu именно "
        "этой difficulty, без случайной анимации."
    )
    try:
        with st.spinner("Загружаю публичный .osu для превью…"):
            preview = gameplay.load_preview(
                beatmap_id, _gameplay_cache_dir(store)
            )
    except gameplay.GameplayError as exc:
        st.error(f"Превью недоступно: {exc}")
        st.caption(
            "Приложение не подменяет геймплей обложкой или случайными нотами. "
            "Файл мог быть удалён или скрыт; проверьте соединение с "
            "osu.ppy.sh и повторите."
        )
        st.markdown(
            "[Открыть mapset на osu!]("
            + config.BEATMAPSET_URL.format(id=item.beatmapset.id)
            + ")"
        )
        return
    st.html(
        gameplay.preview_html(preview),
        unsafe_allow_javascript=True,
    )


def _reveal_search_in_library() -> None:
    """Make newly fetched sets visible instead of hiding them behind scopes."""

    st.session_state["library_scope"] = "all"
    st.session_state["library_preset_mode"] = False
    st.session_state["library_sort"] = "recent_added"
    st.session_state["library_page"] = 1
    st.session_state["_library_after_search"] = True


def _merge_expanded_result(
    previous: DiscoveryResult | None,
    expanded: DiscoveryResult,
) -> DiscoveryResult:
    """Merge an expanded run with the earlier visible collection.

    The discovery engine normally returns a superset, but a bounded/cached run
    can omit a previous page. Keep the expanded candidate for overlapping sets
    and append only old sets that are genuinely absent.
    """

    if not isinstance(previous, DiscoveryResult) or not previous.scored:
        return expanded
    expanded_ids = {item.beatmapset.id for item in expanded.scored}
    old_only = [
        item
        for item in previous.scored
        if item.beatmapset.id not in expanded_ids
    ]
    if not old_only:
        return expanded
    merged = list(expanded.scored)
    seen = {item.beatmapset.id for item in merged}
    for item in old_only:
        if item.beatmapset.id in seen:
            continue
        seen.add(item.beatmapset.id)
        merged.append(item)
    merged.sort(
        key=lambda item: (
            -item.score,
            item.beatmapset.id,
            item.beatmap.id,
        )
    )
    return replace(
        expanded,
        scored=tuple(merged),
        total_fetched=max(expanded.total_fetched, previous.total_fetched),
        passed_filters=max(expanded.passed_filters, previous.passed_filters),
        passed_relevance=max(
            expanded.passed_relevance, previous.passed_relevance
        ),
        notes=(
            f"Объединено с предыдущей выдачей: добавлено "
            f"{len(merged) - len(expanded.scored)} старых карт, не попавших "
            "в расширенный срез.",
            *expanded.notes,
        )[:12],
    )


def _mark_search_expansion(previous_set_ids: set[int]) -> None:
    """Remember which maps are genuinely new after an expanded API search."""

    result = st.session_state.get("result")
    if not isinstance(result, DiscoveryResult):
        return
    current_ids = [item.beatmapset.id for item in result.scored]
    added = [
        set_id for set_id in current_ids if set_id not in previous_set_ids
    ]
    if added:
        st.session_state["result_new_ids"] = added
        # Filtering is opt-in. The default stays the full cumulative result.
        st.session_state["result_only_new"] = False
        st.session_state["result_expand_notice"] = (
            f"Расширенный поиск добавил {len(added)} новых карт. "
            "Они сохранены в SQLite; при необходимости включите "
            "«Только новые карты»."
        )
    else:
        st.session_state.pop("result_new_ids", None)
        st.session_state.pop("result_only_new", None)
        st.session_state["result_expand_notice"] = (
            "Расширенный поиск не нашёл новых карт: API вернул те же "
            "совпадения после текущих фильтров и целевого гейта. "
            "Предыдущая выдача сохранена."
        )


def _run_expanded_search(store: Storage, cfg: PresetConfig) -> None:
    result = st.session_state.get("result")
    previous_ids = (
        {item.beatmapset.id for item in result.scored}
        if isinstance(result, DiscoveryResult)
        else set()
    )
    previous_visible = int(st.session_state.get("result_visible", 15))
    previous_sort = st.session_state.get("result_sort_mode")
    if _execute_search(store, cfg, expand_limits=True):
        expanded = st.session_state.get("result")
        if isinstance(expanded, DiscoveryResult):
            merged = _merge_expanded_result(result, expanded)
            st.session_state["result"] = merged
            total = len(merged.scored)
            st.session_state["result_visible"] = min(
                max(1, previous_visible), max(1, total)
            )
            if isinstance(previous_sort, str):
                st.session_state["result_sort_mode"] = previous_sort
        _mark_search_expansion(previous_ids)
        st.rerun()


def _execute_search(
    store: Storage,
    cfg: PresetConfig,
    *,
    local_only: bool = False,
    expand_limits: bool = False,
) -> bool:
    try:
        validate_filters(cfg.filters)
        validate_ranking(cfg.ranking)
        presets.validate_preset_config(cfg)
    except (FilterConfigError, RankingConfigError, PresetError) as exc:
        st.error(f"Поиск не запущен: {exc}")
        return False

    _clear_random_state()
    try:
        api_config = config.load_api_config()
    except ValueError as exc:
        st.error(f"Конфигурация API некорректна: {exc}")
        return False

    local_result: DiscoveryResult | None = None

    def local_fallback() -> DiscoveryResult | None:
        nonlocal local_result
        if local_result is None:
            try:
                local_result = _local_result(store, cfg)
            except (StorageError, ValueError) as exc:
                st.warning(f"Локальный кеш недоступен: {exc}")
                return None
        return local_result

    if local_only or not api_config.configured:
        cached = local_fallback()
        if cached is None or not cached.scored:
            if api_config.configured:
                st.error(
                    "В локальном кеше нет карт под текущие строгие фильтры. "
                    "Новый API-поиск не запускался: выбран режим «Только "
                    "локальный кеш». Фиктивные карты не подставляются."
                )
            else:
                st.error(
                    "Нет OSU_CLIENT_ID / OSU_CLIENT_SECRET, а в локальном кеше "
                    "нет карт под текущие строгие фильтры. Реальный поиск без "
                    "credentials не выполняется; фиктивные карты не "
                    "подставляются."
                )
            return False
        note = (
            "Выбран только локальный кеш: карты переранжированы под активный "
            "пресет без нового API-поиска."
            if api_config.configured
            else "API не настроен, поэтому показан только локальный кеш. Для "
            "расширения выдачи добавьте credentials."
        )
        cached = replace(
            cached,
            notes=tuple([*cached.notes, note])[:12],
        )
        _remember_result(store, cfg, cached, surface="search")
        _reveal_search_in_library()
        return True

    try:
        client = _get_api(api_config)
        discovery_settings = copy.deepcopy(cfg.discovery)
        if expand_limits:
            discovery_settings.max_requests = min(
                60, int(discovery_settings.max_requests) + 6
            )
            discovery_settings.max_pages_per_strategy = min(
                10, int(discovery_settings.max_pages_per_strategy) + 2
            )
            discovery_settings.max_candidates = min(
                2000, int(discovery_settings.max_candidates) + 200
            )
        engine = DiscoveryEngine(client, store, discovery_settings)
        with st.spinner(
            "Ищу карты в osu! API: несколько стратегий и страниц…"
            if not expand_limits
            else "Ищу больше карт: увеличены страницы и бюджет запросов…"
        ):
            result = engine.run(cfg.filters, cfg.ranking, record=True)
        if expand_limits:
            result = replace(
                result,
                notes=tuple(
                    [
                        "Поиск повторён с увеличенными лимитами страниц и "
                        "запросов (только для этого запуска; настройки пресета "
                        "не изменены).",
                        *result.notes,
                    ]
                )[:12],
            )
    except OsuApiError as exc:
        cached = local_fallback()
        if cached is not None and cached.scored:
            st.warning(f"osu! API: {exc.user_message()}. Показан локальный кеш.")
            _remember_result(store, cfg, cached, surface="search")
            _reveal_search_in_library()
            return True
        st.error(f"osu! API: {exc.user_message()}")
        return False
    except (StorageError, ValueError) as exc:
        st.error(f"Поиск остановлен: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - UI boundary
        logger.error("search failed: %s", type(exc).__name__)
        st.error(
            "Непредвиденная ошибка поиска. Подробности не содержат секретов; "
            "проверьте раздел «Диагностика» и повторите позже."
        )
        return False

    _remember_result(store, cfg, result, surface="search")
    _reveal_search_in_library()
    return True


@dataclass(frozen=True)
class RandomPool:
    """A candidate pool read from the persistent local catalog."""

    result: DiscoveryResult
    source_note: str
    catalog_size: int = 0


def _collect_random_pool(
    store: Storage,
    cfg: PresetConfig,
) -> RandomPool:
    """Collect candidates from SQLite only.

    Random selection is deliberately local-first: it never loads API
    credentials, acquires a token, fetches search pages or downloads ``.osu``
    files. Online discovery remains a separate, explicit user action.
    """

    try:
        catalog_size = store.beatmapset_count()
        # The randomizer needs the whole eligible universe, not just the first
        # screen of top-ranked cards. Ranking still influences sampling
        # weights, but the display cap no longer truncates the pool.
        local = _local_result(store, cfg, all_candidates=True)
    except (StorageError, ValueError) as exc:
        raise ValueError(f"Локальный каталог недоступен: {exc}") from exc

    if catalog_size == 0:
        source_note = (
            "Локальный каталог SQLite пуст. Случайная подборка не запускает "
            "онлайн-поиск и не подставляет фиктивные карты."
        )
    else:
        source_note = (
            f"Локальный каталог SQLite: {catalog_size} сохранённых карт; "
            f"для переранжирования рассмотрено {local.total_fetched}. "
            "Сетевых запросов нет."
        )
    return RandomPool(
        result=local,
        source_note=source_note,
        catalog_size=catalog_size,
    )


def _random_empty_message(
    selection: randomizer.RandomSelection,
    pool: RandomPool,
) -> str:
    """Explain why a local-only random selection has no cards."""

    parts = [selection.note]
    considered = pool.result.total_fetched
    if pool.catalog_size == 0:
        parts.append(
            "Локальный каталог пуст: в SQLite нет сохранённых beatmapsets. "
            "Случайная подборка никогда не запускает онлайн-поиск. Нажмите "
            "«Искать», чтобы явно выполнить поиск в osu! API и заполнить "
            "каталог, затем повторите."
        )
        return " ".join(parts)

    if selection.pool_size == 0:
        parts.append(
            f"В локальном каталоге {pool.catalog_size} карт (рассмотрено "
            f"{considered}), но ни одна не прошла строгие фильтры и "
            "обязательный гейт. Онлайн-поиск не запускался: измените фильтры "
            "или нажмите «Искать», чтобы явно расширить каталог."
        )
        return " ".join(parts)

    if selection.available == 0:
        parts.append(
            "В текущей random-сессии показаны все доступные карты. Можно "
            "разрешить повторно показанные карты, изменить фильтры или "
            "нажать «Искать», чтобы явно расширить локальный каталог."
        )
        return " ".join(parts)

    parts.append(
        f"Локальный каталог: {pool.catalog_size} карт; онлайн-поиск не "
        "запускался. Чтобы расширить каталог, нажмите «Искать»."
    )
    return " ".join(parts)


def _random_meta(
    selection: randomizer.RandomSelection,
    pool: RandomPool,
    *,
    round_number: int,
    desired: int,
    seed: int | None,
    session_seen: int,
    recent_history: int,
) -> dict[str, Any]:
    local = pool.result
    return {
        "round": round_number,
        "pool": selection.pool_size,
        "available": selection.available,
        "count": selection.count,
        "requested": desired,
        "shortfall": selection.shortfall,
        "excluded": selection.excluded,
        "seed": seed,
        "note": selection.note,
        "source": pool.source_note,
        "origin": local.origin,
        "local_only": True,
        "api_configured": False,
        "api_error": None,
        "api_unique": 0,
        "local_unique": len(local.scored),
        "api_strict_passed": 0,
        "local_strict_passed": local.passed_filters,
        "api_relevance_passed": 0,
        "local_relevance_passed": local.passed_relevance,
        "local_considered": local.total_fetched,
        "catalog_size": pool.catalog_size,
        "api_requests": 0,
        "cache_hits": local.cache_hits,
        "complete": bool(local.complete),
        "session_seen": session_seen,
        "recent_history": recent_history,
    }


def _execute_random_selection(store: Storage, cfg: PresetConfig) -> None:
    try:
        validate_filters(cfg.filters)
        validate_ranking(cfg.ranking)
        presets.validate_preset_config(cfg)
    except (FilterConfigError, RankingConfigError, PresetError) as exc:
        st.error(f"Случайная подборка не запущена: {exc}")
        return

    scope_key = _random_scope_key(cfg)
    if st.session_state.get("random_scope_key") != scope_key:
        _clear_random_state()
        st.session_state["random_scope_key"] = scope_key

    try:
        desired = int(st.session_state.get("random_size", 12))
    except (TypeError, ValueError):
        desired = 12
    desired = max(3, min(40, desired))
    avoid_seen = bool(st.session_state.get("random_avoid_seen", True))
    pool_round = max(1, int(st.session_state.get("random_round", 0)) + 1)
    last_ids = {
        int(value)
        for value in st.session_state.get("random_last_ids", [])
        if str(value).isdigit()
    }
    session_seen = {
        int(value)
        for value in st.session_state.get("random_seen_ids", [])
        if isinstance(value, int) or str(value).isdigit()
    }
    recent_ids: set[int] = set()
    if avoid_seen:
        # History is a cross-session fallback.  The in-session set is what
        # guarantees no repeats after the bounded recent window rolls over.
        recent_ids = {
            item.id
            for item in store.recent_sets(
                60, surfaces=("random", "random_pick")
            )
        }
        avoid_ids = recent_ids | last_ids | session_seen
    else:
        # Turning the option off must actually allow the last selection again.
        avoid_ids = set()
    seed = secrets.randbits(64)

    try:
        pool = _collect_random_pool(store, cfg)
    except (StorageError, ValueError) as exc:
        st.error(f"Случайная подборка не собрана: {exc}")
        return

    diversity_active = cfg.ranking.diversity > 0
    selection = randomizer.select_random_subset(
        pool.result.scored,
        desired,
        seed=seed,
        avoid_ids=avoid_ids,
        skill_weights=cfg.ranking.skill_weights,
        max_per_artist=(
            cfg.ranking.max_per_artist if diversity_active else None
        ),
        max_per_mapper=(
            cfg.ranking.max_per_mapper if diversity_active else None
        ),
    )
    st.session_state["random_round"] = pool_round
    seen_ids = set(session_seen)
    if selection.items:
        seen_ids.update(item.beatmapset.id for item in selection.items)
    st.session_state["random_seen_ids"] = sorted(seen_ids)
    meta = _random_meta(
        selection,
        pool,
        round_number=pool_round,
        desired=desired,
        seed=seed,
        session_seen=len(seen_ids),
        recent_history=len(recent_ids),
    )

    if not selection.items:
        empty_result = replace(
            pool.result,
            scored=(),
            notes=tuple(
                dict.fromkeys(
                    [
                        note
                        for note in (
                            *pool.result.notes,
                            pool.source_note,
                            selection.note,
                        )
                        if note
                    ]
                )
            )[:12],
        )
        _remember_result(store, cfg, empty_result, surface="random")
        st.session_state["random_meta"] = meta
        st.session_state["random_notice"] = _random_empty_message(
            selection, pool
        )
        _flash("Новых кандидатов для случайной подборки нет.")
        st.rerun()

    notes = [
        note
        for note in (*pool.result.notes, pool.source_note, selection.note)
        if note
    ]
    selected_result = replace(
        pool.result,
        scored=selection.items,
        notes=tuple(dict.fromkeys(notes))[:12],
        passed_filters=pool.result.passed_filters,
    )
    _remember_result(store, cfg, selected_result, surface="random")
    st.session_state["random_last_ids"] = [
        item.beatmapset.id for item in selection.items
    ]
    st.session_state["random_meta"] = meta
    notice: str | None = None
    if selection.shortfall:
        notice = (
            f"{selection.note} Чтобы расширить каталог, нажмите «Искать»."
        )
    elif selection.available <= selection.requested:
        # The batch consumed the whole eligible pool. Say so instead of
        # presenting a reordered copy of the same maps as a fresh selection.
        notice = (
            f"{selection.note} Это все доступные карты: следующий набор "
            "потребует явного поиска или изменения фильтров."
        )
    st.session_state["random_notice"] = notice
    _flash(
        f"Случайная подборка: {selection.count} карт; пул "
        f"{selection.pool_size}, доступно новых {selection.available}."
    )
    st.rerun()


def _shuffle_results() -> None:
    result = st.session_state.get("result")
    if not isinstance(result, DiscoveryResult) or not result.scored:
        return
    ids = [item.beatmap.id for item in result.scored]
    previous = st.session_state.get("result_display_order") or ids
    seed = secrets.randbits(64)
    st.session_state["result_shuffle_seed"] = seed
    st.session_state["result_display_order"] = randomizer.shuffled_ids(
        ids, seed=seed, previous=previous
    )
    st.session_state["result_pick_id"] = None
    st.session_state.pop("preview_beatmap_id", None)


def _pick_random_result(store: Storage) -> None:
    result = st.session_state.get("result")
    if not isinstance(result, DiscoveryResult) or not result.scored:
        return
    pool = list(result.scored)
    if st.session_state.get("result_only_new"):
        new_ids = {
            int(value)
            for value in (st.session_state.get("result_new_ids") or ())
        }
        filtered = [
            item for item in pool if item.beatmapset.id in new_ids
        ]
        if filtered:
            pool = filtered
    current = st.session_state.get("result_pick_id")
    avoid: set[int] = set()
    if isinstance(current, int) and len(pool) > 1:
        current_item = next(
            (
                item
                for item in pool
                if item.beatmap.id == current
            ),
            None,
        )
        if current_item is not None:
            avoid.add(current_item.beatmapset.id)
    selection = randomizer.select_random_subset(
        pool,
        1,
        seed=secrets.randbits(64),
        avoid_ids=avoid,
        skill_weights=(
            result.ranking_used.skill_weights if result.ranking_used else {}
        ),
    )
    if selection.items:
        picked = selection.items[0]
        st.session_state["result_pick_id"] = picked.beatmap.id
        st.session_state["result_display_order"] = None
        st.session_state.pop("preview_beatmap_id", None)
        try:
            # Random Pick is a randomization too: remember the shown set in
            # the same bounded recent window used by Random Selection.
            store.record_history(
                [(picked.beatmapset.id, picked.score, 1)],
                surface="random_pick",
            )
        except StorageError as exc:
            st.session_state["random_notice"] = (
                f"Карта выбрана, но история не записана: {exc}"
            )


def _reset_result_order() -> None:
    st.session_state["result_display_order"] = None
    st.session_state["result_pick_id"] = None
    st.session_state["result_sort_mode"] = "Рекомендация"


def _show_more_results() -> None:
    st.session_state["result_visible"] = (
        int(st.session_state.get("result_visible", 15)) + 15
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _render_result(
    store: Storage,
    result: DiscoveryResult,
    *,
    cfg: PresetConfig | None = None,
    can_expand_search: bool = False,
) -> None:
    source = {
        "api": "osu! API",
        "mixed": "osu! API + локальный кеш",
        "local_cache": "локальный кеш",
    }.get(result.origin, result.origin)
    st.caption(
        f"{result.count} карт · {source} · {result.api_requests} API-запросов"
        f" · Recommendation — внутренний порядок 0–100, не вероятность."
    )
    if st.session_state.get("_library_after_search"):
        st.caption(
            "Карты сохранены в SQLite. При переходе в «Библиотеку» откроется "
            "весь каталог с сортировкой «Недавно добавленные», включая карты, "
            "которые ещё ждут анализа."
        )
    with st.expander("Детали выдачи", expanded=False):
        st.caption(
            f"Запросов: {result.api_requests} (успешно "
            f"{getattr(result, 'requests_succeeded', 0)}, ошибок "
            f"{getattr(result, 'requests_failed', 0)}) · HTTP-попыток: "
            f"{getattr(result, 'http_attempts', 0)} · OAuth: "
            f"{getattr(result, 'oauth_requests', 0)} · страниц: "
            f"{getattr(result, 'pages_attempted', 0)} · кэш-хитов: "
            f"{result.cache_hits}."
        )
        if result.origin in {"api", "mixed"}:
            raw_stop = getattr(result, "stop_reason", "") or "—"
            stop_labels = {
                "complete": "все стратегии завершены",
                "request_budget": "бюджет запросов",
                "page_limit": "лимит страниц",
                "deadline": "дедлайн",
                "network_failure": "сетевая ошибка/таймаут",
                "rate_limit": "HTTP 429",
                "forbidden": "HTTP 403",
                "request_failed": "ошибка запроса",
                "candidate_limit": "лимит кандидатов",
                "limits": "лимиты",
            }
            stop = stop_labels.get(raw_stop, raw_stop)
            deadline = getattr(result, "deadline_seconds", 0.0)
            st.caption(
                f"Discovery: дедлайн {deadline:g} с · остановка: "
                f"{stop} · полнота: {'да' if result.complete else 'нет'}."
            )
        if result.sequence_required:
            sequence_state = (
                "полный" if result.sequences_complete else "неполный"
            )
            st.caption(
                "Sequence-анализ: "
                f"{result.sequence_analyzed} проанализировано "
                f"({result.sequence_partial} частично), "
                f"{result.sequence_not_analyzed} не анализировалось, "
                f"{result.sequence_source_unavailable} без .osu, "
                f"{result.sequence_failed} с ошибкой · "
                f".osu скачано: {result.sequence_downloads} · "
                f"из локального кэша/БД: {result.sequence_cache_hits} · "
                f"полнота sequence-данных: {sequence_state}."
            )

    random_meta = st.session_state.get("random_meta")
    if isinstance(random_meta, dict):
        st.info(
            f"Локальная подборка · раунд {random_meta.get('round', 1)} · "
            f"{random_meta.get('count', 0)} карт из "
            f"{random_meta.get('available', 0)} новых кандидатов · "
            f"пул {random_meta.get('pool', 0)} · сетевых запросов 0."
        )
        meta_note = random_meta.get("note")
        if meta_note and "Случайная подвыборка" not in str(meta_note):
            st.warning(str(meta_note))
        source_note = random_meta.get("source")
        if source_note:
            st.caption(str(source_note))

    timings = getattr(result, "timings", {})
    gate_targets = (
        result.ranking_used.relevance_targets if result.ranking_used else []
    )
    if (
        result.notes
        or result.errors
        or result.exclusions
        or result.excluded
        or timings
        or gate_targets
    ):
        with st.expander("Почему карт мало: лимиты, ошибки и исключения"):
            if gate_targets:
                target_names = ", ".join(
                    SKILLS[target].name if target in SKILLS else target
                    for target in gate_targets
                )
                st.caption(
                    "Обязательный гейт: хотя бы один из целевых тегов "
                    f"({target_names}) должен быть выше "
                    f"{result.ranking_used.relevance_threshold:.0%}. "
                    "Строгие фильтры применяются до него."
                )
            if timings:
                labels = {
                    "fetch": "Получение страниц",
                    "filter": "Строгие фильтры",
                    "attributes": "Difficulty attributes",
                    "sequences": "Sequence-анализ .osu",
                    "rank": "Ранжирование",
                    "total": "Всего",
                }
                st.markdown("**Время этапов**")
                st.dataframe(
                    [
                        {
                            "Этап": labels.get(key, key),
                            "Секунды": value,
                        }
                        for key, value in timings.items()
                    ],
                    hide_index=True,
                    width="stretch",
                )
            for note in result.notes:
                st.caption(f"• {note}")
            for error in result.errors:
                st.caption(f"• Ошибка: {error}")
            if result.excluded:
                st.markdown("**Причины исключения**")
                st.dataframe(
                    [
                        {"Причина": reason, "Карт": count}
                        for reason, count in result.excluded.items()
                    ],
                    hide_index=True,
                    width="stretch",
                )
            if result.exclusions:
                st.markdown("**Примеры исключённых карт**")
                st.dataframe(
                    [
                        {
                            "Карта": f"{item.title} (#{item.beatmapset_id})",
                            "Причина": item.reason,
                        }
                        for item in result.exclusions
                    ],
                    hide_index=True,
                    width="stretch",
                )

    if not result.scored:
        st.info(
            "Подходящих карт в этой выдаче нет. Это не доказательство, что их "
            "нет в osu! — смотрите причины исключений и лимиты выше."
        )
        return

    items = list(result.scored)
    expand_notice = st.session_state.get("result_expand_notice")
    if expand_notice:
        st.info(str(expand_notice))
    new_ids = {
        int(value)
        for value in (st.session_state.get("result_new_ids") or ())
    }
    show_only_new = bool(
        new_ids and st.session_state.get("result_only_new", False)
    )
    if new_ids:
        new_cols = st.columns([2.2, 1.2, 2.2])
        if new_cols[0].button(
            "Показать всю выдачу" if show_only_new else "Только новые карты",
            key="toggle_new_results",
            use_container_width=True,
            help="Оставить только beatmapsets, которых не было в прошлой "
            "выдаче, или снова показать всю выдачу.",
        ):
            st.session_state["result_only_new"] = not show_only_new
            st.rerun()
        new_cols[1].caption(f"Новых: {len(new_ids)}")
        new_cols[2].caption(
            "Новые карты также сохранены в SQLite для локальной библиотеки."
            if show_only_new
            else "Показана вся выдача; новые карты отмечены в уведомлении."
        )
        if show_only_new:
            items = [
                item
                for item in items
                if item.beatmapset.id in new_ids
            ]
            if not items:
                items = list(result.scored)
                show_only_new = False
    weights = result.ranking_used.skill_weights if result.ranking_used else {}
    order = st.session_state.get("result_display_order")
    if not isinstance(order, list):
        order = None

    control_cols = st.columns([1.6, 1.35, 1.0])
    sort_mode = control_cols[0].selectbox(
        "Сортировка результатов",
        options=(
            "Рекомендация",
            "Соответствие навыкам",
            "★ по возрастанию",
            "★ по убыванию",
            "Короче",
            "Дольше",
            "Новее",
        ),
        key="result_sort_mode",
        disabled=order is not None,
        help="ORDER меняет только порядок показа, а не score и не фильтры. "
        "Перемешивание использует отдельный порядок.",
    )
    if order is not None:
        positions = {
            int(beatmap_id): index
            for index, beatmap_id in enumerate(order)
        }
        items.sort(
            key=lambda item: positions.get(
                item.beatmap.id,
                len(positions),
            )
        )
        control_cols[1].button(
            "↩ Вернуть рекомендацию",
            key="reset_result_order",
            on_click=_reset_result_order,
            use_container_width=True,
        )
        control_cols[2].caption("Порядок перемешан без API-запроса.")
    else:
        if sort_mode == "Соответствие навыкам":
            def fit_key(item: ScoredCandidate) -> tuple[bool, float]:
                fit = (
                    skill_fit(item.candidate.skillsets, weights)
                    if weights
                    else None
                )
                return (
                    fit is None,
                    -(fit.score if fit is not None else 0.0),
                )

            items.sort(key=fit_key)
        elif sort_mode == "★ по возрастанию":
            items.sort(
                key=lambda item: (
                    item.beatmap.difficulty_rating is None,
                    item.beatmap.difficulty_rating or 0.0,
                )
            )
        elif sort_mode == "★ по убыванию":
            items.sort(
                key=lambda item: (
                    item.beatmap.difficulty_rating is None,
                    -(item.beatmap.difficulty_rating or 0.0),
                )
            )
        elif sort_mode == "Короче":
            items.sort(
                key=lambda item: (
                    (
                        item.beatmap.total_length
                        or item.beatmap.hit_length
                    )
                    is None,
                    item.beatmap.total_length
                    or item.beatmap.hit_length
                    or 0,
                )
            )
        elif sort_mode == "Дольше":
            items.sort(
                key=lambda item: (
                    (
                        item.beatmap.total_length
                        or item.beatmap.hit_length
                    )
                    is None,
                    -(
                        item.beatmap.total_length
                        or item.beatmap.hit_length
                        or 0
                    ),
                )
            )
        elif sort_mode == "Новее":
            items.sort(
                key=lambda item: (
                    bool(item.beatmapset.ranked_date),
                    item.beatmapset.ranked_date or "",
                ),
                reverse=True,
            )
        if st.session_state.get("result_pick_id") is not None:
            control_cols[1].button(
                "🎲 Ещё случайную",
                key="another_random_pick",
                on_click=_pick_random_result,
                use_container_width=True,
            )

    pick_id = st.session_state.get("result_pick_id")
    picked: ScoredCandidate | None = None
    if isinstance(pick_id, int):
        picked = next(
            (item for item in items if item.beatmap.id == pick_id),
            None,
        )
        if picked is None:
            st.session_state["result_pick_id"] = None
    visible = int(st.session_state.get("result_visible", 15))
    if picked is not None:
        shown = [picked]
        st.caption(
            f"🎯 Одна случайная карта из {len(items)} подходящих. Строгие "
            "фильтры и персональный score не менялись; нажмите «Ещё "
            "случайную» для другой."
        )
    else:
        shown = items[:visible]

    if picked is None:
        total = len(items)
        summary_cols = st.columns([2.4, 1.4, 1.2])
        summary_cols[0].caption(
            f"Показано {len(shown)} из {total}. Сортировка не меняет "
            "фильтры, score и целевой гейт."
        )
        if total > visible:
            remaining = total - len(shown)
            summary_cols[1].button(
                f"Показать ещё {min(15, remaining)}",
                key="show_more_results_top",
                on_click=_show_more_results,
                use_container_width=True,
            )
        else:
            summary_cols[1].caption("Все загруженные совпадения показаны")
            if can_expand_search and cfg is not None:
                if summary_cols[2].button(
                    "Искать больше",
                    key="expand_search_limits",
                    use_container_width=True,
                    help="Повторит API-поиск с увеличенным числом страниц и "
                    "бюджетом запросов. Изменения лимитов действуют только "
                    "на этот запуск и не сохраняются в пресет.",
                ):
                    _run_expanded_search(store, cfg)
            else:
                summary_cols[2].caption(
                    "Больше карт после фильтров и гейта нет; причины — в "
                    "«Почему карт мало»."
                )

    if picked is not None:
        _render_card(
            store,
            picked,
            items.index(picked) + 1,
            weights,
        )
    else:
        # osu!-style discovery grid: three compact cards per row on desktop;
        # Streamlit stacks the columns on narrow screens.
        for start in range(0, len(shown), 3):
            columns = st.columns(3, gap="medium")
            for offset, item in enumerate(shown[start : start + 3]):
                with columns[offset]:
                    _render_card(
                        store,
                        item,
                        start + offset + 1,
                        weights,
                    )
    if picked is None and len(items) > visible:
        remaining = len(items) - len(shown)
        st.button(
            f"Показать ещё {min(15, remaining)}",
            key="show_more_results",
            on_click=_show_more_results,
        )
    elif picked is None and can_expand_search and cfg is not None:
        if st.button(
            "Искать больше",
            key="expand_search_limits_bottom",
            help="Повторит API-поиск с увеличенным числом страниц и бюджетом "
            "запросов. Настройки пресета не изменяются.",
        ):
            _run_expanded_search(store, cfg)

    st.caption(
        "Ссылки ведут только на официальные beatmapsets. Превью строится из "
        "публичного .osu выбранной difficulty; оценка производительности "
        "нигде не рассчитывается и не подменяется числом из звёзд."
    )


def _search_advanced_filters(cfg: PresetConfig) -> None:
    """Collapsed secondary controls for the compact search screen."""

    filters = cfg.filters
    top = st.columns([1.0, 2.0])
    mode = top[0].selectbox(
        "Режим",
        list(MODES),
        index=(
            list(MODES).index(filters.modes[0])
            if filters.modes and filters.modes[0] in MODES
            else 0
        ),
        format_func=lambda value: MODE_LABELS.get(value, value),
        key=_ck("flt_mode"),
    )
    filters.modes = [mode]
    statuses = top[1].multiselect(
        "Статусы",
        options=list(STATUSES),
        default=[item for item in filters.statuses if item in STATUSES],
        format_func=lambda value: STATUS_RU.get(value, value),
        key=_ck("flt_statuses"),
        help="Пустой список недопустим: поиску нужен хотя бы один статус.",
    )
    filters.statuses = statuses

    st.markdown("**Звёзды и длительность**")
    star_cols = st.columns([1.0, 1.0, 1.0, 1.0])
    filters.star_tolerance = float(
        star_cols[0].number_input(
            "Допуск soft, ★",
            min_value=0.0,
            max_value=2.0,
            value=float(filters.star_tolerance),
            step=0.05,
            key=_ck("flt_tolerance"),
            disabled=filters.star_mode != "soft",
        )
    )
    duration_field = star_cols[1].selectbox(
        "Длина — поле",
        options=list(DURATION_FIELDS),
        index=(
            list(DURATION_FIELDS).index(filters.duration_field)
            if filters.duration_field in DURATION_FIELDS
            else 0
        ),
        format_func=lambda value: DURATION_LABELS[value],
        key=_ck("flt_duration_field"),
    )
    filters.duration_field = duration_field
    star_cols[2].caption(
        "Максимум длительности задаётся в основной строке поиска. "
        "Поле длины синхронизировано с разделом «Фильтры»."
    )
    st.caption(
        "Точные диапазоны BPM, AR, OD, CS и HP — ниже; звёздный допуск "
        "действует только в режиме soft."
    )

    st.markdown("**BPM, AR, OD, CS, HP**")
    filters.bpm_min, filters.bpm_max = _range_inputs(
        "BPM",
        filters.bpm_min,
        filters.bpm_max,
        "flt_bpm",
        default_low=120,
        default_high=240,
        minimum=0,
        maximum=1000,
        step=1.0,
    )
    filters.ar_min, filters.ar_max = _range_inputs(
        "AR",
        filters.ar_min,
        filters.ar_max,
        "flt_ar",
        default_low=7,
        default_high=10,
        minimum=0,
        maximum=11,
        step=0.1,
    )
    filters.od_min, filters.od_max = _range_inputs(
        "OD",
        filters.od_min,
        filters.od_max,
        "flt_od",
        default_low=6,
        default_high=10,
        minimum=0,
        maximum=11,
        step=0.1,
    )
    filters.cs_min, filters.cs_max = _range_inputs(
        "CS",
        filters.cs_min,
        filters.cs_max,
        "flt_cs",
        default_low=3,
        default_high=6,
        minimum=0,
        maximum=11,
        step=0.1,
    )
    filters.hp_min, filters.hp_max = _range_inputs(
        "HP",
        filters.hp_min,
        filters.hp_max,
        "flt_hp",
        default_low=3,
        default_high=8,
        minimum=0,
        maximum=11,
        step=0.1,
    )

    st.markdown("**Текст, артисты и мапперы**")
    artist_cols = st.columns(2)
    filters.include_artists = _split_csv(
        artist_cols[0].text_input(
            "Включить артистов (через запятую)",
            value=", ".join(filters.include_artists),
            key=_ck("flt_include_artists"),
        )
    )
    filters.exclude_artists = _split_csv(
        artist_cols[1].text_input(
            "Исключить артистов",
            value=", ".join(filters.exclude_artists),
            key=_ck("flt_exclude_artists"),
        )
    )
    mapper_cols = st.columns(2)
    filters.include_mappers = _split_csv(
        mapper_cols[0].text_input(
            "Включить мапперов",
            value=", ".join(filters.include_mappers),
            key=_ck("flt_include_mappers"),
        )
    )
    filters.exclude_mappers = _split_csv(
        mapper_cols[1].text_input(
            "Исключить мапперов",
            value=", ".join(filters.exclude_mappers),
            key=_ck("flt_exclude_mappers"),
        )
    )

    st.markdown("**Скрытие**")
    flags = st.columns(3)
    filters.exclude_nsfw = flags[0].checkbox(
        "Скрывать NSFW",
        value=filters.exclude_nsfw,
        key=_ck("flt_nsfw"),
    )
    filters.hide_seen = flags[1].checkbox(
        "Скрывать уже показанные",
        value=filters.hide_seen,
        key=_ck("flt_hide_seen"),
    )
    filters.hide_played = flags[2].checkbox(
        "Скрывать «уже играл»",
        value=filters.hide_played,
        key=_ck("flt_hide_played"),
    )
    flags2 = st.columns(2)
    filters.hide_favorites = flags2[0].checkbox(
        "Скрывать избранное",
        value=filters.hide_favorites,
        key=_ck("flt_hide_favorites"),
    )
    filters.only_favorites = flags2[1].checkbox(
        "Только избранное",
        value=filters.only_favorites,
        key=_ck("flt_only_favorites"),
    )

    st.markdown("**Случайная подборка и лимиты API**")
    random_cols = st.columns([1.0, 1.3, 1.3])
    random_cols[0].number_input(
        "Карт в подборке",
        min_value=3,
        max_value=40,
        value=int(st.session_state.get("random_size", 12)),
        step=1,
        key="random_size",
    )
    random_cols[1].checkbox(
        "Не повторять недавно показанные",
        value=bool(st.session_state.get("random_avoid_seen", True)),
        key="random_avoid_seen",
        help="Последние показанные карты исключаются из новой подборки. "
        "Если новых кандидатов мало, приложение честно сообщит об этом.",
    )
    random_cols[2].checkbox(
        "Только локальный кеш",
        value=bool(st.session_state.get("search_local_only", False)),
        key="search_local_only",
        help="Отключает сеть только для явной кнопки «Искать». Случайная "
        "подборка всегда использует локальный каталог SQLite и не делает "
        "API-запросов.",
    )
    discovery = cfg.discovery
    limit_cols = st.columns(4)
    discovery.max_requests = int(
        limit_cols[0].number_input(
            "Максимум запросов",
            min_value=1,
            max_value=60,
            value=int(discovery.max_requests),
            step=1,
            key=_ck("dsc_max_requests"),
        )
    )
    discovery.max_pages_per_strategy = int(
        limit_cols[1].number_input(
            "Страниц на стратегию",
            min_value=1,
            max_value=10,
            value=int(discovery.max_pages_per_strategy),
            step=1,
            key=_ck("dsc_pages"),
        )
    )
    discovery.max_candidates = int(
        limit_cols[2].number_input(
            "Максимум кандидатов",
            min_value=10,
            max_value=2000,
            value=int(discovery.max_candidates),
            step=10,
            key=_ck("dsc_candidates"),
            help="Ограничивает число карточек в выдаче поиска. Случайная "
            "подборка получает весь подходящий локальный каталог и этим "
            "лимитом не обрезается.",
        )
    )
    discovery.fetch_sequences_limit = int(
        limit_cols[3].number_input(
            "Скачивать .osu для анализа",
            min_value=0,
            max_value=50,
            value=int(discovery.fetch_sequences_limit),
            step=1,
            key=_ck("dsc_sequences"),
            help="Сколько .osu скачать за один поиск для Streams/Bursts. "
            "0 отключает загрузку; уже сохранённые файлы всё равно "
            "используются, но анализируется не более 50 карт из кэша. "
            "Это отдельно от лимита osu! API.",
        )
    )
    strategy_cols = st.columns(4)
    discovery.use_oldest = strategy_cols[0].checkbox(
        "Старые (ranked_asc)",
        value=discovery.use_oldest,
        key=_ck("dsc_oldest"),
    )
    discovery.use_rating = strategy_cols[1].checkbox(
        "По рейтингу",
        value=discovery.use_rating,
        key=_ck("dsc_rating"),
    )
    discovery.use_popular = strategy_cols[2].checkbox(
        "По избранному",
        value=discovery.use_popular,
        key=_ck("dsc_popular"),
    )
    discovery.expand_seeds = strategy_cols[3].checkbox(
        "Расширять по вашим оценкам",
        value=discovery.expand_seeds,
        key=_ck("dsc_seeds"),
    )
    st.caption(
        "Стратегии: recent по статусам + oldest/rating/favourites + до трёх "
        "запросов по вашим положительным оценкам. Это относится только к "
        "явной кнопке «Искать»: случайная подборка не углубляет пагинацию и "
        "не делает сетевых запросов. Онлайн-поиск также ограничен общим "
        "дедлайном discovery (120 с), который проверяется между запросами. "
        "Streams/Bursts оцениваются только по найденным в .osu цепочкам: "
        "длиннее 9 нот — stream, 5–9 нот — burst."
    )


def page_search(store: Storage, cfg: PresetConfig) -> None:
    _page_intro(
        "DISCOVERY",
        "Обзор карт",
        "Соберите подборку по пресету и строгим фильтрам. Поиск в osu! API "
        "запускается только кнопкой; локальная библиотека остаётся доступна "
        "офлайн.",
    )
    try:
        api_config = config.load_api_config()
        configured = api_config.configured
    except ValueError as exc:
        configured = False
        st.error(f"Некорректная конфигурация API: {exc}")
    if not configured:
        st.warning(
            "Онлайн-поиск недоступен без OSU_CLIENT_ID / OSU_CLIENT_SECRET. "
            "Обзор и библиотека работают по локальному каталогу SQLite."
        )

    filters = cfg.filters
    selected_skills = [
        f"{skill_label(key)} {value:g}"
        for key, value in cfg.ranking.skill_weights.items()
        if value > 0
    ]
    try:
        counts = library.counts(store)
        catalog_size = store.beatmapset_count()
    except StorageError:
        counts = {"total": 0, "analyzed": 0, "partial": 0, "pending": 0}
        catalog_size = 0
    def local_number(value: int) -> str:
        return f"{value:,}".replace(",", " ")

    st.markdown(
        "<div class='discovery-stats'>"
        f"<div><strong>{local_number(counts.get('total', 0))}</strong>"
        f"<span>{_ru_plural(counts.get('total', 0), 'сложность', 'сложности', 'сложностей')} в локальном каталоге</span></div>"
        f"<div><strong>{local_number(counts.get('analyzed', 0) + counts.get('partial', 0))}</strong>"
        "<span>с анализом .osu</span></div>"
        f"<div><strong>{local_number(counts.get('pending', 0))}</strong>"
        "<span>ждут анализа</span></div>"
        f"<div><strong>{local_number(catalog_size)}</strong>"
        "<span>наборов</span></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Активный профиль: «{cfg.name}» · "
        + (
            " · ".join(selected_skills)
            if selected_skills
            else "навыки не выбраны"
        )
    )

    st.markdown(
        "<div class='section-label'>Поиск и быстрые фильтры</div>",
        unsafe_allow_html=True,
    )
    query_text = st.text_input(
        "Что искать",
        value=filters.query or "",
        key=_ck("flt_query"),
        placeholder="название, артист, маппер или тег (необязательно)",
        help="Передаётся в API как q и дополнительно проверяется локально.",
    )
    filters.query = query_text.strip()

    stars_cols = st.columns([1.0, 1.0, 1.25, 1.1])
    filters.star_mode = stars_cols[2].selectbox(
        "Режим звёзд",
        options=["strict", "soft", "off"],
        index=(
            ["strict", "soft", "off"].index(filters.star_mode)
            if filters.star_mode in ("strict", "soft", "off")
            else 0
        ),
        format_func=lambda value: STAR_MODE_LABELS[value],
        key=_ck("flt_star_mode"),
        help="strict — жёсткие границы; soft — допуск с пометкой; off — без "
        "границ по звёздам.",
    )
    stars_min_input = stars_cols[0].number_input(
        "★ минимум",
        min_value=0.0,
        max_value=12.0,
        value=float(filters.stars_min if filters.stars_min is not None else 4.0),
        step=0.1,
        key=_ck("flt_stars_min"),
        disabled=filters.star_mode == "off",
    )
    stars_max_input = stars_cols[1].number_input(
        "★ максимум",
        min_value=0.0,
        max_value=12.0,
        value=float(filters.stars_max if filters.stars_max is not None else 6.9),
        step=0.1,
        key=_ck("flt_stars_max"),
        disabled=filters.star_mode == "off",
    )
    if filters.star_mode == "off":
        filters.stars_min = None
        filters.stars_max = None
    else:
        filters.stars_min = float(stars_min_input)
        filters.stars_max = float(stars_max_input)

    duration_cols = st.columns([1.0, 1.0, 2.1])
    use_duration = duration_cols[0].checkbox(
        "Ограничить длительность",
        value=filters.duration_min is not None
        or filters.duration_max is not None,
        key=_ck("flt_duration_on"),
    )
    if use_duration:
        filters.duration_max = int(
            duration_cols[1].number_input(
                "Максимум, сек",
                min_value=1,
                max_value=7200,
                value=int(filters.duration_max or 180),
                step=5,
                key=_ck("flt_duration_max"),
            )
        )
    else:
        filters.duration_min = None
        filters.duration_max = None
    duration_cols[2].caption(
        "Длительность считается по полю "
        f"«{DURATION_LABELS.get(filters.duration_field, filters.duration_field)}». "
        "Поле, допуск и точные диапазоны — в дополнительных фильтрах."
    )

    result = st.session_state.get("result")
    has_result = isinstance(result, DiscoveryResult) and bool(result.scored)
    actions = st.columns([1.0, 1.4, 1.15, 1.45])
    if actions[0].button(
        "Искать",
        type="primary",
        key="run_search",
        use_container_width=True,
        help="Явный поиск в osu! API. Ограничен числом запросов, страниц на "
        "стратегию и общим дедлайном discovery; частичный результат и причина "
        "остановки показываются под выдачей.",
    ):
        if _execute_search(
            store,
            cfg,
            local_only=bool(st.session_state.get("search_local_only", False)),
        ):
            st.rerun()
    if actions[1].button(
        "Случайная подборка",
        key="random_selection",
        use_container_width=True,
        help="Выбирает новых кандидатов из локального каталога SQLite, "
        "исключает недавно показанные и не делает API-запросов. Пресет, "
        "фильтры и навыки не меняются.",
    ):
        _execute_random_selection(store, cfg)
    actions[2].button(
        "Перемешать",
        key="shuffle_results",
        on_click=_shuffle_results,
        disabled=not has_result,
        use_container_width=True,
        help="Меняет только порядок текущей выдачи. API-запросов нет.",
    )
    actions[3].button(
        "Случайная карта",
        key="random_pick",
        on_click=_pick_random_result,
        args=(store,),
        disabled=not has_result,
        use_container_width=True,
        help="Показывает одну карту из текущей выдачи, прошедшую строгие "
        "фильтры. Повторное нажатие выбирает другую.",
    )

    with st.expander("Дополнительные фильтры и лимиты", expanded=False):
        _search_advanced_filters(cfg)
        st.caption(
            "Ещё больше параметров, пресеты, навыки, веса, оформление и "
            "диагностика — в разделе «Настройки»."
        )

    _render_dirty_bar(store, cfg)

    notice = st.session_state.pop("random_notice", None)
    if notice:
        st.warning(str(notice))


    result = st.session_state.get("result")
    if isinstance(result, DiscoveryResult):
        snapshot = st.session_state.get("result_config")
        if snapshot is not None and snapshot != cfg.to_dict():
            st.warning(
                "Настройки изменились после этой выдачи. Результаты ниже "
                "относятся к прежней конфигурации — запустите поиск или "
                "случайную подборку заново."
            )
        can_expand_search = bool(
            configured
            and not result.complete
            and not st.session_state.get("search_local_only", False)
        )
        _render_result(
            store,
            result,
            cfg=cfg,
            can_expand_search=can_expand_search,
        )
    else:
        st.info(
            "Результатов пока нет. Нажмите «Искать» или «Случайная подборка»."
        )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def _library_cover_html(
    *,
    cover_url: str,
    title: str,
    status: str,
    difficulties: int,
    url: str,
) -> str:
    fallback = html.escape((title or "osu!")[:2].upper())
    cover = html.escape(cover_url, quote=True)
    return (
        "<div class='library-cover'>"
        + f"<div class='library-cover-fallback'>{fallback}</div>"
        + (
            f"<img src='{cover}' alt='' loading='lazy' "
            "onerror=\"this.style.display='none'\">"
            if cover
            else ""
        )
        + "<div class='library-cover-top'>"
        f"<span class='library-status'>{html.escape(STATUS_RU.get(status, status))}</span>"
        + (
            f"<span class='library-count'>{difficulties} сл.</span>"
            if difficulties > 0
            else ""
        )
        + "</div>"
        f"<a class='library-cover-link' href='{html.escape(url, quote=True)}' "
        "target='_blank' rel='noopener noreferrer' aria-label='Открыть на osu!'></a>"
        "</div>"
    )


def _library_patterns_html(entry: library.LibraryEntry) -> str:
    labels = entry.labels[:6]
    if labels:
        chips = "".join(
            f"<span class='pattern-chip'>{html.escape(PATTERN_LABELS.get(label, label))}</span>"
            for label in labels
        )
        return f"<div class='library-patterns'>{chips}</div>"
    if entry.state in {library.ANALYZED, library.PARTIAL}:
        text = "выраженных паттернов не найдено"
    else:
        text = "паттерны недоступны до анализа .osu"
    return f"<div class='library-patterns muted'>{text}</div>"


def _set_library_preview(entry: library.LibraryEntry) -> None:
    st.session_state["library_preview_entry"] = entry


def _clear_library_preview() -> None:
    st.session_state.pop("library_preview_entry", None)


def _render_library_entry(
    store: Storage,
    entry: library.LibraryEntry,
    current_scores: dict[int, ScoredCandidate],
    *,
    preset_selected: bool = False,
) -> None:
    stars = f"{entry.stars:.2f}★" if entry.stars is not None else "★ ?"
    bpm = f"{entry.effective_bpm:.0f}" if entry.effective_bpm else "?"
    length = _format_seconds(entry.effective_length)
    ar = f"{entry.ar:g}" if entry.ar is not None else "?"
    od = f"{entry.od:g}" if entry.od is not None else "?"
    tone = library.STATE_TONE.get(entry.state, "muted")
    scored = current_scores.get(entry.beatmap_id)
    match_html = ""
    if scored is not None:
        match = _match_factor(scored)
        match_text = (
            "—" if match is None or match.value is None else f"{round(match.value * 100)}%"
        )
        match_html = (
            "<div class='library-match'>"
            f"<span>Match <strong>{match_text}</strong></span>"
            f"<span>Recommendation <strong>{scored.display_score}</strong></span>"
            f"<span>Evidence <strong>{round(scored.coverage * 100)}%</strong></span>"
            "</div>"
        )
    st.markdown(
        "<div class='library-diff'>"
        "<div class='library-diff-head'>"
        f"<span class='library-diff-stars'>{html.escape(stars)}</span>"
        f"<span class='library-diff-version'>{html.escape(entry.version or 'unknown')}</span>"
        f"<span class='library-diff-mode'>{html.escape(entry.mode)}</span>"
        + (
            "<span class='library-preset-pick'>выбрана пресетом</span>"
            if preset_selected
            else ""
        )
        + f"<span class='library-state {tone}'>{html.escape(entry.short_state)}</span>"
        "</div>"
        f"<div class='library-diff-meta'>BPM {bpm} · AR {ar} · OD {od} · "
        f"{length}</div>"
        + _library_patterns_html(entry)
        + match_html
        + (
            "<div class='library-partial'>Результат покрывает только часть "
            "карты.</div>"
            if entry.is_partial
            else ""
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    with st.container(key=f"library_actions_{entry.beatmap_id}"):
        actions = st.columns([1.0, 0.72, 1.0])
        if actions[0].button(
            "Детали",
            key=f"library_details_{entry.beatmap_id}",
            use_container_width=True,
        ):
            _library_details_dialog(entry)
        actions[1].button(
            "▶",
            key=f"library_preview_{entry.beatmap_id}",
            help=(
                "Открыть реальное превью этой difficulty"
                if entry.mode == "osu"
                else "Превью поддерживается только для osu!standard"
            ),
            disabled=entry.mode != "osu",
            use_container_width=True,
            on_click=_set_library_preview,
            args=(entry,),
        )
        actions[2].link_button(
            "osu!",
            entry.beatmap_url,
            use_container_width=True,
        )
    selected_preview = st.session_state.get("library_preview_entry")
    if (
        isinstance(selected_preview, library.LibraryEntry)
        and selected_preview.beatmap_id == entry.beatmap_id
    ):
        _render_library_preview_panel(store, entry)


@st.fragment
def _render_library_set(
    store: Storage,
    set_item: library.LibrarySet,
    current_scores: dict[int, ScoredCandidate],
    *,
    preset_beatmap_id: int | None = None,
) -> None:
    with st.container(border=True, key=f"library_set_{set_item.beatmapset_id}"):
        st.markdown(
            _library_cover_html(
                cover_url=set_item.cover_url,
                title=set_item.display_title,
                status=set_item.set_status,
                difficulties=len(set_item.entries),
                url=set_item.set_url,
            ),
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='library-set-copy'>"
            f"<div class='library-set-title'><a href='{html.escape(set_item.set_url, quote=True)}' "
            f"target='_blank' rel='noopener noreferrer'>"
            f"{html.escape(set_item.display_title)}</a></div>"
            f"<div class='library-set-artist'>{html.escape(set_item.display_artist)}</div>"
            f"<div class='library-set-mapper'>маппер: {html.escape(set_item.creator or 'неизвестно')}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        visible = set_item.entries[:3]
        hidden = set_item.entries[3:]
        for entry in visible:
            _render_library_entry(
                store,
                entry,
                current_scores,
                preset_selected=entry.beatmap_id == preset_beatmap_id,
            )
        if hidden:
            with st.expander(
                f"Ещё {len(hidden)} сложностей",
                expanded=False,
                key=f"library_more_{set_item.beatmapset_id}",
            ):
                for entry in hidden:
                    _render_library_entry(
                        store,
                        entry,
                        current_scores,
                        preset_selected=entry.beatmap_id == preset_beatmap_id,
                    )


@st.dialog("Детали сложности", width="large")
def _library_details_dialog(entry: library.LibraryEntry) -> None:
    st.markdown(
        _library_cover_html(
            cover_url=entry.cover_url,
            title=entry.display_title,
            status=entry.effective_status,
            difficulties=0,
            url=entry.beatmap_url,
        ),
        unsafe_allow_html=True,
    )
    st.subheader(f"{entry.display_title} · [{entry.version or 'unknown'}]")
    st.caption(
        f"{entry.display_artist} · маппер: {entry.creator or 'неизвестно'}"
    )
    metrics = st.columns(4)
    metrics[0].metric(
        "Звёзды", f"{entry.stars:.2f}★" if entry.stars is not None else "?"
    )
    metrics[1].metric(
        "BPM", f"{entry.effective_bpm:.0f}" if entry.effective_bpm else "?"
    )
    metrics[2].metric("Длина", _format_seconds(entry.effective_length))
    metrics[3].metric(
        "AR / OD",
        f"{entry.ar:g} / {entry.od:g}"
        if entry.ar is not None and entry.od is not None
        else "?",
    )
    st.markdown(
        f"**Состояние анализа:** {html.escape(entry.state_label)}  \n"
        f"Источник: {html.escape(ANALYSIS_SOURCE_LABELS.get(entry.analysis_source, entry.analysis_source or '—'))} · "
        f"анализатор v{entry.analysis_version or '—'} · "
        f"проанализировано: {html.escape(entry.analyzed_at or '—')}  \n"
        f"Очередь: {html.escape(QUEUE_STATUS_LABELS.get(entry.queue_status, entry.queue_status or '—'))} · "
        f"попыток: {entry.analysis_attempts}/{entry.queue_attempts}"
    )
    if entry.analysis_scope:
        scope_text = (
            entry.analysis_scope.replace("objects", "объектов")
            .replace("sections", "секций")
        )
        st.caption(f"Покрытие: {scope_text}")
    if entry.queue_error:
        st.warning(f"Ошибка очереди: {entry.queue_error}")
    st.markdown(_library_patterns_html(entry), unsafe_allow_html=True)
    sequence = entry.sequence
    if sequence is not None:
        st.markdown("**Измеренные признаки .osu**")
        st.caption(
            f"Объектов: {sequence.analyzed_notes}/{sequence.total_notes}"
            + (" · анализ частичный" if sequence.truncated else "")
            + f" · прыжки {sequence.jump_share:.0%} · широкие "
            f"{sequence.wide_jump_share:.0%} · плавность {sequence.flow_share:.0%}"
            f" · углы {sequence.angle_change_share:.0%} · слайдеры "
            f"{sequence.slider_share:.0%} · плотность "
            f"{sequence.notes_per_second:.1f}/с"
        )
        if sequence.sections:
            st.markdown("**Секции**")
            for section in sequence.sections[:6]:
                labels = ", ".join(
                    PATTERN_LABELS.get(label, label)
                    for label in section.labels
                ) or "без выраженного паттерна"
                st.caption(
                    f"{_format_seconds(section.start_ms / 1000)}–"
                    f"{_format_seconds(section.end_ms / 1000)} · "
                    f"{labels} · объектов {section.object_count}"
                )
    else:
        st.info(
            "Для этой difficulty нет сохранённого sequence-анализа. "
            "Это не означает, что паттернов нет."
        )
    st.link_button("Открыть difficulty на osu!", entry.beatmap_url)


def _render_library_preview_panel(
    store: Storage, entry: library.LibraryEntry
) -> None:
    """Stable page-level preview; never covers navigation or the library."""

    with st.container(border=True, key="library_preview_panel"):
        heading = st.columns([5.0, 1.0])
        heading[0].markdown(
            "<div class='preview-heading'>"
            "<div class='preview-kicker'>ПРЕВЬЮ ГЕЙМПЛЕЯ</div>"
            f"<div class='preview-title'>{html.escape(entry.display_title)}"
            f" · [{html.escape(entry.version or 'unknown')}]</div>"
            f"<div class='preview-sub'>{html.escape(entry.display_artist)} · "
            f"{entry.stars:.2f}★ · {entry.beatmap_id}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        heading[1].button(
            "×",
            key="close_library_preview",
            help="Закрыть превью и вернуться к библиотеке",
            use_container_width=True,
            on_click=_clear_library_preview,
        )
        cache_path = (
            _gameplay_cache_dir(store) / f"{int(entry.beatmap_id)}.osu"
        )
        if cache_path.exists():
            st.caption(
                "Файл уже есть в локальном кэше: повторная загрузка и "
                "API-поиск не выполняются."
            )
        else:
            st.caption(
                "Публичный .osu будет загружен один раз и сохранён в "
                "локальном кэше для следующих превью."
            )
        try:
            with st.spinner("Загружаю .osu для превью…"):
                preview = gameplay.load_preview(
                    entry.beatmap_id, _gameplay_cache_dir(store)
                )
        except gameplay.GameplayError as exc:
            st.error(f"Превью недоступно: {exc}")
            st.caption(
                "Файл мог быть удалён или скрыт. Library, фильтры и "
                "остальные карточки не затронуты."
            )
            st.link_button("Открыть difficulty на osu!", entry.beatmap_url)
            return
        st.html(
            gameplay.preview_html(preview),
            unsafe_allow_javascript=True,
        )


def _library_counts_html(counts: dict[str, int]) -> str:
    return (
        "<div class='library-summary'>"
        f"<div><strong>{counts.get('total', 0):,}</strong><span>всего сложностей</span></div>"
        f"<div><strong>{counts.get('analyzed', 0):,}</strong><span>полный анализ</span></div>"
        f"<div><strong>{counts.get('partial', 0):,}</strong><span>частичный анализ</span></div>"
        f"<div><strong>{counts.get('pending', 0):,}</strong><span>в очереди</span></div>"
        f"<div><strong>{counts.get('failed', 0) + counts.get('unavailable', 0):,}</strong>"
        "<span>проблемы источника</span></div>"
        "</div>".replace(",", " ")
    )


def _library_preset_evaluation(
    store: Storage, cfg: PresetConfig
) -> library_preset.LibraryPresetResult:
    key = library_preset.preset_cache_signature(store, cfg)
    cache = st.session_state.get("_library_preset_cache")
    if (
        isinstance(cache, dict)
        and cache.get("key") == key
        and isinstance(cache.get("value"), library_preset.LibraryPresetResult)
    ):
        return cache["value"]
    with st.spinner("Применяю активный пресет к локальному каталогу…"):
        value = library_preset.evaluate_preset(store, cfg)
    st.session_state["_library_preset_cache"] = {
        "key": key,
        "value": value,
    }
    return value


def _library_clear_manual_filters() -> None:
    for key in (
        "library_query",
        "library_statuses",
        "library_other_patterns",
        "library_mode",
        "library_stars",
        "library_bpm",
        "library_length",
        "library_page",
    ):
        st.session_state.pop(key, None)


def page_library(store: Storage, cfg: PresetConfig) -> None:
    _page_intro(
        "LOCAL LIBRARY",
        "Библиотека карт",
        "Всё, что уже сохранено в SQLite: фильтруйте, сортируйте и открывайте "
        "анализ без обращения к osu! API. Обложки и превью — по требованию.",
    )
    try:
        counts = library.counts(store)
    except StorageError as exc:
        st.error(f"Локальный каталог недоступен: {exc}")
        return
    if counts.get("total", 0) == 0:
        st.info(
            "Локальный каталог пуст. Выполните поиск в osu! API или "
            "импортируйте данные — после этого карты появятся здесь."
        )
        return
    st.markdown(_library_counts_html(counts), unsafe_allow_html=True)
    st.caption(
        "Источник: SQLite. Счётчики синхронизированы с analysis_queue; "
        "фильтры и сортировка не делают сетевых запросов."
    )
    after_search = bool(
        st.session_state.pop("_library_after_search", False)
    )
    if after_search:
        st.info(
            "Поиск сохранил карты в SQLite. Открыт весь каталог без "
            "preset-гейта и сортировка «Недавно добавленные», чтобы новые "
            "карты были видны; не прошедшие анализ остаются в состоянии "
            "очереди/ожидания. Если включены ручные фильтры Библиотеки, "
            "сбросьте их, чтобы увидеть весь каталог."
        )

    scope = st.segmented_control(
        "Коллекция",
        options=list(library.SCOPE_OPTIONS),
        format_func=lambda value: library.SCOPE_LABELS[value],
        default="analyzed",
        key="library_scope",
    )
    scope = scope or "analyzed"
    preset_scope_supported = scope in {"analyzed", "all", "partial"}
    preset_row = st.columns([1.35, 3.65])
    preset_mode = preset_row[0].toggle(
        "Под активный пресет",
        value=True,
        key="library_preset_mode",
        help="Применяет hard filters, целевой гейт и ранжирование текущего "
        "пресета к локальному каталогу. API не используется.",
    )
    effective_preset = bool(preset_mode and preset_scope_supported)
    config_digest = hashlib.sha256(
        json.dumps(
            cfg.to_dict(), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    preset_filter_lines = active_filter_summary(cfg.filters)
    preset_filter_summary = "; ".join(preset_filter_lines[:4])
    if effective_preset:
        preset_row[1].caption(
            f"Пресет «{cfg.name}» · {preset_filter_summary}"
        )
    elif preset_mode and not preset_scope_supported:
        preset_row[1].caption(
            "Для очереди, недоступных источников и ошибок анализа пресетный "
            "гейт не применяется: показан ручной режим."
        )
    else:
        preset_row[1].caption("Ручной режим: показан локальный каталог.")

    if st.session_state.get("_library_last_effective_preset") != effective_preset:
        st.session_state["_library_last_effective_preset"] = effective_preset
        if not after_search:
            st.session_state["library_sort"] = (
                "preset_fit" if effective_preset else "recent_analysis"
            )
        st.session_state["library_page"] = 1

    relevant_patterns = pattern_filters.relevant_patterns(
        cfg.ranking.skill_weights, cfg.ranking.trait_targets
    )
    pattern_scope = scope in {"analyzed", "all", "partial"}
    default_patterns = (
        list(relevant_patterns)
        if effective_preset and pattern_scope
        else []
    )
    signature = (
        st.session_state.get("active_preset_id"),
        cfg.name,
        config_digest,
        pattern_scope,
        effective_preset,
        tuple(relevant_patterns),
    )
    if st.session_state.get("_library_pattern_signature") != signature:
        st.session_state["_library_pattern_signature"] = signature
        st.session_state["library_preset_patterns"] = default_patterns
        st.session_state.pop("library_patterns", None)
        if not pattern_scope:
            st.session_state.pop("library_other_patterns", None)
        st.session_state["library_page"] = 1
    st.session_state.setdefault(
        "library_preset_patterns", default_patterns
    )

    sort_options = list(library.SORT_OPTIONS)
    sort_labels = dict(library.SORT_LABELS)
    if effective_preset:
        sort_options = ["preset_fit", *sort_options]
        sort_labels = {"preset_fit": "Соответствие пресету", **sort_labels}
    if st.session_state.get("library_sort") not in sort_options:
        st.session_state["library_sort"] = (
            "preset_fit" if effective_preset else "recent_analysis"
        )
    shuffle_seed = st.session_state.get("library_shuffle_seed")
    with st.container(key="library_toolbar"):
        top = st.columns([2.3, 1.15, 0.9, 1.2])
        query = top[0].text_input(
            "Поиск по библиотеке",
            key="library_query",
            placeholder="название, артист, маппер или difficulty",
        )
        sort = top[1].selectbox(
            "Сортировка",
            options=sort_options,
            format_func=lambda value: sort_labels[value],
            key="library_sort",
            disabled=shuffle_seed is not None,
        )
        top[2].caption("Локально")
        top[2].caption("0 API-запросов")
        if top[3].button(
            "Перемешать заново" if shuffle_seed is not None else "Перемешать",
            key="library_shuffle",
            use_container_width=True,
            help="Случайный порядок всей подходящей локальной коллекции. "
            "Seed сохраняется между страницами и rerun.",
        ):
            st.session_state["library_shuffle_seed"] = secrets.randbits(64)
            st.session_state["library_page"] = 1
            st.rerun()
        pattern_row = st.columns([2.6, 1.4])
        preset_patterns = pattern_row[0].multiselect(
            "Паттерны пресета" if effective_preset else "Паттерны",
            options=list(relevant_patterns),
            format_func=pattern_filters.pattern_name,
            key="library_preset_patterns",
            placeholder=(
                "Любые паттерны"
                if not relevant_patterns
                else (
                    "Выберите паттерны пресета"
                    if effective_preset
                    else "Выберите паттерны"
                )
            ),
            disabled=not relevant_patterns or not pattern_scope,
            help="Показаны только метки, которые текущий анализатор .osu "
            "действительно умеет измерять и сохранять для целей пресета.",
        )
        pattern_row[1].caption(
            pattern_filters.preset_pattern_note(
                cfg.ranking.skill_weights, cfg.ranking.trait_targets
            )
        )
        pattern_row[1].caption(
            "Пустой выбор — без фильтра по паттернам."
            if pattern_scope
            else "Для очереди и ошибок паттерновый фильтр недоступен."
        )
        if shuffle_seed is not None:
            shuffle_cols = st.columns([1.2, 0.9, 3.0])
            if shuffle_cols[0].button(
                "Обычная сортировка",
                key="library_clear_shuffle",
                use_container_width=True,
            ):
                st.session_state.pop("library_shuffle_seed", None)
                st.session_state["library_page"] = 1
                st.rerun()
            shuffle_cols[1].caption("Случайный порядок")
            shuffle_cols[2].caption(
                f"Seed {int(shuffle_seed)} · порядок стабилен между страницами "
                "и rerun, пока вы не перемешаете снова."
            )
        else:
            st.caption(
                "Сортировка применяется ко всей отфильтрованной коллекции; "
                "«Перемешать» задаёт стабильный локальный порядок."
            )

    with st.expander("Фильтры библиотеки", expanded=False):
        filter_cols = st.columns([1.0, 1.0, 1.0])
        statuses = filter_cols[0].multiselect(
            "Статус",
            options=list(library.known_statuses(store)),
            format_func=lambda value: STATUS_RU.get(value, value),
            key="library_statuses",
            placeholder="Любой статус",
        )
        modes = filter_cols[1].selectbox(
            "Режим",
            options=["all", *library.known_modes(store)],
            format_func=lambda value: "Все режимы" if value == "all" else value,
            key="library_mode",
        )
        other_options = [
            label
            for label in pattern_filters.all_pattern_options()
            if label not in relevant_patterns
        ]
        if "library_other_patterns" in st.session_state:
            current_other = st.session_state.get("library_other_patterns") or []
            st.session_state["library_other_patterns"] = [
                item for item in current_other if item in other_options
            ]
        other_patterns = filter_cols[2].multiselect(
            "Другие паттерны",
            options=other_options,
            format_func=pattern_filters.pattern_name,
            key="library_other_patterns",
            placeholder=(
                "Все измеряемые паттерны"
                if pattern_scope
                else "Недоступно для этой коллекции"
            ),
            disabled=not pattern_scope,
            help="Полный список доступных меток анализатора. Он не ограничен "
            "целями активного пресета; пустой выбор не фильтрует.",
        )
        ranges = st.columns(3)
        stars_range = ranges[0].slider(
            "Звёзды",
            0.0,
            12.0,
            (0.0, 12.0),
            0.1,
            key="library_stars",
        )
        bpm_range = ranges[1].slider(
            "BPM",
            0.0,
            400.0,
            (0.0, 400.0),
            5.0,
            key="library_bpm",
        )
        length_range = ranges[2].slider(
            "Длина, секунд",
            0,
            600,
            (0, 600),
            15,
            key="library_length",
        )
        if st.button("Сбросить фильтры", key="library_clear_filters"):
            _library_clear_manual_filters()
            st.session_state["library_preset_patterns"] = list(
                default_patterns
            )
            st.session_state["library_page"] = 1
            st.rerun()

    selected_patterns = tuple(
        dict.fromkeys((*preset_patterns, *other_patterns))
    )
    criteria_signature = (
        query,
        scope,
        tuple(statuses),
        selected_patterns,
        modes,
        tuple(stars_range),
        tuple(bpm_range),
        tuple(length_range),
        sort,
        effective_preset,
        st.session_state.get("active_preset_id"),
        config_digest,
        shuffle_seed,
    )
    if st.session_state.get("_library_criteria_signature") != criteria_signature:
        st.session_state["_library_criteria_signature"] = criteria_signature
        st.session_state["library_page"] = 1
    filters = library.LibraryFilters(
        query=query,
        scope=scope,
        statuses=tuple(statuses),
        patterns=selected_patterns,
        mode=None if modes == "all" else modes,
        stars_min=stars_range[0] if stars_range[0] > 0 else None,
        stars_max=stars_range[1] if stars_range[1] < 12 else None,
        bpm_min=bpm_range[0] if bpm_range[0] > 0 else None,
        bpm_max=bpm_range[1] if bpm_range[1] < 400 else None,
        length_min=length_range[0] if length_range[0] > 0 else None,
        length_max=length_range[1] if length_range[1] < 600 else None,
        sort=sort,
        page=int(st.session_state.get("library_page", 1)),
        per_page=8,
        shuffle_seed=shuffle_seed,
    )
    preset_eval = (
        _library_preset_evaluation(store, cfg)
        if effective_preset
        else None
    )
    if preset_eval is not None:
        gate_text = (
            f"целевой гейт — {preset_eval.result.passed_relevance}"
            if preset_eval.target_gate
            else "целевой гейт не задан"
        )
        st.caption(
            f"Пресет «{cfg.name}»: hard filters — "
            f"{preset_eval.result.passed_filters}; {gate_text}. "
            + preset_eval.limitation
        )
        if preset_eval.missing_evidence:
            st.caption(
                f"{preset_eval.missing_evidence} карт не были оценены из-за "
                "недостатка анализа; это не подтверждённое несоответствие. "
                "Отключите «Под активный пресет», чтобы увидеть их в ручном "
                "режиме."
            )
    try:
        page = library.query_sets(
            store,
            filters,
            preset_order=(
                preset_eval.set_order if preset_eval is not None else None
            ),
            preset_beatmaps=(
                preset_eval.beatmap_by_set
                if preset_eval is not None
                else None
            ),
        )
    except StorageError as exc:
        st.error(f"Библиотека не загрузилась: {exc}")
        return
    st.session_state["library_page"] = page.page
    preview_entry = st.session_state.get("library_preview_entry")
    if isinstance(preview_entry, library.LibraryEntry):
        visible_ids = {
            entry.beatmap_id
            for set_item in page.sets
            for entry in set_item.entries
        }
        if preview_entry.beatmap_id not in visible_ids:
            st.session_state.pop("library_preview_entry", None)
    current = st.session_state.get("result")
    current_scores = (
        {item.beatmap.id: item for item in current.scored}
        if isinstance(current, DiscoveryResult)
        else {}
    )
    if preset_eval is not None:
        # Do not leak scores from an unrelated Overview search into preset
        # mode. Other difficulties remain browsable but are not given a score.
        current_scores = dict(preset_eval.scored_by_beatmap)
    st.markdown(
        "<div class='library-result-head'>"
        f"<strong>{page.total_sets:,}</strong> "
        f"{_ru_plural(page.total_sets, 'набор', 'набора', 'наборов')} · "
        f"<strong>{page.total_difficulties:,}</strong> "
        f"{_ru_plural(page.total_difficulties, 'сложность', 'сложности', 'сложностей')}"
        f"<span>Страница {page.page} из {page.pages}</span>"
        "</div>".replace(",", " "),
        unsafe_allow_html=True,
    )
    if preset_eval is not None:
        gate_phrase = (
            " и целевой гейт" if preset_eval.target_gate else ""
        )
        st.caption(
            f"Показаны beatmapsets, прошедшие hard filters{gate_phrase} "
            "пресета; на карточках — реальные score, coverage и недостающие "
            "факторы. Это не вероятность."
        )
    if shuffle_seed is not None:
        st.caption(
            "Активно стабильное перемешивание всей подходящей коллекции. "
            "Порядок не меняется при переходе по страницам."
        )
    if not page.sets:
        active_lines = []
        if preset_eval is not None:
            active_lines.append(
                f"пресет «{cfg.name}»: {preset_eval.limitation}"
            )
        elif preset_mode:
            active_lines.append(
                "режим «Под активный пресет» не применяется к этой коллекции"
            )
        active_lines.append(f"коллекция: {library.SCOPE_LABELS[scope]}")
        if query:
            active_lines.append(f"поиск: «{query}»")
        if selected_patterns:
            active_lines.append(
                "паттерны: "
                + ", ".join(
                    pattern_filters.pattern_name(item)
                    for item in selected_patterns
                )
            )
        if modes != "all":
            active_lines.append(f"режим карт: {modes}")
        if statuses:
            active_lines.append("статусы: " + ", ".join(statuses))
        if stars_range != (0.0, 12.0):
            active_lines.append(
                f"звёзды: {stars_range[0]:g}..{stars_range[1]:g}"
            )
        if bpm_range != (0.0, 400.0):
            active_lines.append(
                f"BPM: {bpm_range[0]:g}..{bpm_range[1]:g}"
            )
        if length_range != (0, 600):
            active_lines.append(
                f"длина: {length_range[0]}..{length_range[1]} с"
            )
        st.info(
            "По этим фильтрам в локальном каталоге ничего нет. "
            "Активные ограничения: " + "; ".join(active_lines) + "."
        )
        empty_actions = st.columns([1.2, 1.2, 2.6])
        if empty_actions[0].button(
            "Сбросить ручные фильтры",
            key="library_empty_reset",
            use_container_width=True,
        ):
            _library_clear_manual_filters()
            st.session_state["library_preset_patterns"] = list(
                default_patterns
            )
            st.session_state["library_page"] = 1
            st.rerun()
        if empty_actions[1].button(
            "Показать без пресета",
            key="library_empty_disable_preset",
            disabled=not preset_mode,
            use_container_width=True,
        ):
            st.session_state["library_preset_mode"] = False
            st.session_state["library_page"] = 1
            st.rerun()
        empty_actions[2].caption(
            "Если карты не проходят целевой гейт, отключите пресет: "
            "недостаток анализа не считается подтверждённым несоответствием."
        )
        return
    for start in range(0, len(page.sets), 2):
        columns = st.columns(2, gap="large")
        for offset, set_item in enumerate(page.sets[start : start + 2]):
            with columns[offset]:
                _render_library_set(
                    store,
                    set_item,
                    current_scores,
                    preset_beatmap_id=(
                        preset_eval.beatmap_by_set.get(set_item.beatmapset_id)
                        if preset_eval is not None
                        else None
                    ),
                )
    navigation = st.columns([1.0, 1.0, 2.5, 1.0, 1.0])
    if navigation[0].button(
        "← Назад",
        key="library_prev",
        disabled=page.page <= 1,
        use_container_width=True,
    ):
        st.session_state["library_page"] = page.page - 1
        st.rerun()
    if navigation[1].button(
        "Вперёд →",
        key="library_next",
        disabled=page.page >= page.pages,
        use_container_width=True,
    ):
        st.session_state["library_page"] = page.page + 1
        st.rerun()
    navigation[2].caption(
        f"Страница {page.page} из {page.pages} · "
        f"{page.total_sets} beatmapsets / {page.total_difficulties} difficulty"
    )
    if navigation[3].button(
        "В начало",
        key="library_first",
        disabled=page.page <= 1,
        use_container_width=True,
    ):
        st.session_state["library_page"] = 1
        st.rerun()
    if navigation[4].button(
        "В конец",
        key="library_last",
        disabled=page.page >= page.pages,
        use_container_width=True,
    ):
        st.session_state["library_page"] = page.pages
        st.rerun()


def page_analysis(store: Storage, cfg: PresetConfig) -> None:
    _page_intro(
        "ANALYSIS",
        "Анализ и очередь",
        "Постоянный worker разбирает каталог в фоне. Здесь видны прогресс, "
        "ошибки и управление очередью — без сетевой работы в rerun-пути.",
    )
    worker = analysis_worker.worker_status(store)
    queue = worker.get("queue", {})
    beatmaps = max(1, int(worker.get("beatmaps", 0)))
    analyzed = int(worker.get("analyzed", 0))
    partial = int(worker.get("partial", 0))
    progress = min(1.0, analyzed / beatmaps)

    metrics = st.columns(4)
    metrics[0].metric(
        "Проанализировано",
        f"{analyzed}/{beatmaps}",
        delta=f"{partial} частично" if partial else None,
    )
    metrics[1].metric(
        "В очереди",
        int(queue.get("pending", 0)) + int(queue.get("processing", 0)),
    )
    metrics[2].metric("Ошибок", int(queue.get("failed", 0)))
    metrics[3].metric(
        "Worker",
        "пауза"
        if worker.get("paused")
        else ("работает" if worker.get("thread_alive") else "ожидает"),
    )
    st.progress(progress, text=f"Покрытие анализа: {progress:.0%}")
    st.caption(
        f"API: {'настроен' if worker.get('api_configured') else 'не настроен'} · "
        f"владелец: {worker.get('owner') or '—'} · "
        f"heartbeat: {round(worker.get('heartbeat_age') or 0)} с назад · "
        f"последний автопоиск: {worker.get('last_discovery_at') or '—'}"
    )
    if worker.get("last_error"):
        st.warning("Последняя ошибка worker: " + str(worker["last_error"]))
    if not worker.get("enabled"):
        st.info(
            "Фоновый worker отключён переменной OSU_PICKER_WORKER=0. "
            "Кнопки ниже всё равно меняют сохранённое состояние очереди."
        )

    controls = st.columns([1.0, 1.0, 1.0])
    if controls[0].button(
        "▶ Возобновить" if worker.get("paused") else "⏸ Пауза",
        key="analysis_toggle_pause",
        use_container_width=True,
    ):
        analysis_worker.set_paused(store, not bool(worker.get("paused")))
        _flash("Фоновая очередь обновлена.")
        st.rerun()
    if controls[1].button(
        "Переочередить каталог",
        key="analysis_requeue",
        use_container_width=True,
    ):
        try:
            added = analysis_worker.enqueue_catalog(
                store,
                allow_download=bool(worker.get("api_configured")),
            )
        except Exception as exc:  # noqa: BLE001 - UI boundary
            st.error(f"Очередь не обновлена: {type(exc).__name__}")
        else:
            _flash(f"В очередь добавлено/обновлено: {added}.")
            st.rerun()
    if controls[2].button(
        "Обновить каталог сейчас",
        key="analysis_discovery_now",
        use_container_width=True,
        disabled=not worker.get("api_configured"),
        help="Ставит bounded-запрос discovery в очередь worker. "
        "Кнопка не выполняет сетевой запрос во время отрисовки.",
    ):
        store.request_worker_discovery()
        _flash("Запрос автоматического discovery поставлен worker.")
        st.rerun()

    left, right = st.columns([1.0, 1.0])
    with left:
        st.subheader("Очередь")
        st.dataframe(
            [
                {
                    "Состояние": QUEUE_STATUS_LABELS.get(key, key),
                    "Карт": value,
                }
                for key, value in sorted(queue.items())
                if key != "total"
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(f"Всего строк очереди: {queue.get('total', 0)}.")
    with right:
        st.subheader("Сохранённый анализ")
        analysis_stats = worker.get("analysis", {})
        if analysis_stats:
            st.dataframe(
                [
                    {
                        "Состояние": evidence.label(key),
                        "Карт": value,
                    }
                    for key, value in sorted(analysis_stats.items())
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info(
                "Анализ ещё не сохранён. Worker начнёт с локальных .osu, "
                "если они есть; отсутствующие файлы появятся в очереди "
                "только при доступном API."
            )
        failures = store.queue_failures(limit=8)
        if failures:
            st.markdown("**Последние проблемы**")
            st.dataframe(
                [
                    {
                        "Карта": f"{row.get('artist', '')} — "
                        f"{row.get('title', '')}",
                        "Статус": row.get("status"),
                        "Попыток": row.get("attempts"),
                        "Ошибка": row.get("last_error") or "—",
                    }
                    for row in failures
                ],
                hide_index=True,
                width="stretch",
            )
    st.caption(
        "Pending — ещё не обработано; processing — взято worker; done — "
        "результат сохранён; unavailable — нет .osu без API или источник "
        "недоступен; failed — исчерпаны bounded-попытки. Пустой анализ не "
        "подменяется отрицательным результатом."
    )


def page_presets(store: Storage, cfg: PresetConfig) -> None:
    st.header("📦 Пресеты")
    st.caption(
        "Встроенные пресеты — обычные записи в SQLite: их можно менять, "
        "переименовывать, дублировать и удалять. Это не затрагивает оценки "
        "и избранное."
    )
    all_items = store.list_presets()
    active_id = st.session_state["active_preset_id"]
    items = presets.visible_presets(store, include_id=active_id)

    st.subheader("Каталог по группам")
    st.caption(
        "Explore — открывать, Farm — короткие NM-aim-карты, Train — "
        "конкретный навык, Custom — ваши профили. Поиск ищет по имени, "
        "описанию и названию группы."
    )
    search_cols = st.columns([3.2, 1.0])
    preset_query = search_cols[0].text_input(
        "Поиск пресета",
        key="preset_catalog_search",
        placeholder="Aim, streams, reading, farm, custom…",
    )
    visible_items = catalog.search_presets(items, preset_query)
    if search_cols[1].button(
        "Сбросить поиск",
        key="preset_catalog_clear",
        disabled=not bool(preset_query),
        use_container_width=True,
    ):
        st.session_state["preset_catalog_search"] = ""
        st.rerun()
    grouped = catalog.group_presets_by_category(visible_items)
    if not visible_items:
        st.warning("По этому запросу пресетов нет.")
    for group in catalog.PRESET_GROUPS:
        group_items = grouped.get(group.id, [])
        if not group_items and group.id != "custom":
            continue
        has_active = any(item.id == active_id for item in group_items)
        with st.expander(
            f"{group.icon} {group.label} · {len(group_items)}",
            expanded=has_active or bool(preset_query.strip()),
        ):
            st.caption(group.description)
            if not group_items:
                st.info(
                    "Пользовательских пресетов пока нет. Создайте профиль из "
                    "любого готового набора ниже."
                )
            for item in group_items:
                row = st.columns([4.3, 1.05])
                active_mark = " · **активный**" if item.id == active_id else ""
                builtin_mark = (
                    "встроенный" if item.is_builtin else "пользовательский"
                )
                with row[0]:
                    st.markdown(f"**{item.name}**{active_mark}")
                    description = " ".join(item.config.description.split())
                    if len(description) > 190:
                        description = description[:187].rstrip() + "…"
                    st.caption(f"{builtin_mark} · {description}")
                with row[1]:
                    if item.id == active_id:
                        st.caption("Выбран")
                    elif st.button(
                        "Активировать",
                        key=f"preset_group_activate_{item.id}",
                        use_container_width=True,
                    ):
                        try:
                            if _config_is_dirty(store, cfg):
                                st.session_state["_pending_preset_id"] = item.id
                                st.rerun()
                            _apply_preset(store, item.id)
                            st.rerun()
                        except StorageError as exc:
                            st.error(str(exc))

    legacy_items = presets.legacy_presets(store)
    if legacy_items:
        with st.expander(
            f"🗄 Legacy built-ins · {len(legacy_items)} сохранено", expanded=False
        ):
            st.caption(
                "Старые встроенные профили скрыты из основного каталога, но "
                "не удалены и не перезаписаны. Их можно открыть, "
                "дублировать или использовать как основу для Custom."
            )
            for item in legacy_items:
                legacy_row = st.columns([4.3, 1.05])
                with legacy_row[0]:
                    st.markdown(f"**{item.name}**")
                    legacy_description = " ".join(item.config.description.split())
                    if len(legacy_description) > 180:
                        legacy_description = (
                            legacy_description[:177].rstrip() + "…"
                        )
                    st.caption(legacy_description)
                with legacy_row[1]:
                    if item.id == active_id:
                        st.caption("Выбран")
                    elif st.button(
                        "Открыть",
                        key=f"preset_legacy_activate_{item.id}",
                        use_container_width=True,
                    ):
                        try:
                            if _config_is_dirty(store, cfg):
                                st.session_state["_pending_preset_id"] = item.id
                                st.rerun()
                            _apply_preset(store, item.id)
                            st.rerun()
                        except StorageError as exc:
                            st.error(str(exc))

    st.subheader("Текущий пресет")
    with st.form("preset_rename_form"):
        new_name = st.text_input("Новое имя", value=cfg.name, key="preset_new_name")
        rename = st.form_submit_button(
            "Переименовать", key="preset_rename_submit"
        )
    if rename:
        try:
            saved = presets.rename_preset(store, active_id, new_name)
            # Keep unsaved filters and skills in the session; only the name
            # belongs to the database row that was just renamed.
            cfg.name = saved.name
            st.session_state["active_config"] = cfg
            _flash("Пресет переименован.")
            st.rerun()
        except (PresetError, StorageError) as exc:
            st.error(str(exc))

    action_cols = st.columns(3)
    if action_cols[0].button("Дублировать", key="preset_duplicate"):
        try:
            if _config_is_dirty(store, cfg):
                # Duplicate what the user currently sees, not the stale saved
                # copy, so this action cannot silently discard unsaved edits.
                copy_cfg = copy.deepcopy(cfg)
                copy_cfg.name = f"{cfg.name} copy"
                copy_cfg.description = cfg.description
                presets.validate_preset_config(copy_cfg)
                created = store.create_preset(copy_cfg, is_builtin=False)
            else:
                created = presets.duplicate_preset(store, active_id)
            _flash(f"Создан «{created.name}».")
            _activate(store, created.id)
        except (PresetError, StorageError) as exc:
            st.error(str(exc))
    if action_cols[1].button("Сбросить к defaults", key="preset_reset"):
        try:
            saved = presets.reset_preset_to_default(store, active_id)
            st.session_state["active_config"] = PresetConfig.from_dict(
                saved.config.to_dict()
            )
            st.session_state["_reset_editors"] = True
            _flash("Пресет сброшен к значениям по умолчанию.")
            st.rerun()
        except (PresetError, StorageError) as exc:
            st.error(str(exc))
    delete_ok = action_cols[2].checkbox(
        "Подтвердить удаление", key="preset_delete_confirm"
    )
    if action_cols[2].button(
        "Удалить текущий", key="preset_delete", disabled=not delete_ok
    ):
        try:
            if len(all_items) <= 1:
                raise PresetError("Нельзя удалить последний пресет")
            store.delete_preset(active_id)
            remaining = presets.visible_presets(store)
            if not remaining:
                remaining = store.list_presets()
            fallback = next(
                (item for item in remaining if item.name == "Mixed Aim"),
                remaining[0],
            )
            _apply_preset(store, fallback.id)
            _flash("Пресет удалён; оценки и избранное сохранены.")
            st.rerun()
        except (PresetError, StorageError) as exc:
            st.error(str(exc))

    st.subheader("Создать пресет")
    with st.form("preset_create_form"):
        created_name = st.text_input("Имя нового пресета", key="preset_create_name")
        base_id = st.selectbox(
            "Взять за основу",
            options=[None] + [item.id for item in all_items],
            format_func=lambda pid: "С текущими настройками"
            if pid is None
            else next(
                (item.name for item in all_items if item.id == pid), str(pid)
            ),
            key="preset_base",
        )
        create = st.form_submit_button("Создать", key="preset_create_submit")
    if create:
        try:
            name = created_name.strip()
            if not name:
                raise PresetError("Имя пресета не может быть пустым")
            if store.get_preset_by_name(name) is not None:
                raise PresetError(f"Пресет с именем «{name}» уже существует")
            if base_id is None:
                new_config = PresetConfig.from_dict(cfg.to_dict())
                new_config.name = name
                new_config.description = (
                    "Создан из текущих несохранённых настроек."
                )
                presets.validate_preset_config(new_config)
                created = store.create_preset(new_config, is_builtin=False)
            else:
                created = presets.create_preset(store, name, base_id)
            _flash(f"Создан «{created.name}».")
            _activate(store, created.id)
        except (PresetError, StorageError) as exc:
            st.error(str(exc))

    st.subheader("JSON: экспорт и импорт")
    export_choice = st.selectbox(
        "Что экспортировать",
        options=["Текущий", "Все"],
        key="preset_export_choice",
    )
    selected_presets = (
        [store.get_preset(active_id)]
        if export_choice == "Текущий"
        else items
    )
    selected_presets = [item for item in selected_presets if item is not None]
    try:
        export_text = presets.export_presets_json(selected_presets)
        st.download_button(
            "Скачать пресеты (JSON)",
            data=export_text.encode("utf-8"),
            file_name="osu-picker-presets.json",
            mime="application/json",
            key="preset_export_download",
        )
    except (PresetError, ValueError) as exc:
        st.error(f"Экспорт не готов: {exc}")

    uploaded = st.file_uploader(
        "Импортировать JSON пресетов", type=["json"], key="preset_import_file"
    )
    if uploaded is not None:
        raw = uploaded.getvalue()
        if len(raw) > 2_000_000:
            st.error("Файл слишком большой (лимит 2 МБ).")
        else:
            text = raw.decode("utf-8", errors="replace")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if st.session_state.get("preset_import_digest") != digest:
                st.session_state["preset_import_digest"] = digest
                try:
                    parsed = presets.import_presets_json(text)
                except (PresetError, UnicodeError) as exc:
                    st.session_state.pop("preset_import_payload", None)
                    st.error(f"Импорт отклонён: {exc}")
                else:
                    st.session_state["preset_import_payload"] = [
                        item.to_dict() for item in parsed
                    ]
            payload = st.session_state.get("preset_import_payload")
            if payload:
                st.write("Проверено, но ещё не записано:")
                st.dataframe(
                    [
                        {
                            "Название": item["name"],
                            "Описание": item.get("description", ""),
                        }
                        for item in payload
                    ],
                    hide_index=True,
                    width="stretch",
                )
                if st.button("Импортировать эти пресеты", key="preset_import_confirm"):
                    created_count = 0
                    try:
                        for raw_cfg in payload:
                            imported = PresetConfig.from_dict(raw_cfg)
                            presets.validate_preset_config(imported)
                            name = imported.name
                            existing = store.get_preset_by_name(name)
                            if existing is not None:
                                base = f"{name} (imported)"
                                name = base
                                suffix = 2
                                while store.get_preset_by_name(name) is not None:
                                    name = f"{base} {suffix}"
                                    suffix += 1
                                imported.name = name
                            store.create_preset(imported, is_builtin=False)
                            created_count += 1
                    except (PresetError, StorageError, ValueError) as exc:
                        st.error(f"Импорт остановлен: {exc}")
                    else:
                        st.session_state.pop("preset_import_payload", None)
                        st.session_state.pop("preset_import_digest", None)
                        st.session_state["_drop_import_uploaders"] = True
                        _flash(f"Импортировано пресетов: {created_count}.")
                        st.rerun()

    st.subheader("Восстановление встроенных")
    st.caption(
        "Восстановление создаёт отсутствующие встроенные пресеты и возвращает "
        "существующие встроенные к заводским настройкам. Ваши собственные "
        "пресеты не удаляются."
    )
    restore_ok = st.checkbox(
        "Подтвердить восстановление встроенных", key="preset_restore_confirm"
    )
    if st.button(
        "Восстановить встроенные",
        key="preset_restore",
        disabled=not restore_ok,
    ):
        try:
            stats = presets.restore_builtin_presets(store)
        except (PresetError, StorageError) as exc:
            st.error(str(exc))
        else:
            _flash(
                f"Встроенные: создано {stats['created']}, обновлено {stats['updated']}."
            )
            st.session_state["active_config"] = _load_active(store, active_id)
            st.session_state["_reset_editors"] = True
            st.rerun()


# ---------------------------------------------------------------------------
# Settings and taste profile
# ---------------------------------------------------------------------------


def page_feedback(store: Storage) -> None:
    st.header("⭐ Ваши оценки")
    st.caption(
        "Оценки разделены по смыслу: музыка не штрафует навыки, «слишком "
        "сложно» не считается «не нравится», «уже играл» и «скрыть» — рабочие "
        "сигналы. Вкус строится только из ваших оценок; готовых артистов, "
        "жанров или песен приложение не подставляет."
    )
    feedback = store.all_feedback()
    sets = store.get_beatmapsets(feedback.keys())
    profile = build_taste_profile(feedback, sets)

    metrics = st.columns(4)
    metrics[0].metric("Оценённых карт", profile.rated_sets)
    metrics[1].metric("Уверенность", profile.confidence)
    metrics[2].metric("Позитивных", len(profile.liked))
    metrics[3].metric("Негативных", len(profile.disliked))
    if not profile.enough_data:
        st.warning(
            "Данных мало: вкус не используется как сильный сигнал. Нужно хотя "
            "бы 3 содержательные оценки (не только «слишком сложно/легко»)."
        )
    for note in profile.notes:
        st.info(note)

    cols = st.columns(3)
    with cols[0]:
        st.markdown("**Любимые артисты**")
        rows = profile.top_artists()
        st.write(
            "\n".join(f"- {name}: {value:+.2f}" for name, value in rows)
            if rows
            else "Пока нет данных."
        )
    with cols[1]:
        st.markdown("**Любимые мапперы**")
        rows = profile.top_creators()
        st.write(
            "\n".join(f"- {name}: {value:+.2f}" for name, value in rows)
            if rows
            else "Пока нет данных."
        )
    with cols[2]:
        st.markdown("**Жанры**")
        rows = profile.top_genres()
        st.write(
            "\n".join(
                f"- {GENRES.get(gid, gid)}: {value:+.2f}" for gid, value in rows
            )
            if rows
            else "Пока нет данных."
        )

    if profile.skill_affinity and profile.enough_data:
        st.markdown("**Навыки, связанные с вашими оценками**")
        st.caption(
            "Мягкий сигнал: учитываются верхние навыки оценённых карт. "
            "Одна оценка не делает сильного вывода; фактор вкуса применяется "
            "только при ≥3 содержательных оценках."
        )
        st.write(
            "\n".join(
                f"- {SKILLS[key].name if key in SKILLS else key}: {value:+.2f} "
                f"(оценок: {profile.skill_counts.get(key, 0)})"
                for key, value in profile.top_skills()
            )
        )
    elif profile.skill_affinity:
        st.caption(
            "Есть предварительная связь оценок с навыками, но данных меньше 3 "
            "карт: предпочтения по навыкам не применяются как сильный сигнал."
        )
    else:
        st.caption(
            "Навыки пока не связаны с оценками: либо нет оценок, либо у карт "
            "не хватает измеримых признаков."
        )

    if profile.ignored_verdicts:
        st.caption(
            "Не влияют на вкус: "
            + ", ".join(
                VERDICT_LABELS.get(v, v) for v in profile.ignored_verdicts
            )
        )

    st.subheader("Изменить оценки")
    rows = store.feedback_rows()
    if not rows:
        st.info("Оценок пока нет. Поставьте их на карточках в поиске.")
    else:
        st.dataframe(
            [
                {
                    "Карта": f"{row['artist']} — {row['title']}",
                    "Маппер": row["creator"],
                    "Оценки": verdict_summary(
                        tuple(
                            item["verdict"]
                            for item in rows
                            if item["beatmapset_id"] == row["beatmapset_id"]
                        )
                    ),
                    "Обновлено": row["updated_at"],
                }
                for row in rows
            ],
            hide_index=True,
            width="stretch",
        )
        set_ids = sorted({int(row["beatmapset_id"]) for row in rows})
        selected_set = st.selectbox(
            "Карта для редактирования",
            options=set_ids,
            format_func=lambda sid: (
                f"{sets[sid].display_artist} — {sets[sid].display_title}"
                if sid in sets
                else f"#{sid}"
            ),
            key="taste_edit_set",
        )
        current = store.get_feedback(selected_set)
        edited = st.multiselect(
            "Оценки (сохранение ниже)",
            options=list(VERDICT_ORDER),
            default=list(current),
            format_func=lambda value: VERDICT_LABELS.get(value, value),
            key=f"taste_edit_verdicts_{selected_set}",
        )
        save_cols = st.columns(2)
        if save_cols[0].button("Сохранить оценки", key="taste_save"):
            try:
                store.set_feedback(selected_set, edited)
                _flash("Оценки обновлены.")
                st.rerun()
            except StorageError as exc:
                st.error(str(exc))
        if save_cols[1].button("Удалить все оценки карты", key="taste_delete"):
            try:
                store.remove_feedback(selected_set)
                _flash("Оценки карты удалены (избранное не затронуто).")
                st.rerun()
            except StorageError as exc:
                st.error(str(exc))

    with st.expander("Что означает каждая оценка", expanded=False):
        st.dataframe(
            [
                {
                    "Оценка": VERDICT_LABELS[verdict],
                    "Эффект": VERDICT_HELP[verdict],
                }
                for verdict in VERDICT_ORDER
            ],
            hide_index=True,
            width="stretch",
        )


# ---------------------------------------------------------------------------
# Settings hub
# ---------------------------------------------------------------------------


def page_settings(store: Storage, cfg: PresetConfig) -> None:
    _page_intro(
        "WORKSPACE",
        "Настройки",
        "Пресеты, навыки, фильтры, ранжирование, оформление и диагностика.",
    )
    st.caption(
        "Пресеты, навыки, строгие фильтры, ранжирование, ваши оценки, "
        "оформление и диагностика собраны здесь. Обычный экран поиска "
        "остаётся коротким."
    )
    default = st.session_state.get("settings_section", "presets")
    if default not in SETTINGS_SECTIONS:
        default = "presets"
        st.session_state["settings_section"] = default
    selected = st.selectbox(
        "Раздел настроек",
        options=list(SETTINGS_SECTIONS),
        index=list(SETTINGS_SECTIONS).index(default),
        format_func=lambda value: SETTINGS_LABELS.get(value, value),
        key="settings_section",
    )
    st.divider()

    if selected == "presets":
        page_presets(store, cfg)
    elif selected == "skillsets":
        page_skillsets(store, cfg)
    elif selected == "filters":
        page_filters(cfg)
    elif selected == "ranking":
        page_ranking(store, cfg)
    elif selected == "feedback":
        page_feedback(store)
    elif selected == "appearance":
        page_appearance(store)
    elif selected == "diagnostics":
        page_diagnostics(store, cfg)

    _render_dirty_bar(store, cfg)


# ---------------------------------------------------------------------------
# History, favorites and full data import
# ---------------------------------------------------------------------------


def _link_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        set_id = int(row["beatmapset_id"])
        result.append(
            {
                **row,
                "Карта": f"{row.get('artist', '')} — {row.get('title', '')}",
                "Ссылка": config.BEATMAPSET_URL.format(id=set_id),
            }
        )
    return result


def _render_favorites(store: Storage) -> None:
    favorites = _link_rows(store.favorite_rows())
    if not favorites:
        st.info(
            "Избранного пока нет. Добавляйте карты на карточках обзора или "
            "библиотеки."
        )
        return
    fav_ids = [int(row["beatmapset_id"]) for row in favorites]
    current = st.session_state.get("result")
    current_scores = (
        {item.beatmap.id: item for item in current.scored}
        if isinstance(current, DiscoveryResult)
        else {}
    )
    page = library.query_sets(
        store,
        library.LibraryFilters(
            scope="all",
            set_ids=tuple(fav_ids),
            per_page=48,
            sort="stars_desc",
        ),
    )
    if page.sets:
        for start in range(0, len(page.sets), 2):
            columns = st.columns(2, gap="large")
            for offset, set_item in enumerate(page.sets[start : start + 2]):
                with columns[offset]:
                    _render_library_set(store, set_item, current_scores)
    else:
        st.info("В избранном есть наборы, но в локальном каталоге нет их difficulty.")
    remove_id = st.selectbox(
        "Убрать из избранного",
        options=fav_ids,
        format_func=lambda sid: next(
            (row["Карта"] for row in favorites if row["beatmapset_id"] == sid),
            str(sid),
        ),
        key="history_remove_favorite",
    )
    if st.button("Убрать выбранное из избранного", key="history_remove_fav_btn"):
        try:
            store.set_favorite(remove_id, False)
            _flash("Удалено из избранного.")
            st.rerun()
        except StorageError as exc:
            st.error(str(exc))


def page_favorites(store: Storage) -> None:
    _page_intro(
        "YOUR PICKS",
        "Избранное",
        "Сохранённые карты не смешиваются с оценками и не удаляются при "
        "очистке кеша.",
    )
    _render_favorites(store)


def page_history(store: Storage) -> None:
    _page_intro(
        "ACTIVITY",
        "История",
        "Просмотренные карты и показанные результаты. История не удаляется "
        "при очистке кеша и не влияет на строгие фильтры напрямую.",
    )
    history = _link_rows(store.history_rows(limit=200))
    feedback_by_set = store.all_feedback()

    st.metric("Показов в истории", store.history_count())

    st.subheader("Недавние показы")
    if history:
        st.dataframe(
            [
                {
                    "Карта": row["Карта"],
                    "Маппер": row["creator"],
                    "Когда": row["shown_at"],
                    "Поверхность": row["surface"],
                    "Оценка": verdict_summary(
                        feedback_by_set.get(int(row["beatmapset_id"]), ())
                    )
                    or "—",
                    "Score": row["rank_score"],
                    "Позиция": row["position"],
                    "Ссылка": row["Ссылка"],
                }
                for row in history
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="osu!")
            },
        )
    else:
        st.info("История пуста: она заполняется после успешного поиска.")

    st.subheader("Полный экспорт и импорт данных")
    st.caption(
        "JSON содержит пресеты, оценки, избранное, историю и метаданные карт. "
        "Секреты API туда не попадают. Импорт сначала проверяется целиком."
    )
    try:
        payload = store.export_user_data()
        st.download_button(
            "Скачать полный экспорт",
            data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="osu-picker-user-data.json",
            mime="application/json",
            key="history_export_full",
        )
    except (StorageError, ValueError) as exc:
        st.error(f"Экспорт недоступен: {exc}")

    uploaded = st.file_uploader(
        "Импортировать полный экспорт", type=["json"], key="history_import_file"
    )
    if uploaded is not None:
        raw = uploaded.getvalue()
        if len(raw) > 5_000_000:
            st.error("Файл слишком большой (лимит 5 МБ).")
        else:
            text = raw.decode("utf-8", errors="replace")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if st.session_state.get("history_import_digest") != digest:
                st.session_state["history_import_digest"] = digest
                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    st.session_state.pop("history_import_payload", None)
                    st.error(f"Это не корректный JSON: {exc}")
                else:
                    if not isinstance(parsed, dict):
                        st.session_state.pop("history_import_payload", None)
                        st.error("Ожидался JSON-объект полного экспорта.")
                    else:
                        st.session_state["history_import_payload"] = parsed
            pending = st.session_state.get("history_import_payload")
            if isinstance(pending, dict):
                st.write(
                    "Файл прочитан, но ещё не применён: "
                    f"presets={len(pending.get('presets', []))}, "
                    f"feedback={len(pending.get('feedback', []))}, "
                    f"favorites={len(pending.get('favorites', []))}, "
                    f"history={len(pending.get('history', []))}."
                )
                overwrite = st.checkbox(
                    "Перезаписывать одноимённые пресеты",
                    value=False,
                    key="history_import_overwrite",
                )
                if st.button(
                    "Применить импорт (проверка до записи)",
                    key="history_import_confirm",
                ):
                    try:
                        stats = store.import_user_data(
                            pending, overwrite_presets=overwrite
                        )
                    except (UserDataImportError, StorageError, ValueError) as exc:
                        st.error(f"Импорт отклонён, данные не изменены: {exc}")
                    else:
                        st.session_state.pop("history_import_payload", None)
                        st.session_state.pop("history_import_digest", None)
                        st.session_state["_drop_import_uploaders"] = True
                        st.session_state["result"] = None
                        st.session_state["active_preset_id"] = _choose_preset(store)
                        st.session_state["active_config"] = _load_active(
                            store, st.session_state["active_preset_id"]
                        )
                        st.session_state["_reset_editors"] = True
                        _flash(
                            "Импорт применён: "
                            f"карт {stats.get('beatmapsets', 0)}, "
                            f"пресетов {stats.get('presets_created', 0)}."
                        )
                        st.rerun()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def page_appearance(store: Storage) -> None:
    st.header("🎨 Оформление")
    st.caption(
        "Тема, фон и акцент меняют только внешний вид. Они не входят в пресеты "
        "поиска, не меняют фильтры, навыки, ранжирование и не запускают поиск."
    )
    settings = appearance.load_appearance(store)
    current_path = appearance.background_path(store, settings)
    if settings.background_file and current_path is None:
        st.warning(
            "Файл активного фона не найден. Оформление применится без фона; "
            "загрузите изображение заново или удалите ссылку."
        )

    theme_labels = {
        theme_id: (
            f"{ui_theme.THEME_LABELS[theme_id]} · "
            f"{ui_theme.THEME_DESCRIPTIONS[theme_id]}"
        )
        for theme_id in appearance.THEMES
    }
    theme = st.radio(
        "Тема",
        options=list(appearance.THEMES),
        index=list(appearance.THEMES).index(settings.theme)
        if settings.theme in appearance.THEMES
        else 0,
        format_func=lambda value: theme_labels.get(value, value),
        horizontal=True,
        key=_appearance_widget_key("theme"),
        help="Тема не зависит от фона: любой wallpaper, акцент и затемнение "
        "работают во всех трёх темах.",
    )

    accent = st.color_picker(
        "Акцентный цвет",
        value=settings.accent,
        key=_appearance_widget_key("accent"),
        help="Кнопки, ссылки, фокус, активные вкладки и акцентный цвет.",
    )

    controls_left, controls_right = st.columns([1.15, 1.0])
    with controls_left:
        fit = st.selectbox(
            "Вписывание фона",
            options=list(appearance.FIT_MODES),
            index=list(appearance.FIT_MODES).index(settings.fit)
            if settings.fit in appearance.FIT_MODES
            else 0,
            format_func=lambda value: {
                "cover": "Cover · заполнить экран (может обрезать)",
                "contain": "Contain · показать целиком",
            }.get(value, value),
            key=_appearance_widget_key("fit"),
        )
        pos_cols = st.columns(2)
        position_x = pos_cols[0].slider(
            "Позиция по горизонтали, %",
            min_value=0,
            max_value=100,
            value=int(settings.position_x),
            step=1,
            key=_appearance_widget_key("pos_x"),
        )
        position_y = pos_cols[1].slider(
            "Позиция по вертикали, %",
            min_value=0,
            max_value=100,
            value=int(settings.position_y),
            step=1,
            key=_appearance_widget_key("pos_y"),
        )
        dim = st.slider(
            "Затемнение фона",
            min_value=0.0,
            max_value=0.92,
            value=float(settings.dim),
            step=0.02,
            key=_appearance_widget_key("dim"),
            help="Затемняет только слой фона, не карточки и не текст.",
        )
        blur = st.slider(
            "Размытие фона, px",
            min_value=0,
            max_value=30,
            value=int(settings.blur),
            step=1,
            key=_appearance_widget_key("blur"),
            help="CSS blur применяется к фону. Контент остаётся резким.",
        )
        opacity = st.slider(
            "Непрозрачность фона",
            min_value=0.05,
            max_value=1.0,
            value=float(settings.opacity),
            step=0.05,
            key=_appearance_widget_key("opacity"),
        )
        panel_opacity = st.slider(
            "Плотность панелей",
            min_value=0.35,
            max_value=0.98,
            value=float(settings.panel_opacity),
            step=0.01,
            key=_appearance_widget_key("panel"),
            help="Насколько непрозрачны карточки, статус и раскрытые панели.",
        )

    with controls_right:
        st.subheader("Фон")
        uploaded = st.file_uploader(
            "Загрузить PNG, JPG или WebP",
            type=["png", "jpg", "jpeg", "webp"],
            key=_appearance_widget_key("upload"),
            help="До 12 МБ. Изображение проверяется, уменьшается до 2560 px "
            "по длинной стороне и сохраняется локально как WebP.",
        )
        pending = st.session_state.get("appearance_pending")
        if uploaded is not None:
            raw = uploaded.getvalue()
            digest = hashlib.sha256(raw).hexdigest()
            if st.session_state.get("appearance_upload_digest") != digest:
                st.session_state["appearance_upload_digest"] = digest
                try:
                    processed = appearance.process_image(raw)
                except appearance.AppearanceError as exc:
                    st.session_state.pop("appearance_pending", None)
                    st.error(f"Фон не принят: {exc}")
                else:
                    st.session_state["appearance_pending"] = {
                        "data": processed.data,
                        "name": uploaded.name,
                        "width": processed.width,
                        "height": processed.height,
                        "digest": processed.digest,
                        "source_format": processed.source_format,
                    }
                    pending = st.session_state["appearance_pending"]
        if pending:
            st.image(
                pending["data"],
                caption=(
                    f"Предпросмотр: {pending['name']} · "
                    f"{pending['width']}×{pending['height']} · WebP"
                ),
                use_container_width=True,
            )
            st.caption(
                "Файл ещё не установлен. Нажмите «Применить оформление», "
                "чтобы сохранить его локально."
            )
        elif current_path is not None:
            st.caption(
                f"Активный фон: {settings.background_name or current_path.name} · "
                f"{current_path.stat().st_size / 1024:.0f} КБ"
            )
        else:
            st.info(
                "Фон не задан. Тема работает без изображения; загружать файл "
                "не обязательно."
            )
        st.caption(
            "Изображения не отправляются во внешние сервисы. Они лежат рядом "
            "с локальной БД в папке appearance. В JSON-пресеты и экспорт "
            "настроек попадает только имя файла, не сам файл."
        )

    preview_settings = appearance.AppearanceSettings.from_dict(
        {
            **settings.to_dict(),
            "theme": theme,
            "accent": accent,
            "fit": fit,
            "position_x": position_x,
            "position_y": position_y,
            "dim": dim,
            "blur": blur,
            "opacity": opacity,
            "panel_opacity": panel_opacity,
        }
    )
    pending = st.session_state.get("appearance_pending")
    if pending:
        preview_uri = appearance.bytes_data_uri(pending["data"])
    else:
        preview_uri = appearance.background_data_uri(store, settings)
    st.markdown("### Предпросмотр")
    st.markdown(
        ui_theme.preview_html(preview_settings, preview_uri),
        unsafe_allow_html=True,
    )

    action_cols = st.columns([1.35, 1.0, 1.15])
    if action_cols[0].button(
        "Применить оформление",
        type="primary",
        key="appearance_apply",
        use_container_width=True,
    ):
        updated = appearance.AppearanceSettings.from_dict(preview_settings.to_dict())
        try:
            if pending:
                filename = appearance.install_background(
                    store,
                    appearance.ProcessedImage(
                        data=pending["data"],
                        width=int(pending["width"]),
                        height=int(pending["height"]),
                        source_format=str(pending["source_format"]),
                        digest=str(pending["digest"]),
                    ),
                )
                updated.background_file = filename
                updated.background_name = str(pending["name"])[:160]
            appearance.save_appearance(store, updated)
            appearance.remove_unused_backgrounds(store, updated.background_file)
        except (appearance.AppearanceError, OSError) as exc:
            st.error(f"Оформление не сохранено: {exc}")
        else:
            st.session_state.pop("appearance_pending", None)
            st.session_state.pop("appearance_upload_digest", None)
            st.session_state["_drop_appearance_uploader"] = True
            _bump_appearance_editor()
            st.session_state["_reset_appearance_editors"] = True
            _flash("Оформление сохранено. Поиск и пресеты не изменялись.")
            st.rerun()

    if action_cols[1].button(
        "Удалить фон",
        key="appearance_remove",
        disabled=not bool(settings.background_file or pending),
        use_container_width=True,
    ):
        try:
            if pending:
                st.session_state.pop("appearance_pending", None)
                st.session_state.pop("appearance_upload_digest", None)
                st.session_state["_drop_appearance_uploader"] = True
            appearance.delete_background(store, settings)
            _flash("Фон удалён; выбранная тема сохранена.")
            _bump_appearance_editor()
            st.session_state["_reset_appearance_editors"] = True
            st.rerun()
        except (appearance.AppearanceError, OSError) as exc:
            st.error(f"Фон не удалён: {exc}")

    reset_ok = action_cols[2].checkbox(
        "Подтвердить сброс", key=_appearance_widget_key("reset_confirm")
    )
    if action_cols[2].button(
        "Сбросить оформление",
        key="appearance_reset",
        disabled=not reset_ok,
        use_container_width=True,
    ):
        try:
            appearance.reset_appearance(store)
        except (appearance.AppearanceError, OSError) as exc:
            st.error(f"Сброс не выполнен: {exc}")
        else:
            st.session_state.pop("appearance_pending", None)
            st.session_state.pop("appearance_upload_digest", None)
            st.session_state["_drop_appearance_uploader"] = True
            _bump_appearance_editor()
            st.session_state["_reset_appearance_editors"] = True
            _flash("Оформление сброшено к Modern Glass.")
            st.rerun()

    st.caption(
        "Подсказка: для читаемости оставляйте затемнение 0.30–0.60 и размытие "
        "4–12 px. Blur и затемнение применяются только к фоновому слою."
    )


def page_diagnostics(store: Storage, cfg: PresetConfig) -> None:
    st.header("🩺 Диагностика и ограничения")
    st.caption(
        "Здесь только безопасные сведения: токены и client_secret не "
        "отображаются и не пишутся в SQLite/логи."
    )
    info = store.database_info()
    cols = st.columns(4)
    cols[0].metric("Схема БД", info["schema_version"])
    cols[1].metric("Beatmapsets", info["beatmapsets"])
    cols[2].metric("Пресеты", info["presets"])
    cols[3].metric("Поиски", info["searches"])
    st.caption(
        f"Файл: {info['path']} · размер: {info['size_bytes']} байт · "
        f"история: {info['history']}"
    )

    st.subheader("osu! API")
    try:
        api_config = config.load_api_config()
    except ValueError as exc:
        st.error(f".env: {exc}")
        api_config = None
    if api_config is None or not api_config.configured:
        st.warning(
            "Credentials не заданы. Реальная интеграция не проверена в этой "
            "установке; тесты используют httpx.MockTransport и не ходят в сеть."
        )
    else:
        st.success("Credentials заданы (значения скрыты).")
        if st.button("Проверить OAuth и соединение", key="diag_check_api"):
            try:
                client = _get_api(api_config)
                outcome = client.check_connection()
            except OsuApiError as exc:
                st.error(exc.user_message())
            except Exception as exc:  # noqa: BLE001 - diagnostics boundary
                logger.error("connection check failed: %s", type(exc).__name__)
                st.error(f"Проверка не удалась: {type(exc).__name__}")
            else:
                if outcome.get("ok"):
                    st.success(outcome.get("message", "OK"))
                else:
                    st.error(outcome.get("message", "Не удалось"))
                st.caption(
                    "Токен получен; API-запросы поиска всё равно ограничены "
                    "бюджетом и могут вернуть 403 для отдельных маршрутов."
                )

    st.subheader("Кеш")
    cache = store.cache_stats()
    cache_cols = st.columns(4)
    cache_cols[0].metric("Записей поиска", cache["entries"])
    cache_cols[1].metric("Просрочено", cache["expired"])
    cache_cols[2].metric("Attributes", cache["attributes"])
    cache_cols[3].metric("Размер БД", cache["db_bytes"])
    st.caption(
        f"Самый старый вход: {cache['oldest'] or '—'} · "
        f"самый новый: {cache['newest'] or '—'}"
    )
    cache_cols2 = st.columns(3)
    if cache_cols2[0].button("Очистить кеш", key="diag_clear_cache"):
        result = store.cache_clear()
        _flash(
            "Кеш очищен: поиск "
            f"{result['search_cache']}, attributes {result['attributes']}. "
            "Оценки и избранное сохранены."
        )
        st.rerun()
    if cache_cols2[1].button("Удалить просроченное", key="diag_prune_cache"):
        removed = store.prune_cache()
        _flash(f"Удалено просроченных записей: {removed}.")
        st.rerun()
    vacuum_ok = cache_cols2[2].checkbox("Подтвердить VACUUM", key="diag_vacuum_ok")
    if cache_cols2[2].button(
        "VACUUM", key="diag_vacuum", disabled=not vacuum_ok
    ):
        try:
            store.vacuum()
            _flash("VACUUM выполнен.")
            st.rerun()
        except StorageError as exc:
            st.error(str(exc))

    st.subheader("Превью геймплея")
    gameplay_dir = _gameplay_cache_dir(store)
    gameplay_files = (
        list(gameplay_dir.glob("*.osu")) if gameplay_dir.exists() else []
    )
    gp_cols = st.columns(3)
    gp_cols[0].metric(".osu в кеше", len(gameplay_files))
    gp_cols[1].metric(
        "Размер",
        f"{sum(path.stat().st_size for path in gameplay_files) / 1024:.0f} КБ",
    )
    gp_cols[2].caption(
        "Публичные .osu скачиваются с osu.ppy.sh/osu/{id} только при открытии "
        "превью выбранной difficulty."
    )
    if st.button(
        "Очистить кеш превью",
        key="diag_clear_gameplay_cache",
        disabled=not gameplay_files,
    ):
        removed = 0
        for path in gameplay_files:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        _flash(f"Кеш превью очищен: файлов {removed}.")
        st.rerun()

    st.subheader("Анализ .osu")
    analysis_stats = store.analysis_stats()
    if analysis_stats:
        st.write(
            " · ".join(
                f"{evidence.label(key)}: {value}"
                for key, value in sorted(analysis_stats.items())
            )
        )
    else:
        st.info("Сохранённого анализа .osu пока нет.")

    st.subheader("Последний поиск")
    last = store.last_search_run()
    if last is None:
        st.info("Поисков ещё не было.")
    else:
        st.write(
            f"Запросов (попыток): {last['requests']} · из кеша: "
            f"{last['cache_hits']} · уникальных: {last['total_fetched']} · "
            f"прошло фильтры: "
            f"{last['passed_filters']} · результатов: {last['result_count']} · "
            f"полнота: {'да' if last['complete'] else 'нет'}"
        )
        st.caption(
            f"Стратегии: {', '.join(last.get('strategies') or []) or '—'}"
        )
        if last.get("errors"):
            st.warning("Ошибки: " + "; ".join(last["errors"]))
        if last.get("excluded"):
            st.write("Исключения:", last["excluded"])

    st.subheader("Backup и восстановление SQLite")
    backup_path = st.session_state.get("backup_path")
    if st.button("Создать backup сейчас", key="diag_backup"):
        try:
            path = store.backup_to()
            st.session_state["backup_path"] = str(path)
            _flash("Backup создан.")
            st.rerun()
        except (StorageError, OSError) as exc:
            st.error(f"Backup не создан: {exc}")
    if backup_path and Path(backup_path).exists():
        st.caption(f"Последний backup: {backup_path}")
        st.download_button(
            "Скачать backup",
            data=Path(backup_path).read_bytes(),
            file_name=Path(backup_path).name,
            mime="application/vnd.sqlite3",
            key="diag_backup_download",
        )

    restore_file = st.file_uploader(
        "Восстановить из SQLite backup", type=["db", "sqlite", "sqlite3"],
        key="diag_restore_file",
    )
    if restore_file is not None:
        raw = restore_file.getvalue()
        if len(raw) > 100_000_000:
            st.error("Файл слишком большой (лимит 100 МБ).")
        else:
            temp_path = Path(tempfile.gettempdir()) / f"osu_picker_restore_{hashlib.sha256(raw).hexdigest()[:16]}.db"
            temp_path.write_bytes(raw)
            try:
                version = Storage.validate_backup(temp_path)
            except StorageError as exc:
                st.error(f"Backup отклонён: {exc}")
            else:
                st.info(f"Backup прошёл integrity_check; схема: {version}.")
                restore_ok = st.checkbox(
                    "Подтвердить восстановление (текущая БД будет заменена; "
                    "безопасная копия создаётся автоматически)",
                    key="diag_restore_ok",
                )
                if st.button(
                    "Восстановить",
                    key="diag_restore_btn",
                    disabled=not restore_ok,
                ):
                    try:
                        safety = store.restore_from(temp_path)
                    except (StorageError, OSError) as exc:
                        st.error(f"Восстановление не выполнено: {exc}")
                    else:
                        st.session_state["result"] = None
                        st.session_state["active_preset_id"] = _choose_preset(store)
                        st.session_state["active_config"] = _load_active(
                            store, st.session_state["active_preset_id"]
                        )
                        st.session_state["_reset_editors"] = True
                        st.session_state["_drop_import_uploaders"] = True
                        _clear_result_state()
                        _flash(
                            "База восстановлена. Безопасная копия: "
                            f"{safety.name if safety else '—'}."
                        )
                        st.rerun()

    st.subheader("Честные ограничения")
    st.markdown(
        "- Поиск не является исчерпывающим: osu! API отдаёт страницы, а бюджет "
        "запросов, лимит страниц и общий дедлайн discovery явно ограничены. "
        "Случайная подборка работает только по локальному каталогу SQLite и "
        "не углубляет пагинацию; каталог пополняется явной кнопкой «Искать».\n"
        "- Официальная документация `/beatmapsets/search` помечена как TODO; "
        "имена параметров изолированы в `osu_api.py` и могут потребовать "
        "правки при изменении API.\n"
        "- Навыки — не случайные теги: каждый использует измеримые признаки "
        "или явно помечен как ограниченный. Ранжирование пока не анализирует "
        "геометрию паттернов; превью читает .osu выбранной difficulty, но это "
        "не меняет score.\n"
        "- Flashlight и часть rhythm/tech-категорий поддержаны только через "
        "теги или косвенные признаки; это отмечено в разделе «Настройки → "
        "Скиллсеты».\n"
        "- NM farm-потенциал — эвристика длины, звёзд, aim и плотности. "
        "Оценка производительности не рассчитывается и не подставляется "
        "числом из звёзд.\n"
        "- Внутренний score 0–100 — порядок, а не вероятность и не абсолютная "
        "оценка «качества».\n"
        "- Превью геймплея требует доступа к публичному osu.ppy.sh/osu/{id}; "
        "без сети или при ошибке API показывается честное сообщение, а не "
        "фиктивная анимация. Аудио в превью не входит.\n"
        "- Реальная интеграция с osu! API проверяется только при наличии "
        "credentials; без них она честно считается непроверенной."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    store = load_storage(str(config.db_path()))
    analysis_worker.ensure_worker(store)
    _render_appearance(store)

    if st.session_state.pop("_reset_editors", False):
        _bump_editor_epoch()
        _clear_editor_state()
    if st.session_state.pop("_reset_appearance_editors", False):
        _clear_appearance_state()
    if st.session_state.pop("_drop_appearance_uploader", False):
        st.session_state.pop("appearance_upload", None)
    if st.session_state.pop("_drop_import_uploaders", False):
        for key in (
            "preset_import_file",
            "history_import_file",
            "diag_restore_file",
        ):
            st.session_state.pop(key, None)

    if "active_preset_id" not in st.session_state:
        pid = _choose_preset(store)
        st.session_state["active_preset_id"] = pid
        st.session_state["active_config"] = _load_active(store, pid)
    if "active_config" not in st.session_state:
        st.session_state["active_config"] = _load_active(
            store, st.session_state["active_preset_id"]
        )

    _sidebar(store)
    page = _site_header(store)
    if page != "library":
        st.session_state.pop("library_preview_entry", None)
    cfg: PresetConfig = _active_config(store)

    flash = st.session_state.pop("_flash", None)
    if flash:
        st.toast(flash)

    if page == "search":
        page_search(store, cfg)
    elif page == "library":
        page_library(store, cfg)
    elif page == "analysis":
        page_analysis(store, cfg)
    elif page == "favorites":
        page_favorites(store)
    elif page == "history":
        page_history(store)
    elif page == "settings":
        page_settings(store, cfg)
    else:
        page_search(store, cfg)


if __name__ == "__main__":
    main()
