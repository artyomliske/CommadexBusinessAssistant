"""Контролёр исходящих действий (раздел 6 ТЗ).

Единственная дверь наружу. Требуют Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from repairbot.db.models import DialogState, OutboundMessage
from repairbot.outbound.controller import (
    SWITCH_UNAVAILABLE,
    Controller,
    KillSwitch,
    OutboundRequest,
    contains_stop_word,
    pause_autoreplies,
)
from repairbot.outbound.policy import Audience, Intent, Verdict


class FakeRedis:
    def __init__(self, value: str | None = None) -> None:
        self.store: dict[str, str] = {}
        if value:
            self.store[KillSwitch.KEY] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


def _request(**kw) -> OutboundRequest:
    defaults = {
        "channel": "max",
        "channel_chat_id": "-100500",
        "text": "Штукатурка на кухне завершена",
        "intent": Intent.STATUS_UPDATE,
        "audience": Audience.CLIENT,
        "idempotency_key": kw.pop("key", "k1"),
        "discloses_automation": True,
    }
    defaults.update(kw)
    return OutboundRequest(**defaults)


async def _disclosed(session, chat_id: str = "-100500") -> None:
    """Система уже представилась — чтобы не мешал предохранитель первого контакта."""
    session.add(
        DialogState(
            channel="max",
            channel_chat_id=chat_id,
            automation_disclosed_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()


# --- обычный путь ---


async def test_safe_message_is_allowed_and_recorded(db_session):
    await _disclosed(db_session)

    result = await Controller(db_session).review(_request())

    assert result.verdict is Verdict.ALLOW
    assert result.may_send is True

    record = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert record.verdict == "allow"
    assert record.sent_at is None  # отправка отмечается отдельно


async def test_every_attempt_is_audited_even_when_blocked(db_session):
    """Журнал аудита обязан отвечать на вопрос «почему бот это написал»."""
    await _disclosed(db_session)

    await Controller(db_session).review(
        _request(text="Доплата составит 15 000 руб", key="k-money")
    )

    record = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert record.verdict == "hold"
    assert record.reasons["reasons"]


# --- централизованная остановка ---


async def test_kill_switch_blocks_everything(db_session):
    await _disclosed(db_session)
    switch = KillSwitch(FakeRedis("авария на объекте"))

    result = await Controller(db_session, kill_switch=switch).review(_request())

    assert result.verdict is Verdict.BLOCK
    assert any("остановка" in r for r in result.reasons)


async def test_unreachable_switch_blocks_outbound(db_session):
    """Если состояние предохранителя проверить нельзя — не отправляем.

    Молчаливое «разрешено» означало бы, что при падении Redis система
    начинает писать заказчикам без единой проверки остановки.
    """

    class BrokenRedis:
        async def get(self, key: str) -> str:
            raise ConnectionError("Connection refused")

    await _disclosed(db_session)
    switch = KillSwitch(BrokenRedis())

    result = await Controller(db_session, kill_switch=switch).review(_request())

    assert result.verdict is Verdict.BLOCK
    assert any("проверить состояние остановки" in r for r in result.reasons)


async def test_unreachable_switch_reports_reason_instead_of_raising():
    """Страница черновиков должна открываться и при недоступном Redis."""

    class BrokenRedis:
        async def get(self, key: str) -> str:
            raise ConnectionError("Connection refused")

    assert await KillSwitch(BrokenRedis()).reason() == SWITCH_UNAVAILABLE


async def test_kill_switch_can_be_released(db_session):
    await _disclosed(db_session)
    switch = KillSwitch(FakeRedis())
    await switch.engage("проверка")

    blocked = await Controller(db_session, kill_switch=switch).review(_request(key="k-a"))
    await switch.release()
    allowed = await Controller(db_session, kill_switch=switch).review(_request(key="k-b"))

    assert blocked.verdict is Verdict.BLOCK
    assert allowed.verdict is Verdict.ALLOW


# --- защита от циклов ---


async def test_message_to_bot_is_blocked(db_session):
    """Защита от циклов при взаимодействии с другими ботами."""
    await _disclosed(db_session)

    result = await Controller(db_session).review(_request(recipient_is_bot=True))

    assert result.verdict is Verdict.BLOCK
    assert any("бот" in r for r in result.reasons)


# --- стоп-слово ---


@pytest.mark.parametrize(
    "text",
    ["позовите человека", "хочу говорить с менеджером", "Человек!", "дайте менеджера"],
)
def test_stop_word_detected(text):
    assert contains_stop_word(text) is not None


@pytest.mark.parametrize(
    "text",
    ["человеческий фактор подвёл", "это менеджерская работа", "всё хорошо"],
)
def test_stop_word_not_triggered_by_similar_words(text):
    """«человеческий фактор» не должен останавливать диалог."""
    assert contains_stop_word(text) is None


async def test_paused_dialog_blocks_outbound(db_session):
    await _disclosed(db_session)
    await pause_autoreplies(
        db_session, channel="max", channel_chat_id="-100500", reason="стоп-слово: человек"
    )

    result = await Controller(db_session).review(_request())

    assert result.verdict is Verdict.BLOCK
    assert any("приостановлены" in r for r in result.reasons)


async def test_pause_lasts_24_hours(db_session):
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    until = await pause_autoreplies(
        db_session, channel="max", channel_chat_id="-100777", reason="стоп-слово", now=now
    )

    assert until - now == timedelta(hours=24)


async def test_expired_pause_does_not_block(db_session):
    await _disclosed(db_session)
    db_session.add(
        DialogState(
            channel="max",
            channel_chat_id="-100888",
            autoreply_paused_until=datetime.now(tz=UTC) - timedelta(hours=1),
            automation_disclosed_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()

    result = await Controller(db_session).review(
        _request(channel_chat_id="-100888", key="k-expired")
    )

    assert result.verdict is Verdict.ALLOW


# --- частота и дубликаты ---


async def test_rate_limit_holds_message(db_session):
    await _disclosed(db_session)
    now = datetime.now(tz=UTC)
    for i in range(6):
        db_session.add(
            OutboundMessage(
                channel="max", channel_chat_id="-100500", intent="status_update",
                audience="client", text=f"сообщение {i}", verdict="allow",
                reasons={}, idempotency_key=f"sent-{i}", sent_at=now,
            )
        )
    await db_session.flush()

    result = await Controller(db_session).review(_request(key="k-over"))

    assert result.verdict is Verdict.HOLD
    assert any("частота" in r for r in result.reasons)


async def test_duplicate_text_is_blocked(db_session):
    """Одобренное к отправке уже считается дубликатом.

    Ждать фактической отправки нельзя: два одобренных сообщения стоят
    в очереди, и оба уйдут заказчику.
    """
    await _disclosed(db_session)
    controller = Controller(db_session)

    first = await controller.review(_request(key="k-1"))
    second = await controller.review(_request(key="k-2"))

    assert first.verdict is Verdict.ALLOW
    assert second.verdict is Verdict.BLOCK
    assert any("уже отправлялся" in r for r in second.reasons)


async def test_blocked_text_is_not_a_duplicate(db_session):
    """Заблокированное не ушло — значит повторить его можно.

    Иначе сообщение, не прошедшее из-за аварийной остановки, осталось бы
    заблокированным навсегда уже как «дубликат» самого себя.
    """
    await _disclosed(db_session)
    switch = KillSwitch(FakeRedis("авария"))

    blocked = await Controller(db_session, kill_switch=switch).review(_request(key="k-x"))
    await switch.release()
    retried = await Controller(db_session, kill_switch=switch).review(_request(key="k-y"))

    assert blocked.verdict is Verdict.BLOCK
    assert retried.verdict is Verdict.ALLOW


async def test_held_draft_does_not_block_a_retry(db_session):
    """Черновик, ждущий менеджера, наружу ещё не ушёл."""
    await _disclosed(db_session)
    controller = Controller(db_session)

    held = await controller.review(_request(text="Доплата 15 000 руб", key="h-1"))
    again = await controller.review(_request(text="Доплата 15 000 руб", key="h-2"))

    assert held.verdict is Verdict.HOLD
    assert again.verdict is Verdict.HOLD  # снова черновик, а не отказ по дубликату
    assert not any("уже отправлялся" in r for r in again.reasons)


async def test_same_idempotency_key_is_not_processed_twice(db_session):
    """Повтор задачи не должен приводить ко второй отправке."""
    await _disclosed(db_session)
    controller = Controller(db_session)

    await controller.review(_request(key="same"))
    repeat = await controller.review(_request(key="same"))

    assert repeat.duplicate is True
    assert repeat.verdict is Verdict.BLOCK
    count = (await db_session.execute(select(func.count(OutboundMessage.id)))).scalar_one()
    assert count == 1


# --- первый контакт ---


async def test_first_contact_without_disclosure_is_held(db_session):
    """Диалога ещё нет — значит система не представлялась."""
    result = await Controller(db_session).review(
        _request(discloses_automation=False, key="k-first")
    )

    assert result.verdict is Verdict.HOLD
    assert any("автоматическ" in r for r in result.reasons)


# --- отметка отправки ---


async def test_mark_sent_records_time_and_message_id(db_session):
    await _disclosed(db_session)
    controller = Controller(db_session)
    result = await controller.review(_request())

    await controller.mark_sent(result.outbound_id, channel_message_id="mid.out.1")
    await db_session.flush()

    record = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert record.sent_at is not None
    assert record.channel_message_id == "mid.out.1"


async def test_sent_message_counts_toward_rate_limit(db_session):
    """Пока сообщение не отправлено, оно частоту не расходует."""
    await _disclosed(db_session)
    controller = Controller(db_session)

    for i in range(6):
        result = await controller.review(_request(text=f"текст {i}", key=f"k-{i}"))
        assert result.verdict is Verdict.ALLOW
        await controller.mark_sent(result.outbound_id, channel_message_id=f"m{i}")
    await db_session.flush()

    seventh = await controller.review(_request(text="текст 7", key="k-7"))

    assert seventh.verdict is Verdict.HOLD


# --- предохранители не должны глушить своих ---


async def test_frequency_limit_does_not_apply_to_internal(db_session):
    """Помощник отвечает на прямые вопросы, и шесть за час — обычный темп.

    Предел бережёт заказчика от вала автоответов. Для разговора со
    своим он означал бы молчание ровно на седьмой реплике, причём без
    всякого объяснения для собеседника.
    """
    controller = Controller(db_session)
    for i in range(8):
        outcome = await controller.review(
            OutboundRequest(
                channel="max",
                channel_chat_id="271217034",
                text=f"ответ {i}",
                intent=Intent.INTERNAL_NOTICE,
                audience=Audience.MANAGER,
                idempotency_key=f"assistant:{i}",
            )
        )
        await db_session.execute(
            sql_update(OutboundMessage)
            .where(OutboundMessage.id == outcome.outbound_id)
            .values(sent_at=datetime.now(tz=UTC))
        )
        await db_session.flush()

    assert outcome.verdict is Verdict.ALLOW


async def test_frequency_limit_still_applies_to_clients(db_session):
    """Заказчику десяток сообщений за час — повод перестать их читать."""
    controller = Controller(db_session)
    for i in range(8):
        outcome = await controller.review(
            OutboundRequest(
                channel="max",
                channel_chat_id="-100500",
                text=f"ответ {i}",
                intent=Intent.STATUS_UPDATE,
                audience=Audience.CLIENT,
                idempotency_key=f"client:{i}",
            )
        )
        await db_session.execute(
            sql_update(OutboundMessage)
            .where(OutboundMessage.id == outcome.outbound_id)
            .values(sent_at=datetime.now(tz=UTC))
        )
        await db_session.flush()

    assert outcome.verdict is Verdict.HOLD
    assert any("частота" in r for r in outcome.reasons)


async def test_same_answer_to_a_repeated_question_is_allowed(db_session):
    """Дважды спросили — дважды ответить. Молчать вместо этого нельзя."""
    controller = Controller(db_session)
    for i in range(2):
        outcome = await controller.review(
            OutboundRequest(
                channel="max",
                channel_chat_id="271217034",
                text="Да, ты Артём Лиске.",
                intent=Intent.INTERNAL_NOTICE,
                audience=Audience.MANAGER,
                idempotency_key=f"assistant:repeat:{i}",
            )
        )

    assert outcome.verdict is Verdict.ALLOW


async def test_repeated_text_to_a_client_is_still_blocked(db_session):
    controller = Controller(db_session)
    for i in range(2):
        outcome = await controller.review(
            OutboundRequest(
                channel="max",
                channel_chat_id="-100500",
                text="Сроки уточняем.",
                intent=Intent.STATUS_UPDATE,
                audience=Audience.CLIENT,
                # Иначе решает правило первого контакта, а проверяем мы
                # здесь не его.
                discloses_automation=True,
                idempotency_key=f"client:repeat:{i}",
            )
        )

    assert outcome.verdict is Verdict.BLOCK
