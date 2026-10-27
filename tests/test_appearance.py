from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import appearance
import ui_theme
from appearance import AppearanceError, AppearanceSettings
from storage import Storage


def _image_bytes(
    mode: str = "RGB",
    size: tuple[int, int] = (320, 180),
    fmt: str = "PNG",
    color: object = (220, 80, 140),
) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def test_image_is_validated_normalized_and_capped() -> None:
    processed = appearance.process_image(
        _image_bytes(size=(3000, 1500), fmt="PNG")
    )
    assert processed.source_format == "PNG"
    assert (processed.width, processed.height) == (2560, 1280)
    assert processed.mime_type == "image/webp"
    assert processed.data.startswith(b"RIFF")
    with Image.open(BytesIO(processed.data)) as image:
        assert image.format == "WEBP"
        assert image.size == (2560, 1280)


def test_invalid_and_unsupported_images_are_rejected() -> None:
    with pytest.raises(AppearanceError, match="пуст"):
        appearance.process_image(b"")
    with pytest.raises(AppearanceError, match="повреждён"):
        appearance.process_image(b"this is not an image")
    with pytest.raises(AppearanceError, match="PNG"):
        appearance.process_image(_image_bytes(fmt="GIF"))
    with pytest.raises(AppearanceError, match="МБ"):
        appearance.process_image(b"x" * (appearance.MAX_UPLOAD_BYTES + 1))


def test_excessive_pixel_count_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(appearance, "MAX_PIXELS", 100)
    with pytest.raises(AppearanceError, match="пикселям"):
        appearance.process_image(_image_bytes(size=(20, 20)))


def test_appearance_persists_separately_from_presets(
    storage: Storage, tmp_path: Path
) -> None:
    processed = appearance.process_image(_image_bytes())
    filename = appearance.install_background(storage, processed)
    settings = AppearanceSettings(
        theme="osu_minimal",
        background_file=filename,
        background_name="my background.png",
        fit="contain",
        position_x=12,
        position_y=88,
        dim=0.55,
        blur=9,
        opacity=0.75,
        panel_opacity=0.91,
        accent="#00ccaa",
    )
    appearance.save_appearance(storage, settings)

    loaded = appearance.load_appearance(storage)
    assert loaded == settings
    assert appearance.background_path(storage, loaded) == (
        tmp_path / "appearance" / filename
    )
    assert appearance.background_bytes(storage, loaded) == processed.data
    uri = appearance.background_data_uri(storage, loaded)
    assert uri is not None
    assert uri.startswith("data:image/webp;base64,")

    # Appearance is one preference, never part of a preset configuration.
    assert storage.get_preference(appearance.PREFERENCE_KEY)
    mixed = storage.get_preset_by_name("Mixed Aim")
    assert mixed is not None
    assert "appearance" not in repr(mixed.config.to_dict())
    assert "background_file" not in repr(mixed.config.to_dict())


def test_corrupt_preference_falls_back_to_defaults(storage: Storage) -> None:
    storage.set_preference(appearance.PREFERENCE_KEY, "{not-json")
    assert appearance.load_appearance(storage) == appearance.default_appearance()


def test_only_safe_stored_filenames_are_accepted() -> None:
    loaded = AppearanceSettings.from_dict(
        {
            "background_file": "../escape.webp",
            "theme": "nonsense",
            "fit": "stretch",
            "accent": "red",
            "position_x": 500,
            "opacity": -4,
        }
    )
    # A directory component is stripped; the file can only resolve inside the
    # appearance directory.
    assert loaded.background_file == "escape.webp"
    assert loaded.theme == "modern_glass"
    assert loaded.fit == "cover"
    assert loaded.accent == "#ff66aa"
    assert loaded.position_x == 100
    assert loaded.opacity == 0.05
    loaded.validate()


def test_old_theme_ids_migrate_without_losing_background() -> None:
    legacy_default = AppearanceSettings.from_dict(
        {"theme": "default", "background_file": "background-a.webp"}
    )
    legacy_dark = AppearanceSettings.from_dict({"theme": "dark"})
    legacy_custom = AppearanceSettings.from_dict(
        {"theme": "custom", "accent": "#00ccaa"}
    )
    assert legacy_default.theme == "modern_glass"
    assert legacy_default.background_file == "background-a.webp"
    assert legacy_dark.theme == "dark_clean"
    assert legacy_custom.theme == "modern_glass"
    assert legacy_custom.accent == "#00ccaa"


def test_three_themes_have_distinct_stylesheets() -> None:
    settings = AppearanceSettings()
    css = {
        theme: ui_theme.build_theme_css(
            AppearanceSettings(theme=theme), None
        )
        for theme in appearance.THEMES
    }
    assert set(css) == {"modern_glass", "osu_minimal", "dark_clean"}
    assert "osu! Minimal" in ui_theme.THEME_LABELS["osu_minimal"]
    assert "--osu-bg: #17111D" in css["osu_minimal"]
    assert "--osu-bg: #1B1620" in css["dark_clean"]
    assert "--osu-bg: #19131F" in css["modern_glass"]
    assert len(set(css.values())) == 3


def test_theme_keeps_sidebar_open_control_visible() -> None:
    css = ui_theme.build_theme_css(AppearanceSettings(), None)
    assert 'button[aria-label="Expand sidebar"]' in css
    assert 'button[aria-label="Collapse sidebar"]' in css
    assert '[data-testid="stSidebarCollapseButton"]' in css
    assert '[data-testid="stToolbar"] {\n  display: flex !important;' in css


def test_delete_and_reset_remove_local_files(storage: Storage) -> None:
    processed = appearance.process_image(_image_bytes())
    filename = appearance.install_background(storage, processed)
    settings = AppearanceSettings(
        theme="osu_minimal", background_file=filename, background_name="x.png"
    )
    appearance.save_appearance(storage, settings)
    path = appearance.background_path(storage, settings)
    assert path is not None and path.is_file()

    deleted = appearance.delete_background(storage, settings)
    assert deleted.theme == "osu_minimal"
    assert deleted.background_file is None
    assert not path.exists()
    assert appearance.load_appearance(storage).background_file is None

    other = appearance.install_background(storage, processed)
    appearance.save_appearance(
        storage, AppearanceSettings(theme="dark_clean", background_file=other)
    )
    appearance.reset_appearance(storage)
    assert not (storage.path.parent / "appearance" / other).exists()
    assert appearance.load_appearance(storage) == appearance.default_appearance()


def test_theme_css_applies_effects_only_to_the_fixed_background() -> None:
    settings = AppearanceSettings(
        theme="modern_glass",
        accent="#33ddaa",
        fit="contain",
        position_x=25,
        position_y=75,
        dim=0.5,
        blur=7,
        opacity=0.65,
        panel_opacity=0.9,
    )
    uri = "data:image/webp;base64,AAAA"
    css = ui_theme.build_theme_css(settings, uri)
    assert "--osu-accent: #33ddaa" in css
    assert 'url("data:image/webp;base64,AAAA")' in css
    assert "background-position:25% 75%" in css
    assert "filter:blur(7px)" in css
    assert "opacity:0.65" in css
    assert 'content: ""' in css
    assert "position: fixed" in css
    assert "[data-testid=\"stAppViewContainer\"]::before" in css

    preview = ui_theme.preview_html(settings, uri)
    assert "Recommendation 84" in preview
    assert "data:image/webp;base64,AAAA" in preview
