"""Measurable mapping features and their provenance.

The API does not provide every pattern category. This module therefore keeps
three levels separate:

* directly provided values (difficulty attributes, BPM, AR/OD/CS/HP, counts);
* derived values (aim share, density, stamina proxies);
* sequence evidence from the selected difficulty's public ``.osu`` file
  (stream and burst chains are measured separately);
* mapper tags (low-confidence hints only).

Geometry such as wide/flow/awkward angles is not inferred from stars or BPM.
Those skillsets are marked as limited in ``skillsets.py`` and stay unknown
until a local ``.osu`` parser supplies hit-object geometry.
"""

from __future__ import annotations

import math

import evidence
from models import (
    Beatmap,
    BeatmapFeatures,
    Beatmapset,
    DifficultyAttributes,
    SequenceAnalysis,
)
from patterns import SEQUENCE_FEATURE_KEYS, sequence_features

# Legacy aim-profile traits. Kept for old presets and imports.
TRAITS = ("aim", "speed", "control", "precision", "density", "bursts")

TRAIT_LABELS = {
    "aim": "Aim-нагрузка",
    "speed": "Speed-нагрузка",
    "control": "Aim control (прокси)",
    "precision": "Precision (CS)",
    "density": "Плотность объектов",
    "bursts": "Bursts (.osu, 5–9 нот)",
}

TRAIT_DESCRIPTIONS = {
    "aim": "Доля aim-сложности в difficulty attributes osu!.",
    "speed": "Доля speed-сложности в difficulty attributes osu!.",
    "control": "Aim-доля, взвешенная звёздностью: грубый прокси контроля.",
    "precision": "Circle size: меньшие круги требуют точности.",
    "density": "Объектов в секунду по данным beatmap-объекта.",
    "bursts": "Короткие компактные серии 5–9 нот, найденные в .osu.",
}

FEATURE_LABELS = {
    **TRAIT_LABELS,
    "stars": "Звёздность",
    "bpm": "BPM",
    "length": "Длительность",
    "shortness": "Короткая карта",
    "low_density": "Низкая плотность",
    "ar": "AR",
    "od": "OD",
    "cs": "CS",
    "hp": "HP",
    "high_ar": "Высокий AR",
    "low_ar": "Низкий AR",
    "aim_pressure": "Aim-давление",
    "speed_pressure": "Speed-давление",
    "low_aim": "Низкий aim-вклад",
    "slider_ratio": "Доля слайдеров",
    "slider_factor": "Slider factor",
    "streams": "Streams (.osu, 10+ нот)",
    "stamina": "Stamina (прокси)",
    "stream_longest": "Самая длинная stream-цепочка",
    "stream_chain_count": "Число stream-цепочек",
    "stream_rhythm_quality": "Ровность stream-интервалов",
    "burst_count": "Число burst-цепочек",
    "burst_median": "Медианная длина burst",
    "burst_tempo": "Темп burst-интервалов",
    "burst_rhythm_quality": "Ровность burst-интервалов",
    "jump_share": "Доля прыжковых интервалов",
    "wide_jump_share": "Доля широких прыжков",
    "flow_share": "Доля плавных переходов",
    "angle_change_share": "Доля резких смен угла",
    "slider_share": "Доля слайдеров в анализе",
    "notes_per_second": "Плотность нот в анализе",
    "pattern_variety": "Разнообразие паттернов",
    "rhythm_variability": "Вариативность ритма",
    "difficulty_variety": "Разброс сложностей сета",
    "tag_aim": "Тег aim/jump",
    "tag_wide": "Тег wide/spacing",
    "tag_flow": "Тег flow",
    "tag_tech": "Тег tech",
    "tag_finger": "Тег finger control",
    "tag_alt": "Тег alt",
    "tag_stream": "Тег stream",
    "tag_reading": "Тег reading",
    "tag_fl": "Тег flashlight",
    "tag_rhythm": "Тег rhythm/variable",
    "tag_gimmick": "Тег gimmick/experimental",
    "tag_slider": "Тег slider/SV",
    "tag_precision": "Тег precision/accuracy",
}

