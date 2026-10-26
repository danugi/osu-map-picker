# Personal osu! Map Picker — UX, presets, randomization, themes

## Goal
Make profile switching deterministic, add stable randomization, reduce and group built-in presets without deleting user data, clarify navigation, finish the three-theme appearance system, and keep the verified rosu-pp PP path honest.

## Audit findings
- Preset switching already loads a fresh `PresetConfig` and clears the prior result, but editor widgets reused the same Streamlit keys. After a browser-backed run, old frontend values could be sent back into the new preset and overwrite its filters/skills. Versioning appearance keys had already fixed the same class of bug there.
- Search results were only produced by a full API discovery run. There was no local re-ranking path and no explicit distinction between API results and locally cached candidates.
- Results always followed recommendation order. There was no stable shuffle seed and no random-pick state.
- 30 built-ins overlapped heavily, and the catalog groups were too fragmented.
- Navigation mixed task pages with configuration pages in one flat radio list.
- Appearance had three internal IDs (`default`, `dark`, `custom`) but not the requested named themes; `custom` was doing double duty as "has wallpaper".
- PP uses `rosu-pp-py 4.0.2` in lazer mode, with explicit NM SS semantics and no invented fallback. Keep it and tighten labels/regression coverage rather than replacing it.

## Implementation order
1. Version every config-editor widget key; clear old generations on preset switches. Add sequential profile tests.
2. Add cached local re-ranking (`local_cache` origin) and use it only when it yields enough candidates; API otherwise.
3. Add stable random order, random pick, and Shuffle without API calls.
4. Reduce built-ins to 11 canonical profiles; hide legacy built-ins behind a compatibility section and keep all rows/user presets.
5. Regroup preset catalog to Explore / Farm / Train / Custom.
6. Replace flat navigation with grouped, labelled buttons (Основное / Подбор / Система).
7. Migrate themes to Modern Glass / osu! Minimal / Dark Clean, with distinct CSS; wallpapers remain independent.
8. Keep rosu-pp NM SS PP, update labels to be explicit, add regression tests.
9. Update AppTest/unit tests and README; run full suite and compile checks.

## Acceptance checks
- Sequential Mixed Aim → Reading → Aim Farm / PP → Streams & Stamina keeps each config and never reuses a stale result snapshot.
- Shuffle changes order only; reruns keep order; random pick stays inside strict filters; no API call.
- Legacy built-in rows and all user presets survive migration.
- All navigation entries have labels and an obvious active state; every page renders.
- Three themes produce different CSS while theme changes leave search state untouched.
- PP unavailable is shown as `PP unavailable`; NM values remain SS-scenario only.
