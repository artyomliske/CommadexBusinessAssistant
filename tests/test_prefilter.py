"""Предварительный классификатор: что доходит до модели, а что нет."""

from __future__ import annotations

import pytest

from repairbot.agents.prefilter import Verdict, classify
from repairbot.channels.max import normalizer
from tests.fixtures import max_updates as fx


def _classify_text(text: str | None, **kwargs):
    (event,) = normalizer.normalize(fx.message_created(text=text, **kwargs))
    return classify(event)


@pytest.mark.parametrize(
    "text",
    ["ок", "Хорошо", "принято", "спасибо!", "+", "Добрый день", "да", "?"],
)
def test_acknowledgements_do_not_reach_model(text):
    assert _classify_text(text).verdict is Verdict.SKIP


@pytest.mark.parametrize(
    "text",
    [
        "Штукатурка на кухне закончена",
        "купил грунтовку 2 мешка за 3400",
        "перенесли на 15.08",
        "заказчик жалуется на трещину в санузле",
        "не хватает плитки, нужно докупить",
        "бригада выйдет завтра",
        "оплатите счёт до конца недели",
    ],
)
def test_meaningful_messages_reach_model(text):
    assert _classify_text(text).verdict is Verdict.EXTRACT


def test_attachment_without_text_goes_to_documents():
    (event,) = normalizer.normalize(fx.with_receipt())
    event = event.model_copy(update={"text": None})

    decision = classify(event)

    assert decision.verdict is Verdict.DOCUMENT_ONLY


def test_empty_message_without_attachments_is_skipped():
    decision = _classify_text(None)

    assert decision.verdict is Verdict.SKIP


def test_bot_messages_are_skipped():
    """Защита от циклов при взаимодействии с другими ботами (раздел 6 ТЗ)."""
    payload = fx.message_created(text="Автоматическое уведомление о статусе")
    payload["message"]["sender"]["is_bot"] = True

    (event,) = normalizer.normalize(payload)

    assert classify(event).verdict is Verdict.SKIP


def test_short_message_with_number_still_reaches_model():
    """«5 мешков» короткое, но это факт."""
    assert _classify_text("5 мешков").verdict is Verdict.EXTRACT


def test_date_is_enough_to_extract():
    assert _classify_text("до 20.09").verdict is Verdict.EXTRACT
    assert _classify_text("к 3 сентября").verdict is Verdict.EXTRACT


def test_reply_in_thread_reaches_model():
    """Ответ продолжает содержательный разговор."""
    decision = _classify_text("не получится так", reply_to="mid.parent")

    assert decision.verdict is Verdict.EXTRACT


def test_long_phrase_without_markers_reaches_model():
    """Сомнение разрешается в пользу разбора: пропущенный факт дороже вызова."""
    decision = _classify_text("пусть хозяин сам решает этот вопрос при встрече")

    assert decision.verdict is Verdict.EXTRACT
    assert decision.reason == "развёрнутая фраза"


def test_short_chatter_without_markers_is_skipped():
    assert _classify_text("ну ладно").verdict is Verdict.SKIP


def test_decision_carries_reason():
    decision = _classify_text("оплата прошла")

    assert decision.reason
    assert decision.needs_model is True
