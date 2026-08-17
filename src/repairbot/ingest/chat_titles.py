"""Названия чатов.

Платформа присылает название чата только в двух случаях: когда бота
добавили и когда название сменили. В обычном сообщении его нет — там
лежит один идентификатор. А `GET /chats` в MAX отключён с июня 2026,
и разом весь список не забрать.

Отсюда неприятное следствие: чат, впервые замеченный по сообщению, в
реестре остаётся безымянным навсегда. В панели это выглядит как «—» в
колонке «Чат», и разобрать, откуда пришло сообщение, нельзя.

Лечится точечным запросом по одному чату: он разрешён. Берём только те
чаты, у которых названия ещё нет, — тогда за проход уходит несколько
запросов при подключении и ноль потом.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import ChatRecord
from repairbot.observability import get_logger

log = get_logger(__name__)


class ChatInfoClient(Protocol):
    async def get_chat(self, chat_id: str) -> dict[str, Any]: ...


async def fill_missing_titles(
    session: AsyncSession, client: ChatInfoClient, *, channel: str = "max", limit: int = 25
) -> int:
    """Дозапросить названия у чатов, где их нет. Возвращает число заполненных.

    Отказ по одному чату не должен останавливать остальные: бота могли
    выгнать, и такой чат просто останется без названия.
    """
    rows = await session.execute(
        select(ChatRecord.id, ChatRecord.channel_chat_id)
        .where(
            ChatRecord.channel == channel,
            or_(ChatRecord.title.is_(None), ChatRecord.title == ""),
        )
        .order_by(ChatRecord.id)
        .limit(limit)
    )

    filled = 0
    for chat_id, channel_chat_id in rows.all():
        try:
            info = await client.get_chat(channel_chat_id)
        except Exception as exc:
            log.info("chats.title_unavailable", chat_id=channel_chat_id, error=str(exc))
            continue

        title = (info.get("title") or "").strip()
        if not title:
            # У личного диалога названия нет вовсе — подставляем имя
            # собеседника, иначе в панели останется прочерк.
            title = _dialog_title(info)
        if not title:
            continue

        await session.execute(
            sql_update(ChatRecord).where(ChatRecord.id == chat_id).values(title=title)
        )
        filled += 1
        log.info("chats.title_filled", chat_id=channel_chat_id, title=title)

    return filled


def _dialog_title(info: dict[str, Any]) -> str:
    """Имя собеседника для личного диалога.

    Платформа кладёт его в разные места в зависимости от версии ответа,
    поэтому перебираем известные.
    """
    for key in ("dialog_with_user", "owner", "user"):
        person = info.get(key)
        if isinstance(person, dict):
            name = (person.get("name") or person.get("first_name") or "").strip()
            if name:
                return name
    return ""
