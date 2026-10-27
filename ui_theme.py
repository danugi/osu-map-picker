"""Central design tokens and the single Streamlit stylesheet.

All pages use the same variables.  The background lives on a fixed
pseudo-element, so dim/blur/opacity never affect cards, text or controls.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from appearance import AppearanceSettings


@dataclass(frozen=True, slots=True)
class ThemePalette:
    base: str
    base_alt: str
    panel_rgb: tuple[int, int, int]
    text: str
    muted: str
    border: str


PALETTES = {
    "modern_glass": ThemePalette(
        base="#19131F",
        base_alt="#241A2B",
        panel_rgb=(36, 26, 43),
        text="#F7F0F8",
        muted="#C4B4CC",
        border="rgba(73, 54, 79, 0.78)",
    ),
    "osu_minimal": ThemePalette(
        base="#17111D",
        base_alt="#211827",
        panel_rgb=(33, 24, 39),
        text="#F7F0F8",
        muted="#C4B4CC",
        border="rgba(73, 54, 79, 0.70)",
    ),
    "dark_clean": ThemePalette(
        base="#1B1620",
        base_alt="#271D2E",
        panel_rgb=(39, 29, 46),
        text="#F7F0F8",
        muted="#C4B4CC",
        border="rgba(73, 54, 79, 0.68)",
    ),
}

THEME_LABELS = {
    "modern_glass": "Modern Glass",
    "osu_minimal": "osu! Minimal",
    "dark_clean": "Dark Clean",
}

THEME_DESCRIPTIONS = {
    "modern_glass": "Тёмная нейтральная сцена, плотные поверхности и акцент osu!.",
    "osu_minimal": "Компактные поверхности, розовый акцент и строгие формы.",
    "dark_clean": "Нейтральный графит, спокойные границы и минимальный визуальный шум.",
}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _rgba(rgb: tuple[int, int, int], alpha: float) -> str:
    return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha:g})"


def _readable_on(accent: str) -> str:
    r, g, b = _hex_to_rgb(accent)
    luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return "#171016" if luminance > 0.62 else "#fff8fc"


def build_theme_css(
    settings: AppearanceSettings, background_uri: str | None
) -> str:
    settings.validate()
    palette = PALETTES.get(settings.theme, PALETTES["modern_glass"])
    accent = settings.accent
    accent_rgb = _hex_to_rgb(accent)
    accent_text = _readable_on(accent)
    panel_rgb = palette.panel_rgb
    panel = _rgba(panel_rgb, settings.panel_opacity)
    panel_strong = _rgba(panel_rgb, min(0.98, settings.panel_opacity + 0.08))
    panel_soft = _rgba(panel_rgb, max(0.45, settings.panel_opacity - 0.18))
    accent_soft = _rgba(accent_rgb, 0.15)
    if background_uri:
        background_rule = (
            "background-image:"
            f"linear-gradient(rgba(8, 6, 11, {settings.dim:g}), "
            f"rgba(8, 6, 11, {min(0.96, settings.dim + 0.10):g})), "
            f'url("{background_uri}");'
            f"background-size:{settings.fit};"
            f"background-position:{settings.position_x}% {settings.position_y}%;"
            "background-repeat:no-repeat;"
            f"opacity:{settings.opacity:g};"
            f"filter:blur({settings.blur}px);"
            "transform:scale(1.035);"
        )
    else:
        background_rule = f"background: {palette.base};"
    css = f"""
<style>
:root {{
  --osu-accent: {accent};
  --osu-accent-rgb: {accent_rgb[0]}, {accent_rgb[1]}, {accent_rgb[2]};
  --osu-accent-text: {accent_text};
  --osu-accent-soft: {accent_soft};
  --osu-lavender: #B9A3D0;
  --osu-bg: {palette.base};
  --osu-bg-alt: {palette.base_alt};
  --osu-surface: #241A2B;
  --osu-surface-raised: #302239;
  --osu-surface-input: #211827;
  --osu-panel: {panel};
  --osu-panel-strong: {panel_strong};
  --osu-panel-soft: {panel_soft};
  --osu-text: {palette.text};
  --osu-muted: {palette.muted};
  --osu-border: {palette.border};
  --osu-border-strong: rgba(255, 255, 255, 0.18);
  --osu-success: #76d7a2;
  --osu-warning: #ffc266;
  --osu-danger: #ff7f9f;
  --osu-radius-btn: 8px;
  --osu-radius-sm: 8px;
  --osu-radius-md: 10px;
  --osu-radius-lg: 12px;
  --osu-space-1: 0.35rem;
  --osu-space-2: 0.65rem;
  --osu-space-3: 1rem;
  --osu-space-4: 1.5rem;
  --osu-space-5: 2.2rem;
  --osu-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.22);
  --osu-shadow-md: 0 2px 6px rgba(0, 0, 0, 0.24);
  --osu-transition: 120ms ease;
  --osu-font: "Segoe UI Variable Text", "Segoe UI", Inter, system-ui, -apple-system, sans-serif;
}}

