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


@dataclass(frozen=True, slots=True)
class PresetNode:
    """Stable catalog identity and parent link for one built-in preset.

    ``id`` and ``parent_id`` are catalog identities, not name prefixes. The
    ``name`` field is the persisted ``PresetConfig`` name used by the ordinary
    SQLite/search pipeline. A parent node is itself an ordinary selectable
    preset; children are additional independent profiles, never replacements.
    """

    id: str
    name: str
    category_id: str
    order: int
    parent_id: str | None = None
    label: str = ""
    description: str = ""


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

# Active curated hierarchy. Parent presets are ordinary searchable profiles;
# children add narrower evidence-based profiles. A map is never assigned a
# single node: every profile evaluates the same candidate independently.
PRESET_NODES: tuple[PresetNode, ...] = (
    PresetNode(
        "discovery",
        "Discovery",
        "explore",
        10,
        label="Discovery",
        description="Новизна, старые и незнакомые карты.",
    ),
    PresetNode(
        "allrounder",
        "All-rounder",
        "explore",
        20,
        label="All-rounder",
        description="Сбалансированный универсальный профиль.",
    ),
    PresetNode(
        "aim_farm",
        "Aim Farm",
        "farm",
        10,
        label="Aim Farm",
        description="Короткие Ranked aim-карты для NM-фарма.",
    ),
    PresetNode(
        "aim",
        "Aim",
        "train",
        10,
        label="Aim",
        description="Широкий aim-профиль: jumps, control и bursts.",
    ),
    PresetNode(
        "aim.jump",
        "Jump Aim",
        "train",
        11,
        parent_id="aim",
        label="Jump Aim",
        description="Jump-паттерны по измеренной геометрии .osu.",
    ),
    PresetNode(
        "aim.flow",
        "Flow Aim",
        "train",
        12,
        parent_id="aim",
        label="Flow Aim",
        description="Связанные траектории по измеренному flow_share.",
    ),
    PresetNode(
        "aim.wide",
        "Wide Aim",
        "train",
        13,
        parent_id="aim",
        label="Wide Aim",
        description="Широкий spacing по измеренной доле широких прыжков.",
    ),
    PresetNode(
        "aim.control",
        "Aim Control",
        "train",
        14,
        parent_id="aim",
        label="Aim Control",
        description="Смены угла и контроль траектории.",
    ),
    PresetNode(
        "aim.bursts",
        "Aim + Bursts",
        "train",
        15,
        parent_id="aim",
        label="Aim + Bursts",
        description="Aim вместе с измеренными burst-сериями 5–9 нот.",
    ),
    PresetNode(
        "speed",
        "Speed",
        "train",
        20,
        label="Speed",
        description="Широкий скоростной профиль.",
    ),
    PresetNode(
        "speed.high_bpm",
        "High BPM",
        "train",
        21,
        parent_id="speed",
        label="High BPM",
        description="Высокий BPM и speed-давление.",
    ),
    PresetNode(
        "speed.bursts",
        "Speed Bursts",
        "train",
        22,
        parent_id="speed",
        label="Speed Bursts",
        description="Burst-серии как отдельное доказательство.",
    ),
    PresetNode(
        "speed.aim",
        "Speed + Aim",
        "train",
        23,
        parent_id="speed",
        label="Speed + Aim",
        description="Скорость, неотделимая от движения курсора.",
    ),
    PresetNode(
        "streams",
        "Streams",
        "train",
        30,
        label="Streams",
        description="Единая osu!-категория потоков и bursts.",
    ),
    PresetNode(
        "streams.burst",
        "Burst-oriented Streams",
        "train",
        31,
        parent_id="streams",
        label="Burst-oriented Streams",
        description="Короткие 5–9-нотные серии внутри категории Streams.",
    ),
    PresetNode(
        "streams.continuous",
        "Continuous Streams",
        "train",
        32,
        parent_id="streams",
        label="Continuous Streams",
        description="Длинные непрерывные потоковые цепочки.",
    ),
    PresetNode(
        "stamina",
        "Stamina",
        "train",
        40,
        label="Stamina",
        description="Длительная stream-выносливость.",
    ),
    PresetNode(
        "finger",
        "Finger Control",
        "train",
        50,
        label="Finger Control",
        description="Широкий профиль контроля пальцев.",
    ),
    PresetNode(
        "finger.alternating",
        "Alternating",
        "train",
        51,
        parent_id="finger",
        label="Alternating",
        description="Alternation-профиль; часть доказательств остаётся прокси.",
    ),
    PresetNode(
        "finger.double_tap",
        "Double-tap / Burst Control",
        "train",
        52,
        parent_id="finger",
        label="Double-tap / Burst Control",
        description="Короткие burst-группы как tapping-нагрузка.",
    ),
    PresetNode(
        "tech",
        "Tech",
        "train",
        60,
        label="Tech",
        description="Широкий технический профиль.",
    ),
    PresetNode(
        "tech.slider",
        "Slider Tech",
        "train",
        61,
        parent_id="tech",
        label="Slider Tech",
        description="Слайдеры, SV и техничные слайдерные паттерны.",
    ),
    PresetNode(
        "tech.rhythm",
        "Rhythm Control",
        "train",
        62,
        parent_id="tech",
        label="Rhythm Control",
        description="Измеренная ритмическая вариативность.",
    ),
)

