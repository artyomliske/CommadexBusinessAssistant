"""Приём нормализованных событий.

Инварианты:
  * повторная доставка вебхука не создаёт вторую запись в журнале —
    идемпотентность обеспечена уникальным индексом (channel, chat_id, dedup_key),
    а не проверкой в памяти процесса;
  * запись сообщения и запись в журнал происходят в одной транзакции;
  * реестр чатов ведётся здесь же, по событиям bot_added / bot_started,
    потому что `GET /chats` платформой отключён.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import (
    AttachmentRecord,
    ChannelIdentity,
    ChatRecord,
    Event,
    Message,
)
from repairbot.domain.events import Channel, EventType, InboundEvent
from repairbot.ingest.object_resolver import UNRESOLVED, ObjectResolver, Resolution
from repairbot.memory.working import WorkingMemory
from repairbot.observability import get_logger

log = get_logger(__name__)

_MESSAGE_EVENTS = {EventType.MESSAGE_CREATED, EventType.MESSAGE_EDITED}
_MEMBERSHIP_GAINED = {EventType.BOT_ADDED, EventType.BOT_STARTED}


@dataclass(frozen=True, slots=True)
class ChatRef:
    """Ссылка на запись чата после upsert. Не ORM-объект: в сессию не попадает."""

    id: int
    object_id: int | None


@dataclass(frozen=True, slots=True)
class IdentityRef:
    id: int
    person_id: int | None


@dataclass(slots=True)
class IngestResult:
    event_id: int | None
    message_id: int | None
    chat_id: int
    object_id: int | None
    duplicate: bool
    object_source: str | None = None
    """Откуда узнан объект: chat, text, reply, context. None — не узнан."""

    @property
    def stored(self) -> bool:
        return self.event_id is not None


class IngestService:
    def __init__(
        self,
        session: AsyncSession,
        working_memory: WorkingMemory | None = None,
        resolver: ObjectResolver | None = None,
    ) -> None:
        self._session = session
        self._wm = working_memory
        self._resolver = resolver or ObjectResolver(session)

    async def ingest(self, event: InboundEvent) -> IngestResult:
        chat = await self._upsert_chat(event)
        identity = await self._upsert_identity(event)

        message_row_id: int | None = None
        resolution = Resolution(object_id=chat.object_id, source="chat" if chat.object_id else None)
        if event.event_type in _MESSAGE_EVENTS and event.message_id:
            resolution = await self._resolve_object(event, chat, identity)
            message_row_id = await self._upsert_message(event, chat, identity, resolution)

        event_id = await self._append_event(event, chat, message_row_id, resolution)
        duplicate = event_id is None

        if not duplicate and self._wm is not None:
            await self._wm.append(event)

        if not duplicate:
            await self._session.execute(
                sql_update(ChatRecord)
                .where(ChatRecord.id == chat.id)
                .values(last_event_at=event.occurred_at)
            )

        log.info(
            "ingest.event",
            channel=event.channel.value,
            event_type=event.event_type.value,
            chat_id=event.chat.channel_chat_id,
            object_id=resolution.object_id,
            object_source=resolution.source,
            duplicate=duplicate,
        )
        return IngestResult(
            event_id=event_id,
            message_id=message_row_id,
            chat_id=chat.id,
            object_id=resolution.object_id,
            object_source=resolution.source,
            duplicate=duplicate,
        )

    async def _resolve_object(
        self, event: InboundEvent, chat: ChatRef, identity: IdentityRef | None
    ) -> Resolution:
        """К какому объекту относится сообщение.

        Отказ разрешителя — не повод потерять сообщение: неузнанный
        объект штатно означает «разберёт человек», и приём должен
        доработать до записи в журнал.
        """
        try:
            return await self._resolver.resolve(
                text=event.text,
                chat_id=chat.id,
                chat_object_id=chat.object_id,
                author_identity_id=identity.id if identity else None,
                reply_to_message_id=event.reply_to_message_id,
                channel=event.channel.value,
                channel_chat_id=event.chat.channel_chat_id,
                sent_at=event.occurred_at,
            )
        except Exception:
            log.exception("ingest.resolve_failed", chat_id=event.chat.channel_chat_id)
            return UNRESOLVED

    # --- реестр чатов ---

    async def _upsert_chat(self, event: InboundEvent) -> ChatRef:
        values: dict[str, object] = {
            "channel": event.channel.value,
            "channel_chat_id": event.chat.channel_chat_id,
            "kind": event.chat.kind.value,
            "title": event.chat.title,
        }
        if event.event_type in _MEMBERSHIP_GAINED:
            values["bot_is_member"] = True
        elif event.event_type is EventType.BOT_REMOVED:
            values["bot_is_member"] = False

        # Не перезатираем title и kind пустыми значениями из технических событий.
        on_update = {k: v for k, v in values.items() if v is not None}
        on_update.pop("channel", None)
        on_update.pop("channel_chat_id", None)

        stmt = (
            pg_insert(ChatRecord)
            .values(**values)
            .on_conflict_do_update(constraint="uq_chat", set_=on_update)
            .returning(ChatRecord.id, ChatRecord.object_id)
        )
        row = (await self._session.execute(stmt)).one()
        return ChatRef(id=row.id, object_id=row.object_id)

    # --- участники ---

    async def _upsert_identity(self, event: InboundEvent) -> IdentityRef | None:
        actor = event.actor
        if actor is None:
            return None

        values = {
            "channel": event.channel.value,
            "channel_user_id": actor.channel_user_id,
            "username": actor.username,
            "display_name": actor.display_name,
            "is_bot": actor.is_bot,
        }
        on_update = {
            k: v
            for k, v in values.items()
            if v is not None and k not in ("channel", "channel_user_id")
        }
        stmt = (
            pg_insert(ChannelIdentity)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_channel_identity",
                set_=on_update or {"channel": event.channel.value},
            )
            .returning(ChannelIdentity.id, ChannelIdentity.person_id)
        )
        row = (await self._session.execute(stmt)).one()
        return IdentityRef(id=row.id, person_id=row.person_id)

    # --- сообщения ---

    async def _upsert_message(
        self,
        event: InboundEvent,
        chat: ChatRef,
        identity: IdentityRef | None,
        resolution: Resolution,
    ) -> int:
        assert event.message_id is not None

        values = {
            "channel": event.channel.value,
            "channel_chat_id": event.chat.channel_chat_id,
            "channel_message_id": event.message_id,
            "chat_id": chat.id,
            "object_id": resolution.object_id,
            "object_source": resolution.source,
            "author_identity_id": identity.id if identity else None,
            "reply_to_message_id": event.reply_to_message_id,
            "text": event.text,
            "sent_at": event.occurred_at,
            "raw": event.raw,
        }
        # При правке сообщения объект не пересматриваем, если его уже
        # назначил человек: разбор текста не должен отменять решение,
        # принятое руками.
        stmt = (
            pg_insert(Message)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_message",
                set_={"text": event.text, "raw": event.raw},
            )
            .returning(Message.id)
        )
        message_id = (await self._session.execute(stmt)).scalar_one()

        if event.attachments:
            await self._replace_attachments(message_id, event)
        return message_id

    async def _replace_attachments(self, message_id: int, event: InboundEvent) -> None:
        """Вложения пишем один раз: при редактировании сообщения не дублируем."""
        existing = set(
            (
                await self._session.execute(
                    select(AttachmentRecord.channel_file_id).where(
                        AttachmentRecord.message_id == message_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for attachment in event.attachments:
            if attachment.channel_file_id and attachment.channel_file_id in existing:
                continue
            self._session.add(
                AttachmentRecord(
                    message_id=message_id,
                    kind=attachment.kind.value,
                    channel_file_id=attachment.channel_file_id,
                    source_url=attachment.url,
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                    size_bytes=attachment.size_bytes,
                    payload=attachment.payload,
                )
            )

    # --- журнал ---

    async def _append_event(
        self,
        event: InboundEvent,
        chat: ChatRef,
        message_row_id: int | None,
        resolution: Resolution,
    ) -> int | None:
        """Добавить запись в журнал. None — событие уже было принято."""
        stmt = (
            pg_insert(Event)
            .values(
                object_id=resolution.object_id,
                channel=event.channel.value,
                channel_chat_id=event.chat.channel_chat_id,
                channel_message_id=event.message_id,
                source_message_id=message_row_id,
                event_type=event.event_type.value,
                payload=event.raw,
                dedup_key=event.dedup_key,
                occurred_at=event.occurred_at,
                applied=False,
            )
            .on_conflict_do_nothing(constraint="uq_event_dedup")
            .returning(Event.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def chats_without_object(
    session: AsyncSession, channel: Channel | None = None
) -> list[ChatRecord]:
    """Чаты, не привязанные к объекту.

    Основание — раздел 10 ТЗ: еженедельный контроль объектов без
    подключённого чата.
    """
    stmt = select(ChatRecord).where(ChatRecord.object_id.is_(None))
    if channel is not None:
        stmt = stmt.where(ChatRecord.channel == channel.value)
    return list((await session.execute(stmt)).scalars().all())
