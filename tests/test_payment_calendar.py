"""Платёжный календарь: напоминания и отметка оплаты. Требуют Postgres."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from repairbot.agents import payment_calendar as pc
from repairbot.db.models import OutboundMessage, RecurringPayment
from repairbot.outbound.controller import Controller
from repairbot.outbound.policy import Verdict

TODAY = date(2026, 8, 6)


async def _payment(session, **kw) -> RecurringPayment:
    return await pc.create(
        session,
        title=kw.pop("title", "Мобильная связь МТС"),
        next_due_on=kw.pop("next_due_on", TODAY + timedelta(days=2)),
        period=kw.pop("period", "monthly"),
        category=kw.pop("category", "telecom"),
        amount=kw.pop("amount", Decimal("1450")),
        notify_days_before=kw.pop("notify_days_before", 3),
        **kw,
    )


# --- создание ---


async def test_created_payment_anchors_the_day_of_month(db_session):
    """Якорь нужен, чтобы 31-е возвращалось после короткого месяца."""
    payment = await _payment(db_session, next_due_on=date(2026, 1, 31))

    assert payment.day_of_month == 31


async def test_weekly_payment_has_no_month_anchor(db_session):
    payment = await _payment(db_session, period="weekly")

    assert payment.day_of_month is None


async def test_title_is_required(db_session):
    with pytest.raises(pc.PaymentError, match="Название"):
        await _payment(db_session, title="   ")


async def test_unknown_period_is_refused(db_session):
    with pytest.raises(pc.PaymentError, match="периодичность"):
        await _payment(db_session, period="каждое полнолуние")


# --- что попадает в напоминание ---


async def test_payment_within_notice_window_is_reminded(db_session):
    await _payment(db_session, next_due_on=TODAY + timedelta(days=2))

    upcoming, overdue = await pc.due_for_reminder(db_session, today=TODAY)

    assert len(upcoming) == 1
    assert not overdue


async def test_distant_payment_is_left_alone(db_session):
    """Напоминать за месяц — приучить не читать напоминания."""
    await _payment(db_session, next_due_on=TODAY + timedelta(days=20))

    upcoming, overdue = await pc.due_for_reminder(db_session, today=TODAY)

    assert not upcoming and not overdue


async def test_overdue_payment_is_reported_separately(db_session):
    await _payment(db_session, next_due_on=TODAY - timedelta(days=4))

    upcoming, overdue = await pc.due_for_reminder(db_session, today=TODAY)

    assert not upcoming
    assert len(overdue) == 1
    assert overdue[0].is_overdue


async def test_archived_payment_is_not_reminded(db_session):
    payment = await _payment(db_session)
    await pc.archive(db_session, payment.id)

    upcoming, overdue = await pc.due_for_reminder(db_session, today=TODAY)

    assert not upcoming and not overdue


async def test_reminder_is_not_repeated_daily(db_session):
    """Ежедневная задача не должна слать одно и то же до самого срока."""
    await _payment(db_session, next_due_on=TODAY + timedelta(days=3))

    first = await pc.send_reminder(
        db_session, controller=None, channel="max", chat_id="", today=TODAY
    )
    second_upcoming, _ = await pc.due_for_reminder(
        db_session, today=TODAY + timedelta(days=1)
    )

    assert first.upcoming == 1
    assert second_upcoming == []


async def test_overdue_notice_comes_once_but_after_the_upcoming_one(db_session):
    """Напоминание до срока и сообщение о просрочке — разные поводы."""
    await _payment(db_session, next_due_on=TODAY + timedelta(days=1))

    await pc.send_reminder(
        db_session, controller=None, channel="max", chat_id="", today=TODAY
    )
    later = TODAY + timedelta(days=3)
    upcoming, overdue = await pc.due_for_reminder(db_session, today=later)

    assert not upcoming
    assert len(overdue) == 1


# --- текст ---


async def test_message_puts_overdue_first(db_session):
    await _payment(db_session, title="Хостинг", next_due_on=TODAY - timedelta(days=5))
    await _payment(db_session, title="Связь", next_due_on=TODAY + timedelta(days=1))

    upcoming, overdue = await pc.due_for_reminder(db_session, today=TODAY)
    text = pc.render_reminder(upcoming, overdue, TODAY)

    assert text.index("Просрочено") < text.index("Ближайшие")
    assert "5 дней назад" in text
    assert "завтра" in text


async def test_message_says_when_the_amount_is_unknown(db_session):
    """Связь по факту и коммунальные по счётчику заранее не известны."""
    await _payment(db_session, title="Электричество", amount=None)

    upcoming, overdue = await pc.due_for_reminder(db_session, today=TODAY)
    text = pc.render_reminder(upcoming, overdue, TODAY)

    assert "сумма по счёту" in text


async def test_total_is_not_shown_across_currencies(db_session):
    """Складывать рубли с долларами нельзя, а притворяться — хуже."""
    await _payment(db_session, title="Связь", amount=Decimal("1450"))
    await _payment(db_session, title="Хостинг", amount=Decimal("20"), currency="USD")

    upcoming, _ = await pc.due_for_reminder(db_session, today=TODAY)
    text = pc.render_reminder(upcoming, [], TODAY)

    assert "Итого" not in text


async def test_total_counts_known_amounts_and_mentions_the_rest(db_session):
    await _payment(db_session, title="Связь", amount=Decimal("1450"))
    await _payment(db_session, title="Электричество", amount=None)

    upcoming, _ = await pc.due_for_reminder(db_session, today=TODAY)
    text = pc.render_reminder(upcoming, [], TODAY)

    assert "Итого к оплате: 1 450 RUB и ещё 1 по счёту" in text


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней"), (21, "21 день")],
)
def test_days_are_declined(days, expected):
    assert pc._days_word(days) == expected


# --- отправка ---


async def test_reminder_goes_through_the_controller(db_session):
    """Путь наружу мимо журнала аудита не должен существовать."""
    await _payment(db_session, next_due_on=TODAY + timedelta(days=1))

    result = await pc.send_reminder(
        db_session,
        controller=Controller(db_session),
        channel="max",
        chat_id="-100777",
        today=TODAY,
    )

    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert message.audience == "manager"
    assert message.verdict == Verdict.ALLOW.value
    assert result.outbound_id == message.id


async def test_amounts_are_allowed_because_the_reader_is_internal(db_session):
    """Раздел 6 запрещает денежные сведения без подтверждения **внешним**.

    Здесь адресат внутренний и деньги его собственные, поэтому сумма в
    тексте не превращает напоминание в черновик.
    """
    await _payment(db_session, amount=Decimal("1450"), next_due_on=TODAY)

    result = await pc.send_reminder(
        db_session,
        controller=Controller(db_session),
        channel="max",
        chat_id="-100777",
        today=TODAY,
    )

    assert result.verdict == Verdict.ALLOW.value
    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert "1 450" in message.text


async def test_nothing_due_sends_nothing(db_session):
    await _payment(db_session, next_due_on=TODAY + timedelta(days=30))

    result = await pc.send_reminder(
        db_session,
        controller=Controller(db_session),
        channel="max",
        chat_id="-100777",
        today=TODAY,
    )

    assert result.outbound_id is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None


# --- оплата ---


async def test_paying_moves_the_date_forward(db_session):
    payment = await _payment(db_session, next_due_on=date(2026, 1, 31))

    await pc.mark_paid(db_session, payment.id, on=date(2026, 1, 30))

    assert payment.last_paid_on == date(2026, 1, 30)
    assert payment.next_due_on == date(2026, 2, 28)


async def test_paying_resets_the_reminder_flags(db_session):
    """Новая дата — новый повод напомнить."""
    payment = await _payment(db_session, next_due_on=TODAY)
    await pc.send_reminder(
        db_session, controller=None, channel="max", chat_id="", today=TODAY
    )
    assert payment.notified_for is not None

    await pc.mark_paid(db_session, payment.id, on=TODAY)

    assert payment.notified_for is None
    assert payment.overdue_notified_for is None


async def test_one_off_payment_leaves_the_calendar_after_payment(db_session):
    """Иначе он висел бы просроченным навсегда."""
    payment = await _payment(db_session, period="once", title="Пошлина")

    await pc.mark_paid(db_session, payment.id, on=TODAY)

    assert payment.active is False
    assert await pc.load_calendar(db_session, today=TODAY) == []


async def test_paying_after_a_long_delay_does_not_land_in_the_past(db_session):
    """Платёж, пропущенный трижды, не должен догоняться по одному шагу."""
    payment = await _payment(db_session, next_due_on=date(2026, 1, 15))

    await pc.mark_paid(db_session, payment.id, on=date(2026, 6, 20))

    assert payment.next_due_on == date(2026, 7, 15)


async def test_missing_payment_is_reported(db_session):
    with pytest.raises(pc.PaymentError, match="не найден"):
        await pc.mark_paid(db_session, 9999)


# --- календарь ---


async def test_calendar_shows_overdue_beyond_the_horizon(db_session):
    """Забытый месяц назад платёж — самое важное на этой странице."""
    await _payment(db_session, title="Хостинг", next_due_on=TODAY - timedelta(days=90))
    await _payment(db_session, title="Далёкий", next_due_on=TODAY + timedelta(days=90))

    items = await pc.load_calendar(db_session, today=TODAY, horizon_days=45)

    assert [i.payment.title for i in items] == ["Хостинг"]


# --- бот действительно доносит напоминание до мессенджера ---


class FakeAdapter:
    """Канал, записывающий отправленное вместо обращения к платформе."""

    from repairbot.domain.events import Channel as _Channel

    channel = _Channel.MAX

    def __init__(self) -> None:
        self.sent: list[str] = []

    def normalize(self, payload):  # pragma: no cover — здесь не используется
        return []

    async def send_text(self, message) -> str:
        self.sent.append(message.text)
        return f"mid.out.{len(self.sent)}"

    async def fetch_history(self, *a, **kw):  # pragma: no cover
        return []

    async def aclose(self) -> None:
        pass


@pytest.fixture
def channel_adapter():
    from repairbot.channels import registry

    adapter = FakeAdapter()
    registry.register(adapter)
    yield adapter
    registry.clear()


async def test_reminder_reaches_the_messenger(db_session, channel_adapter):
    """Весь путь: напоминание → контролёр → отправка в канал.

    Ради этого календарь и делался: страница в панели напоминает только
    тому, кто на неё зашёл.
    """
    from repairbot.outbound.sender import send_approved

    await _payment(db_session, title="Хостинг", next_due_on=TODAY, amount=Decimal("990"))

    controller = Controller(db_session)
    result = await pc.send_reminder(
        db_session,
        controller=controller,
        channel="max",
        chat_id="-100777",
        today=TODAY,
    )
    assert result.outbound_id is not None

    sent = await send_approved(db_session, result.outbound_id, controller=controller)

    assert sent.sent is True
    assert "Хостинг" in channel_adapter.sent[0]
    assert "990" in channel_adapter.sent[0]

    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert message.sent_at is not None


async def test_halted_outbound_stops_the_reminder(db_session, channel_adapter):
    """Аварийная остановка перекрывает и напоминания тоже.

    Иначе «стоп» останавливал бы разговоры с заказчиками, но не всё
    остальное, что бот шлёт сам.
    """

    class HaltedSwitch:
        async def reason(self) -> str:
            return "проверка"

    await _payment(db_session, next_due_on=TODAY)

    result = await pc.send_reminder(
        db_session,
        controller=Controller(db_session, kill_switch=HaltedSwitch()),
        channel="max",
        chat_id="-100777",
        today=TODAY,
    )

    assert result.outbound_id is None
    assert result.verdict == Verdict.BLOCK.value
    assert channel_adapter.sent == []
