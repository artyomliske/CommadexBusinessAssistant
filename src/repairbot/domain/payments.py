"""Регулярные платежи: расчёт дат.

Чистые функции, без базы. Здесь живёт единственное нетривиальное место
платёжного календаря — вычисление следующей даты, и вынесено оно отдельно
именно потому, что ошибается на нём почти каждый.

**Про конец месяца.** Платёж 31-го числа в феврале приходится на 28-е.
Наивный «прибавить месяц и запомнить, что получилось» превращает 31-е в
28-е навсегда: в марте платёж останется 28-го, хотя должен вернуться на
31-е. Поэтому день месяца хранится **отдельно** от даты и служит якорем,
а фактическая дата вычисляется от него с прижатием к концу месяца.

**Про часовые пояса.** Платёж — это дата, а не момент времени: «связь
оплачивается 5-го» не зависит от того, где вы находитесь. Поэтому всюду
`date`, а не `datetime`, и никаких переводов зон.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from enum import StrEnum


class Period(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    WEEKLY = "weekly"
    ONCE = "once"
    """Разовый платёж: напомнить и закрыть."""

    @property
    def title(self) -> str:
        return _PERIOD_TITLES[self]


_PERIOD_TITLES: dict[Period, str] = {
    Period.MONTHLY: "ежемесячно",
    Period.QUARTERLY: "раз в квартал",
    Period.YEARLY: "раз в год",
    Period.WEEKLY: "еженедельно",
    Period.ONCE: "разово",
}


class Category(StrEnum):
    SUBSCRIPTION = "subscription"
    TELECOM = "telecom"
    RENT = "rent"
    TAX = "tax"
    SALARY = "salary"
    UTILITIES = "utilities"
    OTHER = "other"

    @property
    def title(self) -> str:
        return _CATEGORY_TITLES[self]


_CATEGORY_TITLES: dict[Category, str] = {
    Category.SUBSCRIPTION: "подписка",
    Category.TELECOM: "связь и интернет",
    Category.RENT: "аренда",
    Category.TAX: "налоги и взносы",
    Category.SALARY: "зарплата",
    Category.UTILITIES: "коммунальные",
    Category.OTHER: "прочее",
}


def clamp_to_month(year: int, month: int, day: int) -> date:
    """Дата в указанном месяце с прижатием дня к последнему числу.

    31 февраля не существует, но «платёж 31-го числа» существует. Прижимаем
    к 28/29 — и это единственное место, где такое прижатие происходит,
    поэтому якорный день не теряется.
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def add_months(anchor: date, months: int, *, day_of_month: int | None = None) -> date:
    """Прибавить месяцы, сохранив якорный день месяца."""
    day = day_of_month or anchor.day
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return clamp_to_month(year, month, day)


def next_due(
    current: date,
    period: Period,
    *,
    day_of_month: int | None = None,
    after: date | None = None,
) -> date | None:
    """Следующая дата платежа после `current`.

    `after` сдвигает результат вперёд, пока он не окажется позже указанной
    даты. Это нужно после долгого простоя: платёж, пропущенный трижды, не
    должен «догонять» по одному шагу за запуск — иначе календарь будет
    неделю показывать прошлое.

    Для разового платежа следующей даты нет: он закрывается насовсем.
    """
    if period is Period.ONCE:
        return None

    step = _advance(current, period, day_of_month)
    if after is None:
        return step

    # Предохранитель от бесконечного цикла: шаг всегда положительный,
    # но испорченные данные в базе могут дать иное.
    guard = 0
    while step <= after and guard < 1000:
        step = _advance(step, period, day_of_month)
        guard += 1
    return step


def _advance(current: date, period: Period, day_of_month: int | None) -> date:
    match period:
        case Period.WEEKLY:
            return current + timedelta(days=7)
        case Period.MONTHLY:
            return add_months(current, 1, day_of_month=day_of_month)
        case Period.QUARTERLY:
            return add_months(current, 3, day_of_month=day_of_month)
        case Period.YEARLY:
            day = day_of_month or current.day
            return clamp_to_month(current.year + 1, current.month, day)
        case _:  # pragma: no cover — ONCE обработан выше
            return current


def days_until(due_on: date, today: date) -> int:
    """Сколько дней до платежа. Отрицательное — просрочен."""
    return (due_on - today).days
