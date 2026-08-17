"""Расчёт дат платежей.

Единственное нетривиальное место платёжного календаря — и то, на чём
ошибаются чаще всего.
"""

from __future__ import annotations

from datetime import date

import pytest

from repairbot.domain.payments import (
    Period,
    add_months,
    clamp_to_month,
    days_until,
    next_due,
)


def test_month_end_is_clamped_not_overflowed():
    """31 февраля не существует, а «платёж 31-го числа» существует."""
    assert clamp_to_month(2026, 2, 31) == date(2026, 2, 28)
    assert clamp_to_month(2028, 2, 31) == date(2028, 2, 29)
    assert clamp_to_month(2026, 4, 31) == date(2026, 4, 30)
    assert clamp_to_month(2026, 3, 31) == date(2026, 3, 31)


def test_anchor_day_returns_after_a_short_month():
    """Вот ради чего якорный день хранится отдельно от даты.

    Наивное «прибавить месяц к прошлой дате» превратило бы 31 января в
    28 февраля, а потом навсегда оставило бы платёж 28-го числа.
    """
    january = date(2026, 1, 31)

    february = next_due(january, Period.MONTHLY, day_of_month=31)
    march = next_due(february, Period.MONTHLY, day_of_month=31)

    assert february == date(2026, 2, 28)
    assert march == date(2026, 3, 31)


def test_without_an_anchor_the_day_would_stick():
    """Поведение без якоря показано намеренно: так видно цену решения."""
    february = next_due(date(2026, 1, 31), Period.MONTHLY)
    march = next_due(february, Period.MONTHLY)

    assert february == date(2026, 2, 28)
    assert march == date(2026, 3, 28)


def test_new_year_rolls_over():
    assert next_due(date(2026, 12, 5), Period.MONTHLY) == date(2027, 1, 5)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_quarterly_and_yearly():
    assert next_due(date(2026, 1, 10), Period.QUARTERLY) == date(2026, 4, 10)
    assert next_due(date(2026, 3, 1), Period.YEARLY) == date(2027, 3, 1)


def test_yearly_on_leap_day_clamps():
    """29 февраля бывает раз в четыре года, а платёж — каждый год."""
    assert next_due(date(2028, 2, 29), Period.YEARLY) == date(2029, 2, 28)


def test_weekly():
    assert next_due(date(2026, 8, 6), Period.WEEKLY) == date(2026, 8, 13)


def test_one_off_payment_has_no_next_date():
    """Разовый платёж закрывается насовсем, а не повторяется через месяц."""
    assert next_due(date(2026, 8, 6), Period.ONCE) is None


def test_long_downtime_does_not_leave_the_calendar_in_the_past():
    """Пропущенные платежи не должны догоняться по одному за запуск.

    Иначе после отпуска календарь неделю показывал бы прошлое.
    """
    result = next_due(
        date(2026, 1, 15), Period.MONTHLY, day_of_month=15, after=date(2026, 6, 20)
    )

    assert result == date(2026, 7, 15)


def test_advance_after_returns_strictly_future():
    """Ровно на границе дата тоже должна сдвинуться вперёд."""
    result = next_due(date(2026, 5, 15), Period.MONTHLY, after=date(2026, 6, 15))

    assert result == date(2026, 7, 15)


@pytest.mark.parametrize(
    ("due", "today", "expected"),
    [
        (date(2026, 8, 10), date(2026, 8, 6), 4),
        (date(2026, 8, 6), date(2026, 8, 6), 0),
        (date(2026, 8, 1), date(2026, 8, 6), -5),
    ],
)
def test_days_until_counts_overdue_as_negative(due, today, expected):
    assert days_until(due, today) == expected


def test_period_titles_are_human_readable():
    assert Period.MONTHLY.title == "ежемесячно"
    assert Period.ONCE.title == "разово"
