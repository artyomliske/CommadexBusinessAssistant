"""Приём событий: идемпотентность, реестр чатов, привязка к объекту.

Требуют Postgres. Без доступной базы пропускаются (см. conftest).
"""

from __future__ import annotations

from sqlalchemy import func, select

from repairbot.channels.max import normalizer
from repairbot.db.models import (
    AttachmentRecord,
    ChannelIdentity,
    ChatRecord,
    Event,
    Message,
    RepairObject,
)
from repairbot.ingest.service import IngestService
from tests.fixtures import max_updates as fx


async def test_message_creates_chat_message_and_event(db_session):
    (event,) = normalizer.normalize(fx.message_created())

    result = await IngestService(db_session).ingest(event)
    await db_session.commit()

    assert result.duplicate is False
    assert result.event_id is not None

    chat = (await db_session.execute(select(ChatRecord))).scalar_one()
    assert chat.channel_chat_id == "-100500"
    assert chat.bot_is_member is True

    message = (await db_session.execute(select(Message))).scalar_one()
    assert message.text == "Штукатурка на кухне закончена"
    assert message.chat_id == chat.id

    journal = (await db_session.execute(select(Event))).scalar_one()
    assert journal.event_type == "message_created"
    assert journal.source_message_id == message.id
    assert journal.applied is False


async def test_repeated_delivery_is_idempotent(db_session):
    payload = fx.message_created()
    ingest = IngestService(db_session)

    first = await ingest.ingest(normalizer.normalize(payload)[0])
    await db_session.commit()
    second = await ingest.ingest(normalizer.normalize(payload)[0])
    await db_session.commit()

    assert first.duplicate is False
    assert second.duplicate is True

    assert (await db_session.execute(select(func.count(Event.id)))).scalar_one() == 1
    assert (await db_session.execute(select(func.count(Message.id)))).scalar_one() == 1


async def test_attachments_stored_once_on_edit(db_session):
    ingest = IngestService(db_session)

    await ingest.ingest(normalizer.normalize(fx.with_receipt())[0])
    await db_session.commit()

    edited = fx.with_receipt()
    edited["update_type"] = "message_edited"
    edited["message"]["body"]["text"] = "Чек за грунтовку (исправлено)"
    await ingest.ingest(normalizer.normalize(edited)[0])
    await db_session.commit()

    assert (await db_session.execute(select(func.count(AttachmentRecord.id)))).scalar_one() == 2
    message = (await db_session.execute(select(Message))).scalar_one()
    assert message.text == "Чек за грунтовку (исправлено)"
    # Событий два: создание и редактирование — журнал фиксирует оба.
    assert (await db_session.execute(select(func.count(Event.id)))).scalar_one() == 2


async def test_events_inherit_object_from_chat(db_session):
    obj = RepairObject(code="obj_17", address="Ленина 5, кв. 12")
    db_session.add(obj)
    await db_session.flush()

    ingest = IngestService(db_session)
    await ingest.ingest(normalizer.normalize(fx.bot_added())[0])
    await db_session.commit()

    chat = (await db_session.execute(select(ChatRecord))).scalar_one()
    chat.object_id = obj.id
    await db_session.commit()

    result = await ingest.ingest(normalizer.normalize(fx.message_created())[0])
    await db_session.commit()

    assert result.object_id == obj.id
    message = (await db_session.execute(select(Message))).scalar_one()
    assert message.object_id == obj.id


async def test_bot_removed_clears_membership(db_session):
    ingest = IngestService(db_session)
    await ingest.ingest(normalizer.normalize(fx.bot_added())[0])
    await db_session.commit()

    removed = fx.bot_added()
    removed["update_type"] = "bot_removed"
    await ingest.ingest(normalizer.normalize(removed)[0])
    await db_session.commit()

    chat = (await db_session.execute(select(ChatRecord))).scalar_one()
    assert chat.bot_is_member is False


async def test_chat_title_not_overwritten_by_message_event(db_session):
    ingest = IngestService(db_session)
    await ingest.ingest(normalizer.normalize(fx.chat_title_changed())[0])
    await db_session.commit()

    await ingest.ingest(normalizer.normalize(fx.message_created())[0])
    await db_session.commit()

    chat = (await db_session.execute(select(ChatRecord))).scalar_one()
    assert chat.title == "Объект Ленина 5 — бригада"


async def test_identity_reused_across_messages(db_session):
    ingest = IngestService(db_session)

    await ingest.ingest(normalizer.normalize(fx.message_created(mid="mid.1"))[0])
    await ingest.ingest(normalizer.normalize(fx.message_created(mid="mid.2"))[0])
    await db_session.commit()

    identities = (await db_session.execute(select(ChannelIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].channel_user_id == "777"
    assert identities[0].username == "ivan_p"


async def test_unknown_event_still_lands_in_journal(db_session):
    (event,) = normalizer.normalize(fx.unknown_update())

    result = await IngestService(db_session).ingest(event)
    await db_session.commit()

    assert result.stored
    journal = (await db_session.execute(select(Event))).scalar_one()
    assert journal.event_type == "unknown"
    assert journal.payload["emoji"] == "👍"
