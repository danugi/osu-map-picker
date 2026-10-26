"""Universal osu!standard skillset catalog and fit evaluation.

A skillset is not a random tag. It is a typed definition with:

* the measurable features it depends on;
* weights for those features (or component skillsets);
* how much support the data can honestly provide;
* the limitations of that estimate.

The same evaluator is used by every preset and by the UI. A beatmapset can
fit several skillsets at once; the user chooses relative weights.
"""

from __future__ import annotations

from dataclasses import dataclass

from features import FEATURE_LABELS, SOURCE_LABELS
from models import BeatmapFeatures, SkillScore
from patterns import SEQUENCE_FEATURE_KEYS

SUPPORT_LABELS = {
    "measured": "Измеряемая основа",
    "proxy": "Прокси-оценка",
    "limited": "Ограниченная поддержка",
}

CONFIDENCE_LABELS = {
    "none": "нет данных",
    "low": "низкая",
    "medium": "средняя",
    "high": "высокая",
}

FAMILY_ORDER = (
    "aim",
    "speed",
    "streams",
    "finger",
    "accuracy",
    "reading",
    "tech",
    "flashlight",
    "combined",
)

FAMILY_LABELS = {
    "aim": "Aim",
    "speed": "Speed",
    "streams": "Streams и stamina",
    "finger": "Finger control и tapping",
    "accuracy": "Accuracy и precision",
    "reading": "Reading",
    "tech": "Tech и pattern control",
    "flashlight": "Flashlight",
    "combined": "Общие и комбинированные",
}

_SUPPORT_RANK = {"measured": 0, "proxy": 1, "limited": 2}
_CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    id: str
    name: str
    family: str
    description: str
    support: str = "limited"
    feature_weights: tuple[tuple[str, float], ...] = ()
    components: tuple[tuple[str, float], ...] = ()
    # Evidence that must be present before a score may be produced. ``required``
    # is an AND list; ``required_any`` is an OR list. They are deliberately
    # separate from feature weights: a high score from other features can never
    # compensate for missing mandatory evidence.
    required_features: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    required_components: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    applicable_filters: tuple[str, ...] = ("stars", "length", "statuses")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.feature_weights)

    @property
    def component_names(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.components)


@dataclass(frozen=True, slots=True)
class SkillFit:
    score: float
    coverage: float
    available: tuple[str, ...]
    missing: tuple[str, ...]
    confidence: str

    @property
    def display_score(self) -> int:
        return int(round(max(0.0, min(1.0, self.score)) * 100))


