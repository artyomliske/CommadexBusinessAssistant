"""Догрузка истории чата (этап 1 ТЗ).

Применяется при подключении объекта: бот только что добавлен в чат, но
переписка там уже велась. Метод `GET /messages` позволяет забрать её.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import update as sql_update

from repairbot.channels.base import ChannelAdapter
from repairbot.db.models import ChatRecord
from repairbot.db.session import session_scope
from repairbot.ingest.service import IngestService
from repairbot.memory.working import WorkingMemory
from repairbot.observability import get_logger

log = get_logger(__name__)


async def backfill_chat(
    adapter: ChannelAdapter,
    channel_chat_id: str,
    *,
    max_messages: int = 1000,
    working_memory: WorkingMemory | None = None,
) -> dict[str, int]:
    """Загрузить историю чата в журнал.

    Повторный запуск безопасен: идемпотентность приёма отбросит уже
    принятые сообщения.
    """
    events = await adapter.fetch_history(channel_chat_id, limit=max_messages)

    stored = 0
    duplicates = 0
    async with session_scope() as session:
        ingest = IngestService(session, working_memory)
        # Идём от старых к новым, чтобы порядок в журнале совпадал с ходом переписки.
        for event in sorted(events, key=lambda e: e.occurred_at):
            result = await ingest.ingest(event)
            if result.duplicate:
                duplicates += 1
            else:
                stored += 1

        await session.execute(
            sql_update(ChatRecord)
            .where(
                ChatRecord.channel == adapter.channel.value,
                ChatRecord.channel_chat_id == channel_chat_id,
            )
            .values(history_backfilled_at=datetime.now(tz=UTC))
        )

    log.info(
        "history.backfilled",
        chat_id=channel_chat_id,
        stored=stored,
        duplicates=duplicates,
        fetched=len(events),
    )
    return {"fetched": len(events), "stored": stored, "duplicates": duplicates}


async def chats_pending_backfill(channel: str = "max") -> list[str]:
    """Чаты, где бот состоит, но история ещё не загружена."""
    async with session_scope() as session:
        rows = await session.execute(
            select(ChatRecord.channel_chat_id).where(
                ChatRecord.channel == channel,
                ChatRecord.bot_is_member.is_(True),
                ChatRecord.history_backfilled_at.is_(None),
            )
        )
        return list(rows.scalars().all())
