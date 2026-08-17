"""Решения менеджера по черновикам исходящих (раздел 6 ТЗ).

«Черновик направляется менеджеру с вариантами „Отправить“, „Изменить“,
„Отклонить“. Отклонённые черновики накапливаются для последующей доработки
промптов».

Решение не подменяет контролёр, а дополняет его: менеджер снимает
удержание, но всё остальное — пауза диалога, аварийная остановка — по-
прежнему проверяется в момент отправки.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import OutboundMessage, User
from repairbot.observability import get_logger
from repairbot.outbound.policy import Verdict

log = get_logger(__name__)


class DraftError(Exception):
    """Решение применить нельзя."""


@dataclass(slots=True)
class DraftDecision:
    outbound_id: int
    decision: str
    ready_to_send: bool


async def load_drafts(session: AsyncSession, *, limit: int = 100) -> list[OutboundMessage]:
    """Черновики, ждущие решения менеджера."""
    rows = await session.execute(
        select(OutboundMessage)
        .where(
            OutboundMessage.verdict == Verdict.HOLD.value,
            OutboundMessage.decision.is_(None),
        )
        .order_by(OutboundMessage.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def load_audit(session: AsyncSession, *, limit: int = 100) -> list[OutboundMessage]:
    """Журнал исходящих: и отправленное, и заблокированное."""
    rows = await session.execute(
        select(OutboundMessage).order_by(OutboundMessage.id.desc()).limit(limit)
    )
    return list(rows.scalars().all())


async def approve(
    session: AsyncSession, outbound_id: int, *, user: User, edited_text: str | None = None
) -> DraftDecision:
    """Отправить черновик, при необходимости с правкой менеджера.

    Исходный текст модели не затирается: он остаётся в `text`, правка
    ложится в `edited_text`. Так видно и что предложила модель, и что
    исправил человек, — иначе доработку промптов не на чем строить.
    """
    message = await _load_pending(session, outbound_id)

    edited = (edited_text or "").strip() or None
    if edited == message.text:
        edited = None

    message.decision = "edited" if edited else "sent"
    message.edited_text = edited
    message.decided_by = user.id
    message.decided_at = datetime.now(tz=UTC)

    # Сбрасываем в транзакцию здесь: к моменту возврата решение должно быть
    # записано. Иначе вызывающий код поставит задачу на отправку раньше,
    # чем изменение доедет до базы.
    await session.flush()

    log.info(
        "draft.approved",
        outbound_id=outbound_id,
        user=user.login,
        edited=bool(edited),
    )
    return DraftDecision(outbound_id, message.decision, ready_to_send=True)


async def reject(
    session: AsyncSession, outbound_id: int, *, user: User, reason: str | None = None
) -> DraftDecision:
    """Отклонить черновик.

    Запись остаётся: раздел 6 требует накапливать отклонённое для
    последующей доработки промптов.
    """
    message = await _load_pending(session, outbound_id)

    message.decision = "rejected"
    message.decided_by = user.id
    message.decided_at = datetime.now(tz=UTC)
    reasons = dict(message.reasons or {})
    reasons["rejected_by_manager"] = reason or ""
    message.reasons = reasons
    await session.flush()

    log.info("draft.rejected", outbound_id=outbound_id, user=user.login, reason=reason)
    return DraftDecision(outbound_id, "rejected", ready_to_send=False)


async def _load_pending(session: AsyncSession, outbound_id: int) -> OutboundMessage:
    message = (
        await session.execute(
            select(OutboundMessage)
            .where(OutboundMessage.id == outbound_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if message is None:
        raise DraftError(f"Черновик {outbound_id} не найден")
    if message.decision is not None:
        # Второй менеджер, открывший ту же очередь.
        raise DraftError(f"По черновику {outbound_id} решение уже принято")
    if message.sent_at is not None:
        raise DraftError(f"Черновик {outbound_id} уже отправлен")
    return message


async def rejected_for_prompt_tuning(
    session: AsyncSession, *, limit: int = 200
) -> list[OutboundMessage]:
    """Отклонённые черновики — материал для доработки промптов."""
    rows = await session.execute(
        select(OutboundMessage)
        .where(OutboundMessage.decision == "rejected")
        .order_by(OutboundMessage.id.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
