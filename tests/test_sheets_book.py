"""Структура книги, диапазоны A1 и квота Sheets API."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from repairbot.domain.facts import Fact, FactType, WorkStatus
from repairbot.integrations.sheets import book
from repairbot.integrations.sheets.book import Sheet
from repairbot.integrations.sheets.client import QuotaLimiter

AT = datetime(2026, 8, 15, 9, 30, tzinfo=UTC)


def test_every_sheet_has_headers():
    for sheet in book.OBJECT_SHEETS:
        assert sheet in book.HEADERS, sheet


def test_editable_columns_point_inside_headers():
    """Иначе правки читались бы из несуществующих колонок."""
    for sheet, spec in book.EDITABLE_COLUMNS.items():
        headers = book.HEADERS[sheet]
        assert spec.key_index < len(headers)
        assert headers[spec.key_index] == "Источник"
        for index in spec.editable_indices:
            assert index < len(headers)


def test_editable_sheets_are_declared_human_editable():
    assert set(book.EDITABLE_COLUMNS) <= book.HUMAN_EDITABLE


def test_sheet_names_with_spaces_are_quoted():
    assert book.a1_range(Sheet.SCHEDULE, "A1") == "'График работ'!A1"
    assert book.a1_range(Sheet.SUMMARY, "A1") == "Сводка!A1"


def test_apostrophe_in_sheet_name_is_doubled():
    assert book.a1_range("Объект 'Мира'", "A1") == "'Объект ''Мира'''!A1"


@pytest.mark.parametrize(
    ("index", "expected"), [(0, "A"), (5, "F"), (25, "Z"), (26, "AA"), (27, "AB")]
)
def test_column_labels(index, expected):
    assert book.column_label(index) == expected


def test_work_progress_goes_to_schedule():
    fact = Fact(
        type=FactType.WORK_PROGRESS,
        confidence=0.92,
        summary="штукатурка на кухне завершена",
        stage="штукатурка",
        room="кухня",
        status=WorkStatus.DONE,
    )

    assert book.sheet_for(fact.type) is Sheet.SCHEDULE
    row = book.fact_row(fact, occurred_at=AT, source="msg:mid.1")
    assert len(row) == len(book.HEADERS[Sheet.SCHEDULE])
    assert row[0] == "штукатурка"
    assert row[2] == "завершено"
    assert row[5] == "0.92"


def test_purchase_goes_to_purchases_with_amount():
    fact = Fact(
        type=FactType.PURCHASE,
        confidence=0.71,
        summary="грунтовка 2 шт",
        item="грунтовка",
        qty=2,
        amount=3400,
        currency="RUB",
    )

    assert book.sheet_for(fact.type) is Sheet.PURCHASES
    row = book.fact_row(fact, occurred_at=AT, source="msg:mid.2")
    assert len(row) == len(book.HEADERS[Sheet.PURCHASES])
    assert row[2] == 2  # без дробной части
    assert row[4] == 3400


def test_low_confidence_estimate_row_is_marked():
    fact = Fact(
        type=FactType.ESTIMATE_CHANGE,
        confidence=0.6,
        summary="доплата за перенос розеток",
        amount=5000,
    )

    row = book.fact_row(fact, occurred_at=AT, source="msg:mid.3")

    assert row[4] == "требует подтверждения"
    # Колонку менеджера система не заполняет.
    assert row[5] == ""


def test_issue_goes_to_open_questions():
    fact = Fact(type=FactType.ISSUE, confidence=0.9, summary="трещина в санузле")

    assert book.sheet_for(fact.type) is Sheet.OPEN_QUESTIONS
    row = book.fact_row(fact, occurred_at=AT, source="msg:mid.4")
    assert len(row) == len(book.HEADERS[Sheet.OPEN_QUESTIONS])
    assert row[3] == "открыт"
    assert row[4] == ""  # решение вписывает человек


def test_every_fact_type_has_a_sheet():
    for fact_type in FactType:
        assert book.sheet_for(fact_type) in book.OBJECT_SHEETS


def test_source_link_falls_back_to_event_id():
    assert book.source_link("mid.9", 42) == "msg:mid.9"
    assert book.source_link(None, 42) == "event:42"


def test_journal_row_width_matches_headers():
    row = book.journal_row(
        occurred_at=AT,
        event_type="message_created",
        description="привезли плитку",
        confidence=None,
        applied=True,
        source="msg:mid.5",
    )

    assert len(row) == len(book.HEADERS[Sheet.JOURNAL])
    assert row[3] == ""  # достоверность не указана
    assert row[4] == "да"


def test_summary_rows_width_matches_headers():
    rows = book.summary_rows({"current_stage": "штукатурка"}, updated_at=AT)

    assert rows
    for row in rows:
        assert len(row) == len(book.HEADERS[Sheet.SUMMARY])


async def test_quota_limiter_allows_burst_within_window():
    limiter = QuotaLimiter(max_requests=60, window_seconds=60)
    started = time.monotonic()

    await asyncio.gather(*(limiter.acquire() for _ in range(60)))

    assert time.monotonic() - started < 0.2


async def test_quota_limiter_blocks_beyond_window():
    """61-й запрос в минуту должен подождать, а не получить 429."""
    limiter = QuotaLimiter(max_requests=3, window_seconds=0.5)
    started = time.monotonic()

    for _ in range(4):
        await limiter.acquire()

    assert time.monotonic() - started >= 0.4
