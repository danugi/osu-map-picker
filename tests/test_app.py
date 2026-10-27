from __future__ import annotations

import copy
from io import BytesIO
from pathlib import Path

import pytest
from conftest import make_beatmap, make_beatmapset
from PIL import Image
from streamlit.testing.v1 import AppTest

import analysis
import analysis_worker
import app as app_module
import appearance
import discovery
import gameplay
import osu_api
import patterns
import presets
from models import (
    DifficultyAttributes,
    DiscoveryResult,
    SearchPage,
    SequenceAnalysis,
)
from randomizer import RandomSelection
from storage import Storage
from skillsets import SKILL_IDS
from test_patterns import make_osu

APP = str(Path(__file__).resolve().parents[1] / "app.py")
NAV = (
    "search",
    "library",
    "analysis",
    "favorites",
    "history",
    "settings",
)
HEADERS = (
    "Обзор карт",
    "Библиотека карт",
    "Анализ и очередь",
    "Избранное",
    "История",
    "Настройки",
)
SETTINGS_SECTIONS = (
    "presets",
    "skillsets",
    "filters",
    "ranking",
    "feedback",
    "appearance",
    "diagnostics",
)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    monkeypatch.setenv("OSU_PICKER_DB", str(db))
    monkeypatch.delenv("OSU_CLIENT_ID", raising=False)
    monkeypatch.delenv("OSU_CLIENT_SECRET", raising=False)
    return db


def _texts(at: AppTest) -> str:
    parts = []
    for name in ("markdown", "caption", "warning", "error", "info", "success"):
        for element in getattr(at, name):
            parts.append(str(element.value))
    return "\n".join(parts)


