"""Названия чатов: платформа их в сообщениях не присылает.

Чат, впервые замеченный по сообщению, остаётся в реестре безымянным —
в панели это прочерк в колонке «Чат», и понять, откуда пришло сообщение,
нельзя. `GET /chats` в MAX отключён, поэтому названия дозапрашиваются
поштучно и только у тех, у кого их нет.
"""

from __future__ import annotations

from sqlalchemy import select

from repairbot.db.models import ChatRecord
from repairbot.ingest.chat_titles import fill_missing_titles


class FakeClient:
    def __init__(self, chats: dict, fail: set[str] | None = None) -> None:
        self.chats = chats
        self.fail = fail or set()
        self.asked: list[str] = []

    async def get_chat(self, chat_id: str) -> dict:
        self.asked.append(chat_id)
        if chat_id in self.fail:
            raise RuntimeError("403 нет доступа")
        return self.chats.get(chat_id, {})


async def _chat(session, channel_chat_id: str, title: str | None = None) -> ChatRecord:
    chat = ChatRecord(channel="max", channel_chat_id=channel_chat_id, title=title)
    session.add(chat)
    await session.flush()
    return chat


async def test_missing_title_is_fetched(db_session):
    chat = await _chat(db_session, "-100500")
    client = FakeClient({"-100500": {"title": "Тех.группа"}})

    filled = await fill_missing_titles(db_session, client)

    assert filled == 1
    await db_session.refresh(chat)
    assert chat.title == "Тех.группа"


async def test_known_title_is_not_requested_again(db_session):
    """После подключения задача не должна делать ни одного запроса."""
    await _chat(db_session, "-100500", title="Тех.группа")
    client = FakeClient({})

    filled = await fill_missing_titles(db_session, client)

    assert filled == 0
    assert client.asked == []


async def test_empty_title_counts_as_missing(db_session):
    """Пустая строка — то же самое, что и её отсутствие."""
    await _chat(db_session, "-100500", title="")
    client = FakeClient({"-100500": {"title": "Заказ материала"}})

    assert await fill_missing_titles(db_session, client) == 1


async def test_refusal_on_one_chat_does_not_stop_the_rest(db_session):
    """Бота могли выгнать из чата — остальные это не должно задевать."""
    await _chat(db_session, "-1")
    good = await _chat(db_session, "-2")
    client = FakeClient({"-2": {"title": "Инфо Офис"}}, fail={"-1"})

    filled = await fill_missing_titles(db_session, client)

    assert filled == 1
    await db_session.refresh(good)
    assert good.title == "Инфо Офис"


async def test_dialog_gets_the_name_of_the_person(db_session):
    """У личного диалога названия нет — иначе в панели остался бы прочерк."""
    chat = await _chat(db_session, "271217034")
    client = FakeClient({"271217034": {"dialog_with_user": {"name": "Артём Лиске"}}})

    await fill_missing_titles(db_session, client)

    await db_session.refresh(chat)
    assert chat.title == "Артём Лиске"


async def test_chat_without_any_name_is_left_alone(db_session):
    """Записать пустое имя — то же самое, что не записать, но с запросом."""
    chat = await _chat(db_session, "-100500")
    client = FakeClient({"-100500": {}})

    filled = await fill_missing_titles(db_session, client)

    assert filled == 0
    await db_session.refresh(chat)
    assert chat.title is None


async def test_other_channels_are_not_touched(db_session):
    """У Telegram свой клиент, и запрашивать его чаты через MAX нельзя."""
    db_session.add(ChatRecord(channel="telegram", channel_chat_id="-777"))
    await db_session.flush()
    client = FakeClient({})

    await fill_missing_titles(db_session, client)

    assert client.asked == []


async def test_batch_is_limited(db_session):
    """Проход не должен упираться в ограничение платформы по запросам."""
    for i in range(5):
        await _chat(db_session, f"-{i}")
    client = FakeClient({})

    await fill_missing_titles(db_session, client, limit=2)

    assert len(client.asked) == 2


async def test_titles_are_stored_for_the_right_chats(db_session):
    await _chat(db_session, "-1")
    await _chat(db_session, "-2")
    client = FakeClient({"-1": {"title": "Первый"}, "-2": {"title": "Второй"}})

    await fill_missing_titles(db_session, client)

    rows = dict(
        (
            await db_session.execute(
                select(ChatRecord.channel_chat_id, ChatRecord.title)
            )
        ).all()
    )
    assert rows == {"-1": "Первый", "-2": "Второй"}