def _base(
    skill_id: str,
    name: str,
    family: str,
    description: str,
    weights: dict[str, float],
    *,
    support: str = "limited",
    required: tuple[str, ...] = (),
    required_any: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> SkillDefinition:
    return SkillDefinition(
        id=skill_id,
        name=name,
        family=family,
        description=description,
        support=support,
        feature_weights=tuple(weights.items()),
        required_features=required,
        required_any=required_any,
        limitations=limitations,
    )


def _combo(
    skill_id: str,
    name: str,
    family: str,
    description: str,
    components: dict[str, float],
    *,
    required_components: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> SkillDefinition:
    return SkillDefinition(
        id=skill_id,
        name=name,
        family=family,
        description=description,
        support="limited",
        components=tuple(components.items()),
        required_components=required_components,
        limitations=limitations,
    )


_DEFINITIONS: list[SkillDefinition] = []


def _add(definition: SkillDefinition) -> None:
    if any(item.id == definition.id for item in _DEFINITIONS):
        raise RuntimeError(f"duplicate skillset id: {definition.id}")
    _DEFINITIONS.append(definition)


# --- Aim: deliberately compact user-facing taxonomy -----------------------
_add(
    _base(
        "aim_classic",
        "Classic Aim / Aim Farm",
        "aim",
        "Обычные jump-паттерны и карты, подходящие для aim-фарма. "
        "Основа — официальные aim/speed-атрибуты, звёздность и плотность.",
        {"aim": 0.45, "aim_pressure": 0.25, "stars": 0.15, "density": 0.15},
        support="measured",
        required_any=("aim_pressure",),
        limitations=(
            "Без difficulty attributes остаются теги/метаданные низкой уверенности.",
        ),
    )
)
_add(
    _base(
        "aim_wide",
        "Wide Aim",
        "aim",
        "Большие расстояния и широкий размах прыжков. Пока это прокси по "
        "aim-давлению, низкой плотности и тегу wide/spacing.",
        {"aim_pressure": 0.45, "low_density": 0.25, "tag_wide": 0.20, "aim": 0.10},
        required_any=("aim_pressure",),
        limitations=(
            "Реальная ширина прыжков не измеряется без геометрии .osu.",
        ),
    )
)
_add(
    _base(
        "aim_flow",
        "Flow Aim",
        "aim",
        "Связанные движения и непрерывные траектории. Прокси по aim, "
        "плотности и доле слайдеров.",
        {"aim_pressure": 0.35, "aim": 0.25, "density": 0.20, "slider_ratio": 0.20},
        required_any=("aim_pressure",),
        limitations=(
            "Flow-геометрия и углы не измеряются по метаданным API.",
        ),
    )
)
_add(
    _base(
        "aim_control",
        "Aim Control",
        "aim",
        "Смена направлений, углы и контроль перемещения. Использует "
        "control-прокси, aim-давление, slider factor и CS.",
        {"control": 0.35, "aim_pressure": 0.25, "slider_factor": 0.20, "cs": 0.10, "tag_tech": 0.10},
        required_any=("control", "aim_pressure", "slider_factor"),
        limitations=(
            "Углы и резкость смены направления требуют локального анализа .osu.",
        ),
    )
)
_add(
    _base(
        "aim_precision",
        "Precision Aim",
        "aim",
        "Акцент на точности наведения: маленькие круги, высокий OD и "
        "aim-давление.",
        {"cs": 0.35, "od": 0.25, "aim": 0.25, "aim_pressure": 0.15},
        support="proxy",
        required_any=("aim_pressure",),
        limitations=(
            "CS/OD измеряются напрямую, но «точность наведения» — не отдельное "
            "поле API.",
        ),
    )
)
_add(
    _base(
        "aim_bursts",
        "Aim + Bursts",
        "aim",
        "Прыжки в сочетании с короткими burst-цепочками 5–9 нот из .osu. "
        "Без найденных burst-цепочек это не подтверждение паттерна.",
        {"bursts": 0.50, "burst_median": 0.20, "aim": 0.20, "speed": 0.10},
        support="measured",
        required=("bursts",),
        limitations=("Burst-цепочки и их длина берутся из .osu.",),
    )
)

# --- Speed ---------------------------------------------------------------
_add(
    _base(
        "speed_high_bpm",
        "High-BPM patterns",
        "speed",
        "Высокий BPM и speed-нагрузка. BPM и difficulty attributes берутся "
        "напрямую, плотность — из счётчиков объектов.",
        {"bpm": 0.40, "speed_pressure": 0.30, "speed": 0.20, "density": 0.10},
        support="measured",
        required_any=("speed_pressure",),
        limitations=("BPM сам по себе не определяет рисунок паттерна.",),
    )
)
_add(
    _base(
        "speed_short_bursts",
        "Short bursts",
        "speed",
        "Короткие burst-цепочки 5–9 нот, найденные в .osu. Длина, число "
        "цепочек и их доля считаются отдельно от streams.",
        {"bursts": 0.50, "burst_count": 0.30, "burst_median": 0.20},
        support="measured",
        required=("bursts",),
        limitations=("Нужен публичный .osu этой difficulty.",),
    )
)
_add(
    _base(
        "speed_streams",
        "Continuous streams",
        "speed",
        "Непрерывные stream-цепочки 10+ нот из .osu: доля карты, самая "
        "длинная цепочка и ровность интервалов.",
        {"streams": 0.55, "stream_longest": 0.25, "stream_rhythm_quality": 0.20},
        support="measured",
        required=("streams",),
        limitations=("Cutstreams и широкие потоки требуют компактной геометрии.",),
    )
)
_add(
    _base(
        "speed_bursts",
        "Speed bursts",
        "speed",
        "Скоростные burst-цепочки: доля, количество и ровность интервалов. "
        "Не использует streams, BPM или плотность как доказательство.",
        {"bursts": 0.55, "burst_count": 0.25, "burst_rhythm_quality": 0.20},
        support="measured",
        required=("bursts",),
        limitations=("Короткие серии отделены от длинных stream-цепочек.",),
    )
)
_add(
    _base(
        "burst_control",
        "Burst control",
        "speed",
        "Контроль коротких серий 5–9 нот: доля burst-нот, число цепочек и "
        "медианная длина. Stream-цепочки в этот расчёт не входят.",
        {"bursts": 0.45, "burst_count": 0.35, "burst_median": 0.20},
        support="measured",
        required=("bursts",),
        limitations=("Burst-серия должна быть компактной и ритмичной.",),
    )
)
_add(
    _base(
        "speed_density",
        "High-density patterns",
        "speed",
        "Высокая плотность объектов в секунду и speed-нагрузка.",
        {"density": 0.55, "speed": 0.25, "bpm": 0.20},
        support="measured",
        required_any=("speed_pressure",),
        limitations=("Плотность измеряется, но расположение объектов — нет.",),
    )
)
_add(
    _base(
        "speed_consistency",
        "Speed consistency",
        "speed",
        "Способность держать скорость. Прокси по speed-давлению, плотности "
        "и stamina; разброс интервалов недоступен.",
        {"speed_pressure": 0.35, "density": 0.30, "stamina": 0.25, "length": 0.10},
        required_any=("speed_pressure",),
        limitations=("Стабильность интервалов требует локального анализа .osu.",),
    )
)
_add(
    _combo(
        "speed_aim",
        "Speed + aim",
        "speed",
        "Комбинация скорости и classic aim.",
        {"aim_classic": 0.50, "speed_high_bpm": 0.50},
        required_components=("aim_classic", "speed_high_bpm"),
    )
)
_add(
    _combo(
        "speed_finger",
        "Speed + finger control",
        "speed",
        "Скорость вместе с работой пальцев.",
        {"speed_high_bpm": 0.60, "finger_control": 0.40},
    )
)

# --- Streams and stamina -------------------------------------------------
_add(
    _base(
        "stream_consistency",
        "Stream consistency",
        "streams",
        "Долгое удержание stream-рисунка: доля stream-нот, ровность "
        "интервалов, длина и число найденных цепочек из .osu.",
        {
            "streams": 0.40,
            "stream_longest": 0.25,
            "stream_rhythm_quality": 0.20,
            "stream_chain_count": 0.15,
        },
        support="measured",
        required=("streams",),
        limitations=("Нужен публичный .osu этой difficulty.",),
    )
)
_add(
    _base(
        "stream_long",
        "Long streams",
        "streams",
        "Длинные непрерывные stream-цепочки 10+ нот: длина цепочки, доля "
        "карты и ровность интервалов.",
        {"streams": 0.35, "stream_longest": 0.40, "stream_rhythm_quality": 0.25},
        support="measured",
        required=("streams",),
        limitations=("Длина считается по максимальной цепочке, а не по карте.",),
    )
)
_add(
    _base(
        "stream_short",
        "Short streams",
        "streams",
        "Короткие stream-цепочки: доля, число цепочек и ровность интервалов. "
        "Burst-серии 5–9 нот сюда не входят.",
        {"streams": 0.55, "stream_chain_count": 0.25, "stream_rhythm_quality": 0.20},
        support="measured",
        required=("streams",),
        limitations=("Цепочка короче 10 нот классифицируется как burst, не stream.",),
    )
)
_add(
    _base(
        "stream_burst",
        "Burst streams",
        "streams",
        "Комбинированный рисунок: есть и stream-цепочки 10+ нот, и отдельные "
        "burst-серии 5–9 нот. Оба признака берутся из .osu раздельно.",
        {"streams": 0.50, "bursts": 0.50},
        support="measured",
        required=("streams", "bursts"),
        limitations=("Не является целевым навыком чистого Streams-пресета.",),
    )
)
_add(
    _base(
        "stream_jump",
        "Jump streams",
        "streams",
        "Потоки с прыжками: stream-цепочки вместе с aim-нагрузкой и "
        "плотностью. Stream-доказательство обязательно.",
        {"streams": 0.50, "aim": 0.30, "density": 0.20},
        support="measured",
        required=("streams",),
        limitations=("Комбинация jump-геометрии и stream-рисунка остаётся эвристикой.",),
    )
)
_add(
    _base(
        "stamina_finger",
        "Finger stamina",
        "streams",
        "Выносливость пальцев: длительность, плотность и intensity.",
        {"stamina": 0.45, "density": 0.30, "tag_finger": 0.25},
        limitations=("Усталость и finger stamina нельзя точно вычислить по API.",),
    )
)
_add(
    _base(
        "stream_control",
        "Stream control",
        "streams",
        "Контроль stream-рисунка: stream-цепочки, ровность интервалов, OD и "
        "доля слайдеров.",
        {"streams": 0.50, "stream_rhythm_quality": 0.20, "od": 0.15, "slider_ratio": 0.15},
        support="measured",
        required=("streams",),
        limitations=("Контроль stream-геометрии остаётся частичной эвристикой.",),
    )
)
_add(
    _combo(
        "stream_reading",
        "Streams + reading",
        "streams",
        "Stream-нагрузка вместе с reading-профилем.",
        {"stream_consistency": 0.50, "ar_challenge": 0.50},
    )
)
_add(
    _combo(
        "stamina_alt",
        "Alternating stamina",
        "streams",
        "Выносливость и alternation: stamina + alternating.",
        {"stamina_finger": 0.50, "alternating": 0.50},
        required_components=("stamina_finger", "alternating"),
    )
)

# --- Finger control and tapping -----------------------------------------
_add(
    _base(
        "finger_control",
        "Finger control",
        "finger",
        "Контроль пальцев и tapping. Плотность, speed-давление, OD и тег "
        "finger control; без тега уверенность низкая.",
        {"density": 0.30, "speed_pressure": 0.25, "tag_finger": 0.25, "od": 0.20},
        limitations=("Finger control не является полем API; основа — прокси.",),
    )
)
_add(
    _base(
        "alternating",
        "Alternating",
        "finger",
        "Alternation и равномерная работа пальцев. Использует speed, тег alt, "
        "плотность и низкий aim-вклад.",
        {"speed": 0.30, "tag_alt": 0.30, "density": 0.20, "low_aim": 0.20},
        limitations=("Схема alternation не измеряется без .osu.",),
    )
)
_add(
    _base(
        "double_tap_control",
        "Double-tap control",
        "finger",
        "Контроль коротких burst-групп: burst-доля, медианная длина и "
        "finger-тег как подсказка.",
        {"bursts": 0.55, "burst_median": 0.20, "tag_finger": 0.25},
        required=("bursts",),
        limitations=("Double-tap-паттерны не определяются по метаданным API.",),
    )
)
_add(
    _base(
        "variable_rhythm",
        "Variable rhythm",
        "finger",
        "Переменный ритм. Поддержка только при наличии тега rhythm/variable.",
        {"tag_rhythm": 1.0},
        required=("tag_rhythm",),
        limitations=("Без тега и .osu-анализа переменный ритм не определяется.",),
    )
)
_add(
    _base(
        "rhythm_changes",
        "Rhythm changes",
        "finger",
        "Смены ритма. Тег rhythm/variable плюс tech-тег.",
        {"tag_rhythm": 0.70, "tag_tech": 0.30},
        required=("tag_rhythm",),
        limitations=("Смены ритма требуют анализа таймингов .osu.",),
    )
)
_add(
    _base(
        "mixed_patterns",
        "Mixed patterns",
        "finger",
        "Смешанные паттерны: tech-тег, слайдеры, плотность и ритм.",
        {"tag_tech": 0.35, "slider_ratio": 0.30, "density": 0.15, "tag_rhythm": 0.20},
        limitations=("Смешение паттернов не измеряется по API.",),
    )
)
_add(
    _base(
        "tapping_consistency",
        "Tapping consistency",
        "finger",
        "Стабильный tapping: speed, плотность, stamina и OD.",
        {"speed": 0.30, "density": 0.30, "stamina": 0.25, "od": 0.15},
        required_any=("speed_pressure",),
        limitations=("Стабильность нажатий требует локальных данных игры.",),
    )
)
_add(
    _base(
        "uneven_patterns",
        "Uneven patterns",
        "finger",
        "Неравномерные паттерны: rhythm/tech-теги.",
        {"tag_rhythm": 0.60, "tag_tech": 0.40},
        required=("tag_rhythm",),
        limitations=("Неравномерность интервалов не измеряется без .osu.",),
    )
)
_add(
    _base(
        "complex_bursts",
        "Complex bursts",
        "finger",
        "Сложные короткие серии: burst-доля, ровность интервалов и tech-тег "
        "как подсказка.",
        {"bursts": 0.60, "burst_rhythm_quality": 0.25, "tag_tech": 0.15},
        support="measured",
        required=("bursts",),
        limitations=("Структура complex bursts остаётся частичной эвристикой.",),
    )
)
_add(
    _base(
        "speed_control",
        "Speed control",
        "finger",
        "Контроль скорости: speed-давление, плотность, OD и stamina.",
        {"speed_pressure": 0.35, "density": 0.25, "od": 0.20, "stamina": 0.20},
        required_any=("speed_pressure",),
        limitations=("Контроль скорости — прокси, не измерение точности.",),
    )
)

# --- Accuracy and precision ---------------------------------------------
_add(
    _base(
        "accuracy_focused",
        "Accuracy-focused maps",
        "accuracy",
        "Карты с акцентом на точность: высокий OD, CS, звёздность и низкая "
        "плотность.",
        {"od": 0.40, "cs": 0.25, "stars": 0.15, "low_density": 0.20},
        support="proxy",
        required_any=("od",),
        limitations=("OD/CS измеряются напрямую; «accuracy-карта» — эвристика.",),
    )
)
_add(
    _base(
        "od_precision",
        "OD / precision challenge",
        "accuracy",
        "Высокий OD и маленькие круги. Прямые метаданные карты.",
        {"od": 0.60, "cs": 0.40},
        support="measured",
        required_any=("od",),
        limitations=("OD/CS не описывают рисунок паттерна.",),
    )
)
_add(
    _base(
        "rhythm_accuracy",
        "Rhythm accuracy",
        "accuracy",
        "Точность ритма: OD и тег rhythm.",
        {"od": 0.35, "tag_rhythm": 0.65},
        required=("tag_rhythm",),
        limitations=("Ритмическая точность требует анализа таймингов .osu.",),
    )
)
_add(
    _base(
        "consistent_timing",
        "Consistent timing",
        "accuracy",
        "Стабильный тайминг: OD, длительность и низкая плотность.",
        {"od": 0.45, "length": 0.20, "low_density": 0.20, "stamina": 0.15},
        limitations=("Стабильность тайминга не измеряется по метаданным.",),
    )
)
_add(
    _base(
        "low_density_precision",
        "Low-density precision",
        "accuracy",
        "Точность на разреженных паттернах: низкая плотность, OD, CS.",
        {"low_density": 0.40, "od": 0.35, "cs": 0.25},
        support="proxy",
        required_any=("od",),
        limitations=("Разреженность измеряется, точность наведения — нет.",),
    )
)
_add(
    _base(
        "high_density_precision",
        "High-density precision",
        "accuracy",
        "Точность на плотных паттернах: плотность, OD, CS.",
        {"density": 0.40, "od": 0.35, "cs": 0.25},
        support="proxy",
        required_any=("od",),
        limitations=("Плотность измеряется, точность наведения — нет.",),
    )
)
_add(
    _combo(
        "control_patterns",
        "Control-oriented patterns",
        "accuracy",
        "Контрольные паттерны: aim control + accuracy-профиль.",
        {"aim_control": 0.50, "accuracy_focused": 0.50},
    )
)

# --- Reading -------------------------------------------------------------
_add(
    _base(
        "ar_challenge",
        "AR challenge",
        "reading",
        "Челлендж по approach rate: высокий или низкий AR плюс reading-тег.",
        {"high_ar": 0.40, "low_ar": 0.30, "tag_reading": 0.30},
        required_any=("high_ar", "low_ar"),
        limitations=("AR измеряется, но читаемость паттерна — нет.",),
    )
)
_add(
    _base(
        "high_ar",
        "High-AR reading",
        "reading",
        "Высокий AR и плотность: чтение быстрых появлений объектов.",
        {"high_ar": 0.70, "density": 0.30},
        support="measured",
        required_any=("high_ar",),
        limitations=("Высокий AR измеряется, реальная читаемость — нет.",),
    )
)
_add(
    _base(
        "low_ar",
        "Low-AR reading",
        "reading",
        "Низкий AR и плотность: чтение перекрывающихся объектов.",
        {"low_ar": 0.70, "density": 0.30},
        support="measured",
        required_any=("low_ar",),
        limitations=("Низкий AR измеряется, визуальная сложность — нет.",),
    )
)
_add(
    _base(
        "density_changes",
        "Density changes",
        "reading",
        "Смены плотности. Поддержка только по reading/rhythm-тегам.",
        {"tag_reading": 0.60, "tag_rhythm": 0.40},
        required=("tag_reading",),
        limitations=("Смены плотности требуют покадрового анализа .osu.",),
    )
)
_add(
    _base(
        "pattern_recognition",
        "Pattern recognition",
        "reading",
        "Распознавание паттернов: reading/tech-теги и плотность.",
        {"tag_reading": 0.55, "tag_tech": 0.25, "density": 0.20},
        limitations=("Распознавание паттернов не измеряется по API.",),
    )
)
_add(
    _base(
        "unusual_rhythms",
        "Unusual rhythms",
        "reading",
        "Необычные ритмы: rhythm/tech-теги.",
        {"tag_rhythm": 0.70, "tag_tech": 0.30},
        required=("tag_rhythm",),
        limitations=("Необычный ритм требует анализа таймингов .osu.",),
    )
)
_add(
    _base(
        "visual_complexity",
        "Visual complexity",
        "reading",
        "Визуальная сложность: плотность, слайдеры, низкий AR и reading-тег.",
        {"density": 0.30, "slider_ratio": 0.30, "low_ar": 0.20, "tag_reading": 0.20},
        limitations=("Визуальная сложность не измеряется метаданными.",),
    )
)
_add(
    _combo(
        "reading_aim",
        "Reading + aim",
        "reading",
        "Чтение вместе с контролем наведения.",
        {"aim_control": 0.50, "ar_challenge": 0.50},
        required_components=("aim_control", "ar_challenge"),
    )
)
_add(
    _combo(
        "reading_streams",
        "Reading + streams",
        "reading",
        "Чтение stream-паттернов.",
        {"stream_consistency": 0.50, "ar_challenge": 0.50},
    )
)

# --- Tech and pattern control -------------------------------------------
_add(
    _base(
        "tech",
        "Tech",
        "tech",
        "Техничные карты: tech-тег, слайдеры, плотность и BPM.",
        {"tag_tech": 0.35, "slider_ratio": 0.30, "density": 0.20, "bpm": 0.15},
        limitations=("Tech — широкая категория; тег не заменяет анализ паттернов.",),
    )
)
_add(
    _base(
        "angular_control",
        "Angular control",
        "tech",
        "Углы и контроль траектории: aim control + tech.",
        {"control": 0.45, "aim_pressure": 0.25, "tag_tech": 0.30},
        required_any=("control", "aim_pressure"),
        limitations=("Углы не измеряются без геометрии .osu.",),
    )
)
_add(
    _base(
        "slider_heavy",
        "Slider-heavy patterns",
        "tech",
        "Много слайдеров и высокий slider factor. Доля слайдеров считается "
        "по счётчикам объектов.",
        {"slider_ratio": 0.55, "slider_factor": 0.25, "length": 0.20},
        support="measured",
        required_any=("slider_ratio", "slider_factor"),
        limitations=("Сложность слайдеров не сводится к их количеству.",),
    )
)
_add(
    _base(
        "sv_changes",
        "Slider velocity changes",
        "tech",
        "Смены SV. Поддержка только по тегу slider/SV и доле слайдеров.",
        {"tag_slider": 0.50, "slider_ratio": 0.50},
        required=("tag_slider",),
        limitations=("SV-изменения требуют таймингов и геометрии .osu.",),
    )
)
_add(
    _base(
        "repeated_patterns",
        "Repeated patterns",
        "tech",
        "Повторяющиеся паттерны: tech-тег, плотность, rhythm-тег.",
        {"tag_tech": 0.50, "density": 0.30, "tag_rhythm": 0.20},
        limitations=("Повторяемость не измеряется по метаданным API.",),
    )
)
_add(
    _base(
        "unusual_spacing",
        "Unusual spacing",
        "tech",
        "Необычный spacing: тег wide/spacing, aim-давление, низкая плотность.",
        {"tag_wide": 0.50, "aim_pressure": 0.30, "low_density": 0.20},
        limitations=("Spacing и углы требуют геометрии .osu.",),
    )
)
_add(
    _base(
        "rhythm_complexity",
        "Rhythm complexity",
        "tech",
        "Сложный ритм: rhythm/tech-теги.",
        {"tag_rhythm": 0.70, "tag_tech": 0.30},
        required=("tag_rhythm",),
        limitations=("Ритмическая сложность требует анализа таймингов .osu.",),
    )
)
_add(
    _base(
        "pattern_switching",
        "Pattern switching",
        "tech",
        "Смены паттернов: tech-тег, слайдеры, плотность.",
        {"tag_tech": 0.60, "slider_ratio": 0.20, "density": 0.20},
        limitations=("Смены паттернов не измеряются по API.",),
    )
)
_add(
    _base(
        "gimmick",
        "Gimmick patterns",
        "tech",
        "Гиммики: тег gimmick/experimental плюс rhythm-тег.",
        {"tag_gimmick": 0.60, "tag_rhythm": 0.40},
        required=("tag_gimmick",),
        limitations=("Гиммики определяются только по тегам/локальному анализу.",),
    )
)
_add(
    _combo(
        "mixed_technical",
        "Mixed technical patterns",
        "tech",
        "Смешанная техничность: tech + aim control + reading.",
        {"tech": 0.50, "aim_control": 0.30, "ar_challenge": 0.20},
    )
)

# --- Flashlight: modifier-specific, not a universal difficulty score ----
_add(
    _base(
        "fl_friendly",
        "Flashlight-friendly patterns",
        "flashlight",
        "FL-совместимость. Без тега flashlight признак неизвестен; это не "
        "универсальная характеристика сложности.",
        {"tag_fl": 0.40, "low_density": 0.30, "low_ar": 0.30},
        required=("tag_fl",),
        limitations=(
            "Flashlight — модификатор. Реальная FL-сложность не вычисляется "
            "по метаданным; поддержка ограничена тегом и базовыми признаками.",
        ),
    )
)
_add(
    _combo(
        "fl_aim",
        "FL aim",
        "flashlight",
        "FL-совместимость вместе с aim.",
        {"fl_friendly": 0.50, "aim_classic": 0.50},
        required_components=("fl_friendly", "aim_classic"),
        limitations=("Нет отдельного FL-анализа наведения.",),
    )
)
_add(
    _combo(
        "fl_reading",
        "FL reading",
        "flashlight",
        "FL-совместимость вместе с reading.",
        {"fl_friendly": 0.50, "ar_challenge": 0.50},
        required_components=("fl_friendly", "ar_challenge"),
        limitations=("Нет отдельного FL-анализа читаемости.",),
    )
)
_add(
    _combo(
        "fl_streams",
        "FL streams",
        "flashlight",
        "FL-совместимость вместе со streams.",
        {"fl_friendly": 0.50, "stream_consistency": 0.50},
        limitations=("Нет отдельного FL-анализа stream-рисунка.",),
    )
)
_add(
    _combo(
        "fl_control",
        "FL control",
        "flashlight",
        "FL-совместимость вместе с aim control.",
        {"fl_friendly": 0.50, "aim_control": 0.50},
        limitations=("Нет отдельного FL-анализа контроля.",),
    )
)
_add(
    _combo(
        "fl_precision",
        "FL precision",
        "flashlight",
        "FL-совместимость вместе с precision.",
        {"fl_friendly": 0.50, "aim_precision": 0.50},
        limitations=("Нет отдельного FL-анализа точности.",),
    )
)

# --- General / combined --------------------------------------------------
_add(
    _combo(
        "balanced",
        "Balanced / all-rounder",
        "combined",
        "Сбалансированный профиль без доминирования одного навыка.",
        {
            "aim_classic": 0.20,
            "speed_high_bpm": 0.20,
            "stream_consistency": 0.15,
            "finger_control": 0.15,
            "accuracy_focused": 0.15,
            "tech": 0.15,
        },
    )
)
_add(
    _combo(
        "mixed_skills",
        "Mixed skillsets",
        "combined",
        "Смесь aim, speed, finger control и accuracy.",
        {
            "aim_classic": 0.35,
            "speed_high_bpm": 0.25,
            "finger_control": 0.20,
            "accuracy_focused": 0.20,
        },
    )
)
_add(
    _combo(
        "aim_speed",
        "Aim + speed",
        "combined",
        "Aim вместе со скоростью.",
        {"aim_classic": 0.50, "speed_high_bpm": 0.50},
    )
)
_add(
    _combo(
        "aim_stamina",
        "Aim + stamina",
        "combined",
        "Aim вместе с выносливостью.",
        {"aim_classic": 0.50, "stamina_finger": 0.50},
    )
)
_add(
    _combo(
        "aim_finger",
        "Aim + finger control",
        "combined",
        "Aim вместе с контролем пальцев.",
        {"aim_classic": 0.50, "finger_control": 0.50},
    )
)
_add(
    _combo(
        "streams_reading",
        "Streams + reading",
        "combined",
        "Streams вместе с чтением.",
        {"stream_consistency": 0.50, "ar_challenge": 0.50},
    )
)
_add(
    _combo(
        "tech_accuracy",
        "Tech + accuracy",
        "combined",
        "Техничность вместе с точностью.",
        {"tech": 0.50, "accuracy_focused": 0.50},
    )
)
_add(
    _combo(
        "speed_stamina",
        "Speed + stamina",
        "combined",
        "Скорость вместе с выносливостью.",
        {"speed_high_bpm": 0.50, "stamina_finger": 0.50},
        required_components=("speed_high_bpm", "stamina_finger"),
    )
)
_add(
    _base(
        "difficulty_variety",
        "High difficulty variety",
        "combined",
        "В beatmapset есть сложности с заметным разбросом звёзд. Это "
        "свойство сета, а не одной карты.",
        {"difficulty_variety": 1.0},
        support="measured",
        limitations=("Разброс считается только при наличии минимум двух сложностей.",),
    )
)
_add(
    _base(
        "experimental",
        "Experimental / unusual maps",
        "combined",
        "Экспериментальные карты: тег gimmick/experimental и tech/rhythm.",
        {"tag_gimmick": 0.50, "tag_tech": 0.30, "tag_rhythm": 0.20},
        required=("tag_gimmick",),
        limitations=("Экспериментальность определяется только по тегам/анализу.",),
    )
)


SKILL_DEFINITIONS: tuple[SkillDefinition, ...] = tuple(_DEFINITIONS)
SKILLS: dict[str, SkillDefinition] = {item.id: item for item in SKILL_DEFINITIONS}
SKILL_IDS: tuple[str, ...] = tuple(SKILLS)


def skill_uses_sequences(skill_id: str, seen: set[str] | None = None) -> bool:
    """Whether a skill needs public ``.osu`` sequence evidence."""

    visited = seen if seen is not None else set()
    if skill_id in visited:
        return False
    visited.add(skill_id)
    definition = SKILLS.get(skill_id)
    if definition is None:
        return False
    if set(definition.feature_names) & SEQUENCE_FEATURE_KEYS:
        return True
    return any(
        skill_uses_sequences(component, visited)
        for component in definition.component_names
    )

# Public alias used by validation and UI.
DEFINITIONS = SKILL_DEFINITIONS


def skills_by_family(family: str) -> tuple[SkillDefinition, ...]:
    return tuple(item for item in SKILL_DEFINITIONS if item.family == family)


def skill_label(skill_id: str) -> str:
    item = SKILLS.get(skill_id)
    if item is None:
        return skill_id
    return f"{FAMILY_LABELS.get(item.family, item.family)} — {item.name}"


def _source_confidence(sources: list[str], has_required: bool) -> str:
    if not has_required:
        return "none"
    if sources and all(source == "tags" for source in sources):
        return "low"
    if "osu_sequence_partial" in sources:
        return "medium"
    if "osu_sequence" in sources:
        if "derived_proxy" in sources or "tags" in sources:
            return "medium"
        return "high"
    if any(source == "osu_api_attributes" for source in sources):
        return "medium"
    if any(
        source
        in {
            "beatmap_counts",
            "beatmap_duration",
            "beatmap_bpm",
            "beatmap_cs",
            "beatmap_od",
            "beatmap_ar",
            "beatmap_hp",
            "beatmap_stars",
            "beatmapset_metadata",
        }
        for source in sources
    ):
        return "low"
    return "low" if sources else "none"


def _base_score(
    definition: SkillDefinition, features: BeatmapFeatures
) -> SkillScore:
    missing_required = [
        key for key in definition.required_features if key not in features.scores
    ]
    if definition.required_any and not any(
        key in features.scores for key in definition.required_any
    ):
        missing_required.extend(definition.required_any)
    available = [
        (key, weight)
        for key, weight in definition.feature_weights
        if key in features.scores
    ]
    missing = [
        key
        for key, _ in definition.feature_weights
        if key not in features.scores
    ]
    if missing_required or not available:
        return SkillScore(
            skill_id=definition.id,
            name=definition.name,
            score=None,
            confidence="none",
            support=definition.support,
            evidence=(),
            missing=tuple(dict.fromkeys(missing_required + missing)),
            limitations=definition.limitations,
        )
    total = sum(weight for _, weight in available)
    value = sum(weight * features.scores[key] for key, weight in available) / total
    sources = [
        features.sources.get(key, "")
        for key, _ in available
        if features.sources.get(key)
    ]
    confidence = _source_confidence(sources, True)
    if definition.support == "limited" and confidence == "medium":
        # Geometry/pattern categories cannot become high-confidence just
        # because the map has difficulty attributes.
        confidence = "low"
    evidence = []
    for key, weight in sorted(available, key=lambda item: (-item[1], item[0]))[:6]:
        source = features.sources.get(key, "?")
        evidence.append(
            f"{FEATURE_LABELS.get(key, key)}: {round(features.scores[key] * 100)}% "
            f"({SOURCE_LABELS.get(source, source)})"
        )
    limitations = list(definition.limitations)
    if missing:
        labels = ", ".join(FEATURE_LABELS.get(key, key) for key in missing[:5])
        limitations.append(f"Недоступные признаки: {labels}.")
    return SkillScore(
        skill_id=definition.id,
        name=definition.name,
        score=max(0.0, min(1.0, value)),
        confidence=confidence,
        support=definition.support,
        evidence=tuple(evidence),
        missing=tuple(missing),
        limitations=tuple(dict.fromkeys(limitations)),
        sources=tuple(dict.fromkeys(sources)),
    )


def evaluate_skills(
    features: BeatmapFeatures, only: tuple[str, ...] | list[str] | None = None
) -> dict[str, SkillScore]:
    """Evaluate all (or selected) skillsets from one feature set."""

    wanted = set(only) if only is not None else set(SKILLS)
    cache: dict[str, SkillScore] = {}
    visiting: set[str] = set()

    def evaluate(skill_id: str) -> SkillScore:
        if skill_id in cache:
            return cache[skill_id]
        definition = SKILLS[skill_id]
        if definition.components:
            if skill_id in visiting:
                raise RuntimeError(f"skillset cycle at {skill_id}")
            visiting.add(skill_id)
            parts: list[tuple[SkillDefinition, float, SkillScore]] = []
            missing: list[str] = []
            for component_id, weight in definition.components:
                component = eval_component(component_id)
                if component.score is None:
                    missing.append(component_id)
                else:
                    parts.append((SKILLS[component_id], weight, component))
            visiting.discard(skill_id)
            required_missing = [
                component_id
                for component_id in definition.required_components
                if component_id in missing
            ]
            if not parts or required_missing:
                result = SkillScore(
                    skill_id=skill_id,
                    name=definition.name,
                    score=None,
                    confidence="none",
                    support=definition.support,
                    missing=tuple(missing),
                    limitations=definition.limitations,
                )
            else:
                total = sum(weight for _, weight, _ in parts)
                value = sum(weight * part.score for _, weight, part in parts) / total
                confidence = min(
                    (part.confidence for _, _, part in parts),
                    key=lambda item: _CONFIDENCE_RANK.get(item, 0),
                )
                support = max(
                    (part.support for _, _, part in parts),
                    key=lambda item: _SUPPORT_RANK.get(item, 2),
                )
                evidence = tuple(
                    f"{part.name}: {part.display_score}% "
                    f"({CONFIDENCE_LABELS.get(part.confidence, part.confidence)})"
                    for _, _, part in parts
                )
                limitations = list(definition.limitations)
                for _, _, part in parts:
                    limitations.extend(part.limitations)
                if missing:
                    limitations.append(
                        "Нет данных по компонентам: " + ", ".join(missing) + "."
                    )
                sources = tuple(
                    dict.fromkeys(
                        source
                        for _, _, part in parts
                        for source in part.sources
                        if source
                    )
                )
                result = SkillScore(
                    skill_id=skill_id,
                    name=definition.name,
                    score=max(0.0, min(1.0, value)),
                    confidence=confidence if len(parts) == len(definition.components) else "low",
                    support=support,
                    evidence=evidence,
                    missing=tuple(missing),
                    limitations=tuple(dict.fromkeys(limitations))[:8],
                    sources=sources,
                )
            cache[skill_id] = result
            return result
        result = _base_score(definition, features)
        cache[skill_id] = result
        return result

    def eval_component(skill_id: str) -> SkillScore:
        if skill_id not in SKILLS:
            return SkillScore(
                skill_id=skill_id,
                name=skill_id,
                score=None,
                confidence="none",
                support="limited",
                missing=(skill_id,),
                limitations=("Неизвестный навык.",),
            )
        return evaluate(skill_id)

    return {
        definition.id: evaluate(definition.id)
        for definition in SKILL_DEFINITIONS
        if definition.id in wanted
    }


def skill_fit(
    skillsets: dict[str, SkillScore], weights: dict[str, float]
) -> SkillFit | None:
    """Combine selected skillsets with correct normalization.

    Missing skillsets are excluded from the numerator and reduce coverage;
    they are reported instead of being silently treated as zero.
    """

    positive = {
        key: float(weight)
        for key, weight in weights.items()
        if skill_id_valid(key) and float(weight) > 0
    }
    if not positive:
        return None
    total_weight = sum(positive.values())
    available: list[tuple[str, float, SkillScore]] = []
    missing: list[str] = []
    for key, weight in positive.items():
        item = skillsets.get(key)
        if item is None or item.score is None:
            missing.append(key)
        else:
            available.append((key, weight, item))
    if not available:
        return None
    available_weight = sum(weight for _, weight, _ in available)
    raw_value = (
        sum(weight * item.score for _, weight, item in available) / available_weight
    )
    confidence = min(
        (item.confidence for _, _, item in available),
        key=lambda item: _CONFIDENCE_RANK.get(item, 0),
    )
    coverage = available_weight / total_weight
    if coverage < 1.0 and confidence == "medium":
        confidence = "low"
    # Transparency: partial coverage is not silently treated as a full match.
    value = raw_value * (0.5 + 0.5 * coverage)
    return SkillFit(
        score=max(0.0, min(1.0, value)),
        coverage=coverage,
        available=tuple(key for key, _, _ in available),
        missing=tuple(missing),
        confidence=confidence,
    )


def skill_id_valid(skill_id: str) -> bool:
    return skill_id in SKILLS


def top_skills(
    skillsets: dict[str, SkillScore], n: int = 3, minimum: float = 0.0
) -> list[SkillScore]:
    values = [
        item
        for item in skillsets.values()
        if item.score is not None and item.score >= minimum
    ]
    return sorted(values, key=lambda item: (-(item.score or 0.0), item.skill_id))[:n]