html, body, [class*="css"] {{
  font-family: var(--osu-font);
}}
.stApp {{
  background: var(--osu-bg);
  color: var(--osu-text);
}}
[data-testid="stAppViewContainer"] {{
  position: relative;
  isolation: isolate;
  background: transparent;
}}
[data-testid="stAppViewContainer"]::before {{
  content: "";
  position: fixed;
  inset: -2.5%;
  z-index: 0;
  pointer-events: none;
  {background_rule}
}}
[data-testid="stAppViewContainer"] > * {{
  position: relative;
  z-index: 1;
}}
[data-testid="stHeader"] {{
  background: var(--osu-bg);
}}
[data-testid="stMain"] {{
  background: transparent;
}}
.block-container {{
  max-width: 1420px;
  padding-top: 2.2rem;
  padding-bottom: 5rem;
}}
h1, h2, h3, h4 {{
  color: var(--osu-text);
  letter-spacing: -0.018em;
  font-weight: 700;
}}
h1 {{
  font-size: clamp(2rem, 3.1vw, 3rem);
  line-height: 1.06;
}}
h1::after {{
  content: "";
  display: block;
  width: 48px;
  height: 3px;
  margin-top: 0.5rem;
  border-radius: 2px;
  background: var(--osu-accent);
}}
p, li, label, .stMarkdown {{
  color: var(--osu-text);
}}
[data-testid="stCaptionContainer"], .stCaption, small {{
  color: var(--osu-muted) !important;
}}
a {{
  color: var(--osu-accent) !important;
  text-decoration: none;
  transition: color var(--osu-transition);
}}
a:hover {{
  color: var(--osu-text) !important;
}}

section[data-testid="stSidebar"] {{
  background: var(--osu-bg);
  border-right: 1px solid var(--osu-border);
}}
section[data-testid="stSidebar"] .block-container {{
  padding-top: 1.2rem;
}}
section[data-testid="stSidebar"] h1 {{
  font-size: 1.35rem;
  margin-bottom: 0;
}}
section[data-testid="stSidebar"] h1::after {{
  width: 46px;
  height: 3px;
}}
section[data-testid="stSidebar"] [role="radiogroup"] label {{
  border-radius: var(--osu-radius-btn);
  padding: 0.38rem 0.5rem;
  transition: background var(--osu-transition);
}}
section[data-testid="stSidebar"] [role="radiogroup"] label:hover {{
  background: var(--osu-accent-soft);
}}

[data-testid="stVerticalBlockBorderWrapper"] {{
  background: var(--osu-panel-strong);
  border: 1px solid var(--osu-border) !important;
  border-radius: var(--osu-radius-md) !important;
  box-shadow: var(--osu-shadow-sm);
  overflow: hidden;
  transition: border-color var(--osu-transition), background var(--osu-transition);
}}
[data-testid="stVerticalBlockBorderWrapper"]:hover {{
  border-color: rgba(var(--osu-accent-rgb), 0.32) !important;
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ transition-duration: 1ms !important; }}
}}

