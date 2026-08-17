"""Внутренний помощник: допуск, подтверждения, действия.

Помощник видит деньги и всю картину компании, поэтому главное здесь не
качество ответа, а то, кому он вообще отвечает. Ошибка допуска отдаёт
сводку по компании постороннему и выглядит при этом как исправная работа.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from repairbot.agents import assistant as a
from repairbot.agents import payment_calendar as pc
from repairbot.db.models import ChannelIdentity
from repairbot.llm.base import StructuredResponse
from repairbot.web import objects as objects_service


class FakeRouter:
    """Возвращает заранее заданный ответ модели."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list = []

    async def complete_structured(self, request):
        self.requests.append(request)
        return StructuredResponse(
            payload=self.payload,
            provider="fake",
            model="fake",
            input_tokens=0,
            output_tokens=0,
        )


def _answer(text: str, action: dict | None = None) -> dict:
    payload: dict = {"answer": text}
    if action is not None:
        payload["action"] = action
    return payload


# --- допуск ---


def test_empty_list_means_the_assistant_is_off():
    """Верное значение по умолчанию: молчать."""
    assert a.allowed_user_ids("") == frozenset()


def test_list_is_parsed_and_trimmed():
    assert a.allowed_user_ids(" 271217034 , 282498923 ") == {"271217034", "282498923"}


async def test_person_from_the_list_is_allowed(db_session):
    identity = ChannelIdentity(channel="max", channel_user_id="271217034")
    db_session.add(identity)
    await db_session.flush()

    assert await a.is_allowed(db_session, identity.id, frozenset({"271217034"}))


async def test_stranger_is_refused(db_session):
    """Заказчик, подрядчик, случайный человек из каталога — все сюда."""
    identity = ChannelIdentity(channel="max", channel_user_id="999")
    db_session.add(identity)
    await db_session.flush()

    assert not await a.is_allowed(db_session, identity.id, frozenset({"271217034"}))


async def test_message_without_an_author_is_refused(db_session):
    assert not await a.is_allowed(db_session, None, frozenset({"271217034"}))


async def test_empty_list_refuses_everyone(db_session):
    identity = ChannelIdentity(channel="max", channel_user_id="271217034")
    db_session.add(identity)
    await db_session.flush()

    assert not await a.is_allowed(db_session, identity.id, frozenset())


# --- согласие ---


@pytest.mark.parametrize("text", ["да", "Да!", "ок", "давай", "подтверждаю", "+"])
def test_yes_is_recognized(text):
    assert a.reads_as_yes(text)


@pytest.mark.parametrize("text", ["нет", "отмена", "стоп", "-"])
def test_no_is_recognized(text):
    assert a.reads_as_no(text)


def test_a_question_is_neither():
    """«А что по Ленина 5?» — не согласие и не отказ, а новый вопрос."""
    assert not a.reads_as_yes("а что по Ленина 5?")
    assert not a.reads_as_no("а что по Ленина 5?")


def test_yes_inside_a_sentence_does_not_count():
    """«Да я не про это» начинается с «да», но согласием не является...

    ...однако различить это списком слов нельзя, и мы намеренно
    принимаем только первое слово. Здесь проверяется обратное: слово
    «да» в середине фразы подтверждением не считается.
    """
    assert not a.reads_as_yes("вообще да, но потом")


