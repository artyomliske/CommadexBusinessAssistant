"""Образцы апдейтов MAX в формате, который присылает платформа."""

from __future__ import annotations

from typing import Any

TS = 1_772_000_000_000  # мс эпохи


def message_created(
    *,
    text: str | None = "Штукатурка на кухне закончена",
    mid: str = "mid.001",
    chat_id: int = -100_500,
    user_id: int = 777,
    attachments: list[dict[str, Any]] | None = None,
    reply_to: str | None = None,
    minutes_later: int = 0,
) -> dict[str, Any]:
    ts = TS + minutes_later * 60_000
    message: dict[str, Any] = {
        "sender": {
            "user_id": user_id,
            "name": "Иван Петров",
            "username": "ivan_p",
            "is_bot": False,
        },
        "recipient": {"chat_id": chat_id, "chat_type": "chat"},
        "timestamp": ts,
        "body": {"mid": mid, "seq": 1, "text": text, "attachments": attachments or []},
    }
    if reply_to:
        message["link"] = {"type": "reply", "message": {"mid": reply_to, "text": "исходное"}}
    return {"update_type": "message_created", "timestamp": ts, "message": message}


def dialog_message(*, user_id: int = 42, mid: str = "mid.dlg") -> dict[str, Any]:
    """Личный диалог: chat_id отсутствует, есть только user_id."""
    return {
        "update_type": "message_created",
        "timestamp": TS,
        "message": {
            "sender": {"user_id": user_id, "first_name": "Мария", "last_name": "С."},
            "recipient": {"chat_type": "dialog", "user_id": user_id},
            "timestamp": TS,
            "body": {"mid": mid, "seq": 9, "text": "Когда приедет плитка?"},
        },
    }


def bot_added(*, chat_id: int = -100_500, user_id: int = 12) -> dict[str, Any]:
    return {
        "update_type": "bot_added",
        "timestamp": TS,
        "chat_id": chat_id,
        "user": {"user_id": user_id, "name": "Прораб Сергей", "is_bot": False},
        "is_channel": False,
    }


def bot_started(*, chat_id: int = 555, user_id: int = 555) -> dict[str, Any]:
    return {
        "update_type": "bot_started",
        "timestamp": TS,
        "chat_id": chat_id,
        "user": {"user_id": user_id, "name": "Заказчик"},
        "payload": "obj_17",
        "user_locale": "ru",
    }


def message_removed(*, chat_id: int = -100_500, mid: str = "mid.001") -> dict[str, Any]:
    return {
        "update_type": "message_removed",
        "timestamp": TS,
        "chat_id": chat_id,
        "message_id": mid,
        "user_id": 777,
    }


def chat_title_changed(*, chat_id: int = -100_500) -> dict[str, Any]:
    return {
        "update_type": "chat_title_changed",
        "timestamp": TS,
        "chat_id": chat_id,
        "user": {"user_id": 12, "name": "Прораб Сергей"},
        "title": "Объект Ленина 5 — бригада",
    }


def message_callback(*, chat_id: int = -100_500) -> dict[str, Any]:
    return {
        "update_type": "message_callback",
        "timestamp": TS,
        "callback": {
            "timestamp": TS,
            "callback_id": "cb.9911",
            "payload": "approve:draft_31",
            "user": {"user_id": 12, "name": "Менеджер"},
        },
        "message": {
            "sender": {"user_id": 1, "name": "Бот", "is_bot": True},
            "recipient": {"chat_id": chat_id, "chat_type": "chat"},
            "timestamp": TS,
            "body": {"mid": "mid.draft", "seq": 4, "text": "Черновик ответа заказчику"},
        },
    }


def with_receipt(*, mid: str = "mid.receipt") -> dict[str, Any]:
    return message_created(
        text="Чек за грунтовку",
        mid=mid,
        attachments=[
            {
                "type": "image",
                "payload": {"photo_id": 9081, "token": "tok_img_1", "url": "https://cdn.max.ru/1.jpg"},
                "width": 1200,
                "height": 1600,
            },
            {
                "type": "file",
                "payload": {"fileId": 5511, "token": "tok_file_1", "url": "https://cdn.max.ru/a.pdf"},
                "filename": "смета.pdf",
                "size": 240_512,
            },
        ],
    )


def with_contact(*, phone: str = "+79990001122") -> dict[str, Any]:
    return message_created(
        text=None,
        mid="mid.contact",
        attachments=[
            {
                "type": "contact",
                "payload": {
                    "vcfInfo": "BEGIN:VCARD...",
                    "maxInfo": {"user_id": 777, "name": "Иван Петров", "phone": phone},
                },
            }
        ],
    )


def long_polling_envelope() -> dict[str, Any]:
    """Формат long polling: несколько апдейтов в одном ответе."""
    return {
        "updates": [message_created(mid="mid.a"), message_created(mid="mid.b", text="Второе")],
        "marker": 991,
    }


def unknown_update() -> dict[str, Any]:
    """Новый тип апдейта, о котором код ещё не знает."""
    return {
        "update_type": "reaction_added",
        "timestamp": TS,
        "chat_id": -100_500,
        "user_id": 777,
        "emoji": "👍",
    }