.stButton > button, .stDownloadButton > button, .stLinkButton > a,
[data-testid="stFormSubmitButton"] > button {{
  border-radius: var(--osu-radius-btn) !important;
  border: 1px solid var(--osu-border-strong) !important;
  background: var(--osu-surface-raised) !important;
  color: var(--osu-text) !important;
  font-weight: 600 !important;
  min-height: 2.35rem;
  padding: 0.35rem 0.9rem !important;
  box-shadow: none !important;
  transition: background var(--osu-transition), border-color var(--osu-transition) !important;
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stLinkButton > a:hover,
[data-testid="stFormSubmitButton"] > button:hover {{
  background: #3A2B44 !important;
  border-color: rgba(var(--osu-accent-rgb), 0.45) !important;
  color: var(--osu-text) !important;
}}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {{
  background: var(--osu-accent) !important;
  color: var(--osu-accent-text) !important;
  border-color: var(--osu-accent) !important;
}}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {{
  background: color-mix(in srgb, var(--osu-accent) 88%, white) !important;
  border-color: transparent !important;
}}
.stButton > button:disabled,
[data-testid="stFormSubmitButton"] > button:disabled {{
  opacity: 0.45 !important;
  background: #1B1520 !important;
  border-color: var(--osu-border) !important;
  color: var(--osu-muted) !important;
}}
[class*="st-key-preset_delete"] button,
[class*="st-key-taste_delete"] button,
[class*="st-key-history_remove"] button,
[class*="st-key-appearance_remove"] button {{
  color: var(--osu-danger) !important;
  border-color: color-mix(in srgb, var(--osu-danger) 38%, transparent) !important;
}}
[class*="st-key-preset_delete"] button:hover,
[class*="st-key-taste_delete"] button:hover,
[class*="st-key-history_remove"] button:hover,
[class*="st-key-appearance_remove"] button:hover {{
  background: rgba(255, 127, 159, 0.10) !important;
}}
.stButton > button:focus-visible, input:focus-visible, select:focus-visible {{
  outline: 2px solid var(--osu-accent) !important;
  outline-offset: 2px;
}}

[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div,
.stNumberInput [data-baseweb="input"], .stDateInput [data-baseweb="input"] {{
  background: var(--osu-surface-input) !important;
  border-color: var(--osu-border) !important;
  border-radius: var(--osu-radius-btn) !important;
  color: var(--osu-text) !important;
  transition: border-color var(--osu-transition);
}}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within {{
  border-color: var(--osu-accent) !important;
  box-shadow: 0 0 0 1px var(--osu-accent) !important;
}}
[data-baseweb="popover"] div[role="listbox"] {{
  background: var(--osu-bg-alt) !important;
  border: 1px solid var(--osu-border) !important;
  border-radius: var(--osu-radius-md) !important;
}}
[data-baseweb="tag"],
[data-testid="stMultiSelectTagsContainer"] [data-tag] {{
  background: rgba(var(--osu-accent-rgb), 0.14) !important;
  color: var(--osu-text) !important;
  border: 1px solid rgba(var(--osu-accent-rgb), 0.28) !important;
  border-radius: 6px !important;
}}
[data-testid="stMultiSelectTagsContainer"] [data-tag] span {{
  color: var(--osu-text) !important;
}}

.stTabs [data-baseweb="tab-list"] {{
  gap: 0.35rem;
  border-bottom: 1px solid var(--osu-border);
}}
.stTabs [data-baseweb="tab"] {{
  border-radius: var(--osu-radius-btn);
  padding: 0.35rem 0.85rem;
  color: var(--osu-muted);
}}
.stTabs [aria-selected="true"] {{
  background: var(--osu-accent-soft) !important;
  color: var(--osu-text) !important;
}}
.stTabs [data-baseweb="tab-highlight"] {{
  background: var(--osu-accent) !important;
}}
details, [data-testid="stExpander"] {{
  background: var(--osu-surface-input);
  border: 1px solid var(--osu-border) !important;
  border-radius: var(--osu-radius-md) !important;
  overflow: hidden;
}}
[data-testid="stExpander"] summary {{
  font-weight: 620;
}}
[data-testid="stExpander"] summary:hover {{
  color: var(--osu-accent) !important;
}}

[data-testid="stMetric"] {{
  background: var(--osu-surface);
  border: 1px solid var(--osu-border);
  border-radius: var(--osu-radius-md);
  padding: 0.7rem 0.85rem;
}}
[data-testid="stMetricValue"] {{
  color: var(--osu-text);
  font-size: 1.45rem;
}}
[data-testid="stMetricLabel"] {{
  color: var(--osu-muted);
}}

[data-testid="stAlert"] {{
  border-radius: var(--osu-radius-md) !important;
  border: 1px solid var(--osu-border) !important;
  background: var(--osu-surface) !important;
}}
[data-testid="stAlertContainer"] {{
  background: var(--osu-surface) !important;
  border: 1px solid var(--osu-border) !important;
  border-left: 3px solid var(--osu-accent) !important;
  border-radius: var(--osu-radius-md) !important;
  color: var(--osu-text) !important;
}}
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"])
  [data-testid="stAlertContainer"] {{
  border-left-color: var(--osu-warning) !important;
}}
[data-testid="stAlert"]:has([data-testid="stAlertContentError"])
  [data-testid="stAlertContainer"] {{
  border-left-color: var(--osu-danger) !important;
}}
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"])
  [data-testid="stAlertContainer"] {{
  border-left-color: var(--osu-success) !important;
}}
[data-testid="stAlert"] p {{
  color: var(--osu-text) !important;
}}
[data-testid="stDataFrame"], [data-testid="stTable"] {{
  border: 1px solid var(--osu-border);
  border-radius: var(--osu-radius-md);
  overflow: hidden;
}}
hr {{
  border-color: var(--osu-border) !important;
}}
.stSlider [data-baseweb="slider"] [role="slider"] {{
  background: var(--osu-accent) !important;
  border-color: var(--osu-accent) !important;
}}
.stCheckbox [data-testid="stCheckbox"] span[aria-checked="true"],
.stCheckbox [data-baseweb="checkbox"] span[aria-checked="true"] {{
  background-color: var(--osu-accent) !important;
  border-color: var(--osu-accent) !important;
}}
.stProgress > div > div > div > div {{
  background: var(--osu-accent) !important;
}}
::selection {{
  background: rgba(var(--osu-accent-rgb), 0.35);
}}
::-webkit-scrollbar {{ width: 10px; height: 10px; }}
::-webkit-scrollbar-track {{ background: rgba(0,0,0,0.12); }}
::-webkit-scrollbar-thumb {{
  background: rgba(var(--osu-accent-rgb), 0.34);
  border-radius: 99px;
  border: 2px solid transparent;
  background-clip: padding-box;
}}

.osu-eyebrow {{
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--osu-accent);
  font-size: 0.76rem;
  font-weight: 650;
  letter-spacing: 0.02em;
  margin-bottom: 0.35rem;
}}
.osu-hero-meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin: 0.55rem 0 0.2rem;
}}
.osu-chip {{
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--osu-border);
  background: var(--osu-surface-raised);
  color: var(--osu-text);
  border-radius: 6px;
  padding: 0.2rem 0.52rem;
  font-size: 0.76rem;
  line-height: 1.25;
  white-space: nowrap;
}}
.osu-chip.accent {{
  border-color: rgba(var(--osu-accent-rgb), 0.48);
  background: var(--osu-accent-soft);
  color: var(--osu-accent);
}}
.osu-chip.muted {{ color: var(--osu-muted); }}
.osu-chip.good {{ color: var(--osu-success); border-color: rgba(118, 215, 162, 0.28); }}
.osu-chip.warn {{ color: var(--osu-warning); border-color: rgba(255, 194, 102, 0.28); }}

