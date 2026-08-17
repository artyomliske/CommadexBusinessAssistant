"""Запись фактов в журнал.

Общая для разбора текста и для распознавания документов. Держать её в
одном месте важно из-за одного правила: факт ниже порога достоверности не
применяется автоматически, а уходит человеку. Продублируй эту строчку в
двух модулях — и однажды они разойдутся, причём разойдутся молча, а
разница будет в том, попадёт ли невыверенная сумма в бюджет объекта.

Журнал неизменяем: исправления приходят новыми событиями, а не правкой
прежних. Поэтому у каждой записи есть ключ идемпотентности, и повторный
запуск задачи ничего не удваивает.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import Event
from repairbot.domain.events import FACT_EVENT_PREFIX, NEEDS_HUMAN_EVENT
from repairbot.domain.facts import Fact


async def append_fact(
    session: AsyncSession,
    *,
    source: Event,
    fact: Fact,
    payload: dict,
    dedup_key: str,
) -> bool:
    """Добавить факт в журнал. Возвращает False, если он уже там был."""
    stmt = (
        pg_insert(Event)
        .values(
            object_id=source.object_id,
            actor_id=source.actor_id,
            channel=source.channel,
            channel_chat_id=source.channel_chat_id,
            channel_message_id=source.channel_message_id,
            source_message_id=source.source_message_id,
            event_type=f"{FACT_EVENT_PREFIX}{fact.type.value}",
            payload=payload,
            confidence=Decimal(f"{fact.confidence:.2f}"),
            # Ниже порога — не применяем, отправляем человеку.
            applied=not fact.below_threshold,
            needs_human=fact.below_threshold,
            dedup_key=dedup_key,
            occurred_at=source.occurred_at,
        )
        .on_conflict_do_nothing(constraint="uq_event_dedup")
        .returning(Event.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def append_needs_human(
    session: AsyncSession, *, source: Event, reason: str, dedup_key: str
) -> bool:
    """Отметить, что событию нужен человек."""
    stmt = (
        pg_insert(Event)
        .values(
            object_id=source.object_id,
            actor_id=source.actor_id,
            channel=source.channel,
            channel_chat_id=source.channel_chat_id,
            channel_message_id=source.channel_message_id,
            source_message_id=source.source_message_id,
            event_type=NEEDS_HUMAN_EVENT,
            payload={"reason": reason},
            applied=False,
            needs_human=True,
            dedup_key=dedup_key,
            occurred_at=source.occurred_at,
        )
        .on_conflict_do_nothing(constraint="uq_event_dedup")
        .returning(Event.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
