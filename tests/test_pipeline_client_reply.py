"""Клиентский агент внутри конвейера. Требуют Postgres.

Проверяется стык: вопрос заказчика доходит до агента, ответ проходит
контролёр и попадает в журнал аудита, а не уходит в канал напрямую.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from repairbot.agents.client_agent import DISCLOSURE, ClientAgent
from repairbot.agents.pipeline import EventPipeline
from repairbot.channels import registry
from repairbot.channels.max.adapter import MaxAdapter
from repairbot.db.models import (
    ChannelIdentity,
    DialogState,
    Event,
    LlmCall,
    OutboundMessage,
    Person,
    RepairObject,
)
from repairbot.domain.events import NEEDS_HUMAN_EVENT, Channel
from repairbot.ingest.service import IngestService
from repairbot.llm.base import LlmError, StructuredResponse
from repairbot.outbound.controller import pause_autoreplies
from repairbot.outbound.policy import Verdict
from tests.fixtures import max_updates as fx


class FakeRouter:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload

    async def complete_structured(self, request) -> StructuredResponse:
        if isinstance(self._payload, Exception):
            raise self._payload
        return StructuredResponse(
            provider="claude", model="claude-opus-5", payload=self._payload
        )


class SilentExtractor:
    """Экстрактор, который до модели не доходит.

    Здесь проверяется ответ заказчику, а не извлечение фактов: реальный
    экстрактор потребовал бы отдельного стенда и ничего бы не добавил.
    """

    schema_fingerprint = "test"

    async def extract(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise LlmError("экстрактор в этом тесте не используется")


def _reply(**overrides: Any) -> dict[str, Any]:
    payload = {
        "answer": "Сейчас идёт штукатурка стен, кухня уже готова.",
        "intent": "status_update",
        "grounded": True,
        "missing": None,
    }
    payload.update(overrides)
    return payload


def _pipeline(payload: dict[str, Any] | Exception) -> EventPipeline:
    return EventPipeline(SilentExtractor(), client_agent=ClientAgent(FakeRouter(payload)))


@pytest.fixture
def max_adapter(settings):
    adapter = MaxAdapter(settings)
    registry.register(adapter)
    yield adapter
    registry.clear()


async def _ask(session, question: str, *, link_object: bool = True) -> Event:
    """Принять вопрос заказчика в личном диалоге."""
    from repairbot.channels.max import normalizer

    payload = fx.dialog_message()
    payload["message"]["body"]["text"] = question
    (inbound,) = normalizer.normalize(payload)

    result = await IngestService(session).ingest(inbound)
    await session.flush()

    event = (
        await session.execute(select(Event).where(Event.id == result.event_id))
    ).scalar_one()

    if link_object:
        obj = RepairObject(
            code="obj_17",
            address="Ленина 5, кв. 12",
            state={"current_stage": "штукатурка", "status": "в работе"},
        )
        session.add(obj)
        await session.flush()
        event.object_id = obj.id
        await session.flush()
    return event


async def test_client_question_produces_an_approved_reply(db_session, max_adapter):
    event = await _ask(db_session, "Когда закончите с кухней?")

    outcome = await _pipeline(_reply()).process(db_session, event)

    assert outcome.reply_verdict == Verdict.ALLOW.value
    assert outcome.outbound_id is not None

    message = (
        await db_session.execute(select(OutboundMessage))
    ).scalar_one()
    assert message.audience == "client"
    assert message.intent == "status_update"
    assert message.source_event_id == event.id
    # Ничего никуда не ушло: конвейер только записал решение.
    assert message.sent_at is None


async def test_first_contact_introduces_the_system(db_session, max_adapter):
    """Раздел 6: при первом контакте система представляется автоматической.

    Без этого политика придержала бы сообщение, и первый же вопрос
    заказчика ушёл бы в очередь менеджеру.
    """
    event = await _ask(db_session, "Когда закончите с кухней?")

    outcome = await _pipeline(_reply()).process(db_session, event)

    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert message.text.startswith(DISCLOSURE)
    assert outcome.reply_verdict == Verdict.ALLOW.value


async def test_second_contact_does_not_repeat_the_disclosure(db_session, max_adapter):
    event = await _ask(db_session, "Когда закончите с кухней?")
    db_session.add(
        DialogState(
            channel=Channel.MAX.value,
            channel_chat_id=event.channel_chat_id,
            automation_disclosed_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()

    await _pipeline(_reply()).process(db_session, event)

    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert not message.text.startswith(DISCLOSURE)


async def test_money_in_the_answer_is_held_for_a_manager(db_session, max_adapter):
    """Сканер содержания важнее объявленного намерения.

    Модель назвала ответ обычным статусом, но в тексте сумма — раздел 6
    требует подтверждения человеком независимо от того, чем это назвали.
    """
    event = await _ask(db_session, "Сколько уже потрачено?")

    outcome = await _pipeline(
        _reply(answer="По смете израсходовано 240 000 ₽ из 500 000 ₽.")
    ).process(db_session, event)

    assert outcome.reply_verdict == Verdict.HOLD.value
    assert outcome.outbound_id is None

    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert message.verdict == Verdict.HOLD.value
    assert any("денежные" in r for r in message.reasons["reasons"])


async def test_complaint_is_never_answered_automatically(db_session, max_adapter):
    event = await _ask(db_session, "Это отвратительно, у вас брак по всей стене!")

    outcome = await _pipeline(_reply(intent="status_update")).process(db_session, event)

    assert outcome.reply_verdict == Verdict.HOLD.value
    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert message.intent == "complaint_response"


async def test_stop_word_blocks_the_reply_in_the_same_message(db_session, max_adapter):
    """«Позовите менеджера» ставит диалог на паузу — и ответа не будет.

    Стоп-слово срабатывает раньше в том же проходе конвейера, поэтому
    модель здесь даже не вызывается.
    """
    event = await _ask(db_session, "Позовите менеджера, когда закончите?")

    outcome = await _pipeline(_reply()).process(db_session, event)

    assert outcome.outbound_id is None
    assert outcome.reply_verdict is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None

    state = (await db_session.execute(select(DialogState))).scalar_one()
    assert state.is_paused


async def test_paused_dialog_is_not_answered(db_session, max_adapter):
    event = await _ask(db_session, "Когда закончите с кухней?")
    await pause_autoreplies(
        db_session,
        channel=Channel.MAX.value,
        channel_chat_id=event.channel_chat_id or "",
        reason="менеджер вмешался",
    )
    await db_session.flush()

    outcome = await _pipeline(_reply()).process(db_session, event)

    assert outcome.outbound_id is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None


async def test_question_without_an_object_goes_to_a_human(db_session, max_adapter):
    """Отвечать не по чему: состояние объекта — единственный источник."""
    event = await _ask(db_session, "Когда закончите с кухней?", link_object=False)

    outcome = await _pipeline(_reply()).process(db_session, event)

    assert outcome.outbound_id is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None

    events = (
        await db_session.execute(
            select(Event).where(Event.event_type == NEEDS_HUMAN_EVENT)
        )
    ).scalars().all()
    assert len(events) == 1
    assert "не привязан к объекту" in events[0].payload["reason"]


async def test_ungrounded_answer_goes_to_a_human(db_session, max_adapter):
    event = await _ask(db_session, "Когда сдадите объект?")

    outcome = await _pipeline(
        _reply(grounded=False, missing="в состоянии нет плановой даты")
    ).process(db_session, event)

    assert outcome.outbound_id is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None

    event_row = (
        await db_session.execute(
            select(Event).where(Event.event_type == NEEDS_HUMAN_EVENT)
        )
    ).scalar_one()
    assert "плановой даты" in event_row.payload["reason"]


async def test_model_failure_does_not_lose_the_question(db_session, max_adapter):
    """Приём событий важнее ответа: факты уже записаны, вопрос увидит человек."""
    event = await _ask(db_session, "Когда закончите с кухней?")

    outcome = await _pipeline(LlmError("провайдер недоступен")).process(db_session, event)

    assert outcome.outbound_id is None
    event_row = (
        await db_session.execute(
            select(Event).where(Event.event_type == NEEDS_HUMAN_EVENT)
        )
    ).scalar_one()
    assert "не удалось составить ответ" in event_row.payload["reason"]


async def test_reprocessing_does_not_answer_twice(db_session, max_adapter):
    """Ключ идемпотентности — по исходному событию.

    Повторный запуск задачи после перезапуска воркера не должен слать
    заказчику второй ответ на тот же вопрос.
    """
    event = await _ask(db_session, "Когда закончите с кухней?")
    pipeline = _pipeline(_reply())

    first = await pipeline.process(db_session, event)
    second = await pipeline.process(db_session, event)

    assert first.outbound_id is not None
    assert second.outbound_id is None

    messages = (await db_session.execute(select(OutboundMessage))).scalars().all()
    assert len(messages) == 1


async def test_staff_message_is_not_answered(db_session, max_adapter):
    event = await _ask(db_session, "Когда закончите с кухней?")

    # Роль ищется через сообщение: `Event.actor_id` ссылается на карточку
    # человека, которую приём событий не заводит.
    person = Person(display_name="Прораб Сергей", role="staff")
    db_session.add(person)
    await db_session.flush()
    identity = (await db_session.execute(select(ChannelIdentity))).scalar_one()
    identity.person_id = person.id
    await db_session.flush()

    outcome = await _pipeline(_reply()).process(db_session, event)

    assert outcome.outbound_id is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None


async def test_reply_cost_is_recorded(db_session, max_adapter):
    """Расход на модель — крупнейшая статья эксплуатации (раздел 9 ТЗ)."""
    event = await _ask(db_session, "Когда закончите с кухней?")

    await _pipeline(_reply()).process(db_session, event)

    call = (await db_session.execute(select(LlmCall))).scalar_one()
    assert call.purpose == "client_reply"
    assert call.source_event_id == event.id
