"""Структура книги по объекту и преобразование фактов в строки.

Раздел 5 ТЗ: сводка, график работ, смета, закупки, журнал событий,
открытые вопросы. Дополнительно ведётся сводная книга по всем объектам.

Разделение листов на «наши» и «редактируемые человеком» здесь не формальность:
на первые система пишет свободно, вторые считываются перед записью, чтобы не
затереть ручную правку.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, NamedTuple

from repairbot.domain.facts import Fact, FactType


class Sheet(StrEnum):
    SUMMARY = "Сводка"
    SCHEDULE = "График работ"
    ESTIMATE = "Смета"
    PURCHASES = "Закупки"
    JOURNAL = "Журнал событий"
    OPEN_QUESTIONS = "Открытые вопросы"


OBJECT_SHEETS: tuple[Sheet, ...] = tuple(Sheet)

HUMAN_EDITABLE: frozenset[Sheet] = frozenset({Sheet.ESTIMATE, Sheet.OPEN_QUESTIONS})
"""Листы, где менеджер правит значения руками. Читаются перед записью."""

APPEND_ONLY: frozenset[Sheet] = frozenset({Sheet.JOURNAL, Sheet.PURCHASES})
"""Листы, куда система только добавляет строки."""


HEADERS: dict[Sheet, list[str]] = {
    Sheet.SUMMARY: ["Показатель", "Значение", "Обновлено"],
    Sheet.SCHEDULE: [
        "Этап",
        "Помещение",
        "Состояние",
        "План",
        "Факт",
        "Достоверность",
        "Источник",
    ],
    Sheet.ESTIMATE: [
        "Позиция",
        "Количество",
        "Ед.",
        "Сумма, ₽",
        "Статус",
        "Комментарий менеджера",
        "Достоверность",
        "Источник",
    ],
    Sheet.PURCHASES: [
        "Дата",
        "Материал",
        "Количество",
        "Ед.",
        "Сумма, ₽",
        "Достоверность",
        "Источник",
    ],
    Sheet.JOURNAL: [
        "Время",
        "Тип",
        "Описание",
        "Достоверность",
        "Применено",
        "Источник",
    ],
    Sheet.OPEN_QUESTIONS: [
        "Вопрос",
        "Кому",
        "Заведён",
        "Статус",
        "Решение",
        "Источник",
    ],
}

ROLLUP_SHEET = "Все объекты"
ROLLUP_HEADERS = [
    "Объект",
    "Адрес",
    "Этап",
    "Состояние",
    "Открытых вопросов",
    "Ждут подтверждения",
    "Обновлено",
]


_FACT_SHEET: dict[FactType, Sheet] = {
    FactType.WORK_PROGRESS: Sheet.SCHEDULE,
    FactType.SCHEDULE_CHANGE: Sheet.SCHEDULE,
    FactType.STAFF_ASSIGNMENT: Sheet.SCHEDULE,
    FactType.MEASUREMENT: Sheet.SCHEDULE,
    FactType.PURCHASE: Sheet.PURCHASES,
    FactType.MATERIAL_REQUEST: Sheet.PURCHASES,
    FactType.PAYMENT: Sheet.ESTIMATE,
    FactType.ESTIMATE_CHANGE: Sheet.ESTIMATE,
    FactType.ISSUE: Sheet.OPEN_QUESTIONS,
    FactType.CLIENT_REQUEST: Sheet.OPEN_QUESTIONS,
}


def sheet_for(fact_type: FactType) -> Sheet:
    return _FACT_SHEET.get(fact_type, Sheet.JOURNAL)


def source_link(message_id: str | None, event_id: int) -> str:
    """Ссылка на исходное сообщение.

    Прослеживаемость каждого показателя до сообщения — требование раздела 4
    ТЗ, и в таблице оно должно быть видно глазами.
    """
    return f"msg:{message_id}" if message_id else f"event:{event_id}"


def journal_row(
    *,
    occurred_at: datetime,
    event_type: str,
    description: str,
    confidence: float | None,
    applied: bool,
    source: str,
) -> list[Any]:
    return [
        occurred_at.strftime("%Y-%m-%d %H:%M"),
        event_type,
        description,
        _confidence_cell(confidence),
        "да" if applied else "нет",
        source,
    ]


def fact_row(fact: Fact, *, occurred_at: datetime, source: str) -> list[Any]:
    """Строка для профильного листа факта."""
    sheet = sheet_for(fact.type)

    if sheet is Sheet.SCHEDULE:
        return [
            fact.stage or "",
            fact.room or "",
            fact.status.value if fact.status else "",
            fact.due_date.isoformat() if fact.due_date else "",
            occurred_at.date().isoformat(),
            _confidence_cell(fact.confidence),
            source,
        ]

    if sheet is Sheet.PURCHASES:
        return [
            occurred_at.date().isoformat(),
            fact.item or fact.summary,
            _number(fact.qty),
            fact.unit or "",
            _number(fact.amount),
            _confidence_cell(fact.confidence),
            source,
        ]

    if sheet is Sheet.ESTIMATE:
        return [
            fact.item or fact.summary,
            _number(fact.qty),
            fact.unit or "",
            _number(fact.amount),
            "требует подтверждения" if fact.below_threshold else "применено",
            "",  # колонка менеджера — системой не заполняется
            _confidence_cell(fact.confidence),
            source,
        ]

    if sheet is Sheet.OPEN_QUESTIONS:
        return [
            fact.summary,
            "менеджер",
            occurred_at.date().isoformat(),
            "открыт",
            "",  # решение вписывает человек
            source,
        ]

    return journal_row(
        occurred_at=occurred_at,
        event_type=fact.type.value,
        description=fact.summary,
        confidence=fact.confidence,
        applied=not fact.below_threshold,
        source=source,
    )


def summary_rows(state: dict[str, Any], *, updated_at: datetime) -> list[list[Any]]:
    """Лист «Сводка» перезаписывается целиком — это производная от журнала."""
    stamp = updated_at.strftime("%Y-%m-%d %H:%M")
    known = [
        ("Текущий этап", state.get("current_stage")),
        ("Состояние", state.get("status")),
        ("Начало работ", state.get("started_on")),
        ("План завершения", state.get("planned_finish_on")),
        ("Бюджет, ₽", state.get("budget")),
        ("Израсходовано, ₽", state.get("spent")),
        ("Открытых вопросов", state.get("open_questions_count")),
        ("Ждут подтверждения", state.get("pending_confirmations")),
    ]
    return [[label, "" if value is None else value, stamp] for label, value in known]


def _confidence_cell(confidence: float | None) -> str:
    return "" if confidence is None else f"{confidence:.2f}"


def _number(value: float | None) -> Any:
    if value is None:
        return ""
    # Целые показываем без дробной части: «2», а не «2.0».
    return int(value) if float(value).is_integer() else value


def a1_range(sheet: Sheet | str, cells: str = "") -> str:
    """Диапазон в нотации A1 с корректным экранированием имени листа."""
    title = sheet.value if isinstance(sheet, Sheet) else sheet
    if any(c in title for c in " '!"):
        # Внутри кавычек апостроф удваивается — иначе Sheets отвергнет диапазон.
        quoted = "'" + title.replace("'", "''") + "'"
    else:
        quoted = title
    return f"{quoted}!{cells}" if cells else quoted


class EditableColumns(NamedTuple):
    """Где на листе человек правит значения, а где лежит ключ строки."""

    key_index: int
    """Колонка «Источник» — по ней строка сопоставляется между чтениями."""
    editable_indices: tuple[int, ...]


EDITABLE_COLUMNS: dict[Sheet, EditableColumns] = {
    # Смета: «Статус» и «Комментарий менеджера».
    Sheet.ESTIMATE: EditableColumns(key_index=7, editable_indices=(4, 5)),
    # Открытые вопросы: «Статус» и «Решение».
    Sheet.OPEN_QUESTIONS: EditableColumns(key_index=5, editable_indices=(3, 4)),
}


def column_label(index: int) -> str:
    """Индекс колонки (с нуля) → буква в нотации A1."""
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label
