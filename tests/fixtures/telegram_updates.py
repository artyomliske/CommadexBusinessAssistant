"""Образцы апдейтов Telegram в том виде, в каком их присылает платформа."""

from __future__ import annotations

from typing import Any

TS = 1_772_000_000  # секунды эпохи, в отличие от миллисекунд у MAX


def _user(user_id: int = 777, **kw: Any) -> dict[str, Any]:
    user = {"id": user_id, "is_bot": False, "first_name": "Иван", "last_name": "Петров"}
    user.update(kw)
    return user


def _group(chat_id: int = -1001234567890) -> dict[str, Any]:
    return {"id": chat_id, "type": "supergroup", "title": "Объект Ленина 5 — бригада"}


def _private(user_id: int = 42) -> dict[str, Any]:
    return {"id": user_id, "type": "private", "first_name": "Мария", "last_name": "С."}


def message(
    *,
    text: str | None = "Штукатурка на кухне закончена",
    message_id: int = 5,
    chat: dict[str, Any] | None = None,
    update_id: int = 100,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": message_id,
        "from": _user(),
        "chat": chat or _group(),
        "date": TS,
    }
    if text is not None:
        body["text"] = text
    body.update(extra)
    return {"update_id": update_id, "message": body}


def edited(*, text: str = "Штукатурка закончена (исправлено)") -> dict[str, Any]:
    payload = message(text=text)
    body = payload.pop("message")
    body["edit_date"] = TS + 60
    return {"update_id": payload["update_id"], "edited_message": body}


def private_question(text: str = "Когда закончите с кухней?") -> dict[str, Any]:
    return message(text=text, chat=_private(), message_id=9)


def start_command() -> dict[str, Any]:
    return message(text="/start obj_17", chat=_private(), message_id=1)


def with_photo(caption: str | None = "Чек за грунтовку") -> dict[str, Any]:
    return message(
        text=None,
        message_id=11,
        caption=caption,
        photo=[
            {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 120,
             "file_size": 1200},
            {"file_id": "large", "file_unique_id": "u2", "width": 1200, "height": 1600,
             "file_size": 240_000},
        ],
    )


def with_document() -> dict[str, Any]:
    return message(
        text=None,
        message_id=12,
        caption="Смета",
        document={
            "file_id": "doc-1",
            "file_unique_id": "u3",
            "file_name": "смета.pdf",
            "mime_type": "application/pdf",
            "file_size": 240_512,
        },
    )


def with_contact(phone: str = "+79990001122", user_id: int = 777) -> dict[str, Any]:
    return message(
        text=None,
        message_id=13,
        contact={"phone_number": phone, "first_name": "Иван", "user_id": user_id},
    )


def bot_added(status: str = "member") -> dict[str, Any]:
    return {
        "update_id": 200,
        "my_chat_member": {
            "chat": _group(),
            "from": _user(user_id=12, first_name="Прораб", last_name="Сергей"),
            "date": TS,
            "old_chat_member": {"user": {"id": 1, "is_bot": True}, "status": "left"},
            "new_chat_member": {"user": {"id": 1, "is_bot": True}, "status": status},
        },
    }


def bot_removed() -> dict[str, Any]:
    return bot_added(status="kicked")


def title_changed(title: str = "Объект Ленина 5 — сдан") -> dict[str, Any]:
    return message(text=None, message_id=14, new_chat_title=title)


def user_joined() -> dict[str, Any]:
    return message(
        text=None,
        message_id=15,
        new_chat_members=[_user(user_id=99, first_name="Новый", last_name="Плиточник")],
    )


def callback() -> dict[str, Any]:
    return {
        "update_id": 300,
        "callback_query": {
            "id": "cb.9911",
            "from": _user(user_id=12, first_name="Менеджер"),
            "message": {
                "message_id": 20,
                "from": {"id": 1, "is_bot": True, "first_name": "Бот"},
                "chat": _group(),
                "date": TS,
                "text": "Черновик ответа заказчику",
            },
            "data": "approve:draft_31",
        },
    }


def unknown_update() -> dict[str, Any]:
    """Тип апдейта, о котором код ещё не знает."""
    return {"update_id": 400, "poll_answer": {"poll_id": "p1", "option_ids": [0]}}