SOURCE_LABELS = {
    "osu_api_attributes": "официальные difficulty attributes",
    "osu_sequence": "анализ последовательностей .osu",
    "osu_sequence_partial": "частичный анализ .osu (лимит объектов)",
    "tags": "теги маппера (низкая уверенность)",
    "beatmap_counts": "счётчики объектов карты",
    "beatmap_duration": "длительность карты",
    "beatmap_bpm": "BPM карты",
    "beatmap_cs": "CS карты",
    "beatmap_od": "OD карты",
    "beatmap_ar": "AR карты",
    "beatmap_hp": "HP карты",
    "beatmap_stars": "difficulty_rating карты",
    "beatmapset_metadata": "метаданные beatmapset",
    "derived_proxy": "производный прокси",
}

# Compact set shown in cards; the full feature set stays available to ranking.
PUBLIC_FEATURES = (
    "aim",
    "speed",
    "density",
    "precision",
    "bursts",
    "streams",
    "stamina",
    "slider_ratio",
    "difficulty_variety",
)

_AIM_TAGS = {"aim", "jump", "jumps", "aimslop", "aimcontrol", "spacing"}
_SPEED_TAGS = {"speed", "stream", "streams", "burst", "bursts", "alt", "dt", "tech"}
_FLOW_TAGS = {"flow", "flowaim", "aimflow"}
_WEIRD_TAGS = {"weird", "tech", "technical", "unusual", "experimental", "awkward"}

_TAG_GROUPS = {
    "tag_aim": _AIM_TAGS | {"aimcontrol"},
    "tag_wide": {"wide", "wideaim", "spacing", "crossscreen", "cross"},
    "tag_flow": _FLOW_TAGS,
    "tag_tech": {"tech", "technical", "gimmick", "gimmicky", "sv", "slider"},
    "tag_finger": {"finger", "fingercontrol", "tapping", "trill", "doubletap"},
    "tag_alt": {"alt", "alternating", "alternate", "alternation"},
    "tag_stream": {"stream", "streams", "streamy", "longstream"},
    "tag_reading": {"reading", "read", "lowar", "highar", "visual"},
    "tag_fl": {"flashlight", "fl"},
    "tag_rhythm": {"rhythm", "rhythmic", "variable", "uneven", "complex"},
    "tag_gimmick": {"gimmick", "gimmicky", "experimental", "weird", "unusual"},
    "tag_slider": {"slider", "sliders", "sv", "velocity"},
    "tag_precision": {"precision", "accuracy", "acc", "od"},
}

