from __future__ import annotations

import patterns
import pattern_filters
import presets


def test_only_analyzer_labels_are_supported() -> None:
    assert pattern_filters.SUPPORTED_PATTERNS == patterns.DETECTED_LABELS
    assert "variable_rhythm" not in pattern_filters.SUPPORTED_PATTERNS
    assert set(pattern_filters.PATTERN_RU) == set(
        pattern_filters.SUPPORTED_PATTERNS
    )


def test_russian_labels_are_consistent() -> None:
    assert pattern_filters.pattern_name("jump_aim") == "Прыжковый аим"
    assert pattern_filters.pattern_name("wide_aim") == "Широкий аим"
    assert pattern_filters.pattern_name("flow_aim") == "Плавный аим"
    assert pattern_filters.pattern_name("angle_control") == "Контроль углов"
    assert pattern_filters.pattern_name("slider_heavy") == "Много слайдеров"
    assert pattern_filters.pattern_name("high_density") == "Высокая плотность"
    assert pattern_filters.pattern_name("streams") == "Стримы"
    assert pattern_filters.pattern_name("bursts") == "Бёрсты"
    assert pattern_filters.pattern_name("mixed_patterns") == "Смешанные паттерны"


def test_builtin_presets_map_to_measurable_patterns() -> None:
    by_name = {cfg.name: cfg for cfg in presets.builtin_presets()}

    assert pattern_filters.relevant_patterns(
        by_name["Aim Farm"].ranking.skill_weights
    ) == ("jump_aim",)
    assert pattern_filters.relevant_patterns(
        by_name["Speed"].ranking.skill_weights
    ) == ("high_density", "bursts")
    assert pattern_filters.relevant_patterns(
        by_name["Streams & Stamina"].ranking.skill_weights
    ) == ("streams", "high_density")
    assert pattern_filters.relevant_patterns(
        by_name["Bursts"].ranking.skill_weights
    ) == ("bursts",)
    tech = pattern_filters.relevant_patterns(
        by_name["Tech"].ranking.skill_weights
    )
    assert "mixed_patterns" in tech
    assert "slider_heavy" in tech
    assert "angle_control" in tech


def test_unsupported_goals_do_not_invent_patterns() -> None:
    by_name = {cfg.name: cfg for cfg in presets.builtin_presets()}
    for name in ("Finger Control", "Reading", "Accuracy", "Flashlight"):
        cfg = by_name[name]
        assert pattern_filters.relevant_patterns(
            cfg.ranking.skill_weights
        ) == ()
        assert pattern_filters.unsupported_goal_names(
            cfg.ranking.skill_weights
        )
    assert "variable_rhythm" not in pattern_filters.SKILL_PATTERN_MAP


def test_component_skills_expand_to_supported_patterns() -> None:
    by_name = {cfg.name: cfg for cfg in presets.builtin_presets()}
    all_rounder = pattern_filters.relevant_patterns(
        by_name["All-rounder"].ranking.skill_weights
    )
    assert "jump_aim" in all_rounder
    assert "streams" in all_rounder
    assert "high_density" in all_rounder
    assert set(all_rounder) <= set(pattern_filters.SUPPORTED_PATTERNS)


def test_trait_mapping_is_limited_to_observable_labels() -> None:
    assert pattern_filters.relevant_patterns({}, {"aim": 0.8}) == (
        "jump_aim",
        "wide_aim",
        "flow_aim",
    )
    assert pattern_filters.relevant_patterns({}, {"bursts": 0.7}) == (
        "bursts",
    )
    assert pattern_filters.relevant_patterns({}, {"reading": 1.0}) == ()
