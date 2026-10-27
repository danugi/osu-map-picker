"""Preset-aware filter helpers for analyzer-produced pattern labels.

A skillset is not the same thing as a detected pattern. This module maps only
skillsets whose definitions have a direct, measurable relationship to one of
``patterns.DETECTED_LABELS``. Reading, flashlight, finger alternation and
rhythm variability are deliberately not mapped: the analyzer does not persist
labels that prove them.
"""

from __future__ import annotations

from collections.abc import Mapping

import patterns
import skillsets

PATTERN_RU: dict[str, str] = {
    "jump_aim": "Прыжковый аим",
    "wide_aim": "Широкий аим",
    "flow_aim": "Плавный аим",
    "angle_control": "Контроль углов",
    "slider_heavy": "Много слайдеров",
    "high_density": "Высокая плотность",
    "streams": "Стримы",
    "bursts": "Бёрсты",
    "mixed_patterns": "Смешанные паттерны",
}

SUPPORTED_PATTERNS: tuple[str, ...] = tuple(
    label for label in patterns.DETECTED_LABELS if label in PATTERN_RU
)

# Direct mappings only. Components are expanded recursively below, but these
# entries are the labels the analyzer can measure for that skillset goal.
SKILL_PATTERN_MAP: dict[str, tuple[str, ...]] = {
    "aim_classic": ("jump_aim",),
    "aim_wide": ("wide_aim",),
    "aim_flow": ("flow_aim",),
    "aim_control": ("angle_control", "flow_aim"),
    "aim_bursts": ("bursts",),
    "speed_high_bpm": ("high_density",),
    "speed_short_bursts": ("bursts",),
    "speed_streams": ("streams",),
    "speed_bursts": ("bursts",),
    "burst_control": ("bursts",),
    "speed_density": ("high_density",),
    "stream_consistency": ("streams",),
    "stream_long": ("streams",),
    "stream_short": ("streams",),
    "stream_burst": ("bursts",),
    "stream_jump": ("streams", "jump_aim"),
    "stream_control": ("streams",),
    "mixed_patterns": ("mixed_patterns",),
    "complex_bursts": ("bursts",),
    "high_density_precision": ("high_density",),
    "control_patterns": ("angle_control",),
    "angular_control": ("angle_control",),
    "slider_heavy": ("slider_heavy",),
    "unusual_spacing": ("wide_aim",),
    "tech": ("mixed_patterns", "slider_heavy", "angle_control"),
}

# Legacy trait targets (kept for explicitly imported/legacy presets). Only
# traits with an observable sequence label are included.
TRAIT_PATTERN_MAP: dict[str, tuple[str, ...]] = {
    "aim": ("jump_aim", "wide_aim", "flow_aim"),
    "control": ("angle_control",),
    "speed": ("high_density",),
    "density": ("high_density",),
    "bursts": ("bursts",),
    "sliders": ("slider_heavy",),
    "wide": ("wide_aim",),
    "flow": ("flow_aim",),
}

# These families describe player skills or mods that the current sequence
# analyzer cannot prove. Do not infer their pattern labels from components.
_UNSUPPORTED_RECURSION_FAMILIES = frozenset(
    {"reading", "accuracy", "finger", "flashlight"}
)
_UNSUPPORTED_FAMILIES_FOR_NOTE = frozenset(
    {"reading", "accuracy", "finger", "flashlight"}
)


def _expand_skill(
    skill_id: str,
    weight: float,
    seen: set[str],
    scores: dict[str, float],
) -> None:
    if weight <= 0 or skill_id in seen:
        return
    seen.add(skill_id)
    for label in SKILL_PATTERN_MAP.get(skill_id, ()):
        if label in SUPPORTED_PATTERNS:
            scores[label] = scores.get(label, 0.0) + weight
    definition = skillsets.SKILLS.get(skill_id)
    if definition is None:
        return
    if definition.family in _UNSUPPORTED_RECURSION_FAMILIES:
        return
    for component, component_weight in definition.components:
        _expand_skill(
            component,
            weight * max(0.0, float(component_weight)),
            seen,
            scores,
        )


def relevant_patterns(
    skill_weights: Mapping[str, float],
    trait_targets: Mapping[str, float] | None = None,
) -> tuple[str, ...]:
    """Ordered patterns directly supported by the active preset goals."""

    scores: dict[str, float] = {}
    seen: set[str] = set()
    for skill_id, raw_weight in skill_weights.items():
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            continue
        _expand_skill(str(skill_id), weight, seen, scores)
    for trait_id, raw_weight in (trait_targets or {}).items():
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        for label in TRAIT_PATTERN_MAP.get(str(trait_id), ()):
            if label in SUPPORTED_PATTERNS:
                scores[label] = scores.get(label, 0.0) + weight
    return tuple(
        sorted(
            scores,
            key=lambda label: (
                -scores[label],
                SUPPORTED_PATTERNS.index(label),
            ),
        )
    )


def unsupported_goal_names(
    skill_weights: Mapping[str, float],
    trait_targets: Mapping[str, float] | None = None,
) -> tuple[str, ...]:
    """Active goals that have no honest pattern-label mapping."""

    names: list[str] = []
    for skill_id, raw_weight in skill_weights.items():
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        definition = skillsets.SKILLS.get(str(skill_id))
        if definition is None:
            continue
        directly_supported = bool(SKILL_PATTERN_MAP.get(str(skill_id)))
        recursive: dict[str, float] = {}
        _expand_skill(str(skill_id), weight, set(), recursive)
        if not directly_supported and not recursive:
            if definition.family in _UNSUPPORTED_FAMILIES_FOR_NOTE:
                names.append(definition.name)
    return tuple(dict.fromkeys(names))


def all_pattern_options() -> tuple[str, ...]:
    return SUPPORTED_PATTERNS


def pattern_name(label: str) -> str:
    return PATTERN_RU.get(label, label)


def preset_pattern_note(
    skill_weights: Mapping[str, float],
    trait_targets: Mapping[str, float] | None = None,
) -> str:
    relevant = relevant_patterns(skill_weights, trait_targets)
    if relevant:
        labels = ", ".join(pattern_name(item).lower() for item in relevant)
        return f"Паттерны, связанные с целями пресета: {labels}."
    unsupported = unsupported_goal_names(skill_weights, trait_targets)
    if unsupported:
        names = ", ".join(unsupported[:3])
        return (
            f"Анализатор не подтверждает паттернами цели «{names}». "
            "Используйте общий список паттернов как справочный фильтр."
        )
    return (
        "У этого профиля нет прямых паттерновых целей. "
        "Доступны все измеряемые паттерны."
    )
