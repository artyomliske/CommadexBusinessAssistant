"""Справочник людей. Требуют Postgres.

До появления этого модуля карточка человека читалась в трёх местах и не
создавалась ни в одном — из-за чего проверка «автор не заказчик» в
клиентском агенте не срабатывала никогда.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from repairbot.agents.client_agent import should_answer
from repairbot.db.models import ChannelIdentity, ChatRecord, Message, Person, RepairObject
from repairbot.web import people


async def _identity(
    session,
    *,
    channel_user_id: str = "777",
    display_name: str | None = "Иван Петров",
    channel: str = "max",
    is_bot: bool = False,
) -> ChannelIdentity:
    identity = ChannelIdentity(
        channel=channel,
        channel_user_id=channel_user_id,
        display_name=display_name,
        is_bot=is_bot,
    )
    session.add(identity)
    await session.flush()
    return identity


async def _message(
    session, identity: ChannelIdentity, *, kind: str = "group", object_code: str | None = "obj_17"
) -> None:
    object_id = None
    if object_code:
        obj = (
            await session.execute(select(RepairObject).where(RepairObject.code == object_code))
        ).scalar_one_or_none()
        if obj is None:
            obj = RepairObject(code=object_code, address="Ленина 5")
            session.add(obj)
            await session.flush()
        object_id = obj.id

    chat = ChatRecord(
        channel="max",
        channel_chat_id=f"chat-{kind}-{object_code}",
        kind=kind,
        object_id=object_id,
    )
    session.add(chat)
    await session.flush()

    session.add(
        Message(
            channel="max",
            channel_chat_id=chat.channel_chat_id,
            channel_message_id=f"mid.{identity.id}.{kind}",
            chat_id=chat.id,
            object_id=object_id,
            author_identity_id=identity.id,
            text="Штукатурка закончена",
            sent_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()


# --- назначение ---


async def test_assigning_a_role_creates_the_person_card(db_session):
    """Отдельный экран «создать человека, потом связать» удвоил бы работу."""
    identity = await _identity(db_session)

    person = await people.assign(db_session, identity.id, role="staff")

    assert person.display_name == "Иван Петров"
    assert person.role == "staff"
    await db_session.refresh(identity)
    assert identity.person_id == person.id


async def test_pseudonym_belongs_to_the_person_not_the_account(db_session):
    """Один прораб в MAX и в Telegram должен быть для модели одним человеком."""
    identity = await _identity(db_session)

    person = await people.assign(db_session, identity.id, role="staff")

    assert person.pseudonym == f"[NAME_P{person.id}]"


async def test_reassigning_changes_the_role_without_a_second_card(db_session):
    identity = await _identity(db_session)
    first = await people.assign(db_session, identity.id, role="staff")

    second = await people.assign(db_session, identity.id, role="supplier")

    assert second.id == first.id
    assert second.role == "supplier"
    assert len((await db_session.execute(select(Person))).scalars().all()) == 1


async def test_name_can_be_corrected(db_session):
    """В мессенджере человек подписан «Серёга Плитка»."""
    identity = await _identity(db_session, display_name="Серёга Плитка")

    person = await people.assign(
        db_session, identity.id, role="staff", display_name="Сергей Иванов"
    )

    assert person.display_name == "Сергей Иванов"
    await db_session.refresh(identity)
    # Имя в мессенджере не трогаем: по нему человека узнают в переписке.
    assert identity.display_name == "Серёга Плитка"


async def test_unknown_role_is_refused(db_session):
    identity = await _identity(db_session)

    with pytest.raises(people.PeopleError, match="Неизвестная роль"):
        await people.assign(db_session, identity.id, role="начальник")


async def test_nameless_account_needs_a_name(db_session):
    identity = await _identity(db_session, display_name=None)

    with pytest.raises(people.PeopleError, match="имя"):
        await people.assign(db_session, identity.id, role="client")

    person = await people.assign(
        db_session, identity.id, role="client", display_name="Заказчик с Ленина"
    )
    assert person.display_name == "Заказчик с Ленина"


async def test_unlink_keeps_the_card(db_session):
    """На карточку могут ссылаться события журнала, а журнал неизменяем."""
    identity = await _identity(db_session)
    person = await people.assign(db_session, identity.id, role="staff")

    await people.unlink(db_session, identity.id)

    await db_session.refresh(identity)
    assert identity.person_id is None
    assert (await db_session.execute(select(Person))).scalar_one().id == person.id


# --- список и подсказки ---


async def test_unassigned_accounts_come_first(db_session):
    """Страница нужна, чтобы их разобрать, а не любоваться разобранными."""
    linked = await _identity(db_session, channel_user_id="1", display_name="Разобран")
    await people.assign(db_session, linked.id, role="staff")
    await _identity(db_session, channel_user_id="2", display_name="Не разобран")

    cards = await people.load_identities(db_session)

    assert [c.display_name for c in cards] == ["Не разобран", "Разобран"]


async def test_hint_reads_where_the_person_writes(db_session):
    staff = await _identity(db_session, channel_user_id="1")
    await _message(db_session, staff, kind="group")

    client = await _identity(db_session, channel_user_id="2", display_name="Заказчик")
    await _message(db_session, client, kind="dialog", object_code=None)

    cards = {c.channel_user_id: c for c in await people.load_identities(db_session)}

    assert "похоже на сотрудника" in cards["1"].hint
    assert cards["1"].objects == ["obj_17"]
    assert "похоже на заказчика" in cards["2"].hint
    assert cards["2"].dialogs == 1


async def test_message_counts_are_shown(db_session):
    identity = await _identity(db_session)
    await _message(db_session, identity, kind="group")

    card = (await people.load_identities(db_session))[0]

    assert card.messages == 1
    assert card.last_seen_at is not None


async def test_bots_are_not_counted_as_unassigned(db_session):
    """Бот роли не требует, и торчать в списке «разберите» ему незачем."""
    await _identity(db_session, channel_user_id="9", display_name="Бот", is_bot=True)

    assert await people.unassigned_count(db_session) == 0


async def test_unassigned_count_is_the_size_of_the_blind_spot(db_session):
    await _identity(db_session, channel_user_id="1")
    linked = await _identity(db_session, channel_user_id="2")
    await people.assign(db_session, linked.id, role="client")

    assert await people.unassigned_count(db_session) == 1


# --- ради чего всё это ---


async def test_assigned_staff_role_stops_the_autoreply(db_session):
    """Смысл справочника: без роли прораб получил бы автоответ как заказчик."""
    identity = await _identity(db_session)

    before = should_answer(
        text="Когда закончите с кухней?", chat_kind="dialog", author_role=None
    )
    assert before.answer

    person = await people.assign(db_session, identity.id, role="staff")
    after = should_answer(
        text="Когда закончите с кухней?", chat_kind="dialog", author_role=person.role
    )

    assert not after.answer