PRESET_NODE_BY_ID = {node.id: node for node in PRESET_NODES}
PRESET_NODE_BY_NAME = {node.name: node for node in PRESET_NODES}
PRESET_GROUP_BY_NAME = {
    node.name: node.category_id for node in PRESET_NODES
}


def preset_node(name: str, *, is_builtin: bool = True) -> PresetNode | None:
    if not is_builtin:
        return None
    return PRESET_NODE_BY_NAME.get(name)


def preset_children(
    parent_id: str,
) -> tuple[PresetNode, ...]:
    return tuple(
        sorted(
            (
                node
                for node in PRESET_NODES
                if node.parent_id == parent_id
            ),
            key=lambda node: (node.order, node.name),
        )
    )


def preset_roots(category_id: str | None = None) -> tuple[PresetNode, ...]:
    return tuple(
        sorted(
            (
                node
                for node in PRESET_NODES
                if node.parent_id is None
                and (category_id is None or node.category_id == category_id)
            ),
            key=lambda node: (node.order, node.name),
        )
    )


def preset_path(node_id: str) -> tuple[PresetNode, ...]:
    node = PRESET_NODE_BY_ID.get(node_id)
    if node is None:
        return ()
    path = [node]
    seen = {node.id}
    while path[-1].parent_id is not None:
        parent = PRESET_NODE_BY_ID.get(path[-1].parent_id or "")
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        path.append(parent)
    return tuple(reversed(path))


def preset_group_id(name: str, *, is_builtin: bool = True) -> str:
    node = preset_node(name, is_builtin=is_builtin)
    if node is None:
        return "custom"
    return node.category_id


def preset_group(name: str, *, is_builtin: bool = True) -> PresetGroup:
    return PRESET_GROUP_BY_ID[preset_group_id(name, is_builtin=is_builtin)]


def preset_display_name(name: str, *, is_builtin: bool = True) -> str:
    group = preset_group(name, is_builtin=is_builtin)
    node = preset_node(name, is_builtin=is_builtin)
    if node is None:
        return f"{group.icon} {group.short_label} / {name}"
    path = " / ".join(item.label or item.name for item in preset_path(node.id))
    return f"{group.icon} {group.short_label} / {path}"


def ordered_presets(items: list[object]) -> list[object]:
    """Stable parent/child display order; custom entries stay after built-ins."""

    by_catalog_id = {
        str(getattr(getattr(item, "config", item), "catalog_id", "")): item
        for item in items
        if getattr(getattr(item, "config", item), "catalog_id", "")
    }
    by_name = {
        str(getattr(item, "name", "")): item
        for item in items
    }
    ordered = [
        by_catalog_id.get(node.id) or by_name.get(node.name)
        for node in PRESET_NODES
        if by_catalog_id.get(node.id) is not None or node.name in by_name
    ]
    custom = [
        item
        for item in items
        if not getattr(getattr(item, "config", item), "catalog_id", "")
    ]
    custom.sort(key=lambda item: str(getattr(item, "name", "")).casefold())
    return ordered + custom


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


