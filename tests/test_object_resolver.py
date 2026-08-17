"""Определение объекта из текста сообщения. Требуют Postgres.

Чаты у заказчика функциональные: в одном чате идёт работа сразу по всем
адресам, и объект приходится узнавать из самого сообщения. Ошибка тут
несимметрична — неузнанное попадёт человеку, а приписанное чужому
объекту разойдётся по сводкам молча.
"""

from __future__ import annotations

from sqlalchemy import select

from repairbot.channels.max import normalizer
from repairbot.db.models import ChatRecord, Event, Message, ObjectAlias, RepairObject
from repairbot.ingest.service import IngestService
from tests.fixtures import max_updates as fx


async def _object(session, code: str, address: str, *, aliases: list[str] = ()) -> RepairObject:
    from repairbot.domain.addresses import normalize

    obj = RepairObject(code=code, address=address)
    session.add(obj)
    await session.flush()
    for alias in (address, *aliases):
        session.add(
            ObjectAlias(
                object_id=obj.id, alias=alias, normalized=" ".join(normalize(alias))
            )
        )
    await session.flush()
    return obj


async def _ingest(session, payload):
    return await IngestService(session).ingest(normalizer.normalize(payload)[0])


# --- адрес в тексте ---


async def test_address_in_the_text_decides(db_session):
    obj = await _object(db_session, "obj_1", "Ленина 5")

    result = await _ingest(db_session, fx.message_created(text="на Ленина 5 закончили стяжку"))
    await db_session.commit()

    assert result.object_id == obj.id
    assert result.object_source == "text"


async def test_journal_entry_gets_the_same_object(db_session):
    """Состояние объекта сворачивается из журнала, а не из сообщений."""
    obj = await _object(db_session, "obj_1", "Ленина 5")

    await _ingest(db_session, fx.message_created(text="Ленина 5: сдали стяжку"))
    await db_session.commit()

    journal = (await db_session.execute(select(Event))).scalar_one()
    assert journal.object_id == obj.id


async def test_message_without_an_address_stays_unassigned(db_session):
    await _object(db_session, "obj_1", "Ленина 5")

    result = await _ingest(db_session, fx.message_created(text="привезут завтра"))
    await db_session.commit()

    assert result.object_id is None
    assert result.object_source is None


async def test_two_addresses_at_once_are_not_guessed(db_session):
    """Перекинуть с одного объекта на другой — про оба сразу. Решает человек."""
    await _object(db_session, "obj_1", "Ленина 5")
    await _object(db_session, "obj_2", "Мира 12")

    result = await _ingest(
        db_session, fx.message_created(text="с Ленина 5 перекинуть плитку на Мира 12")
    )
    await db_session.commit()

    assert result.object_id is None


async def test_unknown_address_is_not_matched_to_the_nearest(db_session):
    await _object(db_session, "obj_1", "Ленина 5")

    result = await _ingest(db_session, fx.message_created(text="на Гагарина 7 нужен плиточник"))
    await db_session.commit()

    assert result.object_id is None


# --- привязанный чат ---


async def test_linked_chat_still_wins_over_the_text(db_session):
    """Чат, привязанный руками, — решение человека.

    Упоминание соседнего адреса в разговоре не должно переносить
    сообщение на другой объект.
    """
    here = await _object(db_session, "obj_1", "Ленина 5")
    await _object(db_session, "obj_2", "Мира 12")

    await _ingest(db_session, fx.bot_added())
    await db_session.commit()
    chat = (await db_session.execute(select(ChatRecord))).scalar_one()
    chat.object_id = here.id
    await db_session.commit()

    result = await _ingest(db_session, fx.message_created(text="как на Мира 12 делали"))
    await db_session.commit()

    assert result.object_id == here.id
    assert result.object_source == "chat"


# --- ответ на сообщение ---


async def test_reply_inherits_the_object(db_session):
    obj = await _object(db_session, "obj_1", "Ленина 5")

    await _ingest(db_session, fx.message_created(mid="m1", text="Ленина 5: нужна плитка"))
    await db_session.commit()

    result = await _ingest(
        db_session,
        fx.message_created(mid="m2", text="сколько поддонов?", reply_to="m1", user_id=999),
    )
    await db_session.commit()

    assert result.object_id == obj.id
    assert result.object_source == "reply"


async def test_own_address_beats_the_one_replied_to(db_session):
    """«А на Мира 12?» в ответ — вопрос про другой объект."""
    await _object(db_session, "obj_1", "Ленина 5")
    other = await _object(db_session, "obj_2", "Мира 12")

    await _ingest(db_session, fx.message_created(mid="m1", text="Ленина 5: нужна плитка"))
    await db_session.commit()

    result = await _ingest(
        db_session, fx.message_created(mid="m2", text="а на Мира 12 сколько?", reply_to="m1")
    )
    await db_session.commit()

    assert result.object_id == other.id


# --- недавний разговор ---


async def test_follow_up_from_the_same_person_inherits_the_object(db_session):
    """«Ленина 5» — «сколько плитки?»: адрес звучит один раз."""
    obj = await _object(db_session, "obj_1", "Ленина 5")

    await _ingest(db_session, fx.message_created(mid="m1", text="Ленина 5: нужна плитка"))
    await db_session.commit()

    result = await _ingest(
        db_session, fx.message_created(mid="m2", text="два поддона хватит", minutes_later=2)
    )
    await db_session.commit()

    assert result.object_id == obj.id
    assert result.object_source == "context"


async def test_another_persons_message_does_not_inherit(db_session):
    """В общем чате разговор перебивают, и чужая реплика — не продолжение."""
    await _object(db_session, "obj_1", "Ленина 5")

    await _ingest(db_session, fx.message_created(mid="m1", text="Ленина 5: нужна плитка"))
    await db_session.commit()

    result = await _ingest(
        db_session,
        fx.message_created(mid="m2", text="а мне краску", user_id=999, minutes_later=2),
    )
    await db_session.commit()

    assert result.object_id is None


async def test_the_conversation_goes_stale(db_session):
    """Через час это уже другой разговор, а не продолжение прежнего."""
    await _object(db_session, "obj_1", "Ленина 5")

    await _ingest(db_session, fx.message_created(mid="m1", text="Ленина 5: нужна плитка"))
    await db_session.commit()

    result = await _ingest(
        db_session, fx.message_created(mid="m2", text="два поддона", minutes_later=90)
    )
    await db_session.commit()

    assert result.object_id is None


async def test_a_guess_is_not_inherited_from_a_guess(db_session):
    """Иначе одна догадка тянулась бы через весь день переписки."""
    await _object(db_session, "obj_1", "Ленина 5")

    await _ingest(db_session, fx.message_created(mid="m1", text="Ленина 5: нужна плитка"))
    await _ingest(db_session, fx.message_created(mid="m2", text="два поддона", minutes_later=20))
    await db_session.commit()

    # Третье сообщение опирается только на второе: первое уже вне окна.
    result = await _ingest(
        db_session, fx.message_created(mid="m3", text="и клей", minutes_later=45)
    )
    await db_session.commit()

    assert result.object_id is None


# --- совместимость с прежним поведением ---


async def test_nothing_to_match_against_is_not_an_error(db_session):
    """Пока объектов не завели, приём обязан работать как раньше."""
    result = await _ingest(db_session, fx.message_created(text="на Ленина 5 закончили"))
    await db_session.commit()

    assert result.stored
    assert result.object_id is None
    message = (await db_session.execute(select(Message))).scalar_one()
    assert message.object_id is None
