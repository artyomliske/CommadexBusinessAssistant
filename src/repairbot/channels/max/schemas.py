"""Модели вебхуков MAX Bot API.

Намеренно нестрогие: `extra="allow"`, почти все поля необязательны.
API платформы меняется (раздел 10 ТЗ), и появление нового поля не должно
ронять приём событий. Незнакомые `update_type` нормализуются в UNKNOWN
и всё равно попадают в журнал сырым payload'ом.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MaxBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class MaxUser(MaxBase):
    user_id: int | None = None
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    is_bot: bool = False


class MaxRecipient(MaxBase):
    chat_id: int | None = None
    chat_type: str | None = None
    """dialog | chat | channel"""
    user_id: int | None = None


class MaxAttachment(MaxBase):
    type: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    filename: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None


class MaxMessageBody(MaxBase):
    mid: str | None = None
    seq: int | None = None
    text: str | None = None
    attachments: list[MaxAttachment] = Field(default_factory=list)


class MaxLinkedMessage(MaxBase):
    type: str | None = None
    """reply | forward"""
    sender: MaxUser | None = None
    chat_id: int | None = None
    message: MaxMessageBody | None = None


class MaxMessage(MaxBase):
    sender: MaxUser | None = None
    recipient: MaxRecipient | None = None
    timestamp: int | None = None
    body: MaxMessageBody | None = None
    link: MaxLinkedMessage | None = None


class MaxCallback(MaxBase):
    timestamp: int | None = None
    callback_id: str | None = None
    payload: str | None = None
    user: MaxUser | None = None


class MaxUpdate(MaxBase):
    """Один апдейт MAX.

    Поля различаются по `update_type`: у message_* есть `message`,
    у bot_added/user_added — `chat_id` и `user` на верхнем уровне.
    """

    update_type: str | None = None
    timestamp: int | None = None
    chat_id: int | None = None
    user_id: int | None = None
    message_id: str | None = None
    title: str | None = None
    payload: str | None = None
    is_channel: bool | None = None
    inviter_id: int | None = None
    user: MaxUser | None = None
    message: MaxMessage | None = None
    callback: MaxCallback | None = None
    user_locale: str | None = None


class MaxWebhookEnvelope(MaxBase):
    """Тело запроса вебхука.

    Вебхук доставляет один апдейт плоским объектом, long polling —
    список в `updates`. Принимаем оба варианта.
    """

    updates: list[MaxUpdate] | None = None
    marker: int | None = None


class MaxMessageListResponse(MaxBase):
    """Ответ `GET /messages` — догрузка истории чата."""

    messages: list[MaxMessage] = Field(default_factory=list)