.osu-card-head {{
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.8rem;
}}
.osu-card-title {{
  font-size: 1.14rem;
  font-weight: 750;
  line-height: 1.28;
  margin: 0;
  color: var(--osu-text);
}}
.osu-card-title a {{ color: var(--osu-text) !important; }}
.osu-card-title a:hover {{ color: var(--osu-accent) !important; }}
.osu-card-sub {{
  color: var(--osu-muted);
  font-size: 0.85rem;
  margin-top: 0.24rem;
}}
.osu-card-stats {{
  display: grid;
  grid-template-columns: repeat(4, minmax(64px, 1fr));
  gap: 0.5rem;
  margin: 0.75rem 0 0.45rem;
}}
.osu-stat {{
  background: rgba(48, 34, 57, 0.55);
  border: 1px solid var(--osu-border);
  border-radius: var(--osu-radius-sm);
  padding: 0.48rem 0.58rem;
  min-width: 0;
}}
.osu-stat .k {{
  color: var(--osu-muted);
  font-size: 0.68rem;
  white-space: nowrap;
}}
.osu-stat .v {{
  color: var(--osu-text);
  font-size: 0.95rem;
  font-weight: 720;
  margin-top: 0.1rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.osu-cover {{
  display: block;
  position: relative;
  min-height: 118px;
  aspect-ratio: 2.35 / 1;
  border-radius: var(--osu-radius-sm);
  overflow: hidden;
  border: 1px solid var(--osu-border);
  background: var(--osu-surface-raised);
}}
.osu-cover-link {{
  position: absolute;
  inset: 0;
  z-index: 3;
}}
.osu-cover img {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}}
.osu-cover:hover img {{ opacity: 0.96; }}
.osu-cover-fallback {{
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 118px;
  color: rgba(255,255,255,0.80);
  font-weight: 800;
  font-size: clamp(1.6rem, 4vw, 2.8rem);
  letter-spacing: -0.05em;
}}
.osu-cover-badge {{
  position: absolute;
  left: 0.55rem;
  bottom: 0.55rem;
  border-radius: 4px;
  padding: 0.18rem 0.45rem;
  background: rgba(25, 19, 31, 0.90);
  border: 1px solid rgba(255,255,255,0.16);
  font-size: 0.7rem;
  font-weight: 650;
  color: #fff;
}}
.osu-score {{
  text-align: right;
  min-width: 96px;
}}
.osu-score .num {{
  font-size: 1.55rem;
  line-height: 1;
  font-weight: 800;
  color: var(--osu-accent);
}}
.osu-score .lbl {{
  color: var(--osu-muted);
  font-size: 0.64rem;
  margin-top: 0.2rem;
  white-space: nowrap;
}}
.osu-links {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  margin-top: 0.35rem;
}}
.osu-links a {{
  border-bottom: 1px solid rgba(var(--osu-accent-rgb), 0.32);
  padding-bottom: 1px;
  font-size: 0.84rem;
}}
.osu-config-line {{
  color: var(--osu-muted);
  font-size: 0.83rem;
  line-height: 1.5;
}}
.osu-note {{
  border-left: 3px solid var(--osu-accent);
  padding: 0.55rem 0.8rem;
  background: var(--osu-accent-soft);
  border-radius: 0 var(--osu-radius-sm) var(--osu-radius-sm) 0;
  color: var(--osu-text);
  font-size: 0.88rem;
}}
.osu-divider {{ height: 1px; background: var(--osu-border); margin: 0.8rem 0; }}
.osu-empty {{
  border: 1px dashed var(--osu-border-strong);
  border-radius: var(--osu-radius-md);
  padding: 1.1rem;
  color: var(--osu-muted);
  text-align: center;
}}
.osu-preview {{
  position: relative;
  min-height: 240px;
  border-radius: var(--osu-radius-lg);
  overflow: hidden;
  border: 1px solid var(--osu-border-strong);
  background: var(--osu-bg);
  isolation: isolate;
}}
.osu-preview-bg {{
  position: absolute;
  inset: -8%;
  z-index: 0;
}}
.osu-preview-overlay {{
  position: absolute;
  inset: 0;
  z-index: 1;
}}
.osu-preview-content {{
  position: relative;
  z-index: 2;
  padding: 1.2rem;
  color: var(--osu-text);
}}
.osu-preview-card {{
  margin-top: 1.2rem;
  max-width: 380px;
  border-radius: var(--osu-radius-md);
  border: 1px solid var(--osu-border-strong);
  background: var(--osu-surface);
  padding: 0.85rem;
}}
@media (max-width: 760px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
  .osu-card-stats {{ grid-template-columns: repeat(2, minmax(80px, 1fr)); }}
  .osu-score {{ display: none; }}
  .osu-discovery-orbit {{ display: none; }}
  .osu-discovery-hero {{ padding: 1rem; }}
}}
</style>
"""
    css += """