def test_a_stale_proposal_is_not_confirmable():
    """«Да», сказанное назавтра, не должно заводить забытый объект."""
    old = {"kind": "create_object", "at": (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()}

    assert not a.is_fresh(old)


def test_a_fresh_proposal_is_confirmable():
    fresh = {"kind": "create_object", "at": datetime.now(tz=UTC).isoformat()}

    assert a.is_fresh(fresh)


def test_a_broken_timestamp_is_not_confirmable():
    assert not a.is_fresh({"kind": "create_object", "at": "позавчера"})


def test_nothing_pending_is_not_confirmable():
    assert not a.is_fresh(None)
    assert not a.is_fresh({})


# --- ответы ---


async def test_a_question_gets_an_answer_without_action(db_session):
    router = FakeRouter(_answer("Объектов пока нет."))

    result = await a.Assistant(router).handle(db_session, "что у нас по объектам?")

    assert result.text == "Объектов пока нет."
    assert result.pending is None
    assert result.action_taken is None


async def test_a_request_to_act_is_proposed_not_done(db_session):
    """Действие называется вслух и ждёт согласия."""
    router = FakeRouter(
        _answer("Завести объект «Ленина 5»?", {"kind": "create_object", "address": "Ленина 5"})
    )

    result = await a.Assistant(router).handle(db_session, "заведи объект Ленина 5")

    assert result.pending["kind"] == "create_object"
    assert result.action_taken is None
    assert await objects_service.load_objects(db_session) == []


async def test_confirmation_performs_the_action(db_session):
    router = FakeRouter(_answer("не должно понадобиться"))
    pending = {
        "kind": "create_object",
        "address": "Ленина 5",
        "at": datetime.now(tz=UTC).isoformat(),
    }

    result = await a.Assistant(router).handle(db_session, "да", pending=pending)

    assert result.action_taken == "create_object"
    assert result.pending is None
    assert router.requests == [], "подтверждение не должно стоить вызова модели"
    objects = await objects_service.load_objects(db_session)
    assert [o.address for o in objects] == ["Ленина 5"]


async def test_refusal_cancels_the_action(db_session):
    router = FakeRouter(_answer("не должно понадобиться"))
    pending = {
        "kind": "create_object",
        "address": "Ленина 5",
        "at": datetime.now(tz=UTC).isoformat(),
    }

    result = await a.Assistant(router).handle(db_session, "нет", pending=pending)

    assert result.action_taken is None
    assert result.pending is None
    assert await objects_service.load_objects(db_session) == []


async def test_a_new_question_drops_the_proposal(db_session):
    """Висящее подтверждение, про которое забыли, однажды сработает не вовремя."""
    router = FakeRouter(_answer("По Мира 12 ничего нет."))
    pending = {
        "kind": "create_object",
        "address": "Ленина 5",
        "at": datetime.now(tz=UTC).isoformat(),
    }

    result = await a.Assistant(router).handle(db_session, "а что по Мира 12?", pending=pending)

    assert result.pending is None
    assert await objects_service.load_objects(db_session) == []


async def test_a_failed_action_is_reported_not_hidden(db_session):
    await objects_service.create_object(db_session, address="Ленина 5")
    router = FakeRouter(_answer("не понадобится"))
    pending = {
        "kind": "add_alias",
        "address": "Ленина 5",
        "alias": "5",
        "at": datetime.now(tz=UTC).isoformat(),
    }

    result = await a.Assistant(router).handle(db_session, "да", pending=pending)

    assert "Не получилось" in result.text
    assert result.action_taken is None


async def test_payment_is_marked_paid(db_session):
    await pc.create(
        db_session,
        title="Хостинг",
        next_due_on=datetime.now(tz=UTC).date(),
        period="monthly",
        amount=Decimal("990"),
    )
    router = FakeRouter(_answer("не понадобится"))
    pending = {
        "kind": "mark_paid",
        "payment_title": "хостинг",
        "at": datetime.now(tz=UTC).isoformat(),
    }

    result = await a.Assistant(router).handle(db_session, "да", pending=pending)

    assert result.action_taken == "mark_paid"
    assert "Хостинг" in result.text


async def test_an_ambiguous_payment_is_not_guessed(db_session):
    """Отметить оплаченным не тот платёж — молча потерять деньги."""
    today = datetime.now(tz=UTC).date()
    await pc.create(db_session, title="Связь МТС", next_due_on=today, period="monthly")
    await pc.create(db_session, title="Связь Билайн", next_due_on=today, period="monthly")
    router = FakeRouter(_answer("не понадобится"))
    pending = {
        "kind": "mark_paid",
        "payment_title": "связь",
        "at": datetime.now(tz=UTC).isoformat(),
    }

    result = await a.Assistant(router).handle(db_session, "да", pending=pending)

    assert "несколько" in result.text
    assert result.action_taken is None


# --- контекст ---


async def test_context_mentions_the_object_asked_about(db_session):
    await objects_service.create_object(db_session, address="Ленина 5")

    context = await a.build_context(db_session, "что по Ленина 5?")

    assert "Подробно про Ленина 5" in context.render()


async def test_context_stays_short_when_no_object_is_mentioned(db_session):
    await objects_service.create_object(db_session, address="Ленина 5")

    context = await a.build_context(db_session, "как дела?")

    assert "Подробно про" not in context.render()
    assert "Ленина 5" in context.render(), "общий список объектов нужен всегда"


async def test_context_reports_what_needs_attention(db_session):
    context = await a.build_context(db_session, "что горит?")

    assert "Требует внимания" in context.render()


async def test_context_survives_an_empty_system(db_session):
    """Первый запуск: ни объектов, ни платежей, ни фактов."""
    context = await a.build_context(db_session, "что происходит?")

    assert "Объектов в системе нет" in context.render()


# --- переписка в контексте ---


async def _said(session, text: str, *, who: str = "Дима Кудрявцев", chat: str = "Заказ денег"):
    from datetime import datetime

    from repairbot.db.models import ChannelIdentity, ChatRecord, Message

    identity = (
        await session.execute(
            select(ChannelIdentity).where(ChannelIdentity.channel_user_id == who)
        )
    ).scalar_one_or_none()
    if identity is None:
        identity = ChannelIdentity(channel="max", channel_user_id=who, display_name=who)
        session.add(identity)
    record = (
        await session.execute(select(ChatRecord).where(ChatRecord.title == chat))
    ).scalar_one_or_none()
    if record is None:
        record = ChatRecord(
            channel="max", channel_chat_id=f"-{abs(hash(chat)) % 10**9}", title=chat
        )
        session.add(record)
    await session.flush()

    session.add(
        Message(
            channel="max",
            channel_chat_id=record.channel_chat_id,
            channel_message_id=f"mid.{abs(hash(text)) % 10**9}",
            chat_id=record.id,
            author_identity_id=identity.id,
            text=text,
            sent_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()


async def test_raw_messages_reach_the_context(db_session):
    """Разбор отстаёт от переписки, а вопрос задают про сегодня.

    Пока объект не заведён, «23к взял оплата материала» не превращается
    ни в какой факт — и без сырой переписки помощник отвечает «не вижу»
    на то, что у системы есть.
    """
    await _said(db_session, "23к взял оплата материала Ростовка займ")

    context = await a.build_context(db_session, "на что сегодня ушли 23 тысячи")

    rendered = context.render()
    assert "23к взял оплата материала" in rendered
    assert "Дима Кудрявцев" in rendered
    assert "Заказ денег" in rendered


async def test_context_says_who_is_asking(db_session):
    """«Ты знаешь, кто я» — «нет» выглядит как незнание системы."""
    context = await a.build_context(db_session, "ты знаешь кто я", asked_by="Артём Лиске")

    assert "Спрашивает: Артём Лиске" in context.render()


async def test_the_oldest_messages_are_dropped_first(db_session):
    """Свежее важнее: предел есть, и упираться в него должно старое."""
    await _said(db_session, "самое старое")
    await _said(db_session, "самое свежее")

    context = await a.build_context(db_session, "что нового", messages_limit=1)

    rendered = context.render()
    assert "самое свежее" in rendered
    assert "самое старое" not in rendered


async def test_our_own_replies_are_not_quoted_back(db_session):
    """Иначе помощник цитирует сам себя как источник сведений."""
    from datetime import datetime

    from repairbot.db.models import Message

    db_session.add(
        Message(
            channel="max",
            channel_chat_id="-1",
            channel_message_id="out.1",
            text="Отметил «Хостинг» оплаченным.",
            is_outbound=True,
            sent_at=datetime.now(tz=UTC),
        )
    )
    await db_session.flush()

    assert "Отметил" not in (await a.build_context(db_session, "что нового")).render()


async def test_empty_history_is_stated_plainly(db_session):
    assert "Сообщений в системе нет" in (await a.build_context(db_session, "что нового")).render()


# --- кто спрашивает ---


async def _identity(session, *, name: str, role: str | None = None):
    from repairbot.db.models import ChannelIdentity, Person

    person_id = None
    if role is not None:
        person = Person(display_name=name, role=role)
        session.add(person)
        await session.flush()
        person_id = person.id
    identity = ChannelIdentity(
        channel="max", channel_user_id=name, display_name=name, person_id=person_id
    )
    session.add(identity)
    await session.flush()
    return identity


async def test_assigned_role_is_reported(db_session):
    identity = await _identity(db_session, name="Дима Кудрявцев", role="staff")

    assert await a.author_name(db_session, identity.id) == "Дима Кудрявцев (сотрудник)"


async def test_unassigned_role_is_not_invented(db_session):
    """«Владелец» вместо «прораба» — такая же выдумка, как выдуманная сумма."""
    identity = await _identity(db_session, name="Артём Лиске", role="unknown")

    assert await a.author_name(db_session, identity.id) == "Артём Лиске"


async def test_person_without_a_card_is_named_by_the_messenger(db_session):
    identity = await _identity(db_session, name="Влад")

    assert await a.author_name(db_session, identity.id) == "Влад"


async def test_unknown_author_is_nobody(db_session):
    assert await a.author_name(db_session, None) is None
    assert await a.author_name(db_session, 99999) is None


async def test_knowledge_reaches_the_context(db_session):
    """Записанное человеком объяснение — то, чего в переписке нет.

    В чатах не пишут, что «касса» — наличные в офисе: все и так знают.
    Без этой записи модель читает денежные сообщения вслепую.
    """
    from repairbot.web import knowledge

    await knowledge.create_note(db_session, title="Касса", body="Наличные в офисе.")

    context = await a.build_context(db_session, "что там с кассой")

    assert "Касса: Наличные в офисе." in context.render()
