"""Evidence states shared by features, skills and the UI.

The application must never collapse these states into a single "no data"
label. A feature can be absent because it was measured and not found, because
analysis never ran, because the source file is missing, because the file could
not be parsed, or because the available value is only a proxy.
"""

from __future__ import annotations

MEASURED = "measured"
NOT_DETECTED = "not_detected"
NOT_ANALYZED = "not_analyzed"
SOURCE_UNAVAILABLE = "source_unavailable"
PARTIAL = "partial"
FAILED = "failed"
PROXY = "proxy"

ALL_STATUSES = (
    MEASURED,
    NOT_DETECTED,
    NOT_ANALYZED,
    SOURCE_UNAVAILABLE,
    PARTIAL,
    FAILED,
    PROXY,
)

STATUS_LABELS = {
    MEASURED: "измерено",
    NOT_DETECTED: "проанализировано, не найдено",
    NOT_ANALYZED: "не анализировалось",
    SOURCE_UNAVAILABLE: "источник недоступен",
    PARTIAL: "частичный анализ",
    FAILED: "анализ не удался",
    PROXY: "прокси-оценка",
}

STATUS_SHORT = {
    MEASURED: "измерено",
    NOT_DETECTED: "не найдено",
    NOT_ANALYZED: "не анализировалось",
    SOURCE_UNAVAILABLE: "нет .osu",
    PARTIAL: "частично",
    FAILED: "ошибка анализа",
    PROXY: "прокси",
}

STATUS_EXPLANATIONS = {
    MEASURED: "признак вычислен из достаточных данных.",
    NOT_DETECTED: (
        "анализ выполнялся и не нашёл подходящих цепочек по описанным "
        "критериям; это отрицательный результат, а не отсутствие данных."
    ),
    NOT_ANALYZED: (
        "анализ .osu для этой difficulty ещё не выполнялся; неизвестность "
        "не означает, что паттерна нет."
    ),
    SOURCE_UNAVAILABLE: (
        "нужного .osu-файла нет в локальном кеше; без него паттерн нельзя "
        "измерить."
    ),
    PARTIAL: (
        "анализ покрыл только часть карты; найденное относится к "
        "проанализированному диапазону."
    ),
    FAILED: (
        "файл был доступен, но разбор или анализ завершился ошибкой; "
        "паттерн остаётся неизвестным."
    ),
    PROXY: (
        "значение является косвенной оценкой по метаданным/счётчикам и не "
        "доказывает конкретную геометрию или физическую способность."
    ),
}

# Lower rank is stronger. Used to aggregate component evidence.
_STATUS_RANK = {
    MEASURED: 0,
    PARTIAL: 1,
    PROXY: 2,
    NOT_DETECTED: 3,
    NOT_ANALYZED: 4,
    SOURCE_UNAVAILABLE: 5,
    FAILED: 6,
}


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def short_label(status: str) -> str:
    return STATUS_SHORT.get(status, status)


def explanation(status: str) -> str:
    return STATUS_EXPLANATIONS.get(status, "")


def is_unknown(status: str) -> bool:
    return status in {NOT_ANALYZED, SOURCE_UNAVAILABLE, FAILED}


def aggregate(statuses: list[str] | tuple[str, ...]) -> str:
    """Return the weakest status, i.e. the one that limits the conclusion."""

    if not statuses:
        return NOT_ANALYZED
    return max(statuses, key=lambda item: _STATUS_RANK.get(item, 9))


def coverage(available: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, available / total))