def test_app_starts_without_credentials_and_does_not_search(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    assert "Map Picker" in _texts(at)
    text = _texts(at)
    assert "OSU_CLIENT_ID" in text
    assert "не настроен" in text
    # No result block and no history rows were invented.
    store = Storage(app_env)
    assert store.history_count() == 0
    assert store.beatmapset_count() == 0


def test_sidebar_seeds_all_presets(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    selector = at.sidebar.selectbox[0]
    assert len(selector.options) == 12
    store = Storage(app_env)
    names = {preset.name for preset in store.list_presets()}
    assert names == {
        "Mixed Aim",
        "Discovery",
        "All-rounder",
        "Aim Farm",
        "Speed",
        "Streams & Stamina",
        "Bursts",
        "Finger Control",
        "Reading",
        "Accuracy",
        "Tech",
        "Flashlight",
    }
    assert "Any Taste" not in names
    assert "Aim Farm / PP" not in names
    assert "Streams & Stamina" in selector.options
    assert "Bursts" in selector.options


def test_search_button_without_credentials_is_honest(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    button = next(item for item in at.button if item.key == "run_search")
    button.click().run()
    assert not at.exception
    assert any("Нет OSU_CLIENT_ID" in error.value for error in at.error)
    store = Storage(app_env)
    assert store.history_count() == 0
    assert store.search_run_count() == 0


@pytest.mark.parametrize("index", range(len(NAV)))
def test_every_page_renders(app_env, index: int) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["nav"] = NAV[index]
    at.run()
    assert not at.exception, f"{NAV[index]}: {[e.value for e in at.exception]}"
    headers = [header.value for header in at.header]
    assert HEADERS[index] in headers


@pytest.mark.parametrize("section", SETTINGS_SECTIONS)
def test_every_settings_section_renders(app_env, section: str) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["nav"] = "settings"
    at.session_state["settings_section"] = section
    at.run()
    assert not at.exception, f"{section}: {[e.value for e in at.exception]}"
    assert "Настройки" in [item.value for item in at.header]
    assert _versioned(at.selectbox, "settings_section").value == section


def _button(at: AppTest, key: str):
    return _versioned(at.button, key)


def _versioned(items, key: str):
    return next(
        item
        for item in items
        if item.key == key or (item.key or "").startswith(f"{key}__")
    )


def _has_button(at: AppTest, key: str) -> bool:
    return any(
        item.key == key or (item.key or "").startswith(f"{key}__")
        for item in at.button
    )


def _number(at: AppTest, key: str):
    return _versioned(at.number_input, key)


def _checkbox(at: AppTest, key: str):
    return _versioned(at.checkbox, key)


def _go(at: AppTest, page_id: str) -> None:
    _button(at, f"nav_{page_id}").click().run()


def _go_settings_section(at: AppTest, section: str) -> None:
    if at.session_state.get("nav") != "settings":
        _go(at, "settings")
    _versioned(at.selectbox, "settings_section").set_value(section).run()


def _search_result(at: AppTest):
    return at.session_state["result"]


def _appearance_control(at: AppTest, kind: str, name: str):
    prefix = f"appearance_{name}"
    return next(
        item
        for item in getattr(at, kind)
        if item.key == prefix or (item.key or "").startswith(prefix + "__")
    )


def _set_skills(at: AppTest, selected: list[str]) -> None:
    wanted = set(selected)
    boxes: dict[str, object] = {}
    for item in at.checkbox:
        key = item.key or ""
        if key.startswith("cfgskill_pick_"):
            skill_id = key.removeprefix("cfgskill_pick_").split("__", 1)[0]
            boxes[skill_id] = item
    changed = False
    for skill_id in SKILL_IDS:
        box = boxes.get(skill_id)
        if box is not None and bool(box.value) != (skill_id in wanted):
            box.set_value(skill_id in wanted)
            changed = True
    if changed:
        at.run()


def test_preset_selection_loads_the_whole_configuration(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert at.session_state["active_config"].name == "Mixed Aim"

    at.sidebar.selectbox[0].select("Speed").run()
    assert not at.exception
    cfg = at.session_state["active_config"]
    assert cfg.name == "Speed"
    assert cfg.ranking.trait_targets == {}
    assert cfg.ranking.skill_weights == {
        "speed_high_bpm": 1.0,
        "speed_bursts": 0.55,
        "speed_density": 0.5,
    }

    store = Storage(app_env)
    saved = store.get_preset(at.session_state["active_preset_id"])
    assert saved is not None
    assert saved.config.to_dict() == cfg.to_dict()
    assert "Speed — High-BPM patterns" in _texts(at)


def test_skill_weight_changes_config_but_not_saved_preset_until_save(
    app_env,
) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    original_filters = at.session_state["active_config"].filters.to_dict()

    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic"])
    _number(at, "cfgskill_w_aim_classic").set_value(2.0).run()
    cfg = at.session_state["active_config"]
    assert cfg.ranking.skill_weights == {"aim_classic": 2.0}
    assert cfg.filters.to_dict() == original_filters

    store = Storage(app_env)
    saved = store.get_preset(at.session_state["active_preset_id"])
    assert saved is not None
    assert saved.config.ranking.skill_weights == {
        "aim_classic": 0.6,
        "aim_control": 0.2,
        "aim_bursts": 0.2,
    }
    _go(at, "search")
    assert "Настройки изменены" in _texts(at)
    assert not _button(at, "status_save_active").disabled

    _button(at, "status_discard_active").click().run()
    assert not at.exception
    assert at.session_state["active_config"].ranking.skill_weights == {
        "aim_classic": 0.6,
        "aim_control": 0.2,
        "aim_bursts": 0.2,
    }
    assert "Настройки изменены" not in _texts(at)
    assert not _has_button(at, "status_save_active")


def test_dirty_preset_switch_requires_an_explicit_choice(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic"])
    _number(at, "cfgskill_w_aim_classic").set_value(2.0).run()

    at.sidebar.selectbox[0].select("Speed").run()
    assert not at.exception
    assert at.session_state["active_config"].name == "Mixed Aim"
    assert at.session_state["_pending_preset_id"] is not None
    assert at.session_state["preset_selector"] == at.session_state["active_preset_id"]
    assert "несохранённые изменения" in _texts(at)

    _button(at, "pending_discard_switch").click().run()
    assert not at.exception
    assert at.session_state["active_config"].name == "Speed"
    assert "_pending_preset_id" not in at.session_state

    store = Storage(app_env)
    mixed = store.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    assert mixed.config.ranking.skill_weights == {
        "aim_classic": 0.6,
        "aim_control": 0.2,
        "aim_bursts": 0.2,
    }


def test_dirty_preset_switch_can_save_before_switching(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic"])
    _number(at, "cfgskill_w_aim_classic").set_value(2.0).run()
    at.sidebar.selectbox[0].select("Speed").run()
    assert at.session_state["_pending_preset_id"] is not None

    _button(at, "pending_save_switch").click().run()
    assert not at.exception
    assert at.session_state["active_config"].name == "Speed"
    store = Storage(app_env)
    mixed = store.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    assert mixed.config.ranking.skill_weights == {"aim_classic": 2.0}


def test_filter_editors_on_different_pages_share_one_config(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _number(at, "flt_stars_max").set_value(6.4).run()
    assert at.session_state["active_config"].filters.stars_max == 6.4

    _go_settings_section(at, "filters")
    assert not at.exception
    assert at.session_state["active_config"].filters.stars_max == 6.4
    _number(at, "flt_stars_max").set_value(6.1).run()

    _go(at, "search")
    assert not at.exception
    assert at.session_state["active_config"].filters.stars_max == 6.1
    assert _number(at, "flt_stars_max").value == 6.1


def test_skillset_editor_is_shared_between_pages(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic", "stream_consistency"])
    _number(at, "cfgskill_w_aim_classic").set_value(1.7).run()

    _go(at, "search")
    assert not at.exception
    cfg = at.session_state["active_config"]
    assert cfg.ranking.skill_weights == {
        "aim_classic": 1.7,
        "stream_consistency": 1.0,
    }
    _go_settings_section(at, "skillsets")
    assert _checkbox(at, "cfgskill_pick_aim_classic").value
    assert _checkbox(at, "cfgskill_pick_stream_consistency").value
    assert not _checkbox(at, "cfgskill_pick_aim_control").value
    assert _number(at, "cfgskill_w_aim_classic").value == 1.7


def test_star_mode_off_removes_bounds_instead_of_hiding_them(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    mode = _versioned(at.selectbox, "flt_star_mode")
    mode.set_value("off").run()
    cfg = at.session_state["active_config"]
    assert cfg.filters.stars_min is None
    assert cfg.filters.stars_max is None

    mode = _versioned(at.selectbox, "flt_star_mode")
    mode.set_value("strict").run()
    assert at.session_state["active_config"].filters.stars_min is not None
    assert at.session_state["active_config"].filters.stars_max is not None


def test_save_and_reload_restores_skills_and_filters(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].select("Speed").run()
    _go_settings_section(at, "skillsets")
    _number(at, "cfgskill_w_speed_high_bpm").set_value(1.75).run()
    _go(at, "search")
    _number(at, "flt_stars_max").set_value(6.5).run()
    _button(at, "status_save_active").click().run()

    saved_cfg = at.session_state["active_config"]
    assert saved_cfg.ranking.skill_weights["speed_high_bpm"] == 1.75
    assert saved_cfg.filters.stars_max == 6.5
    assert not _has_button(at, "status_save_active")

    at.sidebar.selectbox[0].select("Mixed Aim").run()
    at.sidebar.selectbox[0].select("Speed").run()
    reloaded = at.session_state["active_config"]
    assert reloaded.ranking.skill_weights["speed_high_bpm"] == 1.75
    assert reloaded.filters.stars_max == 6.5


def test_save_as_new_preset_keeps_the_original_unchanged(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic"])
    _number(at, "cfgskill_w_aim_classic").set_value(1.5).run()
    _go_settings_section(at, "presets")
    next(
        item for item in at.text_input if item.key == "preset_create_name"
    ).set_value("My Aim Draft")
    _button(at, "preset_create_submit").click().run()
    assert not at.exception
    assert at.session_state["active_config"].name == "My Aim Draft"

    store = Storage(app_env)
    created = store.get_preset_by_name("My Aim Draft")
    original = store.get_preset_by_name("Mixed Aim")
    assert created is not None
    assert created.config.ranking.skill_weights == {"aim_classic": 1.5}
    assert original is not None
    assert original.config.ranking.skill_weights == {
        "aim_classic": 0.6,
        "aim_control": 0.2,
        "aim_bursts": 0.2,
    }


def test_aim_farm_settings_do_not_leak_into_other_presets(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].select("Aim Farm").run()
    farm = at.session_state["active_config"]
    assert farm.filters.duration_max == 150
    assert farm.ranking.factors["farm_nm"].enabled
    assert farm.ranking.skill_weights == {"aim_classic": 1.0}

    _number(at, "flt_duration_max").set_value(130).run()
    assert at.session_state["active_config"].filters.duration_max == 130
    assert "Настройки изменены" in _texts(at)
    _button(at, "status_discard_active").click().run()
    assert at.session_state["active_config"].filters.duration_max == 150

    at.sidebar.selectbox[0].select("Speed").run()
    speed = at.session_state["active_config"]
    assert speed.name == "Speed"
    assert speed.filters.duration_max == 180
    assert not speed.ranking.factors["farm_nm"].enabled
    assert "aim_classic" not in speed.ranking.skill_weights


def test_custom_mode_can_combine_multiple_skillsets(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].select("Discovery").run()
    assert at.session_state["active_config"].ranking.skill_weights == {}

    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic", "stream_consistency"])
    _number(at, "cfgskill_w_aim_classic").set_value(2.0).run()
    _number(at, "cfgskill_w_stream_consistency").set_value(0.5).run()
    cfg = at.session_state["active_config"]
    assert cfg.ranking.skill_weights == {
        "aim_classic": 2.0,
        "stream_consistency": 0.5,
    }
    assert "Aim" in _texts(at)
    assert "Stream consistency" in _texts(at)


def test_selected_skills_explain_when_mapping_factor_is_disabled(app_env) -> None:
    store = Storage(app_env)
    presets.ensure_builtin_presets(store)
    legacy = store.create_preset(
        copy.deepcopy(presets.known_builtin_by_name("Any Taste")), is_builtin=True
    )
    store.set_preference("selected_preset", str(legacy.id))

    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert at.session_state["active_config"].name == "Any Taste"
    assert not at.session_state["active_config"].ranking.factors["mapping_fit"].enabled

    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic"])
    assert "не влияют на score" in _texts(at)
    _button(at, "cfgskill_enable_mapping").click().run()
    assert not at.exception
    assert at.session_state["active_config"].ranking.factors["mapping_fit"].enabled
    assert at.session_state["active_config"].ranking.skill_weights == {
        "aim_classic": 1.0
    }


def test_duplicating_with_unsaved_edits_copies_what_the_user_sees(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    _go_settings_section(at, "skillsets")
    _set_skills(at, ["aim_classic"])
    _number(at, "cfgskill_w_aim_classic").set_value(1.5).run()
    _go_settings_section(at, "presets")

    _button(at, "preset_duplicate").click().run()
    assert not at.exception
    assert at.session_state["active_config"].name == "Mixed Aim copy"
    assert at.session_state["active_config"].ranking.skill_weights == {
        "aim_classic": 1.5
    }

    store = Storage(app_env)
    original = store.get_preset_by_name("Mixed Aim")
    assert original is not None
    assert original.config.ranking.skill_weights == {
        "aim_classic": 0.6,
        "aim_control": 0.2,
        "aim_bursts": 0.2,
    }


def test_switching_presets_does_not_touch_feedback_or_favorites(app_env) -> None:
    store = Storage(app_env)
    store.save_beatmapset(make_beatmapset(1))
    store.set_feedback(1, ["like"])
    store.set_favorite(1, True)

    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].select("Speed").run()
    at.sidebar.selectbox[0].select("Aim Farm").run()

    assert store.get_feedback(1) == ("like",)
    assert 1 in store.favorite_ids()


def test_secret_is_never_rendered(app_env, monkeypatch) -> None:
    monkeypatch.setenv("OSU_CLIENT_ID", "12345")
    monkeypatch.setenv("OSU_CLIENT_SECRET", "super-secret-value")
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    text = _texts(at)
    assert "super-secret-value" not in text
    assert "настроен" in text


def _background_png() -> bytes:
    image = Image.new("RGB", (640, 360), (44, 120, 180))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_appearance_upload_survives_and_does_not_change_search(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    original = at.session_state["active_config"].to_dict()
    _go_settings_section(at, "appearance")
    assert not at.exception
    at.file_uploader[0].set_value(
        ("my background.png", _background_png(), "image/png")
    ).run()
    assert not at.exception
    assert at.session_state["appearance_pending"]["width"] == 640

    _button(at, "appearance_apply").click().run()
    assert not at.exception
    store = Storage(app_env)
    saved = appearance.load_appearance(store)
    assert saved.theme == "modern_glass"
    assert _appearance_control(at, "radio", "theme").value == "modern_glass"
    assert saved.background_name == "my background.png"
    assert saved.background_file
    path = appearance.background_path(store, saved)
    assert path is not None and path.is_file()
    assert path.parent == app_env.parent / "appearance"
    assert at.session_state["active_config"].to_dict() == original
    assert at.sidebar.selectbox[0].value is not None

    # A fresh script run reads the same local image and keeps search config.
    fresh = AppTest.from_file(APP, default_timeout=90)
    fresh.run()
    _go_settings_section(fresh, "appearance")
    assert not fresh.exception
    theme = _appearance_control(fresh, "radio", "theme")
    assert theme.value == "modern_glass"
    assert "data:image/webp;base64" in _texts(fresh)
    assert fresh.session_state["active_config"].to_dict() == original

    _button(fresh, "appearance_remove").click().run()
    assert not fresh.exception
    removed = appearance.load_appearance(Storage(app_env))
    assert removed.theme == "modern_glass"
    assert removed.background_file is None
    assert not path.exists()
    assert _appearance_control(fresh, "radio", "theme").value == "modern_glass"


def test_invalid_appearance_upload_is_rejected_without_replacing_background(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _go_settings_section(at, "appearance")
    at.file_uploader[0].set_value(
        ("broken.png", b"definitely not an image", "image/png")
    ).run()
    assert not at.exception
    assert "Фон не принят" in _texts(at)
    assert "appearance_pending" not in at.session_state
    assert appearance.load_appearance(Storage(app_env)).background_file is None
    assert not list((app_env.parent / "appearance").glob("background-*"))


def test_theme_controls_persist_without_a_background(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    original = at.session_state["active_config"].to_dict()
    _go_settings_section(at, "appearance")
    _appearance_control(at, "radio", "theme").set_value("dark_clean")
    _appearance_control(at, "slider", "blur").set_value(11)
    _appearance_control(at, "slider", "dim").set_value(0.60)
    at.run()
    _button(at, "appearance_apply").click().run()
    assert not at.exception
    saved = appearance.load_appearance(Storage(app_env))
    assert saved.theme == "dark_clean"
    assert saved.blur == 11
    assert saved.dim == 0.60
    assert at.session_state["active_config"].to_dict() == original
    assert "Dark Clean" in _texts(at)

    fresh = AppTest.from_file(APP, default_timeout=90)
    fresh.run()
    _go_settings_section(fresh, "appearance")
    assert _appearance_control(fresh, "radio", "theme").value == "dark_clean"
    assert _appearance_control(fresh, "slider", "blur").value == 11


def test_skill_hierarchy_counts_search_and_pins(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "skillsets"
    at.run()
    labels = [item.label for item in at.expander]
    assert any(label.startswith("🎯 Aim · навыков 3") for label in labels)
    assert any(
        label.startswith("⚡ Speed & Streams · навыков 0") for label in labels
    )
    assert any(
        label.startswith("🖐 Stamina & Finger Control · навыков 0")
        for label in labels
    )
    assert any(label.startswith("✚ Mixed Skillsets · навыков 0") for label in labels)
    assert "Aim Control" in _texts(at)
    assert "Выбрано навыков: 3" in _texts(at)

    _versioned(at.text_input, "cfgskill_search").set_value(
        "stamina"
    ).run()
    assert _checkbox(at, "cfgskill_pick_stamina_alt") is not None
    assert "Поиск по каталогу" in _texts(at)
    _button(at, "cfgskill_pin_stamina_alt").click().run()
    saved = Storage(app_env).get_preference("pinned_skills_v1")
    assert saved is not None and "stamina_alt" in saved

    _button(at, "cfgskill_clear_search").click().run()
    assert any("Закреплённые навыки" in item.label for item in at.expander)


def test_preset_catalog_is_grouped_with_counts(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "presets"
    at.run()
    labels = [item.label for item in at.expander]
    assert "🧭 Explore · 3" in labels
    assert "💯 Farm · 1" in labels
    assert "🎯 Train · 8" in labels
    assert "🧩 Custom · 0" in labels
    assert "Aim Farm" in _texts(at)
    assert "Streams & Stamina" in _texts(at)


def test_navigation_is_compact_and_switches_pages(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    for page_id in NAV:
        assert _button(at, f"nav_{page_id}") is not None
    assert len(NAV) == 6
    assert _button(at, "nav_search").proto.type == "primary"
    assert _button(at, "nav_settings").proto.type == "secondary"
    assert not any(item.key == "nav_presets" for item in at.button)
    assert not any(item.key == "nav_filters" for item in at.button)

    _go(at, "settings")
    assert "Настройки" in [header.value for header in at.header]
    _versioned(at.selectbox, "settings_section").set_value("filters").run()
    assert "🧰 Фильтры" in [header.value for header in at.header]
    _go(at, "search")
    assert "Обзор карт" in [header.value for header in at.header]


def test_sequential_preset_switching_loads_each_configuration(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    store = Storage(app_env)
    sequence = ("Mixed Aim", "Reading", "Aim Farm", "Streams & Stamina")
    for name in sequence:
        at.sidebar.selectbox[0].select(name).run()
        assert not at.exception
        cfg = at.session_state["active_config"]
        saved = store.get_preset(at.session_state["active_preset_id"])
        assert saved is not None
        assert cfg.name == name
        assert saved.name == name
        assert cfg.to_dict() == saved.config.to_dict()
        assert at.session_state.get("result") is None
        assert at.session_state["preset_selector"] == saved.id

    assert cfg.ranking.skill_weights == {
        "stream_consistency": 0.90,
        "stamina_finger": 0.65,
        "stream_long": 0.55,
        "speed_stamina": 0.35,
    }
    assert cfg.filters.duration_max == 360


def _seed_short_cached_maps(store: Storage) -> None:
    beatmapsets = [
        make_beatmapset(
            index,
            title=f"Cached map {index}",
            # The cache must contain genuinely versatile maps so a preset
            # switch can be tested against real target-tag criteria. Titles
            # are deliberately unrelated; only tags and measured features
            # are allowed to establish relevance.
            tags=(
                "aim",
                "stream",
                "tech",
                "reading",
                "finger",
                "speed",
                "alt",
                "rhythm",
            ),
            beatmaps=(
                make_beatmap(
                    index * 10 + 1,
                    index,
                    stars=5.2 + index / 100,
                    total_length=90,
                    hit_length=85,
                    count_circles=500,
                    count_sliders=200,
                    count_spinners=0,
                ),
            ),
        )
        for index in range(1, 13)
    ]
    store.save_beatmapsets(beatmapsets)
    # Local reranking cannot fetch difficulty attributes. Seed them so
    # presets whose mandatory evidence requires official aim/speed pressure
    # can still be exercised against the cache.
    for index in range(1, 13):
        store.save_attributes(
            index * 10 + 1,
            {
                "star_rating": 5.2 + index / 100,
                "aim_difficulty": 3.4,
                "speed_difficulty": 1.6,
                "slider_factor": 1.2,
                "max_combo": 900,
            },
        )


def test_local_cache_search_reranks_for_each_switched_preset(app_env) -> None:
    store = Storage(app_env)
    _seed_short_cached_maps(store)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()

    for name in (
        "Mixed Aim",
        "Reading",
        "Tech",
        "Aim Farm",
        "Mixed Aim",
    ):
        at.sidebar.selectbox[0].select(name).run()
        _button(at, "run_search").click().run()
        assert not at.exception
        result = _search_result(at)
        assert result is not None, _texts(at)
        assert result.origin == "local_cache"
        assert result.api_requests == 0
        assert result.count == 12
        assert at.session_state["result_config"]["name"] == name
        assert result.ranking_used is not None
        assert (
            result.ranking_used.skill_weights
            == at.session_state["active_config"].ranking.skill_weights
        )

    assert store.search_run_count() == 0


def test_cards_distinguish_analyzed_from_missing_osu(app_env) -> None:
    store = Storage(app_env)
    _seed_short_cached_maps(store)
    cache = analysis.cache_dir_for(store)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "11.osu").write_text(
        make_osu(
            [(1000 + index * 125, 100 + index * 4, 100) for index in range(12)]
        ),
        encoding="utf-8",
    )
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    assert not at.exception, _texts(at)
    text = _texts(at).casefold()
    assert "паттерны .osu" in text
    assert "источник недоступен" in text or ".osu-файла нет" in text
    assert "измерено" in text


def test_analysis_queue_processes_cached_file_and_page_reports_it(
    app_env,
) -> None:
    store = Storage(app_env)
    _seed_short_cached_maps(store)
    cache = analysis.cache_dir_for(store)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "11.osu").write_text(
        make_osu(
            [(1000 + index * 125, 100, 100) for index in range(12)]
        ),
        encoding="utf-8",
    )
    added = analysis_worker.enqueue_catalog(store, allow_download=False)
    assert added >= 1
    assert analysis_worker.process_once(
        store, limit=1, allow_download=False
    ) == 1
    stats = store.analysis_stats()
    assert stats.get("measured", 0) >= 1

    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "analysis"
    at.run()
    assert not at.exception, _texts(at)
    assert "Анализ и очередь" in [
        header.value for header in at.header
    ]
    labels = {metric.label for metric in at.metric}
    assert {"Проанализировано", "В очереди", "Ошибок", "Worker"} <= labels


def test_local_streams_and_bursts_use_only_cached_osu_evidence(app_env) -> None:
    store = Storage(app_env)
    _seed_short_cached_maps(store)
    cache = Path(app_env).parent / "gameplay_cache"
    cache.mkdir(parents=True, exist_ok=True)
    stream = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(12)
    ]
    burst = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(6)
    ]
    (cache / "11.osu").write_text(make_osu(stream), encoding="utf-8")
    (cache / "21.osu").write_text(make_osu(burst), encoding="utf-8")

    at = AppTest.from_file(APP, default_timeout=90)
    at.run()

    at.sidebar.selectbox[0].select("Streams & Stamina").run()
    _button(at, "run_search").click().run()
    assert not at.exception
    streams = _search_result(at)
    assert streams is not None and streams.count == 1
    assert streams.scored[0].beatmapset.id == 1
    assert streams.api_requests == 0
    assert streams.sequence_analyzed == 2
    assert streams.sequence_unanalyzed == 10
    assert streams.sequence_downloads == 0
    assert streams.sequence_cache_hits == 2
    text = _texts(at)
    assert "Sequence-анализ: 2 проанализировано" in text
    assert "10 без .osu" in text
    assert ".osu скачано: 0" in text
    assert "из локального кэша/БД: 2" in text

    at.sidebar.selectbox[0].select("Bursts").run()
    _button(at, "run_search").click().run()
    assert not at.exception
    bursts = _search_result(at)
    assert bursts is not None and bursts.count == 1
    assert bursts.scored[0].beatmapset.id == 2
    assert bursts.api_requests == 0


def test_specialized_card_shows_target_gate_and_threshold(app_env) -> None:
    store = Storage(app_env)
    _seed_short_cached_maps(store)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    at.sidebar.selectbox[0].select("Tech").run()
    _button(at, "run_search").click().run()
    assert not at.exception
    result = _search_result(at)
    assert result is not None and result.count == 12
    assert "Целевые теги пресета" in _texts(at)

    gate_table = next(
        frame.value
        for frame in at.dataframe
        if "Целевой тег" in getattr(frame.value, "columns", [])
    )
    assert list(gate_table.columns) == [
        "Целевой тег",
        "Соответствие",
        "Порог",
        "Статус",
        "Источник",
        "Уверенность",
    ]
    assert gate_table.iloc[0]["Целевой тег"] == "Tech"
    assert (
        gate_table.iloc[0]["Статус"]
        == "выше порога (ограниченная поддержка)"
    )
    assert "тег" in gate_table.iloc[0]["Источник"].casefold()

    timing_table = next(
        frame.value
        for frame in at.dataframe
        if "Этап" in getattr(frame.value, "columns", [])
    )
    assert "Sequence-анализ .osu" in set(timing_table["Этап"])

    _go_settings_section(at, "ranking")
    slider = _versioned(at.slider, "rnk_relevance_threshold")
    assert slider.value == 0.55
    assert slider.disabled is False
    assert "Tech" in _texts(at)
    slider.set_value(0.70).run()
    assert not at.exception
    assert (
        at.session_state["active_config"].ranking.relevance_threshold == 0.70
    )


def test_shuffle_and_random_pick_do_not_touch_config(app_env) -> None:
    store = Storage(app_env)
    _seed_short_cached_maps(store)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    result = _search_result(at)
    assert result is not None and result.count == 12
    assert "Наибольший вклад" in _texts(at)
    assert "Итог = сумма вкладов известных факторов" in _texts(at)
    before_config = at.session_state["active_config"].to_dict()
    before_ids = [item.beatmap.id for item in result.scored]
    before_history = store.history_count()

    _button(at, "shuffle_results").click().run()
    assert not at.exception
    first_seed = at.session_state["result_shuffle_seed"]
    first_order = list(at.session_state["result_display_order"])
    assert sorted(first_order) == sorted(before_ids)
    assert first_order != before_ids
    assert at.session_state["result_pick_id"] is None
    assert [item.beatmap.id for item in _search_result(at).scored] == before_ids

    at.run()
    assert at.session_state["result_shuffle_seed"] == first_seed
    assert at.session_state["result_display_order"] == first_order

    _button(at, "shuffle_results").click().run()
    assert not at.exception
    second_order = at.session_state["result_display_order"]
    assert second_order != first_order
    assert sorted(second_order) == sorted(before_ids)
    assert at.session_state["active_config"].to_dict() == before_config
    assert store.history_count() == before_history
    assert store.search_run_count() == 0

    _button(at, "random_pick").click().run()
    assert not at.exception
    picked_id = at.session_state["result_pick_id"]
    assert picked_id in before_ids
    assert at.session_state["active_config"].to_dict() == before_config
    assert store.search_run_count() == 0

    _button(at, "random_pick").click().run()
    assert not at.exception
    second_pick = at.session_state["result_pick_id"]
    assert second_pick in before_ids
    assert second_pick != picked_id
    assert [item.beatmap.id for item in _search_result(at).scored] == before_ids
    assert store.history_count() == before_history + 2
    assert {
        row["surface"] for row in store.history_rows(limit=2)
    } == {"random_pick"}


def _seed_many_cached_maps(store: Storage, count: int = 30) -> None:
    beatmapsets = [
        make_beatmapset(
            index,
            title=f"Cached map {index}",
            beatmaps=(
                make_beatmap(
                    index * 10 + 1,
                    index,
                    stars=5.2 + (index % 8) / 100,
                    total_length=90 + index,
                    hit_length=85 + index,
                ),
            ),
        )
        for index in range(1, count + 1)
    ]
    store.save_beatmapsets(beatmapsets)


class _FakeSearchApi:
    """Deterministic online-search double used by explicit search tests."""

    def __init__(self, beatmapsets) -> None:
        self.beatmapsets = tuple(beatmapsets)
        self.search_calls = 0

    def search_beatmapsets(self, query):
        self.search_calls += 1
        return SearchPage(beatmapsets=self.beatmapsets)

    def get_beatmap_attributes(self, beatmap_id: int):
        return DifficultyAttributes(
            beatmap_id=beatmap_id,
            star_rating=5.3,
            aim_difficulty=3.2,
            speed_difficulty=1.5,
            slider_factor=1.2,
            max_combo=900,
        )


def test_collect_random_pool_reads_only_filtered_local_catalog(
    app_env, monkeypatch
) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 5)
    store.save_beatmapset(
        make_beatmapset(
            99,
            title="Too hard for the active strict filter",
            beatmaps=(
                make_beatmap(
                    991,
                    99,
                    stars=8.5,
                    total_length=90,
                    hit_length=85,
                ),
            ),
        )
    )
    cfg = presets.builtin_by_name("Mixed Aim")
    assert cfg is not None

    def fail(*args, **kwargs):
        raise AssertionError("random pool must not touch the API")

    monkeypatch.setattr(app_module.config, "load_api_config", fail)
    monkeypatch.setattr(app_module, "_get_api", fail)
    pool = app_module._collect_random_pool(store, cfg)
    assert pool.catalog_size == 6
    assert pool.result.origin == "local_cache"
    assert pool.result.api_requests == 0
    assert pool.result.strategies == ("local-cache",)
    assert pool.result.passed_filters == 5
    assert pool.result.count == 5
    assert 99 not in {item.beatmapset.id for item in pool.result.scored}
    assert all(item.score >= 0 for item in pool.result.scored)

    selection = app_module.randomizer.select_random_subset(
        pool.result.scored, 3, seed=7
    )
    assert selection.count == 3
    assert {item.beatmapset.id for item in selection.items} <= {
        item.beatmapset.id for item in pool.result.scored
    }
    assert "SQLite" in pool.source_note
    assert "Сетевых запросов нет" in pool.source_note


def test_random_scope_reacts_to_ranking_and_filter_changes() -> None:
    cfg = presets.builtin_by_name("Mixed Aim")
    assert cfg is not None
    baseline = app_module._random_scope_key(cfg)
    cfg.filters.stars_min = 5.0
    filters_key = app_module._random_scope_key(cfg)
    assert filters_key != baseline
    cfg.ranking.skill_weights = {"aim_classic": 2.0}
    ranking_key = app_module._random_scope_key(cfg)
    assert ranking_key != filters_key


def test_random_pool_is_not_truncated_to_the_display_cap(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 450)
    cfg = presets.builtin_by_name("Mixed Aim")
    assert cfg is not None
    cfg.filters.duration_min = None
    cfg.filters.duration_max = None
    cfg.discovery.max_candidates = 400

    pool = app_module._collect_random_pool(store, cfg)
    assert pool.result.passed_filters == 450
    assert pool.result.count == 450
    assert pool.result.count > cfg.discovery.max_candidates

    first = app_module.randomizer.select_random_subset(
        pool.result.scored, 12, seed=17
    )
    second = app_module.randomizer.select_random_subset(
        pool.result.scored,
        12,
        seed=18,
        avoid_ids={item.beatmapset.id for item in first.items},
    )
    first_ids = {item.beatmapset.id for item in first.items}
    second_ids = {item.beatmapset.id for item in second.items}
    assert len(first_ids) == 12
    assert len(second_ids) == 12
    assert first_ids.isdisjoint(second_ids)

    # At least one sampled map must come from outside the old 400-card
    # display slice; the pool is genuinely broader than the result page.
    positions = {
        index: item.beatmapset.id
        for index, item in enumerate(pool.result.scored)
    }
    chosen_positions = [
        index for index, set_id in positions.items() if set_id in first_ids
    ]
    assert any(index >= 400 for index in chosen_positions)


def test_empty_random_message_explains_local_catalog_limits() -> None:
    empty_selection = RandomSelection(
        items=(),
        requested=12,
        pool_size=0,
        available=0,
        excluded=0,
        seed=1,
        shortfall=True,
        note="В текущем пуле нет карт, прошедших строгие фильтры.",
    )
    empty_pool = app_module.RandomPool(
        result=DiscoveryResult(origin="local_cache", complete=False),
        source_note="",
        catalog_size=0,
    )
    empty_message = app_module._random_empty_message(
        empty_selection, empty_pool
    )
    assert "Локальный каталог пуст" in empty_message
    assert "Искать" in empty_message
    assert "более глубокие страницы" not in empty_message.casefold()

    filtered_pool = app_module.RandomPool(
        result=DiscoveryResult(
            origin="local_cache", total_fetched=10, complete=False
        ),
        source_note="",
        catalog_size=10,
    )
    filtered_message = app_module._random_empty_message(
        empty_selection, filtered_pool
    )
    assert "ни одна не прошла строгие фильтры" in filtered_message
    assert "онлайн-поиск не запускался" in filtered_message.casefold()
    assert "более глубокие страницы" not in filtered_message.casefold()

    exhausted_selection = RandomSelection(
        items=(),
        requested=12,
        pool_size=5,
        available=0,
        excluded=5,
        seed=1,
        shortfall=True,
        note="Нет новых подходящих карт.",
    )
    exhausted_message = app_module._random_empty_message(
        exhausted_selection, filtered_pool
    )
    assert "random-сессии" in exhausted_message
    assert "«Искать»" in exhausted_message
    assert "более глубокие страницы" not in exhausted_message.casefold()


def test_random_selection_ignores_plain_search_history_window(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 30)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    assert store.history_count() == 30

    random_size = _number(at, "random_size")
    random_size.set_value(12).run()
    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    result = _search_result(at)
    assert result is not None
    assert result.count == 12
    meta = at.session_state["random_meta"]
    assert meta["excluded"] == 0
    assert meta["available"] == 30
    assert meta["pool"] == 30
    assert meta["api_unique"] == 0
    assert meta["local_unique"] == 30
    assert meta["local_strict_passed"] == 30
    assert meta["local_relevance_passed"] == 30
    assert meta["session_seen"] == 12


def test_random_selection_uses_new_candidates_and_reports_shortfall(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 30)
    store.set_favorite(1, True)
    store.set_feedback(1, ["like"])

    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    before_config = at.session_state["active_config"].to_dict()
    random_size = _number(at, "random_size")
    random_size.set_value(12).run()

    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    first = _search_result(at)
    assert first is not None and first.origin == "local_cache"
    assert first.count == 12
    first_ids = {item.beatmapset.id for item in first.scored}
    assert len(first_ids) == 12
    assert at.session_state["active_config"].to_dict() == before_config
    assert store.favorite_ids() == {1}
    assert store.get_feedback(1) == ("like",)

    meta = at.session_state["random_meta"]
    assert meta["round"] == 1 and meta["count"] == 12
    at.run()
    assert at.session_state["random_meta"]["round"] == 1
    assert [item.beatmapset.id for item in _search_result(at).scored] == [
        item.beatmapset.id for item in first.scored
    ]

    _button(at, "random_selection").click().run()
    assert not at.exception
    second = _search_result(at)
    second_ids = {item.beatmapset.id for item in second.scored}
    assert second.count == 12
    assert first_ids.isdisjoint(second_ids)
    assert at.session_state["random_meta"]["round"] == 2

    _button(at, "random_selection").click().run()
    assert not at.exception
    third = _search_result(at)
    third_ids = {item.beatmapset.id for item in third.scored}
    assert third.count == 6
    assert at.session_state["random_meta"]["shortfall"] is True
    assert first_ids.isdisjoint(third_ids)
    assert second_ids.isdisjoint(third_ids)
    assert "Новых кандидатов только 6" in _texts(at)

    _button(at, "random_selection").click().run()
    assert not at.exception
    assert at.session_state["result"].count == 0
    assert at.session_state["random_meta"]["count"] == 0
    assert "Нет новых подходящих карт" in _texts(at)
    empty_text = " ".join(str(item.value) for item in at.warning).casefold()
    assert "более глубокие страницы" not in empty_text
    assert "«искать»" in empty_text
    assert "api не настроен" not in empty_text
    assert at.session_state["active_config"].to_dict() == before_config
    assert store.search_run_count() == 0


def test_random_selection_is_local_only_with_configured_api(
    app_env, monkeypatch
) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 30)
    monkeypatch.setenv("OSU_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OSU_CLIENT_SECRET", "test-client-secret")
    calls: list[str] = []

    def forbid(*args, **kwargs):
        calls.append("network")
        raise AssertionError("random selection must not use the osu! API")

    # AppTest executes app.py in its own module, so patch the source modules
    # that the script imports rather than the test-process app module.
    monkeypatch.setattr(osu_api, "OsuApiClient", forbid)
    monkeypatch.setattr(discovery.DiscoveryEngine, "run", forbid)

    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _number(at, "random_size").set_value(12).run()
    for _ in range(3):
        _button(at, "random_selection").click().run()
        assert not at.exception, _texts(at)
        result = _search_result(at)
        assert result is not None
        assert result.origin == "local_cache"
        assert result.api_requests == 0

    assert calls == []
    assert store.search_run_count() == 0
    assert at.session_state["random_meta"]["api_requests"] == 0
    assert at.session_state["random_meta"]["local_considered"] == 30


def test_random_selection_empty_catalog_is_honest_and_prompt(
    app_env, monkeypatch
) -> None:
    monkeypatch.setenv("OSU_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OSU_CLIENT_SECRET", "test-client-secret")
    calls: list[str] = []

    def forbid(*args, **kwargs):
        calls.append("network")
        raise AssertionError("empty local catalog must not trigger discovery")

    monkeypatch.setattr(osu_api, "OsuApiClient", forbid)
    monkeypatch.setattr(discovery.DiscoveryEngine, "run", forbid)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "random_selection").click().run()

    assert not at.exception, _texts(at)
    text = _texts(at)
    assert "Локальный каталог пуст" in text
    assert "«Искать»" in text
    assert "более глубокие страницы" not in text.casefold()
    assert calls == []
    store = Storage(app_env)
    assert store.beatmapset_count() == 0
    assert store.search_run_count() == 0


def test_empty_local_catalog_without_credentials_is_honest(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    text = _texts(at)
    assert "Локальный каталог пуст" in text
    assert "«Искать»" in text
    store = Storage(app_env)
    assert store.beatmapset_count() == 0
    assert store.search_run_count() == 0


def test_explicit_search_populates_catalog_for_later_local_random(
    app_env, monkeypatch
) -> None:
    sets = tuple(
        make_beatmapset(
            500 + index,
            title=f"Online map {index}",
            beatmaps=(
                make_beatmap(
                    5000 + index,
                    500 + index,
                    stars=5.1 + index / 100,
                    total_length=110,
                    hit_length=100,
                ),
            ),
        )
        for index in range(1, 6)
    )
    monkeypatch.setenv("OSU_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OSU_CLIENT_SECRET", "test-client-secret")

    class FakeClient(_FakeSearchApi):
        def __init__(self, api_config, *args, **kwargs):
            super().__init__(sets)

    monkeypatch.setattr(osu_api, "OsuApiClient", FakeClient)

    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    assert not at.exception, _texts(at)
    online = _search_result(at)
    assert online is not None and online.origin == "api"
    assert online.count == 5
    assert online.api_requests > 0

    store = Storage(app_env)
    assert store.beatmapset_count() == 5
    assert store.search_run_count() == 1

    calls: list[str] = []

    def forbid(*args, **kwargs):
        calls.append("network")
        raise AssertionError("random selection must stay local")

    # The script imported the fake class into its own namespace; patch the
    # class methods and the engine so any accidental network work is caught.
    monkeypatch.setattr(FakeClient, "search_beatmapsets", forbid)
    monkeypatch.setattr(FakeClient, "get_beatmap_attributes", forbid)
    monkeypatch.setattr(discovery.DiscoveryEngine, "run", forbid)
    _button(at, "random_selection").click().run()

    assert not at.exception, _texts(at)
    local = _search_result(at)
    assert local is not None
    assert local.origin == "local_cache"
    assert local.api_requests == 0
    assert {item.beatmapset.id for item in local.scored} == {
        501,
        502,
        503,
        504,
        505,
    }
    assert calls == []
    assert store.search_run_count() == 1


def test_random_avoid_seen_off_allows_repeating_the_last_selection(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 3)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _number(at, "random_size").set_value(3).run()

    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    first = _search_result(at)
    assert first is not None
    first_ids = {item.beatmapset.id for item in first.scored}
    assert len(first_ids) == 3
    assert _checkbox(at, "random_avoid_seen").value is True

    _button(at, "random_selection").click().run()
    assert not at.exception
    assert at.session_state["result"].count == 0

    _checkbox(at, "random_avoid_seen").set_value(False).run()
    _button(at, "random_selection").click().run()
    assert not at.exception
    repeated = _search_result(at)
    assert repeated is not None
    assert {item.beatmapset.id for item in repeated.scored} == first_ids
    # With only three eligible maps and a three-card batch, membership cannot
    # change; the UI must say so instead of pretending this is a new pool.
    assert "это все доступные карты" in _texts(at).casefold()


def test_random_session_resets_when_filters_change(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 12)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _number(at, "random_size").set_value(3).run()

    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    assert at.session_state["random_meta"]["round"] == 1
    assert at.session_state["random_meta"]["session_seen"] == 3

    _go_settings_section(at, "filters")
    _versioned(at.number_input, "flt_stars_min").set_value(5.0).run()
    _go(at, "search")
    _number(at, "random_size").set_value(3).run()
    _button(at, "random_selection").click().run()
    assert not at.exception
    meta = at.session_state["random_meta"]
    assert meta["round"] == 1
    assert meta["session_seen"] == 3


def test_random_session_does_not_repeat_across_large_selections(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 82)
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    _number(at, "random_size").set_value(40).run()

    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    first = _search_result(at)
    assert first is not None and first.count == 40
    first_ids = {item.beatmapset.id for item in first.scored}

    _button(at, "random_selection").click().run()
    assert not at.exception
    second = _search_result(at)
    assert second is not None and second.count == 40
    second_ids = {item.beatmapset.id for item in second.scored}
    assert first_ids.isdisjoint(second_ids)

    _button(at, "random_selection").click().run()
    assert not at.exception
    third = _search_result(at)
    assert third is not None
    third_ids = {item.beatmapset.id for item in third.scored}
    assert third.count == 2
    assert first_ids.isdisjoint(third_ids)
    assert second_ids.isdisjoint(third_ids)
    assert at.session_state["random_meta"]["session_seen"] == 82


def test_random_selection_keeps_strict_filters(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 2)
    store.save_beatmapset(
        make_beatmapset(
            99,
            title="Too hard",
            beatmaps=(
                make_beatmap(
                    991,
                    99,
                    stars=8.5,
                    total_length=90,
                    hit_length=85,
                ),
            ),
        )
    )
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _number(at, "random_size").set_value(3).run()
    _button(at, "random_selection").click().run()
    assert not at.exception, _texts(at)
    result = _search_result(at)
    assert result is not None
    assert result.count == 2
    assert 99 not in {item.beatmapset.id for item in result.scored}
    assert result.origin == "local_cache"
    assert result.api_requests == 0
    assert at.session_state["random_meta"]["local_strict_passed"] == 2
    assert store.search_run_count() == 0


def test_random_pick_is_strict_and_repeatable_from_current_result(app_env) -> None:
    store = Storage(app_env)
    _seed_many_cached_maps(store, 30)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "random_selection").click().run()
    result = _search_result(at)
    assert result is not None and result.count == 12
    strict_ids = [item.beatmap.id for item in result.scored]
    history_before = store.history_count()

    _button(at, "random_pick").click().run()
    assert not at.exception
    first = at.session_state["result_pick_id"]
    assert first in strict_ids
    _button(at, "random_pick").click().run()
    second = at.session_state["result_pick_id"]
    # Repeats between separate clicks are allowed; the pick must stay strict.
    assert first in strict_ids and second in strict_ids
    assert [item.beatmap.id for item in _search_result(at).scored] == strict_ids
    assert store.history_count() == history_before + 2
    assert {
        row["surface"] for row in store.history_rows(limit=2)
    } == {"random_pick"}


def test_gameplay_preview_uses_the_selected_difficulty_without_searching(
    app_env, monkeypatch
) -> None:
    beatmap = make_beatmap(777, 70, stars=5.4, version="Preview Test")
    store = Storage(app_env)
    store.save_beatmapset(
        make_beatmapset(70, title="Preview Song", beatmaps=(beatmap,))
    )
    requested: list[int] = []
    sample = (
        "osu file format v14\n\n[Metadata]\nArtist:Preview Artist\n"
        "Title:Preview Song\nCreator:Mapper\nVersion:Preview Test\n\n"
        "[General]\nMode: 0\n\n[Difficulty]\nApproachRate:9\n"
        "CircleSize:4\nOverallDifficulty:8\n\n[TimingPoints]\n0,500,4,2,1,60,1,0\n"
        "\n[HitObjects]\n100,100,1000,1,0,0:0:0:0:\n"
    )

    def fake_load_preview(
        beatmap_id: int, cache_dir: Path, *, fetcher=None
    ):
        requested.append(beatmap_id)
        return gameplay.parse_osu(sample, beatmap_id=beatmap_id)

    monkeypatch.setattr(gameplay, "load_preview", fake_load_preview)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    assert not at.exception
    _button(at, "preview_777").click().run()
    assert not at.exception
    assert at.session_state["preview_beatmap_id"] == 777
    assert requested == [777]
    assert "Preview Test" in _texts(at)
    assert "именно этой difficulty" in _texts(at)
    assert store.search_run_count() == 0


def test_gameplay_preview_renders_inside_the_selected_card(
    app_env, monkeypatch
) -> None:
    beatmap = make_beatmap(779, 72, stars=5.4, version="Inline Preview")
    store = Storage(app_env)
    store.save_beatmapset(
        make_beatmapset(72, title="Inline Song", beatmaps=(beatmap,))
    )
    sample = (
        "osu file format v14\n\n[Metadata]\nArtist:Preview Artist\n"
        "Title:Inline Song\nCreator:Mapper\nVersion:Inline Preview\n\n"
        "[General]\nMode: 0\n\n[Difficulty]\nApproachRate:9\n"
        "CircleSize:4\nOverallDifficulty:8\n\n[TimingPoints]\n0,500,4,2,1,60,1,0\n"
        "\n[HitObjects]\n100,100,1000,1,0,0:0:0:0:\n"
    )

    def fake_load_preview(
        beatmap_id: int, cache_dir: Path, *, fetcher=None
    ):
        return gameplay.parse_osu(sample, beatmap_id=beatmap_id)

    monkeypatch.setattr(gameplay, "load_preview", fake_load_preview)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    _button(at, "preview_779").click().run()

    assert not at.exception
    card = next(
        container
        for container in at.container
        if any(button.key == "preview_779" for button in container.button)
    )
    assert "▶ Превью геймплея" in [
        subheader.value for subheader in card.subheader
    ]
    assert any(button.key == "close_gameplay_preview" for button in card.button)


def _seed_app_library(store: Storage, count: int = 2) -> list[int]:
    ids: list[int] = []
    for index in range(1, count + 1):
        beatmap = make_beatmap(
            100 + index,
            index,
            stars=4.0 + index / 10,
            version=f"Library {index}",
            bpm=160 + index,
        )
        store.save_beatmapset(
            make_beatmapset(
                index,
                title=f"Library Song {index}",
                artist=f"Library Artist {index}",
                creator=f"Library Mapper {index}",
                beatmaps=(beatmap,),
            )
        )
        sequence = SequenceAnalysis(
            analyzed_notes=100,
            total_notes=100,
            labels=("streams", "jump_aim") if index == 1 else ("bursts",),
        )
        store.save_analysis(
            beatmap.id,
            status="measured" if index == 1 else "partial",
            source="download",
            analyzer_version=patterns.ANALYZER_VERSION,
            file_mtime_ns=1,
            file_size=1,
            scope="" if index == 1 else "preview limit",
            sequence_json=analysis.sequence_to_json(sequence),
        )
        ids.append(beatmap.id)
    return ids


def test_library_page_shows_database_cards_and_analysis_state(
    app_env,
) -> None:
    store = Storage(app_env)
    _seed_app_library(store, 2)
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "library"
    at.run()
    assert not at.exception, _texts(at)
    assert "Библиотека карт" in [header.value for header in at.header]
    text = _texts(at)
    assert "Library Song 1" in text
    assert "Library Song 2" in text
    assert "streams" in text.casefold() or "стримы" in text.casefold()
    assert "частичный анализ" in text.casefold()
    assert any(
        button.key == "library_details_101" for button in at.button
    )


def test_library_pattern_filter_follows_active_preset(app_env) -> None:
    store = Storage(app_env)
    _seed_app_library(store, 1)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    at.sidebar.selectbox[0].select("Aim Farm").run()
    at.session_state["nav"] = "library"
    at.run()
    assert not at.exception, _texts(at)
    assert at.session_state["library_preset_patterns"] == ["jump_aim"]
    widget = next(
        item for item in at.multiselect if item.key == "library_preset_patterns"
    )
    assert widget.options == ["Прыжковый аим"]
    other = next(
        item for item in at.multiselect if item.key == "library_other_patterns"
    )
    assert "Переменный ритм" not in other.options

    at.sidebar.selectbox[0].select("Streams & Stamina").run()
    at.session_state["nav"] = "library"
    at.run()
    assert not at.exception, _texts(at)
    selected = at.session_state["library_preset_patterns"]
    assert selected == ["streams", "high_density"]
    assert "jump_aim" not in selected
    widget = next(
        item for item in at.multiselect if item.key == "library_preset_patterns"
    )
    assert widget.options == ["Стримы", "Высокая плотность"]


def test_library_scope_filter_is_database_backed(app_env) -> None:
    store = Storage(app_env)
    _seed_app_library(store, 2)
    pending = make_beatmap(202, 20, version="Queued")
    store.save_beatmapset(
        make_beatmapset(20, title="Queued Library", beatmaps=(pending,))
    )
    store.enqueue_analysis([(202, 100, "test")])
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "library"
    at.run()
    assert "Library Song 1" in _texts(at)
    assert "Queued Library" not in _texts(at)
    at.session_state["library_scope"] = "pending"
    at.run()
    assert "Queued Library" in _texts(at)
    assert "Library Song 1" not in _texts(at)


def test_library_pagination_moves_between_local_sets(app_env) -> None:
    store = Storage(app_env)
    _seed_app_library(store, 14)
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["nav"] = "library"
    at.run()
    assert not at.exception, _texts(at)
    assert int(at.session_state["library_page"]) == 1
    _button(at, "library_next").click().run()
    assert not at.exception, _texts(at)
    assert int(at.session_state["library_page"]) == 2
    assert "Страница 2" in _texts(at)


def test_library_empty_catalog_explains_next_step(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "library"
    at.run()
    assert not at.exception
    assert "Локальный каталог пуст" in _texts(at)


def test_library_works_without_network(app_env, monkeypatch) -> None:
    store = Storage(app_env)
    _seed_app_library(store, 1)
    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("library used network")
        ),
    )
    at = AppTest.from_file(APP, default_timeout=90)
    at.session_state["nav"] = "library"
    at.run()
    assert not at.exception
    assert "Library Song 1" in _texts(at)


def test_gameplay_preview_error_offers_the_original_mapset(
    app_env, monkeypatch
) -> None:
    beatmap = make_beatmap(778, 71, stars=5.4, version="Unavailable")
    store = Storage(app_env)
    store.save_beatmapset(
        make_beatmapset(71, title="Unavailable Song", beatmaps=(beatmap,))
    )

    def fake_load_preview(beatmap_id: int, cache_dir: Path, *, fetcher=None):
        raise gameplay.GameplayError("osu! вернул пустой .osu-файл.")

    monkeypatch.setattr(gameplay, "load_preview", fake_load_preview)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    _button(at, "run_search").click().run()
    _button(at, "preview_778").click().run()
    assert not at.exception
    text = _texts(at)
    assert "Превью недоступно" in text
    assert "пустой .osu-файл" in text
    assert "Открыть mapset на osu!" in text
    assert "https://osu.ppy.sh/beatmapsets/71" in text
    assert store.search_run_count() == 0


