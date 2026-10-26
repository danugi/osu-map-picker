"""Navigation catalog: preset groups and the skillset hierarchy.

Presets and skillsets are different things and stay in separate catalogs.
The groups are presentation metadata only: a preset is still one ordinary
``PresetConfig`` and a skillset is still one entry in ``skillsets.SKILLS``.
No search engine or hidden configuration lives in this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from skillsets import SKILLS, SKILL_IDS


@dataclass(frozen=True, slots=True)
class PresetGroup:
    id: str
    label: str
    short_label: str
    description: str
    icon: str = "◆"


PRESET_GROUPS: tuple[PresetGroup, ...] = (
    PresetGroup(
        "explore",
        "Explore",
        "Explore",
        "Открывать новое: сбалансированный вкус, Discovery и универсальный профиль.",
        "🧭",
    ),
    PresetGroup(
        "farm",
        "Farm",
        "Farm",
        "Короткие NM-aim-карты с настраиваемой длительностью. Профиль не влияет на остальные пресеты.",
        "💯",
    ),
    PresetGroup(
        "train",
        "Train",
        "Train",
        "Целенаправленная тренировка отдельных навыков и паттернов.",
        "🎯",
    ),
    PresetGroup(
        "custom",
        "Custom",
        "Custom",
        "Ваши собственные профили, созданные из любого пресета.",
        "🧩",
    ),
)

PRESET_GROUP_BY_ID = {group.id: group for group in PRESET_GROUPS}

# Curated built-ins are assigned explicitly. A new built-in without a group
# is a programming error caught by tests, not something the UI guesses.
PRESET_GROUP_BY_NAME = {
    "Mixed Aim": "explore",
    "Discovery": "explore",
    "All-rounder": "explore",
    "Aim Farm": "farm",
    "Speed": "train",
    "Streams & Stamina": "train",
    "Bursts": "train",
    "Finger Control": "train",
    "Reading": "train",
    "Accuracy": "train",
    "Tech": "train",
    "Flashlight": "train",
}


def preset_group_id(name: str, *, is_builtin: bool = True) -> str:
    if not is_builtin:
        return "custom"
    return PRESET_GROUP_BY_NAME.get(name, "custom")


def preset_group(name: str, *, is_builtin: bool = True) -> PresetGroup:
    return PRESET_GROUP_BY_ID[preset_group_id(name, is_builtin=is_builtin)]


def preset_display_name(name: str, *, is_builtin: bool = True) -> str:
    group = preset_group(name, is_builtin=is_builtin)
    return f"{group.icon} {group.short_label} / {name}"


def group_presets_by_category(presets: list[object]) -> dict[str, list[object]]:
    """Group arbitrary objects with ``name`` and optional ``is_builtin``."""

    grouped = {group.id: [] for group in PRESET_GROUPS}
    for preset in presets:
        grouped[
            preset_group_id(
                preset.name, is_builtin=bool(getattr(preset, "is_builtin", True))
            )
        ].append(preset)
    return grouped


def search_presets(presets: list[object], query: str) -> list[object]:
    needle = query.strip().casefold()
    if not needle:
        return list(presets)
    result = []
    for preset in presets:
        group = preset_group(
            preset.name,
            is_builtin=bool(getattr(preset, "is_builtin", True)),
        )
        haystack = " ".join(
            (
                preset.name,
                getattr(
                    getattr(preset, "config", preset), "description", ""
                ),
                group.label,
                group.short_label,
            )
        ).casefold()
        if needle in haystack:
            result.append(preset)
    return result


# ---------------------------------------------------------------------------
# Skillset hierarchy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillSubcategory:
    id: str
    label: str
    description: str
    skills: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillCategory:
    id: str
    label: str
    description: str
    icon: str
    subcategories: tuple[SkillSubcategory, ...]

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(
            skill_id
            for subcategory in self.subcategories
            for skill_id in subcategory.skills
        )


SKILL_CATEGORIES: tuple[SkillCategory, ...] = (
    SkillCategory(
        "aim",
        "Aim",
        "Ведение курсора, прыжки, поток и контроль прицела.",
        "🎯",
        (
            SkillSubcategory(
                "aim_classic",
                "Classic / wide / flow",
                "Основные aim-паттерны и геометрия движения.",
                ("aim_classic", "aim_wide", "aim_flow"),
            ),
            SkillSubcategory(
                "aim_control",
                "Control & bursts",
                "Контроль, точность прицела и aim в коротких всплесках.",
                ("aim_control", "aim_precision", "aim_bursts"),
            ),
        ),
    ),
    SkillCategory(
        "speed",
        "Speed & Streams",
        "Скорость, потоки, bursts, jump streams и выносливость.",
        "⚡",
        (
            SkillSubcategory(
                "speed_core",
                "Speed",
                "Плотность, BPM, стабильность и скорость с aim.",
                (
                    "speed_high_bpm",
                    "speed_density",
                    "speed_consistency",
                    "speed_aim",
                    "speed_finger",
                    "speed_control",
                ),
            ),
            SkillSubcategory(
                "bursts",
                "Bursts",
                "Короткие серии 5–9 нот и сложные burst-паттерны.",
                (
                    "burst_control",
                    "speed_short_bursts",
                    "speed_bursts",
                    "complex_bursts",
                ),
            ),
            SkillSubcategory(
                "streams",
                "Streams",
                "Непрерывные и короткие потоки, их контроль и чтение.",
                (
                    "speed_streams",
                    "stream_consistency",
                    "stream_long",
                    "stream_short",
                    "stream_burst",
                    "stream_control",
                    "stream_reading",
                ),
            ),
            SkillSubcategory(
                "jump_streams",
                "Jump streams",
                "Потоки с прыжками между объектами.",
                ("stream_jump",),
            ),
        ),
    ),
    SkillCategory(
        "finger",
        "Stamina & Finger Control",
        "Выносливость, альтернация, ритм, uneven-паттерны и контроль нажатий.",
        "🖐",
        (
            SkillSubcategory(
                "alternation",
                "Alternation & tapping",
                "Ритмичная работа пальцев и стабильность нажатий.",
                (
                    "finger_control",
                    "alternating",
                    "double_tap_control",
                    "tapping_consistency",
                ),
            ),
            SkillSubcategory(
                "rhythm",
                "Rhythm",
                "Смена ритма и переменный ритм.",
                ("variable_rhythm", "rhythm_changes"),
            ),
            SkillSubcategory(
                "uneven",
                "Uneven patterns",
                "Неравномерные и смешанные паттерны.",
                ("uneven_patterns", "mixed_patterns"),
            ),
            SkillSubcategory(
                "stamina",
                "Stamina",
                "Выносливость пальцев и alternation на длинной дистанции.",
                ("stamina_finger", "stamina_alt"),
            ),
        ),
    ),
    SkillCategory(
        "reading",
        "Reading",
        "Чтение AR, смен плотности и визуальная сложность.",
        "👁",
        (
            SkillSubcategory(
                "ar",
                "AR challenge",
                "Низкий и высокий AR, а также общий AR-challenge.",
                ("ar_challenge", "high_ar", "low_ar"),
            ),
            SkillSubcategory(
                "patterns",
                "Pattern reading",
                "Плотность, распознавание и визуальная сложность.",
                (
                    "density_changes",
                    "pattern_recognition",
                    "visual_complexity",
                    "unusual_rhythms",
                ),
            ),
            SkillSubcategory(
                "combined",
                "Reading + aim / streams",
                "Чтение в связке с прицелом или потоками.",
                ("reading_aim", "reading_streams"),
            ),
        ),
    ),
    SkillCategory(
        "accuracy",
        "Accuracy",
        "OD, ритмическая точность и стабильный тайминг.",
        "◎",
        (
            SkillSubcategory(
                "precision",
                "OD / precision",
                "Точность при разной плотности и контрольных паттернах.",
                (
                    "accuracy_focused",
                    "od_precision",
                    "low_density_precision",
                    "high_density_precision",
                    "control_patterns",
                ),
            ),
            SkillSubcategory(
                "rhythm",
                "Rhythm accuracy",
                "Точность ритма и стабильность тайминга.",
                ("rhythm_accuracy", "consistent_timing"),
            ),
        ),
    ),
    SkillCategory(
        "tech",
        "Tech & Patterns",
        "Слайдеры, смены SV, углы и сложные паттерны.",
        "🧩",
        (
            SkillSubcategory(
                "tech",
                "Tech & slider",
                "Техничные паттерны, слайдеры и контроль углов.",
                (
                    "tech",
                    "angular_control",
                    "slider_heavy",
                    "sv_changes",
                    "repeated_patterns",
                    "unusual_spacing",
                    "rhythm_complexity",
                    "pattern_switching",
                    "gimmick",
                    "mixed_technical",
                ),
            ),
        ),
    ),
    SkillCategory(
        "flashlight",
        "Flashlight",
        "Отдельные FL-направления; модификатор не подразумевается автоматически.",
        "🔦",
        (
            SkillSubcategory(
                "flashlight",
                "Flashlight skills",
                "FL reading, aim, streams, control и precision.",
                (
                    "fl_friendly",
                    "fl_aim",
                    "fl_reading",
                    "fl_streams",
                    "fl_control",
                    "fl_precision",
                ),
            ),
        ),
    ),
    SkillCategory(
        "mixed",
        "Mixed Skillsets",
        "Комбинации и универсальные профили.",
        "✚",
        (
            SkillSubcategory(
                "balanced",
                "Balanced",
                "Сбалансированные и универсальные профили.",
                ("balanced", "mixed_skills", "difficulty_variety"),
            ),
            SkillSubcategory(
                "combinations",
                "Combinations",
                "Aim/speed/stamina/finger/reading в комбинациях.",
                (
                    "aim_speed",
                    "aim_stamina",
                    "aim_finger",
                    "streams_reading",
                    "tech_accuracy",
                    "speed_stamina",
                ),
            ),
            SkillSubcategory(
                "experimental",
                "Experimental",
                "Необычные и экспериментальные паттерны.",
                ("experimental",),
            ),
        ),
    ),
)

SKILL_CATEGORY_BY_ID = {category.id: category for category in SKILL_CATEGORIES}
SKILL_CATEGORY_BY_SKILL = {
    skill_id: category
    for category in SKILL_CATEGORIES
    for skill_id in category.skill_ids
}
SKILL_SUBCATEGORY_BY_SKILL = {
    skill_id: subcategory
    for category in SKILL_CATEGORIES
    for subcategory in category.subcategories
    for skill_id in subcategory.skills
}


def category_for_skill(skill_id: str) -> SkillCategory:
    return SKILL_CATEGORY_BY_SKILL.get(
        skill_id, SKILL_CATEGORY_BY_ID["mixed"]
    )


def subcategory_for_skill(skill_id: str) -> SkillSubcategory | None:
    return SKILL_SUBCATEGORY_BY_SKILL.get(skill_id)


def search_skills(query: str) -> list[str]:
    needle = query.strip().casefold().replace("_", " ")
    if not needle:
        return list(SKILL_IDS)
    result: list[str] = []
    for skill_id in SKILL_IDS:
        definition = SKILLS[skill_id]
        category = category_for_skill(skill_id)
        subcategory = subcategory_for_skill(skill_id)
        haystack = " ".join(
            (
                skill_id,
                definition.name,
                definition.description,
                category.label,
                subcategory.label if subcategory else "",
            )
        ).casefold().replace("_", " ")
        if needle in haystack:
            result.append(skill_id)
    return result


def selected_count(category: SkillCategory, weights: dict[str, float]) -> int:
    return sum(1 for skill_id in category.skill_ids if weights.get(skill_id, 0) > 0)
