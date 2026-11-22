from __future__ import annotations

from types import SimpleNamespace

import pytest

import catalog
import presets
from skillsets import SKILL_IDS, SKILLS


def test_every_skill_appears_exactly_once_in_the_hierarchy() -> None:
    flattened = [
        skill_id
        for category in catalog.SKILL_CATEGORIES
        for skill_id in category.skill_ids
    ]
    assert len(flattened) == len(set(flattened)) == len(SKILL_IDS) == 75
    assert set(flattened) == set(SKILL_IDS)
    assert all(category.subcategories for category in catalog.SKILL_CATEGORIES)
    assert all(
        subcategory.skills
        for category in catalog.SKILL_CATEGORIES
        for subcategory in category.subcategories
    )


def test_skill_categories_use_the_required_grouping() -> None:
    assert [category.label for category in catalog.SKILL_CATEGORIES] == [
        "Aim",
        "Speed & Streams",
        "Stamina & Finger Control",
        "Reading",
        "Accuracy",
        "Tech & Patterns",
        "Flashlight",
        "Mixed Skillsets",
    ]
    stamina = catalog.category_for_skill("stamina_alt")
    assert stamina.id == "finger"
    assert "stamina_alt" in stamina.skill_ids


def test_skill_lookup_and_search_find_categories_and_aliases() -> None:
    for skill_id in SKILL_IDS:
        category = catalog.category_for_skill(skill_id)
        subcategory = catalog.subcategory_for_skill(skill_id)
        assert skill_id in category.skill_ids
        assert subcategory is not None
        assert skill_id in subcategory.skills
        assert catalog.SKILLS[skill_id] is SKILLS[skill_id]

    assert "low_ar" in catalog.search_skills("low ar")
    assert "stream_consistency" in catalog.search_skills("STREAMS")
    assert "stamina_alt" in catalog.search_skills("stamina")
    assert "fl_reading" in catalog.search_skills("flashlight")
    assert catalog.search_skills("") == list(SKILL_IDS)
    assert catalog.search_skills("definitely-not-a-skill") == []


def test_selected_count_uses_positive_weights_only() -> None:
    aim = catalog.SKILL_CATEGORY_BY_ID["aim"]
    assert catalog.selected_count(aim, {}) == 0
    assert catalog.selected_count(
        aim, {"aim_classic": 1.0, "aim_control": 0.0}
    ) == 1
    assert catalog.selected_count(
        aim, {"aim_classic": 2.0, "aim_bursts": 0.5}
    ) == 2


def test_all_builtin_presets_have_a_real_hierarchy_node() -> None:
    builtins = presets.builtin_presets()
    assert len(builtins) == 23
    active_names = {preset.name for preset in builtins}
    assert active_names == set(catalog.PRESET_GROUP_BY_NAME)
    assert active_names == set(catalog.PRESET_NODE_BY_NAME)
    catalog.validate_preset_catalog(active_names)
    assert [group.label for group in catalog.PRESET_GROUPS] == [
        "Explore",
        "Farm",
        "Train",
        "Custom",
    ]
    for preset in builtins:
        group = catalog.preset_group(preset.name, is_builtin=True)
        assert group.id in catalog.PRESET_GROUP_BY_ID
        assert catalog.preset_display_name(preset.name).startswith(group.icon)
        assert preset.catalog_id
        assert preset.category_id == group.id
        node = catalog.PRESET_NODE_BY_ID[preset.catalog_id]
        assert node.name == preset.name
        assert preset.parent_id == node.parent_id
        assert preset.display_order == node.order

    grouped = catalog.group_presets_by_category(builtins)
    assert set(grouped) == {group.id for group in catalog.PRESET_GROUPS}
    assert sum(len(items) for items in grouped.values()) == len(builtins)
    assert len(grouped["explore"]) == 2
    assert len(grouped["farm"]) == 1
    assert len(grouped["train"]) == 20
    assert grouped["custom"] == []

    # Parents are ordinary selectable presets and children are independent.
    assert {node.name for node in catalog.preset_roots("train")} == {
        "Aim",
        "Speed",
        "Streams",
        "Stamina",
        "Finger Control",
        "Tech",
    }
    assert {node.name for node in catalog.preset_children("aim")} == {
        "Jump Aim",
        "Flow Aim",
        "Wide Aim",
        "Aim Control",
        "Aim + Bursts",
    }
    assert catalog.preset_path("aim.jump")[-1].name == "Jump Aim"
    assert [node.name for node in catalog.preset_path("aim.jump")] == [
        "Aim",
        "Jump Aim",
    ]


