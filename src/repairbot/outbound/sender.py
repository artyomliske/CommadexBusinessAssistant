"""Отправка исходящих сообщений.

Единственное место, где сообщение действительно уходит в канал. Отправить
можно только то, что уже одобрено контролёром и записано в журнал аудита:
на вход подаётся идентификатор записи, а не произвольный текст.

Такое устройство исключает обход. Агент физически не может отправить
сообщение мимо проверок — у него нет для этого функции.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.channels import registry
from repairbot.db.models import DialogState, OutboundMessage
from repairbot.domain.events import Channel, OutboundText
from repairbot.observability import get_logger
from repairbot.outbound.controller import Controller
from repairbot.outbound.policy import Audience, Verdict

log = get_logger(__name__)


@dataclass(slots=True)
class SendResult:
    outbound_id: int
    sent: bool
    channel_message_id: str | None = None
    skipped_reason: str | None = None


class SendRefused(RuntimeError):
    """Попытка отправить то, что отправлять нельзя."""


async def send_approved(
    session: AsyncSession, outbound_id: int, *, controller: Controller
) -> SendResult:
    """Отправить одобренное сообщение.

    Проверка одобрения повторяется здесь, а не только в контролёре:
    между решением и отправкой проходит время, за которое запись могли
    отклонить, а диалог — поставить на паузу.
    """
    message = (
        await session.execute(
            select(OutboundMessage)
            .where(OutboundMessage.id == outbound_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if message is None:
        raise SendRefused(f"Запись {outbound_id} не найдена")

    if message.sent_at is not None:
        # Повтор задачи после отправки: молча выходим, а не шлём второй раз.
        log.info("outbound.already_sent", outbound_id=outbound_id)
        return SendResult(outbound_id, sent=False, skipped_reason="уже отправлено")

    approved = message.verdict == Verdict.ALLOW.value or message.decision == "sent"
    if not approved:
        raise SendRefused(
            f"Запись {outbound_id} не одобрена к отправке: "
            f"вердикт «{message.verdict}», решение «{message.decision}»"
        )

    if await _dialog_paused(session, message):
        # Заказчик попросил человека уже после того, как черновик одобрили.
        log.warning("outbound.paused_before_send", outbound_id=outbound_id)
        return SendResult(outbound_id, sent=False, skipped_reason="диалог на паузе")

    text = message.edited_text or message.text
    adapter = registry.get(Channel(message.channel))
    channel_message_id = await adapter.send_text(
        OutboundText(
            channel=Channel(message.channel),
            channel_chat_id=message.channel_chat_id,
            text=text,
            reply_to_message_id=message.reply_to_message_id,
        )
    )

    await controller.mark_sent(outbound_id, channel_message_id=channel_message_id)

    # Первое отправленное заказчику сообщение считается представлением
    # системы: дальше предохранитель первого контакта не срабатывает.
    if message.audience == Audience.CLIENT.value:
        await _mark_disclosed(session, message)

    log.info(
        "outbound.sent",
        outbound_id=outbound_id,
        channel=message.channel,
        chat_id=message.channel_chat_id,
        intent=message.intent,
        edited=bool(message.edited_text),
    )
    return SendResult(outbound_id, sent=True, channel_message_id=channel_message_id)


async def _dialog_paused(session: AsyncSession, message: OutboundMessage) -> bool:
    state = (
        await session.execute(
            select(DialogState).where(
                DialogState.channel == message.channel,
                DialogState.channel_chat_id == message.channel_chat_id,
            )
        )
    ).scalar_one_or_none()
    return bool(state and state.is_paused)


async def _mark_disclosed(session: AsyncSession, message: OutboundMessage) -> None:
    state = (
        await session.execute(
            select(DialogState).where(
                DialogState.channel == message.channel,
                DialogState.channel_chat_id == message.channel_chat_id,
            )
        )
    ).scalar_one_or_none()

    if state is None:
        session.add(
            DialogState(
                channel=message.channel,
                channel_chat_id=message.channel_chat_id,
                automation_disclosed_at=datetime.now(tz=UTC),
            )
        )
    elif state.automation_disclosed_at is None:
        state.automation_disclosed_at = datetime.now(tz=UTC)