<style>
/* Website shell and local-library layout. */
[data-testid="stHeader"] {
  display: block !important;
  height: 0 !important;
  min-height: 0 !important;
  background: transparent !important;
  pointer-events: none;
}
[data-testid="stMainMenu"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"] {
  display: none !important;
}
[data-testid="stToolbar"] {
  display: flex !important;
  width: 0 !important;
  height: 0 !important;
  overflow: visible !important;
  background: transparent !important;
  pointer-events: none;
}
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapseButton"] button {
  visibility: visible !important;
  opacity: 1 !important;
}
[data-testid="stSidebarCollapseButton"] button {
  position: fixed !important;
  top: 16px !important;
  left: 228px !important;
  z-index: 1000;
  width: 34px !important;
  height: 34px !important;
  min-height: 34px !important;
  padding: 0 !important;
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  background: var(--osu-surface-raised) !important;
  color: var(--osu-text) !important;
  border: 1px solid var(--osu-border-strong) !important;
  border-radius: var(--osu-radius-btn) !important;
  box-shadow: none !important;
}
[data-testid="stSidebarCollapseButton"] button:hover {
  border-color: rgba(var(--osu-accent-rgb), 0.45) !important;
}
[data-testid="stHeader"] button[aria-label="Collapse sidebar"],
[data-testid="stHeader"] button[aria-label="Expand sidebar"] {
  pointer-events: auto;
  position: fixed;
  top: 16px;
  z-index: 1000;
  width: 34px;
  height: 34px;
  min-height: 34px;
  padding: 0 !important;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--osu-surface-raised) !important;
  color: var(--osu-text) !important;
  border: 1px solid var(--osu-border-strong) !important;
  border-radius: var(--osu-radius-btn) !important;
  box-shadow: none !important;
}
[data-testid="stHeader"] button[aria-label="Collapse sidebar"] {
  left: 228px;
}
[data-testid="stHeader"] button[aria-label="Expand sidebar"] {
  left: 16px;
}
[data-testid="stHeader"] button[aria-label="Collapse sidebar"]:hover,
[data-testid="stHeader"] button[aria-label="Expand sidebar"]:hover {
  background: var(--osu-surface-raised) !important;
  border-color: rgba(var(--osu-accent-rgb), 0.45) !important;
}
[data-testid="stAppViewContainer"] > .main {
  padding-top: 0 !important;
}
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1480px !important;
  padding-top: 1.35rem !important;
  padding-bottom: 4rem !important;
}
section[data-testid="stSidebar"] {
  width: 274px !important;
  min-width: 274px !important;
  border-right: 1px solid var(--osu-border) !important;
  background: var(--osu-surface) !important;
}
section[data-testid="stSidebar"] .block-container {
  padding: 1.25rem 1rem 2rem !important;
}
.rail-heading {
  color: var(--osu-text);
  font-size: 0.86rem;
  font-weight: 700;
  margin-bottom: 0.2rem;
}
.rail-caption {
  color: var(--osu-muted);
  font-size: 0.78rem;
  line-height: 1.35;
  margin-bottom: 0.8rem;
}
[data-testid="stTextInputRootElement"],
[data-testid="stNumberInputContainer"],
[data-baseweb="select"] > div,
[data-baseweb="input"] {
  background: var(--osu-surface-input) !important;
  border-color: var(--osu-border) !important;
  border-radius: var(--osu-radius-btn) !important;
  color: var(--osu-text) !important;
}
[data-testid="stTextInputRootElement"] input,
[data-testid="stNumberInputContainer"] input,
[data-baseweb="select"] input,
[data-baseweb="select"] span {
  color: var(--osu-text) !important;
}
[data-baseweb="select"] svg { fill: var(--osu-muted) !important; }
[data-testid="stSegmentedControl"] [role="radiogroup"] {
  gap: 0.15rem;
  padding: 0.15rem;
  border: 1px solid var(--osu-border);
  border-radius: var(--osu-radius-btn);
  background: var(--osu-surface-input);
}
[data-testid="stSegmentedControl"] button[role="radio"] {
  background: transparent !important;
  border: 1px solid transparent !important;
  color: var(--osu-muted) !important;
  border-radius: 4px !important;
  min-height: 30px;
  padding: 0.15rem 0.6rem !important;
  font-size: 0.76rem;
}
[data-testid="stSegmentedControl"] button[role="radio"]:hover {
  color: var(--osu-text) !important;
  background: rgba(255,255,255,0.04) !important;
}
[data-testid="stSegmentedControl"] button[role="radio"][aria-checked="true"] {
  color: var(--osu-accent) !important;
  background: rgba(var(--osu-accent-rgb), 0.13) !important;
  border-color: rgba(var(--osu-accent-rgb), 0.34) !important;
}
.stButton > button[kind="secondary"] {
  background: var(--osu-surface-raised) !important;
  color: var(--osu-text) !important;
  border-color: var(--osu-border-strong) !important;
}
.stButton > button[kind="secondary"]:hover {
  background: #3A2B44 !important;
  border-color: rgba(var(--osu-accent-rgb), 0.40) !important;
}
.site-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.35rem 0 1.0rem;
}
.site-brand {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  min-width: 0;
}
.site-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: var(--osu-radius-md);
  background: var(--osu-accent);
  color: var(--osu-accent-text);
  font-size: 1rem;
  font-weight: 800;
  letter-spacing: -0.05em;
}
.site-brand-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.site-brand-kicker {
  color: var(--osu-lavender);
  font-size: 0.7rem;
  font-weight: 600;
}
.site-brand-name {
  color: var(--osu-text);
  font-size: 1.25rem;
  font-weight: 700;
  line-height: 1.12;
  letter-spacing: -0.02em;
}
.site-header-meta {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.site-stat {
  color: var(--osu-muted);
  font-size: 0.76rem;
  white-space: nowrap;
}
.site-stat strong {
  color: var(--osu-text);
  font-size: 0.92rem;
  margin-right: 0.18rem;
}
.site-pill {
  border: 1px solid var(--osu-border);
  border-radius: 6px;
  padding: 0.22rem 0.5rem;
  color: var(--osu-muted);
  font-size: 0.7rem;
  font-weight: 600;
}
.site-pill.online { color: var(--osu-success); border-color: rgba(118, 215, 162, 0.32); }
.site-pill.local { color: var(--osu-warning); border-color: rgba(255, 194, 102, 0.3); }
.st-key-site_nav {
  position: sticky;
  top: 0;
  z-index: 20;
  margin: 0 0 1.25rem;
  padding: 0.2rem 0;
  border-bottom: 1px solid var(--osu-border);
  background: var(--osu-bg);
}
.st-key-site_nav .stButton > button {
  border: 1px solid transparent !important;
  border-radius: var(--osu-radius-btn) !important;
  background: transparent !important;
  color: var(--osu-muted) !important;
  box-shadow: none !important;
  min-height: 38px;
  padding: 0.25rem 0.55rem !important;
  font-size: 0.82rem;
  font-weight: 600;
  letter-spacing: 0;
  text-transform: none;
  transform: none !important;
}
.st-key-site_nav .stButton > button:hover {
  color: var(--osu-text) !important;
  background: var(--osu-surface) !important;
  border-color: var(--osu-border) !important;
}
.st-key-site_nav .stButton > button[kind="primary"] {
  color: var(--osu-accent) !important;
  background: rgba(var(--osu-accent-rgb), 0.10) !important;
  border-color: rgba(var(--osu-accent-rgb), 0.28) !important;
}
.page-kicker {
  color: var(--osu-lavender);
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  margin: 0.2rem 0 0.15rem;
}
h2, [data-testid="stHeadingWithActionElements"] h2 {
  font-size: clamp(1.65rem, 2.5vw, 2.25rem) !important;
  letter-spacing: -0.035em !important;
  margin-top: 0 !important;
}
[data-testid="stCaptionContainer"] {
  max-width: 980px;
}
.discovery-stats, .library-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  margin: 1.0rem 0 0.85rem;
  border: 1px solid var(--osu-border);
  background: var(--osu-border);
  border-radius: var(--osu-radius-md);
  overflow: hidden;
}
.library-summary { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.discovery-stats > div, .library-summary > div {
  background: var(--osu-surface);
  padding: 0.7rem 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}
.discovery-stats strong, .library-summary strong {
  color: var(--osu-text);
  font-size: 1.25rem;
  line-height: 1.1;
}
.discovery-stats span, .library-summary span {
  color: var(--osu-muted);
  font-size: 0.72rem;
}
.section-label {
  color: var(--osu-text);
  font-size: 0.84rem;
  font-weight: 700;
  letter-spacing: 0;
  margin: 1.0rem 0 0.45rem;
}
.library-result-head {
  display: flex;
  align-items: baseline;
  gap: 0.35rem;
  margin: 1.15rem 0 0.6rem;
  color: var(--osu-muted);
  font-size: 0.85rem;
}
.library-result-head strong { color: var(--osu-text); font-size: 1.05rem; }
.library-result-head span { margin-left: auto; color: var(--osu-muted); font-size: 0.75rem; }
[class*="st-key-library_set_"] [data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--osu-surface) !important;
  border-color: var(--osu-border) !important;
  border-radius: 9px !important;
  box-shadow: none !important;
}
[class*="st-key-library_preview_panel"] {
  margin: 1rem 0 1.1rem;
}
[class*="st-key-library_preview_panel"] [data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--osu-surface) !important;
  border-color: var(--osu-border) !important;
}
.preview-heading {
  display: flex;
  flex-direction: column;
  gap: 0.12rem;
}
.preview-kicker {
  color: var(--osu-accent);
  font-size: 0.7rem;
  font-weight: 600;
}
.preview-title {
  color: var(--osu-text);
  font-size: 1.08rem;
  font-weight: 700;
  line-height: 1.25;
}
.preview-sub {
  color: var(--osu-muted);
  font-size: 0.76rem;
}
.library-cover {
  display: block;
  position: relative;
  aspect-ratio: 2.15 / 1;
  min-height: 110px;
  border-radius: var(--osu-radius-sm);
  overflow: hidden;
  background: var(--osu-surface-raised);
  margin-bottom: 0.65rem;
}
.library-cover-link {
  position: absolute;
  inset: 0;
  z-index: 3;
}
.library-cover img {
  position: relative;
  z-index: 1;
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.library-cover:hover img { opacity: 0.96; }
.stDialog .library-cover,
[data-testid="stDialog"] .library-cover {
  aspect-ratio: auto !important;
  min-height: 140px !important;
  height: 180px !important;
  max-height: 180px !important;
}
.library-cover-fallback {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(255,255,255,0.72);
  font-size: 2rem;
  font-weight: 900;
  letter-spacing: -0.06em;
}
.library-cover-top {
  position: absolute;
  inset: 0.5rem 0.5rem auto;
  display: flex;
  justify-content: space-between;
  gap: 0.4rem;
}
.library-status, .library-count {
  background: rgba(25, 19, 31, 0.92);
  border: 1px solid rgba(255,255,255,0.14);
  border-radius: 4px;
  color: #fff;
  padding: 0.14rem 0.4rem;
  font-size: 0.66rem;
  font-weight: 600;
}
.library-count { color: var(--osu-accent); }
.library-set-title {
  font-size: 1rem;
  font-weight: 700;
  line-height: 1.3;
  letter-spacing: -0.01em;
}
.library-set-title a { color: var(--osu-text) !important; }
.library-set-title a:hover { color: var(--osu-accent) !important; }
.library-set-artist { color: var(--osu-text); font-size: 0.84rem; margin-top: 0.12rem; }
.library-set-mapper { color: var(--osu-muted); font-size: 0.72rem; margin-top: 0.08rem; }
.library-diff {
  border-top: 1px solid var(--osu-border);
  margin-top: 0.65rem;
  padding-top: 0.55rem;
}
.library-diff-head {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.library-diff-stars { color: var(--osu-accent); font-weight: 800; font-size: 0.86rem; }
.library-diff-version { color: var(--osu-text); font-size: 0.82rem; font-weight: 650; min-width: 0; }
.library-diff-mode {
  color: var(--osu-muted);
  font-size: 0.68rem;
}
.library-state {
  margin-left: auto;
  border: 1px solid var(--osu-border);
  border-radius: 6px;
  padding: 0.08rem 0.38rem;
  font-size: 0.66rem;
  font-weight: 600;
  white-space: nowrap;
}
.library-state.good { color: var(--osu-success); border-color: rgba(118,215,162,0.3); }
.library-state.warn { color: var(--osu-warning); border-color: rgba(255,194,102,0.3); }
.library-state.bad { color: var(--osu-error); border-color: rgba(255,120,140,0.32); }
.library-state.accent { color: var(--osu-accent); border-color: rgba(var(--osu-accent-rgb),0.35); }
.library-state.muted { color: var(--osu-muted); }
.library-diff-meta { color: var(--osu-muted); font-size: 0.70rem; margin-top: 0.2rem; }
.library-patterns { display: flex; flex-wrap: wrap; gap: 0.25rem; margin-top: 0.32rem; }
.library-patterns.muted { color: var(--osu-muted); font-size: 0.68rem; font-style: italic; }
.pattern-chip {
  color: var(--osu-text);
  border: 1px solid var(--osu-border);
  background: var(--osu-surface-raised);
  border-radius: 6px;
  padding: 0.08rem 0.36rem;
  font-size: 0.66rem;
}
.library-match {
  display: flex;
  gap: 0.7rem;
  flex-wrap: wrap;
  color: var(--osu-muted);
  font-size: 0.66rem;
  margin-top: 0.32rem;
}
.library-match strong { color: var(--osu-text); }
.library-partial { color: var(--osu-warning); font-size: 0.68rem; margin-top: 0.28rem; }
[class*="st-key-library_actions_"] .stButton > button,
[class*="st-key-library_actions_"] .stLinkButton > a {
  min-height: 30px;
  height: 30px;
  padding: 0.1rem 0.4rem !important;
  font-size: 0.72rem !important;
  border-radius: var(--osu-radius-btn) !important;
}
[data-testid="stMetric"] {
  background: var(--osu-surface) !important;
  border-color: var(--osu-border) !important;
  box-shadow: none !important;
}
[data-testid="stDataFrame"], [data-testid="stTable"] {
  border-radius: 6px !important;
  overflow: hidden;
}
[data-testid="stExpander"] summary {
  font-size: 0.8rem;
}
@media (max-width: 1100px) {
  .site-header-meta { display: none; }
  .library-summary, .discovery-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .site-brand-name { font-size: 1.1rem; }
  .site-mark { width: 40px; height: 40px; border-radius: 11px; }
  .st-key-site_nav .stButton > button { font-size: 0.62rem; padding: 0.15rem !important; }
}
</style>
"""
    return css


def preview_html(settings: AppearanceSettings, background_uri: str | None) -> str:
    palette = PALETTES.get(settings.theme, PALETTES["modern_glass"])
    accent_rgb = _hex_to_rgb(settings.accent)
    if background_uri:
        background = (
            f'<div class="osu-preview-bg" style="background-image:url(\'{background_uri}\');'
            f"background-size:{settings.fit};"
            f"background-position:{settings.position_x}% {settings.position_y}%;"
            f"filter:blur({settings.blur}px);opacity:{settings.opacity:g};"
            'transform:scale(1.035)"></div>'
        )
    else:
        background = (
            '<div class="osu-preview-bg" style="background:'
            f"{palette.base}"
            '"></div>'
        )
    overlay = (
        '<div class="osu-preview-overlay" style="background:'
        f"rgba(8,6,11,{settings.dim:g})"
        '"></div>'
    )
    return f"""
<div class="osu-preview">
  {background}
  {overlay}
  <div class="osu-preview-content">
    <div class="osu-eyebrow">Предпросмотр · {html.escape(THEME_LABELS.get(settings.theme, settings.theme))}</div>
    <div style="font-size:1.35rem;font-weight:800;letter-spacing:-.02em">osu! Map Picker</div>
    <div style="color:{palette.muted};font-size:.84rem;margin-top:.2rem">
      Фон, затемнение и акцент применяются только к оформлению.
    </div>
    <div class="osu-preview-card">
      <div style="font-weight:750">Пример карточки beatmapset</div>
      <div style="color:{palette.muted};font-size:.8rem;margin-top:.2rem">6.12★ · 2:18 · score 87 · strict filters</div>
      <div style="display:flex;gap:.35rem;margin-top:.7rem;flex-wrap:wrap">
        <span class="osu-chip accent">Aim</span>
        <span class="osu-chip">Ranked</span>
        <span class="osu-chip good">Recommendation 84</span>
      </div>
    </div>
  </div>
</div>
"""
