"""Нормализация апдейтов Telegram (этап 6).

Выше шлюза о канале никто не знает, поэтому проверяется главное: события
Telegram неотличимы по форме от событий MAX.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from repairbot.channels.telegram import normalizer
from repairbot.domain.events import AttachmentKind, Channel, ChatKind, EventType
from tests.fixtures import telegram_updates as fx


def _one(payload):
    (event,) = normalizer.normalize(payload)
    return event


# --- сообщения ---


def test_group_message_becomes_a_created_event():
    event = _one(fx.message())

    assert event.channel is Channel.TELEGRAM
    assert event.event_type is EventType.MESSAGE_CREATED
    assert event.chat.channel_chat_id == "-1001234567890"
    assert event.chat.kind is ChatKind.GROUP
    assert event.text == "Штукатурка на кухне закончена"
    assert event.actor is not None
    assert event.actor.display_name == "Иван Петров"


def test_seconds_are_read_as_seconds():
    """Telegram отдаёт секунды эпохи, MAX — миллисекунды.

    Перепутать единицы значит получить события 1970 года или 58-тысячного.
    """
    event = _one(fx.message())

    assert event.occurred_at == datetime(2026, 2, 25, 6, 13, 20, tzinfo=UTC)
    assert 2020 < event.occurred_at.year < 2100


def test_private_chat_is_a_dialog():
    event = _one(fx.private_question())

    assert event.chat.kind is ChatKind.DIALOG
    assert event.chat.title == "Мария С."


def test_edited_message_is_a_separate_event():
    """Правка не должна считаться повтором исходного сообщения."""
    created = _one(fx.message())
    edited = _one(fx.edited())

    assert edited.event_type is EventType.MESSAGE_EDITED
    assert edited.dedup_key != created.dedup_key
    assert edited.occurred_at > created.occurred_at


def test_start_in_private_chat_is_bot_started():
    """Аналог bot_started у MAX: заказчик нажал «Начать»."""
    event = _one(fx.start_command())

    assert event.event_type is EventType.BOT_STARTED
    assert "obj_17" in (event.text or "")


def test_start_in_a_group_is_an_ordinary_message():
    """В рабочем чате «/start» — просто текст, а не подключение заказчика."""
    event = _one(fx.message(text="/start"))

    assert event.event_type is EventType.MESSAGE_CREATED


def test_reply_keeps_the_source_message():
    event = _one(
        fx.message(reply_to_message={"message_id": 3, "chat": {"id": -1, "type": "group"}})
    )

    assert event.reply_to_message_id == "3"


# --- вступление бота ---


def test_bot_added_comes_from_a_status_change():
    """Telegram не присылает отдельного типа: приходит смена статуса бота."""
    event = _one(fx.bot_added())

    assert event.event_type is EventType.BOT_ADDED
    assert event.chat.channel_chat_id == "-1001234567890"
    # Автор — тот, кто добавил бота, а не сам бот.
    assert event.actor is not None
    assert event.actor.display_name == "Прораб Сергей"


def test_bot_removed_is_recognised():
    assert _one(fx.bot_removed()).event_type is EventType.BOT_REMOVED


def test_administrator_status_also_means_added():
    assert _one(fx.bot_added(status="administrator")).event_type is EventType.BOT_ADDED


def test_unknown_membership_status_is_refused():
    from repairbot.domain.events import NormalizationError

    with pytest.raises(NormalizationError, match="статус"):
        normalizer.normalize(fx.bot_added(status="странно"))


# --- служебные события ---


def test_title_change_carries_the_new_title():
    event = _one(fx.title_changed())

    assert event.event_type is EventType.CHAT_TITLE_CHANGED
    assert event.chat.title == "Объект Ленина 5 — сдан"
    assert event.text == "Объект Ленина 5 — сдан"


def test_user_joined_names_the_newcomer():
    event = _one(fx.user_joined())

    assert event.event_type is EventType.USER_ADDED
    assert event.actor is not None
    assert event.actor.display_name == "Новый Плиточник"


def test_callback_uses_the_platform_identifier():
    event = _one(fx.callback())

    assert event.event_type is EventType.CALLBACK
    assert event.text == "approve:draft_31"
    assert event.dedup_key == "telegram:callback:cb.9911"


def test_unknown_update_still_lands_in_the_journal():
    """Разбирать не умеем, но терять сведения о нём незачем."""
    event = _one(fx.unknown_update())

    assert event.event_type is EventType.UNKNOWN
    assert event.raw["poll_answer"]["poll_id"] == "p1"


# --- вложения ---


def test_caption_is_the_message_text():
    """«Чек за грунтовку» под фотографией — такой же текст, как и без неё."""
    event = _one(fx.with_photo())

    assert event.text == "Чек за грунтовку"


def test_largest_photo_size_is_taken():
    """Распознавать чек по превью бессмысленно."""
    event = _one(fx.with_photo())

    (photo,) = event.attachments
    assert photo.kind is AttachmentKind.IMAGE
    assert photo.channel_file_id == "large"
    assert photo.width == 1200


def test_document_keeps_its_name_and_type():
    event = _one(fx.with_document())

    (doc,) = event.attachments
    assert doc.kind is AttachmentKind.FILE
    assert doc.filename == "смета.pdf"
    assert doc.mime_type == "application/pdf"


def test_own_contact_gives_a_verified_phone():
    event = _one(fx.with_contact())

    assert event.actor is not None
    assert event.actor.phone == "+79990001122"


def test_someone_elses_contact_does_not_become_the_authors_phone():
    """Человек может переслать чужую визитку.

    Записать её телефон автору значило бы завести неверные данные, которые
    потом всплывут в карточке человека.
    """
    event = _one(fx.with_contact(user_id=999))

    assert event.actor is not None
    assert event.actor.phone is None


# --- идемпотентность ---


def test_dedup_key_includes_the_chat():
    """Номер сообщения в Telegram уникален только внутри чата.

    Без чата в ключе сообщение №5 из одного чата считалось бы повтором
    сообщения №5 из другого.
    """
    first = _one(fx.message(message_id=5, chat={"id": -100, "type": "group"}))
    second = _one(fx.message(message_id=5, chat={"id": -200, "type": "group"}))

    assert first.dedup_key != second.dedup_key


def test_same_update_twice_gives_the_same_key():
    assert _one(fx.message()).dedup_key == _one(fx.message()).dedup_key
