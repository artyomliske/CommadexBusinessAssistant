"""Кто написал сообщение — в ленте событий и в очереди разбора.

Цепочка неочевидная, и однажды её уже прошли неверно: у события есть
поле `actor_id`, оно выглядит именно тем, что нужно, но указывает на
карточку человека и ингестом не заполняется. Автор берётся только через
сообщение.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from repairbot.db.models import ChannelIdentity, Event, Message, Person, RepairObject
from repairbot.web import objects, queries

WHEN = datetime(2026, 8, 14, 9, 5, tzinfo=UTC)


async def _message(
    session,
    *,
    display_name: str | None = "Иван П.",
    username: str | None = "ivan_p",
    person_name: str | None = None,
    linked: bool = True,
    text: str = "Завезли плитку",
) -> Message:
    identity_id = None
    if display_name or username or person_name:
        person_id = None
        if person_name:
            person = Person(display_name=person_name, role="staff")
            session.add(person)
            await session.flush()
            person_id = person.id

        identity = ChannelIdentity(
            channel="max",
            channel_user_id="777",
            display_name=display_name,
            username=username,
            person_id=person_id,
        )
        session.add(identity)
        await session.flush()
        identity_id = identity.id

    obj = None
    if linked:
        obj = RepairObject(code="obj_1", address="Ленина 5")
        session.add(obj)
        await session.flush()

    message = Message(
        channel="max",
        channel_chat_id="-100500",
        channel_message_id="mid.1",
        object_id=obj.id if obj else None,
        author_identity_id=identity_id,
        text=text,
        sent_at=WHEN,
    )
    session.add(message)
    await session.flush()

    session.add(
        Event(
            object_id=obj.id if obj else None,
            channel="max",
            channel_chat_id="-100500",
            source_message_id=message.id,
            event_type="message_created",
            payload={},
            dedup_key=f"max:-100500:mid.1:{message.id}",
            occurred_at=WHEN,
        )
    )
    await session.flush()
    return message


# --- лента событий ---


async def test_feed_shows_who_wrote(db_session):
    await _message(db_session)

    item = (await queries.load_feed(db_session))[0]

    assert item.author == "Иван П."


async def test_the_card_name_wins_over_the_messenger_name(db_session):
    """Имя в карточке назначил человек — оно точнее, чем ник в мессенджере."""
    await _message(db_session, display_name="ваня 🔨", person_name="Иван Петров")

    item = (await queries.load_feed(db_session))[0]

    assert item.author == "Иван Петров"


async def test_username_is_used_when_there_is_no_name(db_session):
    """В MAX имя приходит не всегда — тогда хоть @username."""
    await _message(db_session, display_name=None, username="ivan_p")

    item = (await queries.load_feed(db_session))[0]

    assert item.author == "ivan_p"


async def test_event_without_an_author_is_still_listed(db_session):
    """Системные события — «бота добавили в чат» — автора не имеют.

    Внутреннее соединение выкинуло бы их из ленты целиком.
    """
    await _message(db_session, display_name=None, username=None, person_name=None)

    items = await queries.load_feed(db_session)

    assert len(items) == 1
    assert items[0].author is None


async def test_a_nameless_identity_does_not_hide_the_event(db_session):
    """Учётная запись есть, а имени у неё нет: тоже обычное дело."""
    await _message(db_session, display_name=None, username=None, person_name=None)
    message = (await db_session.execute(select(Message))).scalar_one()
    identity = ChannelIdentity(channel="max", channel_user_id="999")
    db_session.add(identity)
    await db_session.flush()
    message.author_identity_id = identity.id
    await db_session.flush()

    items = await queries.load_feed(db_session)

    assert len(items) == 1
    assert items[0].author is None


# --- очередь разбора ---


async def test_unassigned_queue_shows_who_wrote(db_session):
    """Без автора неясно, у кого спрашивать, к какому объекту это.

    Именно за этим на страницу и приходят.
    """
    await _message(db_session, linked=False, person_name="Иван Петров")

    card = (await objects.load_unassigned(db_session))[0]

    assert card.author == "Иван Петров"


async def test_unassigned_message_without_an_author_is_listed(db_session):
    await _message(db_session, linked=False, display_name=None, username=None)

    cards = await objects.load_unassigned(db_session)

    assert len(cards) == 1
    assert cards[0].author is None
