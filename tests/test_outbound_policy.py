"""Границы автономности (раздел 6 ТЗ).

Проверяется главное правило: самостоятельные действия разрешены
**за исключением вопросов стоимости и сроков**.
"""

from __future__ import annotations

import pytest

from repairbot.outbound.policy import (
    AUTONOMOUS_INTENTS,
    CONFIRMATION_INTENTS,
    Audience,
    Intent,
    Verdict,
    evaluate,
    looks_like_complaint,
    scan,
)


def _decide(intent: Intent, text: str, audience: Audience = Audience.CLIENT, **kw):
    return evaluate(intent, audience=audience, text=text, discloses_automation=True, **kw)


# --- разделение намерений по ТЗ ---


def test_every_intent_is_classified():
    assert AUTONOMOUS_INTENTS | CONFIRMATION_INTENTS == set(Intent)
    assert not (AUTONOMOUS_INTENTS & CONFIRMATION_INTENTS)


@pytest.mark.parametrize(
    "intent",
    [Intent.FINANCIAL, Intent.DEADLINE_TO_CLIENT, Intent.COMPLAINT_RESPONSE,
     Intent.LEGAL, Intent.EXTERNAL_CORRESPONDENCE, Intent.DATA_DELETION],
)
def test_confirmation_intents_are_held(intent):
    """Раздел 6: финансы, сроки, претензии, юридическое, внешняя переписка,
    удаление данных — только с подтверждением."""
    assert _decide(intent, "нейтральный текст").verdict is Verdict.HOLD


def test_status_update_to_client_is_allowed():
    """Ответы заказчику о текущем статусе работ — без подтверждения."""
    decision = _decide(Intent.STATUS_UPDATE, "Штукатурка на кухне завершена, идём дальше")

    assert decision.verdict is Verdict.ALLOW


def test_internal_notice_is_allowed():
    decision = _decide(
        Intent.INTERNAL_NOTICE, "Завтра нужен второй маляр", audience=Audience.STAFF
    )

    assert decision.verdict is Verdict.ALLOW


# --- вторая линия: сканер содержания ---


def test_money_in_status_update_escalates():
    """Намерение назначает модель, и она может ошибиться.

    Сумма в «сообщении о статусе» всё равно поднимает решение до
    подтверждения — ограничение живёт в контролёре, а не в промпте.
    """
    decision = _decide(Intent.STATUS_UPDATE, "Всё идёт по плану, доплата составит 15 000 руб")

    assert decision.verdict is Verdict.HOLD
    assert any("денежные" in r for r in decision.reasons)


def test_date_in_status_update_escalates():
    decision = _decide(Intent.STATUS_UPDATE, "Работы идут, закончим 15.09")

    assert decision.verdict is Verdict.HOLD
    assert any("даты" in r or "сроки" in r for r in decision.reasons)


def test_promise_escalates():
    decision = _decide(Intent.STATUS_UPDATE, "Гарантируем качество на пять лет")

    assert decision.verdict is Verdict.HOLD


def test_legal_wording_escalates():
    decision = _decide(Intent.STATUS_UPDATE, "По договору мы обязаны только это")

    assert decision.verdict is Verdict.HOLD


def test_schedule_digest_may_contain_dates():
    """График на ближайшие дни разрешён — это пересказ известного плана."""
    decision = _decide(Intent.SCHEDULE_DIGEST, "На этой неделе: 12.09 стяжка, 14.09 плитка")

    assert decision.verdict is Verdict.ALLOW


def test_schedule_digest_with_promise_still_escalates():
    """Обещание внутри сводки превращает её в обязательство перед заказчиком."""
    decision = _decide(Intent.SCHEDULE_DIGEST, "На неделе плитка, гарантируем сдачу в срок")

    assert decision.verdict is Verdict.HOLD


def test_money_to_staff_is_not_escalated():
    """Внутреннему адресату сумма не опасна: он её и так знает."""
    decision = _decide(
        Intent.INTERNAL_NOTICE, "Выдали 15 000 руб на материалы", audience=Audience.STAFF
    )

    assert decision.verdict is Verdict.ALLOW


def test_money_to_supplier_is_escalated():
    """Поставщик — внешний адресат, как и заказчик."""
    decision = _decide(
        Intent.CLARIFYING_QUESTION, "Подтвердите цену 15 000 руб", audience=Audience.SUPPLIER
    )

    assert decision.verdict is Verdict.HOLD


# --- представление системы ---


def test_first_contact_without_disclosure_is_held():
    """Раздел 6: явное представление системы как автоматической при первом контакте."""
    decision = evaluate(
        Intent.STATUS_UPDATE,
        audience=Audience.CLIENT,
        text="Здравствуйте! Работы идут по плану.",
        is_first_contact=True,
        discloses_automation=False,
    )

    assert decision.verdict is Verdict.HOLD
    assert any("автоматическ" in r for r in decision.reasons)


def test_first_contact_with_disclosure_is_allowed():
    decision = evaluate(
        Intent.STATUS_UPDATE,
        audience=Audience.CLIENT,
        text="Здравствуйте! Это автоматический помощник компании. Работы идут по плану.",
        is_first_contact=True,
        discloses_automation=True,
    )

    assert decision.verdict is Verdict.ALLOW


def test_disclosure_not_required_for_staff():
    decision = evaluate(
        Intent.INTERNAL_NOTICE,
        audience=Audience.STAFF,
        text="Напоминание о выходе бригады",
        is_first_contact=True,
        discloses_automation=False,
    )

    assert decision.verdict is Verdict.ALLOW


# --- сканер ---


@pytest.mark.parametrize(
    "text",
    ["3400 руб", "15 000 ₽", "стоимость работ", "смета выросла", "нужна доплата", "5 тыс"],
)
def test_scanner_finds_money(text):
    assert scan(text).money is True


@pytest.mark.parametrize("text", ["до 15.09", "к 3 сентября", "завтра", "через 5 дней"])
def test_scanner_finds_dates(text):
    assert scan(text).dates is True


def test_scanner_ignores_neutral_text():
    flags = scan("Бригада вышла, работаем в кухне")

    assert not flags.any


def test_scanner_records_what_matched():
    """В журнале аудита должно быть видно, что именно сработало."""
    flags = scan("доплата 15 000 руб до 15.09")

    assert flags.matches
    assert any(m.startswith("money:") for m in flags.matches)


def test_decision_carries_reasons_and_triggers():
    decision = _decide(Intent.STATUS_UPDATE, "доплата 15 000 руб")

    assert decision.reasons
    assert decision.triggered_by


# --- распознавание претензии во входящем ---


@pytest.mark.parametrize(
    "text",
    ["Это просто брак!", "Я недоволен работой", "Верните деньги", "буду писать претензию"],
)
def test_complaint_detected(text):
    assert looks_like_complaint(text) is True


def test_ordinary_message_is_not_a_complaint():
    assert looks_like_complaint("Когда приедет плитка?") is False