def validate_preset_catalog(
    active_names: set[str] | None = None,
    *,
    nodes: tuple[PresetNode, ...] | None = None,
) -> None:
    """Fail loudly on malformed hierarchy metadata (tests call this)."""

    checked = PRESET_NODES if nodes is None else nodes
    by_id = {node.id: node for node in checked}
    ids: set[str] = set()
    names: set[str] = set()
    sibling_orders: dict[tuple[str, str | None], set[int]] = {}
    sibling_counts: dict[tuple[str, str | None], int] = {}
    for node in checked:
        if node.id in ids:
            raise ValueError(f"duplicate preset node id: {node.id}")
        if node.name in names:
            raise ValueError(f"duplicate preset name: {node.name}")
        ids.add(node.id)
        names.add(node.name)
        if node.category_id not in PRESET_GROUP_BY_ID:
            raise ValueError(
                f"{node.id}: unknown category {node.category_id}"
            )
        if node.parent_id is not None:
            parent = by_id.get(node.parent_id)
            if parent is None:
                raise ValueError(
                    f"{node.id}: unknown parent {node.parent_id}"
                )
            if parent.category_id != node.category_id:
                raise ValueError(
                    f"{node.id}: parent category mismatch"
                )
        sibling_key = (node.category_id, node.parent_id)
        sibling_orders.setdefault(sibling_key, set()).add(node.order)
        sibling_counts[sibling_key] = sibling_counts.get(sibling_key, 0) + 1
    for sibling_key, orders in sibling_orders.items():
        if len(orders) != sibling_counts[sibling_key]:
            raise ValueError(
                "duplicate display order under "
                f"{sibling_key[1] or sibling_key[0] + ' root'}"
            )
    if active_names is not None:
        missing = active_names - names
        if missing:
            raise ValueError(
                "active built-ins missing from catalog: "
                + ", ".join(sorted(missing))
            )
        extra = names - active_names
        if extra:
            raise ValueError(
                "catalog names without active built-in: "
                + ", ".join(sorted(extra))
            )


@dataclass(frozen=True, slots=True)
class PresetTreeEntry:
    node: PresetNode
    item: object | None
    children: tuple["PresetTreeEntry", ...] = ()


def build_preset_tree(
    items: list[object],
    *,
    query: str = "",
) -> tuple[tuple[PresetTreeEntry, ...], list[object]]:
    """Build the active hierarchy without flattening parent/child links.

    Custom presets are returned separately. Searching a child keeps its parent
    visible as context, and searching a parent keeps matching descendants.
    """

    by_catalog_id = {
        str(getattr(getattr(item, "config", item), "catalog_id", "")): item
        for item in items
        if getattr(getattr(item, "config", item), "catalog_id", "")
    }
    by_name = {
        str(getattr(item, "name", "")): item
        for item in items
    }
    needle = query.strip().casefold()

    def matches(node: PresetNode, item: object | None) -> bool:
        if not needle:
            return True
        group = PRESET_GROUP_BY_ID[node.category_id]
        description = ""
        if item is not None:
            description = str(
                getattr(getattr(item, "config", item), "description", "")
            )
        haystack = " ".join(
            (
                node.name,
                node.label,
                node.description,
                description,
                group.label,
                group.short_label,
                " / ".join(
                    item.label or item.name
                    for item in preset_path(node.id)
                ),
            )
        ).casefold()
        return needle in haystack

    def build(
        node: PresetNode, *, force: bool = False
    ) -> PresetTreeEntry | None:
        item = by_catalog_id.get(node.id) or by_name.get(node.name)
        node_matches = force or matches(node, item)
        children = tuple(
            entry
            for child in preset_children(node.id)
            if (
                entry := build(child, force=node_matches)
            )
            is not None
        )
        if item is None and not children:
            return None
        if not node_matches and not children:
            return None
        # A parent may have been deleted while its children still exist. Keep
        # the child entries reachable; the UI renders a non-selectable context
        # row for the missing parent instead of silently hiding the children.
        return PresetTreeEntry(node=node, item=item, children=children)

    roots = tuple(
        entry
        for node in PRESET_NODES
        if node.parent_id is None
        if (entry := build(node)) is not None
    )
    custom = [
        item
        for item in items
        if not getattr(getattr(item, "config", item), "catalog_id", "")
        and not bool(getattr(item, "is_builtin", False))
    ]
    return roots, custom


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
        node = preset_node(
            preset.name,
            is_builtin=bool(getattr(preset, "is_builtin", True)),
        )
        path = ""
        if node is not None:
            path = " / ".join(
                item.label or item.name for item in preset_path(node.id)
            )
        haystack = " ".join(
            (
                preset.name,
                getattr(
                    getattr(preset, "config", preset), "description", ""
                ),
                group.label,
                group.short_label,
                path,
                node.description if node is not None else "",
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
                "Classic / jump / wide / flow",
                "Основные aim-паттерны и геометрия движения.",
                ("aim_classic", "jump_aim", "aim_wide", "aim_flow"),
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
                    "high_bpm",
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
                ("variable_rhythm",),
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
                ("stamina_finger", "stamina_alt", "stream_endurance"),
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
