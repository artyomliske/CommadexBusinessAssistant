"""Нормализация апдейтов MAX во внутренний формат."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from repairbot.channels.max import normalizer
from repairbot.domain.events import (
    AttachmentKind,
    Channel,
    ChatKind,
    EventType,
    NormalizationError,
)
from tests.fixtures import max_updates as fx


def test_message_created():
    (event,) = normalizer.normalize(fx.message_created())

    assert event.channel is Channel.MAX
    assert event.event_type is EventType.MESSAGE_CREATED
    assert event.chat.channel_chat_id == "-100500"
    assert event.chat.kind is ChatKind.GROUP
    assert event.text == "Штукатурка на кухне закончена"
    assert event.message_id == "mid.001"
    assert event.dedup_key == "message_created:mid.001"
    assert event.actor is not None
    assert event.actor.channel_user_id == "777"
    assert event.actor.display_name == "Иван Петров"
    assert event.actor.is_bot is False


def test_timestamp_converted_from_milliseconds():
    (event,) = normalizer.normalize(fx.message_created())

    assert event.occurred_at == datetime.fromtimestamp(fx.TS / 1000, tz=UTC)
    assert event.occurred_at.tzinfo is not None


def test_dialog_without_chat_id_falls_back_to_user_id():
    (event,) = normalizer.normalize(fx.dialog_message())

    assert event.chat.channel_chat_id == "42"
    assert event.chat.kind is ChatKind.DIALOG
    assert event.actor is not None
    assert event.actor.display_name == "Мария С."


def test_reply_link_extracted():
    (event,) = normalizer.normalize(fx.message_created(reply_to="mid.parent"))

    assert event.reply_to_message_id == "mid.parent"


def test_attachments_normalized():
    (event,) = normalizer.normalize(fx.with_receipt())

    assert [a.kind for a in event.attachments] == [AttachmentKind.IMAGE, AttachmentKind.FILE]

    image, document = event.attachments
    assert image.channel_file_id == "tok_img_1"
    assert image.url == "https://cdn.max.ru/1.jpg"
    assert image.width == 1200

    assert document.filename == "смета.pdf"
    assert document.size_bytes == 240_512
    # Исходное представление сохраняется — агенту документов нужны все поля.
    assert document.payload["payload"]["fileId"] == 5511


def test_verified_phone_taken_from_contact_attachment():
    (event,) = normalizer.normalize(fx.with_contact())

    assert event.actor is not None
    assert event.actor.phone == "+79990001122"


def test_phone_absent_without_contact_attachment():
    (event,) = normalizer.normalize(fx.message_created())

    assert event.actor is not None
    assert event.actor.phone is None


def test_bot_added():
    (event,) = normalizer.normalize(fx.bot_added())

    assert event.event_type is EventType.BOT_ADDED
    assert event.chat.channel_chat_id == "-100500"
    assert event.actor is not None
    assert event.actor.channel_user_id == "12"


def test_bot_started():
    (event,) = normalizer.normalize(fx.bot_started())

    assert event.event_type is EventType.BOT_STARTED
    assert event.chat.channel_chat_id == "555"


def test_message_removed():
    (event,) = normalizer.normalize(fx.message_removed())

    assert event.event_type is EventType.MESSAGE_REMOVED
    assert event.message_id == "mid.001"
    assert event.dedup_key == "message_removed:mid.001"


def test_chat_title_changed():
    (event,) = normalizer.normalize(fx.chat_title_changed())

    assert event.event_type is EventType.CHAT_TITLE_CHANGED
    assert event.chat.title == "Объект Ленина 5 — бригада"
    assert event.text == "Объект Ленина 5 — бригада"


def test_callback_uses_callback_identity():
    (event,) = normalizer.normalize(fx.message_callback())

    assert event.event_type is EventType.CALLBACK
    assert event.text == "approve:draft_31"
    assert event.dedup_key == "callback:cb.9911"
    assert event.actor is not None
    assert event.actor.channel_user_id == "12"


def test_long_polling_envelope_yields_all_updates():
    events = normalizer.normalize(fx.long_polling_envelope())

    assert [e.message_id for e in events] == ["mid.a", "mid.b"]


def test_unknown_update_type_is_kept_not_dropped():
    """Новый тип апдейта не должен ронять приём: он попадает в журнал как unknown."""
    (event,) = normalizer.normalize(fx.unknown_update())

    assert event.event_type is EventType.UNKNOWN
    assert event.chat.channel_chat_id == "-100500"
    assert event.raw["emoji"] == "👍"


def test_unknown_fields_do_not_break_parsing():
    payload = fx.message_created()
    payload["brand_new_field"] = {"nested": True}
    payload["message"]["body"]["another_new_one"] = 5

    (event,) = normalizer.normalize(payload)

    assert event.text == "Штукатурка на кухне закончена"


def test_message_without_chat_id_is_rejected():
    payload = fx.message_created()
    payload["message"]["recipient"] = {"chat_type": "chat"}

    with pytest.raises(NormalizationError):
        normalizer.normalize(payload)


def test_message_event_without_message_block_is_rejected():
    with pytest.raises(NormalizationError):
        normalizer.normalize({"update_type": "message_created", "timestamp": fx.TS})


def test_dedup_key_stable_for_repeated_delivery():
    first = normalizer.normalize(fx.unknown_update())[0]
    second = normalizer.normalize(fx.unknown_update())[0]

    assert first.dedup_key == second.dedup_key


def test_dedup_key_differs_for_different_content():
    first = normalizer.normalize(fx.unknown_update())[0]
    other = fx.unknown_update()
    other["emoji"] = "🔥"
    second = normalizer.normalize(other)[0]

    assert first.dedup_key != second.dedup_key
