"""Нормализация апдейтов Telegram во внутренний формат.

Отличия от MAX, которые пришлось учесть:

* **время в секундах**, а не в миллисекундах;
* **удаления не приходят вовсе.** Telegram не уведомляет бота об удалённом
  сообщении, поэтому события `message_removed` в этом канале не будет
  никогда — не потому, что мы его не разбираем;
* **подпись к вложению** лежит в отдельном поле `caption`, а не в `text`;
* **вступление бота** приходит не отдельным типом, а сменой его статуса
  в `my_chat_member`;
* **режим приватности.** По умолчанию бот в группе видит только команды и
  ответы себе. Пока он включён, приём работает, но половина переписки
  бригады до нас не доходит — проверяется командой `check-chat`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from repairbot.channels.telegram.schemas import (
    TgChat,
    TgMessage,
    TgUpdate,
    TgUser,
)
from repairbot.domain.events import (
    Actor,
    Attachment,
    AttachmentKind,
    Channel,
    Chat,
    ChatKind,
    EventType,
    InboundEvent,
    NormalizationError,
)

_CHAT_KINDS: dict[str, ChatKind] = {
    "private": ChatKind.DIALOG,
    "group": ChatKind.GROUP,
    "supergroup": ChatKind.GROUP,
    "channel": ChatKind.CHANNEL,
}

_PRESENT_STATUSES = frozenset({"member", "administrator", "creator", "restricted"})
_ABSENT_STATUSES = frozenset({"left", "kicked"})


def normalize(payload: dict[str, Any]) -> list[InboundEvent]:
    """Разобрать тело вебхука. Telegram присылает ровно один апдейт."""
    update = TgUpdate.model_validate(payload)
    return [normalize_update(update)]


def normalize_update(update: TgUpdate) -> InboundEvent:
    raw = update.raw()

    if update.my_chat_member is not None:
        return _bot_membership(update, raw)
    if update.callback_query is not None:
        return _callback(update, raw)

    message = (
        update.message
        or update.edited_message
        or update.channel_post
        or update.edited_channel_post
    )
    if message is None:
        # Неизвестный тип апдейта в журнал всё равно попадает: разбирать
        # его мы не умеем, но терять сведения о нём незачем.
        return InboundEvent(
            channel=Channel.TELEGRAM,
            event_type=EventType.UNKNOWN,
            chat=Chat(channel_chat_id=str(update.update_id), kind=ChatKind.GROUP),
            occurred_at=_now(),
            dedup_key=f"telegram:unknown:{update.update_id}",
            raw=raw,
        )

    edited = update.edited_message is not None or update.edited_channel_post is not None
    return _from_message(message, edited=edited, raw=raw)


def _from_message(message: TgMessage, *, edited: bool, raw: dict[str, Any]) -> InboundEvent:
    chat = _chat_of(message.chat)
    actor = _actor_of(message.from_user)
    occurred_at = _to_datetime(message.edit_date if edited else message.date) or _now()
    message_id = str(message.message_id)

    event_type = EventType.MESSAGE_EDITED if edited else EventType.MESSAGE_CREATED
    text = message.body_text

    if message.new_chat_members:
        event_type = EventType.USER_ADDED
        actor = _actor_of(message.new_chat_members[0]) or actor
    elif message.left_chat_member is not None:
        event_type = EventType.USER_REMOVED
        actor = _actor_of(message.left_chat_member) or actor
    elif message.new_chat_title is not None:
        event_type = EventType.CHAT_TITLE_CHANGED
        text = message.new_chat_title
        chat = chat.model_copy(update={"title": message.new_chat_title})
    elif (
        not edited
        and chat.kind is ChatKind.DIALOG
        and (text or "").strip().startswith("/start")
    ):
        # Аналог bot_started у MAX: заказчик нажал «Начать» в личном диалоге.
        event_type = EventType.BOT_STARTED

    reply_to = (
        str(message.reply_to_message.message_id) if message.reply_to_message else None
    )
    attachments = _attachments_of(message)

    # Верифицированный номер приходит, только когда человек сам поделился
    # контактом и это его собственный контакт.
    if actor is not None and message.contact is not None:
        if message.contact.user_id and str(message.contact.user_id) == actor.channel_user_id:
            if message.contact.phone_number:
                actor = actor.model_copy(update={"phone": message.contact.phone_number})

    return InboundEvent(
        channel=Channel.TELEGRAM,
        event_type=event_type,
        chat=chat,
        actor=actor,
        occurred_at=occurred_at,
        message_id=message_id,
        reply_to_message_id=reply_to,
        text=text,
        attachments=attachments,
        # Правка того же сообщения — отдельное событие, поэтому в ключ
        # входит и тип: иначе она считалась бы повтором исходного.
        dedup_key=_dedup_key(event_type, chat.channel_chat_id, message_id, occurred_at),
        raw=raw,
    )


def _bot_membership(update: TgUpdate, raw: dict[str, Any]) -> InboundEvent:
    """Бота добавили в чат или удалили.

    Telegram отдельного типа для этого не присылает: приходит смена
    статуса самого бота в `my_chat_member`.
    """
    membership = update.my_chat_member
    assert membership is not None  # проверено вызывающей стороной

    status = (membership.new_chat_member.status if membership.new_chat_member else "") or ""
    if status in _PRESENT_STATUSES:
        event_type = EventType.BOT_ADDED
    elif status in _ABSENT_STATUSES:
        event_type = EventType.BOT_REMOVED
    else:
        raise NormalizationError(f"my_chat_member: неизвестный статус {status!r}")

    occurred_at = _to_datetime(membership.date) or _now()
    chat = _chat_of(membership.chat)
    return InboundEvent(
        channel=Channel.TELEGRAM,
        event_type=event_type,
        chat=chat,
        # Автор здесь — тот, кто добавил бота, а не сам бот.
        actor=_actor_of(membership.from_user),
        occurred_at=occurred_at,
        dedup_key=_dedup_key(event_type, chat.channel_chat_id, status, occurred_at),
        raw=raw,
    )


def _callback(update: TgUpdate, raw: dict[str, Any]) -> InboundEvent:
    callback = update.callback_query
    assert callback is not None  # проверено вызывающей стороной

    source = callback.message
    chat = _chat_of(source.chat) if source else Chat(channel_chat_id="0")
    return InboundEvent(
        channel=Channel.TELEGRAM,
        event_type=EventType.CALLBACK,
        chat=chat,
        actor=_actor_of(callback.from_user),
        occurred_at=_to_datetime(source.date if source else None) or _now(),
        message_id=str(source.message_id) if source else None,
        text=callback.data,
        # Идентификатор нажатия платформа гарантирует уникальным.
        dedup_key=f"telegram:callback:{callback.id}",
        raw=raw,
    )


def _chat_of(chat: TgChat) -> Chat:
    title = chat.title
    if title is None and chat.type == "private":
        title = " ".join(p for p in (chat.first_name, chat.last_name) if p) or None
    return Chat(
        channel_chat_id=str(chat.id),
        kind=_CHAT_KINDS.get(chat.type, ChatKind.GROUP),
        title=title,
    )


def _actor_of(user: TgUser | None) -> Actor | None:
    if user is None:
        return None
    return Actor(
        channel_user_id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        is_bot=user.is_bot,
    )


def _attachments_of(message: TgMessage) -> tuple[Attachment, ...]:
    found: list[Attachment] = []

    if message.photo:
        # Telegram присылает одну фотографию в нескольких размерах.
        # Берём самый крупный: распознавать чек по превью бессмысленно.
        largest = max(message.photo, key=lambda p: (p.width or 0) * (p.height or 0))
        found.append(
            Attachment(
                kind=AttachmentKind.IMAGE,
                channel_file_id=largest.file_id,
                mime_type="image/jpeg",
                size_bytes=largest.file_size,
                width=largest.width,
                height=largest.height,
                payload=largest.model_dump(mode="json", exclude_none=True),
            )
        )

    if message.document is not None:
        found.append(
            Attachment(
                kind=AttachmentKind.FILE,
                channel_file_id=message.document.file_id,
                filename=message.document.file_name,
                mime_type=message.document.mime_type,
                size_bytes=message.document.file_size,
                payload=message.document.model_dump(mode="json", exclude_none=True),
            )
        )

    if message.video is not None:
        found.append(
            Attachment(
                kind=AttachmentKind.VIDEO,
                channel_file_id=message.video.file_id,
                filename=message.video.file_name,
                mime_type=message.video.mime_type,
                size_bytes=message.video.file_size,
                width=message.video.width,
                height=message.video.height,
                duration_seconds=message.video.duration,
                payload=message.video.model_dump(mode="json", exclude_none=True),
            )
        )

    # Голосовое и аудиофайл для нас одно и то же: звук, который придётся
    # расшифровывать, чтобы извлечь факт.
    for media in (message.audio, message.voice):
        if media is not None:
            found.append(
                Attachment(
                    kind=AttachmentKind.AUDIO,
                    channel_file_id=media.file_id,
                    mime_type=media.mime_type,
                    size_bytes=media.file_size,
                    duration_seconds=media.duration,
                    payload=media.model_dump(mode="json", exclude_none=True),
                )
            )

    if message.sticker is not None:
        found.append(
            Attachment(
                kind=AttachmentKind.STICKER,
                channel_file_id=message.sticker.file_id,
                width=message.sticker.width,
                height=message.sticker.height,
                payload=message.sticker.model_dump(mode="json", exclude_none=True),
            )
        )

    if message.contact is not None:
        found.append(
            Attachment(
                kind=AttachmentKind.CONTACT,
                payload=message.contact.model_dump(mode="json", exclude_none=True),
            )
        )

    if message.location is not None:
        found.append(
            Attachment(
                kind=AttachmentKind.LOCATION,
                payload=message.location.model_dump(mode="json", exclude_none=True),
            )
        )

    return tuple(found)


def _dedup_key(
    event_type: EventType, chat_id: str, source: str | None, occurred_at: datetime
) -> str:
    """Ключ идемпотентности.

    Идентификатор сообщения в Telegram уникален только внутри чата,
    поэтому в ключ входит и чат. Без него сообщение №5 из одного чата
    считалось бы повтором сообщения №5 из другого.
    """
    if source:
        return f"telegram:{event_type.value}:{chat_id}:{source}"
    digest = hashlib.sha256(
        f"{event_type.value}|{chat_id}|{occurred_at.isoformat()}".encode()
    ).hexdigest()[:32]
    return f"telegram:{event_type.value}:{digest}"


def _to_datetime(value: int | None) -> datetime | None:
    """Telegram отдаёт секунды эпохи, MAX — миллисекунды."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _now() -> datetime:
    return datetime.now(tz=UTC)
