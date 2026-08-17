"""Модели апдейтов Telegram Bot API.

Нестрогие, как и у MAX: `extra="allow"`, почти всё необязательно. Telegram
добавляет поля в каждой версии, и появление нового не должно ронять приём.

Именование полей приходится обходить: у Telegram есть поле `from`, а это
ключевое слово Python. Берём его псевдонимом.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TgBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class TgUser(TgBase):
    id: int
    is_bot: bool = False
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None

    @property
    def display_name(self) -> str | None:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.username


class TgChat(TgBase):
    id: int
    type: str = "private"
    """private | group | supergroup | channel"""
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TgPhotoSize(TgBase):
    file_id: str
    file_unique_id: str | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None


class TgDocument(TgBase):
    file_id: str
    file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TgVideo(TgDocument):
    width: int | None = None
    height: int | None = None
    duration: int | None = None


class TgAudio(TgDocument):
    duration: int | None = None


class TgVoice(TgBase):
    file_id: str
    file_unique_id: str | None = None
    duration: int | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TgSticker(TgBase):
    file_id: str
    file_unique_id: str | None = None
    emoji: str | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None


class TgContact(TgBase):
    phone_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    user_id: int | None = None


class TgLocation(TgBase):
    latitude: float | None = None
    longitude: float | None = None


class TgMessage(TgBase):
    message_id: int
    from_user: TgUser | None = Field(default=None, alias="from")
    """У Telegram поле называется `from` — ключевое слово Python."""
    sender_chat: TgChat | None = None
    chat: TgChat
    date: int | None = None
    """Секунды эпохи, в отличие от миллисекунд у MAX."""
    edit_date: int | None = None

    text: str | None = None
    caption: str | None = None
    """Подпись к вложению. Для нас это тот же текст сообщения."""

    reply_to_message: TgMessage | None = None

    photo: list[TgPhotoSize] = Field(default_factory=list)
    document: TgDocument | None = None
    video: TgVideo | None = None
    audio: TgAudio | None = None
    voice: TgVoice | None = None
    sticker: TgSticker | None = None
    contact: TgContact | None = None
    location: TgLocation | None = None

    new_chat_members: list[TgUser] = Field(default_factory=list)
    left_chat_member: TgUser | None = None
    new_chat_title: str | None = None

    @property
    def body_text(self) -> str | None:
        """Текст сообщения или подпись к вложению.

        Различать их выше по стеку незачем: «чек за грунтовку» под
        фотографией — такой же текст, как и без неё.
        """
        return self.text or self.caption


class TgChatMember(TgBase):
    user: TgUser | None = None
    status: str = ""
    """creator | administrator | member | restricted | left | kicked"""


class TgChatMemberUpdated(TgBase):
    chat: TgChat
    from_user: TgUser | None = Field(default=None, alias="from")
    date: int | None = None
    old_chat_member: TgChatMember | None = None
    new_chat_member: TgChatMember | None = None


class TgCallbackQuery(TgBase):
    id: str
    from_user: TgUser | None = Field(default=None, alias="from")
    message: TgMessage | None = None
    data: str | None = None


class TgUpdate(TgBase):
    update_id: int
    message: TgMessage | None = None
    edited_message: TgMessage | None = None
    channel_post: TgMessage | None = None
    edited_channel_post: TgMessage | None = None
    my_chat_member: TgChatMemberUpdated | None = None
    """Изменение статуса самого бота: добавили в чат или удалили."""
    callback_query: TgCallbackQuery | None = None

    def raw(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


TgMessage.model_rebuild()
