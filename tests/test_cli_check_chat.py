"""Проверка прав бота в чатах MAX: чтение одного чата и итог по всем.

Право `read_all_messages` выдаётся руками в каждой группе. Без него бот
видит только адресованные ему сообщения, то есть система выглядит
работающей и молча ничего не находит — поэтому проверка обязана
называть непригодный чат непригодным, а не просто печатать ответ API.
"""

from __future__ import annotations

import pytest

from repairbot.cli import _check_one_chat, _print_chat_summary


class _Client:
    def __init__(self, membership=None, chat=None, error=None, chat_error=None):
        self._membership = membership or {}
        self._chat = chat or {}
        self._error = error
        self._chat_error = chat_error

    async def get_chat_membership(self, chat_id: str) -> dict:
        if self._error:
            raise self._error
        return self._membership

    async def get_chat(self, chat_id: str) -> dict:
        if self._chat_error:
            raise self._chat_error
        return self._chat


class _Adapter:
    def __init__(self, client):
        self.client = client


def _adapter(**kw) -> _Adapter:
    return _Adapter(_Client(**kw))


# --- один чат ---


async def test_chat_with_the_permission_is_ready():
    adapter = _adapter(
        membership={"permissions": ["read_all_messages", "write"]},
        chat={"title": "Ленина 5", "type": "chat"},
    )

    report = await _check_one_chat(adapter, "-100500")

    assert report["read_all_messages"] is True
    assert report["ready"] is True
    assert report["title"] == "Ленина 5"


async def test_bot_in_the_chat_without_the_permission_is_not_ready():
    """Участие без права — самый опасный случай: похоже на успех."""
    adapter = _adapter(membership={"permissions": []}, chat={"title": "Ленина 5"})

    report = await _check_one_chat(adapter, "-100500")

    assert report["bot_is_member"] is True
    assert report["ready"] is False


async def test_bot_outside_the_chat_reports_the_reason():
    adapter = _adapter(error=RuntimeError("404 chat not found"))

    report = await _check_one_chat(adapter, "-100500")

    assert report["bot_is_member"] is False
    assert report["ready"] is False
    assert "404" in report["error"]


async def test_unavailable_title_does_not_hide_the_permission():
    """Название — украшение. Из-за него проверка прав пропасть не должна."""
    adapter = _adapter(
        membership={"permissions": ["read_all_messages"]},
        chat_error=RuntimeError("500"),
    )

    report = await _check_one_chat(adapter, "-100500")

    assert report["ready"] is True
    assert "chat_info_error" in report


# --- итог по всем ---


@pytest.fixture
def reports() -> list[dict]:
    return [
        {"chat_id": "-1", "ready": True, "title": "Ленина 5"},
        {"chat_id": "-2", "ready": False, "title": "Мира 12"},
    ]


def test_summary_names_the_chat_that_needs_hands(reports, capsys):
    _print_chat_summary(reports)

    out = capsys.readouterr().out
    assert "-2" in out
    assert "администратором" in out


def test_summary_counts_only_the_blocked_ones(reports, capsys):
    _print_chat_summary(reports)

    assert "Без права read_all_messages: 1" in capsys.readouterr().out


def test_summary_is_silent_about_next_steps_when_all_are_ready(reports, capsys):
    for report in reports:
        report["ready"] = True

    _print_chat_summary(reports)

    out = capsys.readouterr().out
    assert "Все чаты подключены" in out
    assert "администратором" not in out
