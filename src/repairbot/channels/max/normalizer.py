"""Нормализация апдейтов MAX во внутренний формат."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from repairbot.channels.max.schemas import (
    MaxAttachment,
    MaxMessage,
    MaxUpdate,
    MaxWebhookEnvelope,
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

_EVENT_TYPES: dict[str, EventType] = {
    "message_created": EventType.MESSAGE_CREATED,
    "message_edited": EventType.MESSAGE_EDITED,
    "message_removed": EventType.MESSAGE_REMOVED,
    "bot_added": EventType.BOT_ADDED,
    "bot_removed": EventType.BOT_REMOVED,
    "bot_started": EventType.BOT_STARTED,
    "user_added": EventType.USER_ADDED,
    "user_removed": EventType.USER_REMOVED,
    "chat_title_changed": EventType.CHAT_TITLE_CHANGED,
    "message_callback": EventType.CALLBACK,
}

_CHAT_KINDS: dict[str, ChatKind] = {
    "dialog": ChatKind.DIALOG,
    "chat": ChatKind.GROUP,
    "channel": ChatKind.CHANNEL,
}

_ATTACHMENT_KINDS: dict[str, AttachmentKind] = {
    "image": AttachmentKind.IMAGE,
    "video": AttachmentKind.VIDEO,
    "audio": AttachmentKind.AUDIO,
    "file": AttachmentKind.FILE,
    "sticker": AttachmentKind.STICKER,
    "contact": AttachmentKind.CONTACT,
    "location": AttachmentKind.LOCATION,
    "share": AttachmentKind.SHARE,
}


def parse_envelope(payload: dict[str, Any]) -> list[MaxUpdate]:
    """Разобрать тело вебхука: плоский апдейт либо список в `updates`."""
    envelope = MaxWebhookEnvelope.model_validate(payload)
    if envelope.updates is not None:
        return envelope.updates
    return [MaxUpdate.model_validate(payload)]


def normalize(payload: dict[str, Any]) -> list[InboundEvent]:
    updates = parse_envelope(payload)
    return [normalize_update(update) for update in updates]


def normalize_update(update: MaxUpdate) -> InboundEvent:
    event_type = _EVENT_TYPES.get(update.update_type or "", EventType.UNKNOWN)
    occurred_at = _to_datetime(update.timestamp) or _now()
    raw = update.model_dump(mode="json", exclude_none=True)

    if event_type in (EventType.MESSAGE_CREATED, EventType.MESSAGE_EDITED, EventType.CALLBACK):
        if update.message is None:
            raise NormalizationError(f"{update.update_type}: отсутствует блок message")
        return _normalize_with_message(update, event_type, occurred_at, raw)

    chat_id = update.chat_id
    if chat_id is None and update.message is not None:
        chat_id = _chat_id_of(update.message)
    if chat_id is None:
        raise NormalizationError(f"{update.update_type}: не удалось определить chat_id")

    if update.is_channel:
        kind = ChatKind.CHANNEL
    elif event_type is EventType.BOT_STARTED:
        # bot_started приходит из личного диалога: пользователь нажал «Начать».
        kind = ChatKind.DIALOG
    else:
        kind = ChatKind.GROUP

    chat = Chat(channel_chat_id=str(chat_id), kind=kind, title=update.title)
    actor = _actor_from(update.user) if update.user else _actor_from_id(update.user_id)
    message_id = update.message_id

    return InboundEvent(
        channel=Channel.MAX,
        event_type=event_type,
        chat=chat,
        actor=actor,
        occurred_at=occurred_at,
        message_id=message_id,
        text=update.title if event_type is EventType.CHAT_TITLE_CHANGED else None,
        dedup_key=_dedup_key(event_type, message_id, occurred_at, raw),
        raw=raw,
    )


def _normalize_with_message(
    update: MaxUpdate,
    event_type: EventType,
    occurred_at: datetime,
    raw: dict[str, Any],
) -> InboundEvent:
    message = update.message
    assert message is not None  # проверено вызывающей стороной

    chat_id = _chat_id_of(message)
    if chat_id is None:
        raise NormalizationError(f"{update.update_type}: не удалось определить chat_id")

    recipient = message.recipient
    kind = _CHAT_KINDS.get((recipient.chat_type or "").lower() if recipient else "", ChatKind.GROUP)

    body = message.body
    message_id = body.mid if body else None
    attachments = tuple(_normalize_attachment(a) for a in (body.attachments if body else []))

    # Верифицированный телефон приходит вложением contact в ответ на request_contact.
    actor = _actor_from(message.sender)
    if actor is not None:
        phone = _phone_from_attachments(attachments)
        if phone:
            actor = actor.model_copy(update={"phone": phone})

    reply_to: str | None = None
    if message.link and message.link.type == "reply" and message.link.message:
        reply_to = message.link.message.mid

    if event_type is EventType.CALLBACK:
        actor = _actor_from(update.callback.user) if update.callback else actor
        text = update.callback.payload if update.callback else None
        dedup_source = update.callback.callback_id if update.callback else None
    else:
        text = body.text if body else None
        dedup_source = message_id

    return InboundEvent(
        channel=Channel.MAX,
        event_type=event_type,
        chat=Chat(channel_chat_id=str(chat_id), kind=kind),
        actor=actor,
        occurred_at=_to_datetime(message.timestamp) or occurred_at,
        message_id=message_id,
        reply_to_message_id=reply_to,
        text=text,
        attachments=attachments,
        dedup_key=_dedup_key(event_type, dedup_source, occurred_at, raw),
        raw=raw,
    )


def _normalize_attachment(attachment: MaxAttachment) -> Attachment:
    kind = _ATTACHMENT_KINDS.get((attachment.type or "").lower(), AttachmentKind.OTHER)
    payload = attachment.payload or {}
    file_id = payload.get("token") or payload.get("photo_id") or payload.get("fileId")
    return Attachment(
        kind=kind,
        channel_file_id=str(file_id) if file_id is not None else None,
        url=payload.get("url"),
        filename=attachment.filename,
        size_bytes=attachment.size,
        width=attachment.width,
        height=attachment.height,
        duration_seconds=attachment.duration,
        payload=attachment.model_dump(mode="json", exclude_none=True),
    )


def _phone_from_attachments(attachments: tuple[Attachment, ...]) -> str | None:
    for attachment in attachments:
        if attachment.kind is not AttachmentKind.CONTACT:
            continue
        max_info = attachment.payload.get("payload", {}).get("maxInfo") or {}
        phone = max_info.get("phone") or max_info.get("contact_phone")
        if phone:
            return str(phone)
    return None


def _chat_id_of(message: MaxMessage) -> int | None:
    if message.recipient and message.recipient.chat_id is not None:
        return message.recipient.chat_id
    # В диалоге chat_id может отсутствовать — тогда идентификатором служит user_id.
    if message.recipient and message.recipient.user_id is not None:
        return message.recipient.user_id
    return None


def _actor_from(user: Any) -> Actor | None:
    if user is None or user.user_id is None:
        return None
    name = user.name or " ".join(filter(None, [user.first_name, user.last_name])) or None
    return Actor(
        channel_user_id=str(user.user_id),
        username=user.username,
        display_name=name,
        is_bot=bool(user.is_bot),
    )


def _actor_from_id(user_id: int | None) -> Actor | None:
    return Actor(channel_user_id=str(user_id)) if user_id is not None else None


def _to_datetime(timestamp_ms: int | None) -> datetime | None:
    """MAX присылает время в миллисекундах эпохи."""
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _dedup_key(
    event_type: EventType,
    source_id: str | None,
    occurred_at: datetime,
    raw: dict[str, Any],
) -> str:
    """Стабильный ключ идемпотентности.

    Если у события есть собственный идентификатор — берём его. Иначе
    считаем хеш от типа, времени и payload'а: повторная доставка того же
    вебхука даст тот же ключ, а новое событие — другой.
    """
    if source_id:
        return f"{event_type.value}:{source_id}"
    digest = hashlib.sha256(
        f"{event_type.value}|{occurred_at.isoformat()}|{sorted(raw.items())}".encode()
    ).hexdigest()[:32]
    return f"{event_type.value}:{digest}"
