"""Единый внутренний формат событий (раздел 3.1 ТЗ).

Шлюз каналов приводит вебхуки MAX / Telegram / WhatsApp к этим моделям.
Всё, что находится выше шлюза, о конкретном мессенджере не знает.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Channel(StrEnum):
    MAX = "max"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


class EventType(StrEnum):
    """Типы нормализованных событий шлюза.

    Это транспортный слой: бизнес-факты (work_progress, purchase, ...)
    извлекаются на этапе 2 экстрактором и пишутся в тот же журнал
    отдельными типами.
    """

    MESSAGE_CREATED = "message_created"
    MESSAGE_EDITED = "message_edited"
    MESSAGE_REMOVED = "message_removed"
    BOT_ADDED = "bot_added"
    BOT_REMOVED = "bot_removed"
    BOT_STARTED = "bot_started"
    USER_ADDED = "user_added"
    USER_REMOVED = "user_removed"
    CHAT_TITLE_CHANGED = "chat_title_changed"
    CALLBACK = "callback"
    CONTACT_SHARED = "contact_shared"
    UNKNOWN = "unknown"


FACT_EVENT_PREFIX = "fact:"
"""Префикс `event_type` для извлечённых фактов.

Транспортные события шлюза и бизнес-факты лежат в одном журнале, и префикс —
единственное, что их различает. Константа здесь, а не в модуле экстрактора:
её читают и витрина, и веб-интерфейс, и отчёты."""

NEEDS_HUMAN_EVENT = "needs_human"
MANUAL_EDIT_EVENT = "manual_edit"
"""Ручная правка в таблице (раздел 5 ТЗ)."""

FACT_CONFIRMED_EVENT = "fact_confirmed"
FACT_REJECTED_EVENT = "fact_rejected"
"""Решения менеджера по фактам ниже порога. Отклонённые накапливаются для
последующей доработки промптов (раздел 6 ТЗ)."""


class ChatKind(StrEnum):
    DIALOG = "dialog"
    GROUP = "group"
    CHANNEL = "channel"


class AttachmentKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
    STICKER = "sticker"
    CONTACT = "contact"
    LOCATION = "location"
    SHARE = "share"
    OTHER = "other"


class Actor(BaseModel):
    """Автор события в терминах канала."""

    model_config = ConfigDict(frozen=True)

    channel_user_id: str
    username: str | None = None
    display_name: str | None = None
    is_bot: bool = False
    phone: str | None = None
    """Заполняется только из верифицированного `request_contact`."""


class Chat(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_chat_id: str
    kind: ChatKind = ChatKind.GROUP
    title: str | None = None


class Attachment(BaseModel):
    """Вложение в нормализованном виде.

    `url` у большинства каналов живёт недолго, поэтому агент документов
    (этап 3) обязан перекладывать файл в собственное хранилище, а не
    ссылаться на канал.
    """

    kind: AttachmentKind
    channel_file_id: str | None = None
    url: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    """Исходное представление вложения — на случай нехватки полей выше."""


class InboundEvent(BaseModel):
    """Нормализованное входящее событие.

    Ключ идемпотентности — (`channel`, `channel_chat_id`, `dedup_key`).
    Платформа может доставить один и тот же вебхук повторно; повторы
    отбрасываются на уровне БД, а не в памяти процесса.
    """

    model_config = ConfigDict(frozen=True)

    channel: Channel
    event_type: EventType
    chat: Chat
    actor: Actor | None = None
    occurred_at: datetime

    message_id: str | None = None
    reply_to_message_id: str | None = None
    text: str | None = None
    attachments: tuple[Attachment, ...] = ()

    dedup_key: str
    """Стабильный идентификатор события внутри чата."""

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)
    """Исходный payload вебхука. Хранится целиком для разбора инцидентов."""

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


class OutboundText(BaseModel):
    """Исходящее текстовое сообщение.

    Отправляется только через контролёра (раздел 6 ТЗ): напрямую
    клиенты канала из агентов не вызываются.
    """

    channel: Channel
    channel_chat_id: str
    text: str
    reply_to_message_id: str | None = None
    notify: bool = True
    idempotency_key: str | None = None


class NormalizationError(ValueError):
    """Вебхук не удалось привести к внутреннему формату."""


ChannelName = Literal["max", "telegram", "whatsapp"]