def test_retired_presets_have_no_active_hierarchy_node() -> None:
    active_names = {preset.name for preset in presets.builtin_presets()}
    for retired in (
        "Flashlight",
        "Reading",
        "Accuracy",
        "Weird Aim",
        "Reading + Aim",
    ):
        assert retired not in active_names
        assert catalog.preset_node(retired) is None
        assert retired in presets.LEGACY_BUILTIN_NAMES


def test_hierarchy_tree_keeps_parent_context_when_child_matches() -> None:
    items = [
        SimpleNamespace(
            id=index,
            name=preset.name,
            is_builtin=True,
            config=SimpleNamespace(description=preset.description),
        )
        for index, preset in enumerate(presets.builtin_presets(), start=1)
    ]
    roots, custom = catalog.build_preset_tree(items, query="jump aim")
    assert custom == []
    aim = next(entry for entry in roots if entry.node.name == "Aim")
    assert [child.node.name for child in aim.children] == ["Jump Aim"]


def test_hierarchy_tree_keeps_children_when_parent_matches() -> None:
    items = [
        SimpleNamespace(
            id=index,
            name=preset.name,
            is_builtin=True,
            config=SimpleNamespace(description=preset.description),
        )
        for index, preset in enumerate(presets.builtin_presets(), start=1)
    ]
    roots, _ = catalog.build_preset_tree(items, query="Aim")
    aim = next(entry for entry in roots if entry.node.name == "Aim")
    assert {child.node.name for child in aim.children} == {
        "Jump Aim",
        "Flow Aim",
        "Wide Aim",
        "Aim Control",
        "Aim + Bursts",
    }


def test_deleted_parent_does_not_hide_its_children() -> None:
    items = [
        SimpleNamespace(
            id=index,
            name=preset.name,
            is_builtin=True,
            config=SimpleNamespace(description=preset.description),
        )
        for index, preset in enumerate(presets.builtin_presets(), start=1)
        if preset.name != "Aim"
    ]
    roots, _ = catalog.build_preset_tree(items)
    aim = next(entry for entry in roots if entry.node.name == "Aim")
    assert aim.item is None
    assert {child.node.name for child in aim.children} == {
        "Jump Aim",
        "Flow Aim",
        "Wide Aim",
        "Aim Control",
        "Aim + Bursts",
    }


def test_invalid_hierarchy_metadata_is_rejected() -> None:
    duplicate = (
        catalog.PresetNode("one", "One", "train", 1, label="One"),
        catalog.PresetNode("one", "Two", "train", 2, label="Two"),
    )
    with pytest.raises(ValueError, match="duplicate preset node id"):
        catalog.validate_preset_catalog(nodes=duplicate)
    unknown_parent = (
        catalog.PresetNode(
            "orphan",
            "Orphan",
            "train",
            1,
            parent_id="missing",
            label="Orphan",
        ),
    )
    with pytest.raises(ValueError, match="unknown parent"):
        catalog.validate_preset_catalog(nodes=unknown_parent)


def test_user_presets_fall_back_to_custom_group() -> None:
    user = SimpleNamespace(
        name="Aim",
        is_builtin=False,
        config=SimpleNamespace(description="my own edit"),
    )
    assert catalog.preset_group_id(user.name, is_builtin=False) == "custom"
    assert catalog.group_presets_by_category([user])["custom"] == [user]
    assert catalog.search_presets([user], "own edit") == [user]
    assert catalog.search_presets([user], "aim") == [user]
    assert catalog.search_presets([user], "not present") == []


def test_preset_search_matches_name_description_and_group() -> None:
    builtins = presets.builtin_presets()
    assert {item.name for item in catalog.search_presets(builtins, "stream")} >= {
        "Streams",
    }
    assert "Discovery" in {
        item.name for item in catalog.search_presets(builtins, "новизны")
    }
    assert {
        item.name for item in catalog.search_presets(builtins, "farm")
    } == {"Aim Farm"}
    assert catalog.search_presets(builtins, "") == builtins
