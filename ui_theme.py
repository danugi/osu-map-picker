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
        base="#120f16",
        base_alt="#1b1420",
        panel_rgb=(29, 23, 34),
        text="#f8f2f7",
        muted="#b9acb9",
        border="rgba(255, 255, 255, 0.10)",
    ),
    "osu_minimal": ThemePalette(
        base="#0e0e12",
        base_alt="#141419",
        panel_rgb=(19, 19, 24),
        text="#f7f7fa",
        muted="#aaaab6",
        border="rgba(255, 255, 255, 0.13)",
    ),
    "dark_clean": ThemePalette(
        base="#12151b",
        base_alt="#181c24",
        panel_rgb=(25, 29, 37),
        text="#eef2f8",
        muted="#9aa6b7",
        border="rgba(170, 190, 215, 0.13)",
    ),
}

THEME_LABELS = {
    "modern_glass": "Modern Glass",
    "osu_minimal": "osu! Minimal",
    "dark_clean": "Dark Clean",
}

THEME_DESCRIPTIONS = {
    "modern_glass": "Полупрозрачные панели, мягкое свечение и акцент osu!.",
    "osu_minimal": "Почти чёрная сцена, розовый акцент, плотные панели и строгие формы.",
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
    accent_glow = _rgba(accent_rgb, 0.28)
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
        background_rule = (
            "background-image:"
            "radial-gradient(1200px 700px at 12% -10%, "
            f"{_rgba(accent_rgb, 0.20)}, transparent 60%),"
            "radial-gradient(900px 600px at 100% 0%, "
            "rgba(93, 71, 186, 0.18), transparent 55%),"
            "linear-gradient(160deg, "
            f"{palette.base_alt}, {palette.base} 58%, #0b090e);"
        )
    css = f"""
<style>
:root {{
  --osu-accent: {accent};
  --osu-accent-rgb: {accent_rgb[0]}, {accent_rgb[1]}, {accent_rgb[2]};
  --osu-accent-text: {accent_text};
  --osu-accent-soft: {accent_soft};
  --osu-accent-glow: {accent_glow};
  --osu-bg: {palette.base};
  --osu-bg-alt: {palette.base_alt};
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
  --osu-radius-sm: 9px;
  --osu-radius-md: 14px;
  --osu-radius-lg: 20px;
  --osu-space-1: 0.35rem;
  --osu-space-2: 0.65rem;
  --osu-space-3: 1rem;
  --osu-space-4: 1.5rem;
  --osu-space-5: 2.2rem;
  --osu-shadow-sm: 0 8px 24px rgba(0, 0, 0, 0.18);
  --osu-shadow-md: 0 16px 44px rgba(0, 0, 0, 0.28);
  --osu-transition: 160ms cubic-bezier(.2, .75, .25, 1);
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
  background: linear-gradient(180deg, rgba(10, 8, 12, 0.82), transparent);
  backdrop-filter: blur(10px);
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
  width: 74px;
  height: 4px;
  margin-top: 0.55rem;
  border-radius: 99px;
  background: linear-gradient(90deg, var(--osu-accent), transparent);
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
  color: var(--osu-accent-text) !important;
  text-shadow: 0 0 18px var(--osu-accent-glow);
}}

section[data-testid="stSidebar"] {{
  background: linear-gradient(180deg, rgba(17, 13, 21, 0.96), rgba(12, 10, 15, 0.94));
  border-right: 1px solid var(--osu-border);
  backdrop-filter: blur(18px);
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
  border-radius: var(--osu-radius-sm);
  padding: 0.38rem 0.5rem;
  transition: background var(--osu-transition), transform var(--osu-transition);
}}
section[data-testid="stSidebar"] [role="radiogroup"] label:hover {{
  background: var(--osu-accent-soft);
  transform: translateX(2px);
}}

[data-testid="stVerticalBlockBorderWrapper"] {{
  background: linear-gradient(145deg, var(--osu-panel-strong), var(--osu-panel));
  border: 1px solid var(--osu-border) !important;
  border-radius: var(--osu-radius-lg) !important;
  box-shadow: var(--osu-shadow-sm);
  overflow: hidden;
  transition: border-color var(--osu-transition), box-shadow var(--osu-transition),
              transform var(--osu-transition);
  animation: osu-rise 260ms ease-out both;
}}
[data-testid="stVerticalBlockBorderWrapper"]:hover {{
  border-color: rgba(var(--osu-accent-rgb), 0.42) !important;
  box-shadow: var(--osu-shadow-md), 0 0 0 1px rgba(var(--osu-accent-rgb), 0.10);
}}
@keyframes osu-rise {{
  from {{ opacity: 0; transform: translateY(5px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
@media (prefers-reduced-motion: reduce) {{
  [data-testid="stVerticalBlockBorderWrapper"] {{ animation: none; }}
  * {{ transition-duration: 1ms !important; }}
}}

.stButton > button, .stDownloadButton > button, .stLinkButton > a,
[data-testid="stFormSubmitButton"] > button {{
  border-radius: 999px !important;
  border: 1px solid rgba(var(--osu-accent-rgb), 0.48) !important;
  background: linear-gradient(135deg, rgba(var(--osu-accent-rgb), 0.20), rgba(255,255,255,0.03)) !important;
  color: var(--osu-text) !important;
  font-weight: 650 !important;
  min-height: 2.45rem;
  padding: 0.38rem 1.05rem !important;
  box-shadow: none !important;
  transition: transform var(--osu-transition), box-shadow var(--osu-transition),
              background var(--osu-transition) !important;
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stLinkButton > a:hover,
[data-testid="stFormSubmitButton"] > button:hover {{
  background: linear-gradient(135deg, rgba(var(--osu-accent-rgb), 0.38), rgba(255,255,255,0.06)) !important;
  border-color: var(--osu-accent) !important;
  box-shadow: 0 8px 22px rgba(var(--osu-accent-rgb), 0.20) !important;
  transform: translateY(-1px);
  color: var(--osu-text) !important;
}}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {{
  background: linear-gradient(135deg, var(--osu-accent), rgba(var(--osu-accent-rgb), 0.72)) !important;
  color: var(--osu-accent-text) !important;
  border: 1px solid transparent !important;
  box-shadow: 0 8px 26px rgba(var(--osu-accent-rgb), 0.22) !important;
}}
.stButton > button:focus-visible, input:focus-visible, select:focus-visible {{
  outline: 2px solid var(--osu-accent) !important;
  outline-offset: 2px;
}}

[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div,
.stNumberInput [data-baseweb="input"], .stDateInput [data-baseweb="input"] {{
  background: rgba(11, 9, 14, 0.48) !important;
  border-color: var(--osu-border) !important;
  border-radius: var(--osu-radius-sm) !important;
  color: var(--osu-text) !important;
  transition: border-color var(--osu-transition), box-shadow var(--osu-transition);
}}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within {{
  border-color: rgba(var(--osu-accent-rgb), 0.68) !important;
  box-shadow: 0 0 0 3px rgba(var(--osu-accent-rgb), 0.11) !important;
}}
[data-baseweb="popover"] div[role="listbox"] {{
  background: #19141e !important;
  border: 1px solid var(--osu-border) !important;
  border-radius: var(--osu-radius-md) !important;
}}
[data-baseweb="tag"] {{
  background: rgba(var(--osu-accent-rgb), 0.20) !important;
  color: var(--osu-text) !important;
  border-radius: 999px !important;
}}

.stTabs [data-baseweb="tab-list"] {{
  gap: 0.35rem;
  border-bottom: 1px solid var(--osu-border);
}}
.stTabs [data-baseweb="tab"] {{
  border-radius: 999px;
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
  background: rgba(18, 14, 22, 0.36);
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
  background: rgba(12, 9, 15, 0.34);
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
  background: var(--osu-panel-soft) !important;
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
  background: linear-gradient(90deg, var(--osu-accent), rgba(var(--osu-accent-rgb), 0.52)) !important;
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
  font-size: 0.74rem;
  font-weight: 750;
  letter-spacing: 0.14em;
  text-transform: uppercase;
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
  background: rgba(255,255,255,0.045);
  color: var(--osu-text);
  border-radius: 999px;
  padding: 0.22rem 0.62rem;
  font-size: 0.78rem;
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
  grid-template-columns: repeat(5, minmax(70px, 1fr));
  gap: 0.55rem;
  margin: 0.85rem 0 0.55rem;
}}
.osu-stat {{
  background: rgba(8, 6, 11, 0.34);
  border: 1px solid var(--osu-border);
  border-radius: var(--osu-radius-sm);
  padding: 0.48rem 0.58rem;
  min-width: 0;
}}
.osu-stat .k {{
  color: var(--osu-muted);
  font-size: 0.66rem;
  text-transform: uppercase;
  letter-spacing: 0.09em;
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
  position: relative;
  min-height: 148px;
  aspect-ratio: 1.9 / 1;
  border-radius: var(--osu-radius-md);
  overflow: hidden;
  border: 1px solid var(--osu-border);
  background:
    radial-gradient(circle at 28% 25%, rgba(var(--osu-accent-rgb), 0.26), transparent 58%),
    linear-gradient(135deg, #24192c, #120f16);
}}
.osu-cover img {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 260ms ease, filter var(--osu-transition);
}}
.osu-cover:hover img {{ transform: scale(1.035); filter: saturate(1.08); }}
.osu-cover-fallback {{
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 148px;
  color: rgba(255,255,255,0.80);
  font-weight: 800;
  font-size: clamp(1.6rem, 4vw, 2.8rem);
  letter-spacing: -0.05em;
}}
.osu-cover-badge {{
  position: absolute;
  left: 0.55rem;
  bottom: 0.55rem;
  border-radius: 999px;
  padding: 0.2rem 0.55rem;
  background: rgba(10, 8, 12, 0.76);
  border: 1px solid rgba(255,255,255,0.16);
  font-size: 0.72rem;
  font-weight: 700;
  color: #fff;
  backdrop-filter: blur(8px);
}}
.osu-score {{
  text-align: right;
  min-width: 82px;
}}
.osu-score .num {{
  font-size: 1.55rem;
  line-height: 1;
  font-weight: 800;
  color: var(--osu-accent);
}}
.osu-score .lbl {{
  color: var(--osu-muted);
  font-size: 0.67rem;
  text-transform: uppercase;
  letter-spacing: 0.10em;
  margin-top: 0.22rem;
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
  border: 1px solid rgba(255,255,255,0.16);
  background: rgba(20, 15, 24, 0.86);
  padding: 0.85rem;
  backdrop-filter: blur(10px);
}}
@media (max-width: 760px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
  .osu-card-stats {{ grid-template-columns: repeat(2, minmax(80px, 1fr)); }}
  .osu-score {{ display: none; }}
}}
</style>
"""
    if settings.theme == "osu_minimal":
        css += """
<style>
/* osu! Minimal: flat, dense, pink accent, no glass. */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: rgba(19, 19, 24, 0.97) !important;
  border-radius: 8px !important;
  box-shadow: none !important;
  animation: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  border-color: rgba(var(--osu-accent-rgb), 0.55) !important;
  box-shadow: none !important;
}
section[data-testid="stSidebar"] {
  background: #101014 !important;
  backdrop-filter: none !important;
}
[data-testid="stHeader"] {
  background: #0e0e12 !important;
  backdrop-filter: none !important;
}
[data-testid="stMetric"], details, [data-testid="stExpander"],
[data-testid="stAlert"], .osu-preview-card {
  background: #141419 !important;
  border-radius: 8px !important;
  box-shadow: none !important;
  backdrop-filter: none !important;
}
.stButton > button, .stDownloadButton > button, .stLinkButton > a,
[data-testid="stFormSubmitButton"] > button {
  border-radius: 7px !important;
  background: #17171d !important;
  border-color: rgba(255, 255, 255, 0.17) !important;
  box-shadow: none !important;
}
.stButton > button:hover, .stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
  background: rgba(var(--osu-accent-rgb), 0.16) !important;
  transform: none !important;
  box-shadow: none !important;
}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {
  background: var(--osu-accent) !important;
  color: #101014 !important;
  border-color: var(--osu-accent) !important;
}
h1::after { width: 42px; height: 3px; border-radius: 0; }
[data-baseweb="input"], [data-baseweb="textarea"],
[data-baseweb="select"] > div {
  background: #111116 !important;
  border-radius: 6px !important;
}
.osu-chip, [data-baseweb="tag"] { border-radius: 5px !important; }
.osu-note { border-radius: 0 6px 6px 0; }
.osu-cover, .osu-stat, .osu-preview { border-radius: 7px !important; }
</style>
"""
    elif settings.theme == "dark_clean":
        css += """
<style>
/* Dark Clean: calm graphite, compact cards, restrained glow. */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--osu-panel-strong) !important;
  border-radius: 12px !important;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.16) !important;
  animation: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  border-color: rgba(var(--osu-accent-rgb), 0.30) !important;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.20) !important;
  transform: none !important;
}
section[data-testid="stSidebar"] {
  background: #11151c !important;
  backdrop-filter: blur(8px) !important;
}
[data-testid="stHeader"] {
  background: rgba(18, 21, 27, 0.92) !important;
}
.stButton > button, .stDownloadButton > button, .stLinkButton > a,
[data-testid="stFormSubmitButton"] > button {
  border-radius: 9px !important;
  background: rgba(var(--osu-accent-rgb), 0.10) !important;
  box-shadow: none !important;
}
.stButton > button:hover, .stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
  background: rgba(var(--osu-accent-rgb), 0.22) !important;
  transform: none !important;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18) !important;
}
[data-testid="stMetric"], details, [data-testid="stExpander"],
[data-testid="stAlert"], .osu-preview-card {
  background: rgba(25, 29, 37, 0.92) !important;
  border-radius: 10px !important;
}
h1::after { width: 52px; height: 3px; }
.osu-cover, .osu-stat { border-radius: 9px !important; }
</style>
"""
    return css


def preview_html(settings: AppearanceSettings, background_uri: str | None) -> str:
    palette = PALETTES.get(settings.theme, PALETTES["modern_glass"])
    accent_rgb = _hex_to_rgb(settings.accent)
    accent_soft = _rgba(accent_rgb, 0.25)
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
            f"radial-gradient(500px 300px at 18% 5%, {accent_soft}, transparent 62%),"
            f"linear-gradient(145deg,{palette.base_alt},{palette.base})"
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
