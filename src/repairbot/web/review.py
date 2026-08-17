"""Подтверждение и отклонение фактов менеджером.

Раздел 6 ТЗ: значения ниже порога достоверности не применяются
автоматически и направляются менеджеру. Здесь — обработка его решения.

Журнал неизменяем по содержанию, поэтому решение записывается **новым**
событием со ссылкой на исходное; у исходного меняются только флаги
обработки `applied` и `needs_human`, которые для этого и существуют.

Отклонённые факты не удаляются: они накапливаются для последующей
доработки промптов, и это прямое требование раздела 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents.object_state import rebuild_object_state
from repairbot.db.models import Event, User
from repairbot.domain.events import (
    FACT_CONFIRMED_EVENT,
    FACT_REJECTED_EVENT,
)
from repairbot.observability import get_logger

log = get_logger(__name__)


class ReviewError(Exception):
    """Решение применить нельзя."""


@dataclass(slots=True)
class ReviewResult:
    event_id: int
    decision: str
    applied: bool


async def confirm_fact(
    session: AsyncSession,
    *,
    event_id: int,
    user: User,
    correction: str | None = None,
) -> ReviewResult:
    """Применить факт.

    `correction` — уточнение менеджера, если он поправил формулировку. Оно
    записывается в решение, а не поверх исходного факта: так видно и что
    сказала модель, и что исправил человек.
    """
    event = await _load_pending(session, event_id)

    await _append_decision(
        session,
        event,
        event_type=FACT_CONFIRMED_EVENT,
        user=user,
        payload={
            "source_event_id": event.id,
            "fact_type": event.event_type,
            "confidence": float(event.confidence) if event.confidence is not None else None,
            "correction": correction,
            "reviewed_by": user.login,
        },
    )
    await session.execute(
        sql_update(Event)
        .where(Event.id == event.id)
        .values(applied=True, needs_human=False)
    )
    # Подтверждённый факт должен сразу попасть в состояние объекта —
    # иначе менеджер нажал кнопку, а сводка осталась прежней.
    if event.object_id is not None:
        await rebuild_object_state(session, event.object_id)

    log.info("review.confirmed", event_id=event.id, user=user.login, corrected=bool(correction))
    return ReviewResult(event_id=event.id, decision="confirmed", applied=True)


async def reject_fact(
    session: AsyncSession,
    *,
    event_id: int,
    user: User,
    reason: str | None = None,
) -> ReviewResult:
    """Отклонить факт.

    `applied` остаётся False — факт не попадёт в состояние объекта.
    `needs_human` снимается: решение принято, из очереди факт уходит.
    """
    event = await _load_pending(session, event_id)

    await _append_decision(
        session,
        event,
        event_type=FACT_REJECTED_EVENT,
        user=user,
        payload={
            "source_event_id": event.id,
            "fact_type": event.event_type,
            "confidence": float(event.confidence) if event.confidence is not None else None,
            "reason": reason,
            "reviewed_by": user.login,
            # Отклонённое сохраняем целиком: на этом настраиваются промпты.
            "rejected_payload": event.payload,
        },
    )
    await session.execute(
        sql_update(Event)
        .where(Event.id == event.id)
        .values(applied=False, needs_human=False)
    )
    # Пересобираем и здесь: факт уходит из очереди, и счётчик ожидающих
    # подтверждения должен уменьшиться.
    if event.object_id is not None:
        await rebuild_object_state(session, event.object_id)

    log.info("review.rejected", event_id=event.id, user=user.login, reason=reason)
    return ReviewResult(event_id=event.id, decision="rejected", applied=False)


async def _load_pending(session: AsyncSession, event_id: int) -> Event:
    # populate_existing обязателен: без него объект, уже лежащий в карте
    # идентичности сессии, вернулся бы со старыми флагами, и проверка
    # «уже обработано» пропустила бы второе решение.
    event = (
        await session.execute(
            select(Event)
            .where(Event.id == event_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if event is None:
        raise ReviewError(f"Событие {event_id} не найдено")
    if not event.needs_human:
        # Двойной клик или второй менеджер, открывший ту же очередь.
        raise ReviewError(f"Событие {event_id} уже обработано")
    return event


async def _append_decision(
    session: AsyncSession,
    event: Event,
    *,
    event_type: str,
    user: User,
    payload: dict,
) -> None:
    stmt = (
        pg_insert(Event)
        .values(
            object_id=event.object_id,
            actor_id=event.actor_id,
            channel=event.channel,
            channel_chat_id=event.channel_chat_id,
            channel_message_id=event.channel_message_id,
            source_message_id=event.source_message_id,
            event_type=event_type,
            payload=payload,
            applied=True,
            needs_human=False,
            # Одно решение на факт: повторное нажатие не создаст второй записи.
            dedup_key=f"review:{event.id}",
            occurred_at=datetime.now(tz=UTC),
        )
        .on_conflict_do_nothing(constraint="uq_event_dedup")
        .returning(Event.id)
    )
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        raise ReviewError(f"По событию {event.id} решение уже записано")


async def load_rejected_for_prompt_tuning(
    session: AsyncSession, *, limit: int = 200
) -> list[Event]:
    """Отклонённые факты — материал для доработки промптов (раздел 6 ТЗ)."""
    rows = await session.execute(
        select(Event)
        .where(Event.event_type == FACT_REJECTED_EVENT)
        .order_by(Event.id.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