GEOMETRY_NOTE = (
    "Геометрия паттернов (ширина прыжков, плавность, углы, слайдеры) "
    "измеряется только по локальному .osu. Без файла она остаётся "
    "неизвестной или прокси-оценкой, а не нулём."
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def _tag_names(tags: tuple[str, ...]) -> set[str]:
    names: set[str] = set()
    for tag in tags:
        for part in (
            tag.replace("_", " ")
            .replace("-", " ")
            .replace("/", " ")
            .casefold()
            .split()
        ):
            if part:
                names.add(part)
        names.add(tag.strip().casefold())
    return names


def _tag_hint(tags: set[str], keywords: set[str]) -> float:
    return 1.0 if tags & keywords else 0.0


def _status_for_source(source: str) -> str:
    if source in {"osu_api_attributes", "osu_sequence", "osu_sequence_partial"}:
        return evidence.MEASURED
    if source in {
        "beatmap_counts",
        "beatmap_duration",
        "beatmap_bpm",
        "beatmap_cs",
        "beatmap_od",
        "beatmap_ar",
        "beatmap_hp",
        "beatmap_stars",
        "beatmapset_metadata",
    }:
        return evidence.MEASURED
    if source in {"tags", "derived_proxy"}:
        return evidence.PROXY
    return evidence.MEASURED


def _put(
    scores: dict[str, float],
    sources: dict[str, str],
    statuses: dict[str, str],
    key: str,
    value: float | None,
    source: str,
    *,
    status: str | None = None,
) -> None:
    if value is None:
        return
    scores[key] = round(_clamp(value), 3)
    sources[key] = source
    statuses[key] = status or _status_for_source(source)


def _variety(beatmapset: Beatmapset) -> float | None:
    stars = [
        beatmap.difficulty_rating
        for beatmap in beatmapset.beatmaps
        if beatmap.mode == "osu" and beatmap.difficulty_rating is not None
    ]
    if len(stars) < 2:
        return None
    return _clamp((max(stars) - min(stars)) / 4.0)


def derive_features(
    beatmap: Beatmap,
    beatmapset: Beatmapset,
    attributes: DifficultyAttributes | None = None,
    *,
    sequence: SequenceAnalysis | None = None,
    analysis_status: str = evidence.NOT_ANALYZED,
    analysis_scope: str = "",
    analyzer_version: int = 0,
    attributes_status: str = evidence.NOT_ANALYZED,
) -> BeatmapFeatures:
    """Derive only features supported by the data we actually have.

    ``sequence`` is optional per-difficulty evidence from the public ``.osu``
    file. Without it, stream and burst features stay absent rather than being
    replaced by density/BPM proxies. ``analysis_status`` records *why* those
    sequence features are absent, so "not analyzed" cannot be mistaken for
    "analyzed and no pattern found".
    """

    scores: dict[str, float] = {}
    sources: dict[str, str] = {}
    statuses: dict[str, str] = {}
    notes: list[str] = []
    tags = _tag_names(beatmapset.tags)
    confidence = "none"

    share = attributes.aim_share if attributes else None
    if share is not None:
        _put(scores, sources, statuses, "aim", share, "osu_api_attributes")
        _put(scores, sources, statuses, "speed", 1.0 - share, "osu_api_attributes")
        confidence = "medium"
        notes.append(
            "aim/speed — из aim_difficulty и speed_difficulty официального API."
        )
    else:
        aim_hint = _tag_hint(tags, _AIM_TAGS)
        speed_hint = _tag_hint(tags, _SPEED_TAGS)
        if aim_hint or speed_hint:
            total = aim_hint + speed_hint
            _put(scores, sources, statuses, "aim", aim_hint / total, "tags")
            _put(scores, sources, statuses, "speed", speed_hint / total, "tags")
            confidence = "low"
            notes.append(
                "aim/speed — только по тегам маппера (низкая уверенность), "
                "difficulty attributes не загружены."
            )
        elif tags & (_FLOW_TAGS | _WEIRD_TAGS):
            notes.append(
                "В тегах есть упоминание flow/weird, но без геометрии это "
                "не используется как измеренная категория."
            )
    if "aim" in scores:
        _put(
            scores,
            sources,
            statuses,
            "low_aim",
            1.0 - scores["aim"],
            sources.get("aim", "derived_proxy"),
        )

    objects = beatmap.object_count
    seconds = beatmap.hit_length or beatmap.total_length
    density_raw: float | None = None
    if objects and seconds and seconds > 0:
        density_raw = objects / seconds
        _put(scores, sources, statuses, "density", (density_raw - 2.0) / 6.0, "beatmap_counts")
        confidence = "medium" if attributes else "low"
        notes.append(f"Плотность: {density_raw:.1f} объектов/с (hit_length).")

    if beatmap.cs is not None:
        _put(scores, sources, statuses, "precision", (beatmap.cs - 3.5) / 3.5, "beatmap_cs")
        _put(scores, sources, statuses, "cs", beatmap.cs / 10.0, "beatmap_cs")
        if confidence == "none":
            confidence = "low"
        notes.append(f"Precision — прокси по CS {beatmap.cs:g}, не тип паттерна.")

    stars = beatmap.difficulty_rating
    if share is not None and stars is not None:
        star_factor = _clamp((stars - 3.5) / 3.5)
        _put(
            scores,
            sources,
            statuses,
            "control",
            share * (0.45 + 0.55 * star_factor),
            "osu_api_attributes",
        )

    if stars is not None:
        _put(scores, sources, statuses, "stars", stars / 10.0, "beatmap_stars")
    if beatmap.bpm is not None:
        _put(scores, sources, statuses, "bpm", (beatmap.bpm - 100.0) / 200.0, "beatmap_bpm")
    if seconds and seconds > 0:
        _put(scores, sources, statuses, "length", seconds / 300.0, "beatmap_duration")
        _put(scores, sources, statuses, "shortness", 1.0 - seconds / 300.0, "beatmap_duration")
    if "density" in scores:
        _put(scores, sources, statuses, "low_density", 1.0 - scores["density"], "derived_proxy")

    if beatmap.ar is not None:
        _put(scores, sources, statuses, "ar", beatmap.ar / 10.0, "beatmap_ar")
        _put(scores, sources, statuses, "high_ar", (beatmap.ar - 8.0) / 3.0, "beatmap_ar")
        _put(scores, sources, statuses, "low_ar", (6.0 - beatmap.ar) / 6.0, "beatmap_ar")
    if beatmap.od is not None:
        _put(scores, sources, statuses, "od", beatmap.od / 10.0, "beatmap_od")
    if beatmap.hp is not None:
        _put(scores, sources, statuses, "hp", beatmap.hp / 10.0, "beatmap_hp")

    if attributes is not None and attributes.aim_difficulty is not None:
        _put(
            scores,
            sources,
            statuses,
            "aim_pressure",
            attributes.aim_difficulty / 5.0,
            "osu_api_attributes",
        )
    if attributes is not None and attributes.speed_difficulty is not None:
        _put(
            scores,
            sources,
            statuses,
            "speed_pressure",
            attributes.speed_difficulty / 5.0,
            "osu_api_attributes",
        )
    if attributes is not None and attributes.slider_factor is not None:
        _put(
            scores,
            sources,
            statuses,
            "slider_factor",
            (attributes.slider_factor - 0.5) / 2.0,
            "osu_api_attributes",
        )
    if attributes is None:
        for key in ("aim_pressure", "speed_pressure", "slider_factor"):
            statuses.setdefault(key, attributes_status)
    else:
        for key, value in (
            ("aim_pressure", attributes.aim_difficulty),
            ("speed_pressure", attributes.speed_difficulty),
            ("slider_factor", attributes.slider_factor),
        ):
            if value is None:
                statuses.setdefault(key, evidence.SOURCE_UNAVAILABLE)

    if objects:
        sliders = beatmap.count_sliders or 0
        _put(scores, sources, statuses, "slider_ratio", sliders / objects, "beatmap_counts")

    if "density" in scores and seconds and seconds > 0:
        pressure = max(
            scores.get("aim_pressure", 0.0),
            scores.get("speed_pressure", 0.0),
            scores.get("density", 0.0),
        )
        _put(
            scores,
            sources,
            statuses,
            "stamina",
            (0.55 * scores["density"] + 0.45 * pressure)
            * _clamp(seconds / 240.0),
            "derived_proxy",
        )

    if sequence is not None and sequence.total_notes > 0:
        sequence_source = (
            "osu_sequence_partial" if sequence.truncated else "osu_sequence"
        )
        feature_status = (
            evidence.PARTIAL
            if sequence.truncated or analysis_status == evidence.PARTIAL
            else evidence.MEASURED
        )
        for key, value in sequence_features(sequence).items():
            if key in SEQUENCE_FEATURE_KEYS:
                _put(
                    scores,
                    sources,
                    statuses,
                    key,
                    value,
                    sequence_source,
                    status=(
                        feature_status
                        if value > 0
                        else (
                            evidence.PARTIAL
                            if feature_status == evidence.PARTIAL
                            else evidence.NOT_DETECTED
                        )
                    ),
                )
        longest_stream = max(
            (chain.length for chain in sequence.stream_chains), default=0
        )
        notes.append(
            "Анализ .osu: "
            f"{len(sequence.stream_chains)} stream-цепочек "
            f"(макс. {longest_stream} нот) и "
            f"{len(sequence.burst_chains)} burst-цепочек 5–9 нот; "
            "это отдельные максимальные цепочки."
        )
        if sequence.labels:
            notes.append(
                "Измеренные паттерны .osu: "
                + ", ".join(sequence.labels)
                + "."
            )
        if sequence.truncated:
            notes.append(
                "Анализ .osu ограничен первыми "
                f"{sequence.total_notes} объектами; найденное относится к "
                "проанализированной части."
            )
    elif sequence is not None:
        for key in SEQUENCE_FEATURE_KEYS:
            statuses.setdefault(key, evidence.NOT_DETECTED)
        notes.append(
            "Анализ .osu не дал hit objects: признаки потоков остаются "
            "неизвестными, а не нулевыми."
        )
    else:
        # No analysis object at all: keep every sequence feature unknown and
        # explain exactly why (not analyzed / missing file / failed).
        for key in SEQUENCE_FEATURE_KEYS:
            statuses.setdefault(key, analysis_status)
        notes.append(
            "Sequence-признаки не рассчитаны: "
            + evidence.explanation(analysis_status)
        )

    variety = _variety(beatmapset)
    if variety is not None:
        _put(
            scores,
            sources,
            statuses,
            "difficulty_variety",
            variety,
            "beatmapset_metadata",
        )

    for feature, keywords in _TAG_GROUPS.items():
        hint = _tag_hint(tags, keywords)
        if hint:
            _put(scores, sources, statuses, feature, hint, "tags")
            if confidence == "none":
                confidence = "low"

    if tags and any(feature.startswith("tag_") for feature in scores):
        notes.append(
            "Теги маппера — только подсказки низкой уверенности; они не "
            "превращаются в измеренную геометрию."
        )
    if tags & {"flow", "weird", "wide"} and not any(
        key in scores for key in ("wide", "flow")
    ):
        notes.append(
            "Теги flow/weird/wide не создают измеренные признаки wide/flow: "
            "для этого нужна геометрия .osu."
        )

    source_values = set(sources.values())
    if source_values & {"osu_sequence", "osu_sequence_partial"}:
        if source_values & {
            "tags",
            "derived_proxy",
            "osu_sequence_partial",
        }:
            confidence = "medium"
        else:
            confidence = "high"

    return BeatmapFeatures(
        scores=scores,
        sources=sources,
        confidence=confidence,
        notes=tuple(dict.fromkeys(notes)),
        statuses=statuses,
        analysis_status=analysis_status,
        analysis_scope=analysis_scope,
        analyzer_version=analyzer_version,
    )


# Old name used by existing callers and tests.
derive_traits = derive_features


def format_traits(features: BeatmapFeatures) -> list[str]:
    """Compact human-readable feature lines for cards and diagnostics."""
    if not features.available:
        return ["Тип паттерна: недостаточно данных."]
    lines = []
    for key in PUBLIC_FEATURES:
        value = features.scores.get(key)
        if value is None:
            continue
        label = FEATURE_LABELS.get(key, key)
        source = features.sources.get(key, "?")
        lines.append(f"{label}: {round(value * 100)}% (источник: {source})")
    return lines or ["Тип паттерна: недостаточно данных."]


def trait_similarity(targets: dict[str, float], features: BeatmapFeatures) -> float | None:
    """Return 0..1 similarity for measurable target traits, or None.

    A zero target is a real target (e.g. "low density"), so every common
    trait gets a baseline weight instead of disappearing.
    """
    pairs = [
        (target, features.scores[key])
        for key, target in targets.items()
        if key in features.scores
    ]
    if not pairs:
        return None
    weights = [0.25 + abs(target) for target, _ in pairs]
    error = sum(
        weight * abs(target - value)
        for weight, (target, value) in zip(weights, pairs)
    ) / sum(weights)
    return _clamp(1.0 - error)
