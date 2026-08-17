"""Платёжный календарь: напоминания о регулярных платежах.

Речь о собственных расходах компании — подписки, связь, аренда, налоги, —
за которыми иначе не следит никто. К объектам ремонта отношения не имеет.

Про суммы. Раздел 6 запрещает автоматические сообщения с денежными
сведениями **внешним** адресатам: заказчику нельзя называть цифру, которую
не подтвердил человек. Здесь адресат внутренний и деньги его собственные,
поэтому суммы в напоминании есть — политика это и так разрешает, отдельного
исключения не потребовалось.

Напоминание идёт через контролёр, как и всё остальное: путь наружу мимо
журнала аудита не должен существовать даже для мелочей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot import text
from repairbot.db.models import RecurringPayment
from repairbot.domain.payments import Category, Period, days_until, next_due
from repairbot.observability import get_logger
from repairbot.outbound.controller import Controller, OutboundRequest
from repairbot.outbound.policy import Audience, Intent

log = get_logger(__name__)

HORIZON_DAYS = 45
"""Насколько вперёд смотрит календарь на странице."""


class PaymentError(Exception):
    """Действие применить нельзя."""


@dataclass(slots=True)
class DueItem:
    payment: RecurringPayment
    days_left: int

    @property
    def is_overdue(self) -> bool:
        return self.days_left < 0

    @property
    def is_today(self) -> bool:
        return self.days_left == 0


@dataclass(slots=True)
class ReminderResult:
    upcoming: int = 0
    overdue: int = 0
    outbound_id: int | None = None
    verdict: str | None = None
    titles: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "upcoming": self.upcoming,
            "overdue": self.overdue,
            "outbound_id": self.outbound_id,
            "verdict": self.verdict,
            "titles": self.titles[:10],
        }


async def load_calendar(
    session: AsyncSession, *, today: date | None = None, horizon_days: int = HORIZON_DAYS
) -> list[DueItem]:
    """Платежи на ближайшие недели и всё просроченное.

    Просроченное показывается всегда, независимо от горизонта: платёж,
    о котором забыли месяц назад, — самое важное на этой странице.
    """
    today = today or _today()
    rows = await session.execute(
        select(RecurringPayment)
        .where(RecurringPayment.active.is_(True))
        .order_by(RecurringPayment.next_due_on, RecurringPayment.title)
    )
    items = [
        DueItem(payment=p, days_left=days_until(p.next_due_on, today))
        for p in rows.scalars().all()
    ]
    return [i for i in items if i.days_left <= horizon_days]


async def due_for_reminder(
    session: AsyncSession, *, today: date | None = None
) -> tuple[list[DueItem], list[DueItem]]:
    """Что пора напомнить сегодня: (предстоящие, просроченные).

    Отбираем в SQL по признаку «ещё не напоминали про эту дату»: без него
    ежедневная задача слала бы одно и то же каждый день до срока.
    """
    today = today or _today()
    rows = await session.execute(
        select(RecurringPayment).where(
            RecurringPayment.active.is_(True),
            or_(
                RecurringPayment.notified_for.is_(None),
                RecurringPayment.notified_for != RecurringPayment.next_due_on,
                RecurringPayment.overdue_notified_for.is_(None),
                RecurringPayment.overdue_notified_for != RecurringPayment.next_due_on,
            ),
        )
    )

    upcoming: list[DueItem] = []
    overdue: list[DueItem] = []
    for payment in rows.scalars().all():
        left = days_until(payment.next_due_on, today)
        item = DueItem(payment=payment, days_left=left)

        if left < 0:
            if payment.overdue_notified_for != payment.next_due_on:
                overdue.append(item)
        elif left <= payment.notify_days_before:
            if payment.notified_for != payment.next_due_on:
                upcoming.append(item)

    upcoming.sort(key=lambda i: i.days_left)
    overdue.sort(key=lambda i: i.days_left)
    return upcoming, overdue


async def send_reminder(
    session: AsyncSession,
    *,
    controller: Controller | None,
    channel: str,
    chat_id: str,
    today: date | None = None,
) -> ReminderResult:
    """Собрать напоминание и провести его через контролёр."""
    today = today or _today()
    upcoming, overdue = await due_for_reminder(session, today=today)

    result = ReminderResult(upcoming=len(upcoming), overdue=len(overdue))
    result.titles = [i.payment.title for i in (*overdue, *upcoming)]
    if not upcoming and not overdue:
        return result

    if controller is not None and chat_id:
        outcome = await controller.review(
            OutboundRequest(
                channel=channel,
                channel_chat_id=chat_id,
                text=render_reminder(upcoming, overdue, today),
                # Внутреннее уведомление: адресат — сам плательщик.
                intent=Intent.INTERNAL_NOTICE,
                audience=Audience.MANAGER,
                # Ключ по дате: перезапуск воркера не пришлёт второе
                # напоминание за тот же день.
                idempotency_key=f"payments:{today.isoformat()}",
            )
        )
        result.verdict = outcome.verdict.value
        if outcome.may_send:
            result.outbound_id = outcome.outbound_id

    # Помечаем только после того, как контролёр записал решение: иначе
    # заблокированное напоминание считалось бы отправленным и платёж
    # молча выпал бы из календаря.
    for item in upcoming:
        item.payment.notified_for = item.payment.next_due_on
    for item in overdue:
        item.payment.overdue_notified_for = item.payment.next_due_on
    await session.flush()

    log.info("payments.reminded", **result.as_dict())
    return result


def render_reminder(
    upcoming: list[DueItem], overdue: list[DueItem], today: date
) -> str:
    """Текст напоминания. Просроченное — первым."""
    lines: list[str] = []

    if overdue:
        lines.append("Просрочено:")
        for item in overdue:
            lines.append(
                f"• {item.payment.title} — {_money(item.payment)}, "
                f"срок был {item.payment.next_due_on:%d.%m}, "
                f"{_days_word(-item.days_left)} назад"
            )
        if upcoming:
            lines.append("")

    if upcoming:
        lines.append("Ближайшие платежи:")
        for item in upcoming:
            lines.append(
                f"• {item.payment.title} — {_money(item.payment)}, {_when(item, today)}"
            )

    total = _total(upcoming + overdue)
    if total:
        lines.append("")
        lines.append(f"Итого к оплате: {total}")

    return "\n".join(lines)


def _when(item: DueItem, today: date) -> str:
    if item.is_today:
        return "сегодня"
    if item.days_left == 1:
        return "завтра"
    return f"{item.payment.next_due_on:%d.%m}, через {_days_word(item.days_left)}"


def _days_word(days: int) -> str:
    """Дни с правильным окончанием. Правило одно на панель и на сообщения."""
    return text.count(days, "день")


def _money(payment: RecurringPayment) -> str:
    if payment.amount is None:
        # Связь по факту, коммунальные по счётчику: суммы заранее нет.
        return "сумма по счёту"
    return f"{payment.amount:,.0f} {payment.currency}".replace(",", " ")


def _total(items: list[DueItem]) -> str:
    amounts = [i.payment.amount for i in items if i.payment.amount is not None]
    if not amounts:
        return ""
    currencies = {i.payment.currency for i in items if i.payment.amount is not None}
    if len(currencies) > 1:
        # Складывать рубли с долларами нельзя, а притворяться, что можно,
        # хуже, чем не показать итог вовсе.
        return ""
    total = sum(amounts, Decimal(0))
    unknown = sum(1 for i in items if i.payment.amount is None)
    text = f"{total:,.0f} {currencies.pop()}".replace(",", " ")
    return f"{text} и ещё {unknown} по счёту" if unknown else text


# --- изменения ---


async def mark_paid(
    session: AsyncSession,
    payment_id: int,
    *,
    on: date | None = None,
    by: str | None = None,
) -> RecurringPayment:
    """Отметить платёж оплаченным и сдвинуть дату на следующий период.

    Разовый платёж после оплаты снимается с календаря: следующей даты у
    него нет, и оставлять его висеть просроченным было бы враньём.
    """
    payment = await _load(session, payment_id)
    paid_on = on or _today()

    payment.last_paid_on = paid_on
    upcoming = next_due(
        payment.next_due_on,
        Period(payment.period),
        day_of_month=payment.day_of_month,
        after=paid_on,
    )

    if upcoming is None:
        payment.active = False
    else:
        payment.next_due_on = upcoming
        # Новая дата — новый повод напомнить.
        payment.notified_for = None
        payment.overdue_notified_for = None

    await session.flush()
    log.info(
        "payments.paid",
        payment_id=payment_id,
        title=payment.title,
        next_due_on=payment.next_due_on.isoformat(),
        by=by,
    )
    return payment


async def create(
    session: AsyncSession,
    *,
    title: str,
    next_due_on: date,
    period: str = Period.MONTHLY.value,
    category: str = Category.OTHER.value,
    amount: Decimal | None = None,
    currency: str = "RUB",
    notify_days_before: int = 3,
    note: str | None = None,
) -> RecurringPayment:
    title = (title or "").strip()
    if not title:
        raise PaymentError("Название обязательно — иначе непонятно, за что платить")
    if period not in set(Period):
        raise PaymentError(f"Неизвестная периодичность: {period}")
    if category not in set(Category):
        raise PaymentError(f"Неизвестная категория: {category}")
    if not 0 <= notify_days_before <= 60:
        raise PaymentError("Напоминать можно за 0–60 дней")

    payment = RecurringPayment(
        title=title,
        category=category,
        amount=amount,
        currency=currency,
        period=period,
        # Якорь берём из даты платежа: он нужен, чтобы 31-е возвращалось
        # после короткого месяца.
        day_of_month=next_due_on.day if period in _MONTH_ANCHORED else None,
        next_due_on=next_due_on,
        notify_days_before=notify_days_before,
        note=note,
    )
    session.add(payment)
    await session.flush()
    log.info("payments.created", payment_id=payment.id, title=title, period=period)
    return payment


_MONTH_ANCHORED = {Period.MONTHLY.value, Period.QUARTERLY.value, Period.YEARLY.value}


async def archive(session: AsyncSession, payment_id: int, *, by: str | None = None) -> None:
    """Снять платёж с календаря, не удаляя запись."""
    payment = await _load(session, payment_id)
    payment.active = False
    await session.flush()
    log.info("payments.archived", payment_id=payment_id, title=payment.title, by=by)


async def _load(session: AsyncSession, payment_id: int) -> RecurringPayment:
    payment = (
        await session.execute(
            select(RecurringPayment).where(RecurringPayment.id == payment_id)
        )
    ).scalar_one_or_none()
    if payment is None:
        raise PaymentError(f"Платёж {payment_id} не найден")
    return payment


def _today() -> date:
    return datetime.now(tz=UTC).date()
