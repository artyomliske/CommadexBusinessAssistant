"""Распознавание документов: прочтение, факты и границы допустимого."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from repairbot.agents.documents import (
    DocClass,
    DocumentAgent,
    DocumentReading,
    Recognition,
    as_extraction,
    facts_from,
    guess_media_type,
)
from repairbot.domain.facts import FactType, WorkStatus
from repairbot.llm.base import LlmInvalidOutput, StructuredResponse


class FakeRouter:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload
        self.requests: list[Any] = []

    async def complete_structured(self, request) -> StructuredResponse:
        self.requests.append(request)
        if isinstance(self._payload, Exception):
            raise self._payload
        return StructuredResponse(
            provider="claude", model="claude-opus-5", payload=self._payload
        )


def _reading(**overrides: Any) -> DocumentReading:
    payload: dict[str, Any] = {
        "doc_class": DocClass.RECEIPT,
        "confidence": 0.92,
        "summary": "Чек из строительного магазина",
        "vendor": "Леруа",
        "doc_date": date(2026, 3, 14),
        "total": 12450.0,
        "currency": "RUB",
        "lines": [
            {"item": "Грунтовка", "qty": 2, "unit": "шт", "amount": 1200.0},
            {"item": "Штукатурка", "qty": 20, "unit": "мешок", "amount": 11250.0},
        ],
    }
    payload.update(overrides)
    return DocumentReading.model_validate(payload)


# --- чек в факт ---


def test_receipt_becomes_one_purchase_fact():
    """Десять строк одной накладной — это одна трата, а не десять.

    Журнал ведётся о деньгах объекта; позиции остаются в описании.
    """
    facts = facts_from(_reading())

    assert len(facts) == 1
    fact = facts[0]
    assert fact.type is FactType.PURCHASE
    assert fact.amount == 12450.0
    assert fact.currency == "RUB"
    assert fact.due_date == date(2026, 3, 14)
    assert "Леруа" in fact.summary


def test_main_item_is_the_most_expensive_line():
    """Первая строка чека часто оказывается доставкой или мелочью."""
    facts = facts_from(_reading())

    assert facts[0].item == "Штукатурка"


def test_receipt_without_a_total_yields_nothing():
    """Сумма — единственное, ради чего чек разбирали."""
    facts = facts_from(_reading(total=None))

    assert facts == []


def test_unreadable_document_yields_nothing():
    facts = facts_from(_reading(unreadable=True))

    assert facts == []


def test_low_confidence_receipt_goes_to_a_human():
    """Финансовый порог 0,8: распознавание мятой бумаги ошибается в цифрах."""
    facts = facts_from(_reading(confidence=0.7))

    assert facts[0].below_threshold


def test_confident_receipt_is_applied_automatically():
    facts = facts_from(_reading(confidence=0.95))

    assert not facts[0].below_threshold


def test_photo_report_records_progress_but_not_completion():
    """Видно, что работа идёт, — но не видно, что она закончена."""
    facts = facts_from(
        _reading(
            doc_class=DocClass.PHOTO,
            total=None,
            lines=[],
            stage="штукатурка",
            room="кухня",
            summary="Оштукатуренная стена на кухне",
        )
    )

    assert len(facts) == 1
    assert facts[0].type is FactType.WORK_PROGRESS
    assert facts[0].status is WorkStatus.IN_PROGRESS
    assert facts[0].stage == "штукатурка"


def test_photo_without_a_recognisable_stage_yields_nothing():
    """«Какая-то стена» фактом о ходе работ не является."""
    facts = facts_from(
        _reading(doc_class=DocClass.PHOTO, total=None, lines=[], stage=None, room=None)
    )

    assert facts == []


def test_contracts_stay_in_the_archive():
    """Договор не о ходе работ: место ему в архиве, а не в журнале."""
    for doc_class in (DocClass.CONTRACT, DocClass.ACT, DocClass.OTHER):
        assert facts_from(_reading(doc_class=doc_class, total=None, lines=[])) == []


def test_measurement_keeps_the_room():
    facts = facts_from(
        _reading(
            doc_class=DocClass.MEASUREMENT,
            total=None,
            lines=[],
            room="санузел",
            summary="Замер санузла: 1,8 × 2,1 м",
        )
    )

    assert facts[0].type is FactType.MEASUREMENT
    assert facts[0].room == "санузел"


# --- сведение к общему виду ---


def _recognition(reading: DocumentReading) -> Recognition:
    response = StructuredResponse(provider="claude", model="claude-opus-5", payload={})
    return Recognition(reading=reading, response=response, facts=facts_from(reading))


def test_unreadable_document_asks_for_a_human():
    extraction = as_extraction(_recognition(_reading(unreadable=True)))

    assert extraction.needs_human
    assert extraction.facts == []


def test_receipt_without_a_total_asks_for_a_human():
    extraction = as_extraction(_recognition(_reading(total=None)))

    assert extraction.needs_human
    assert "сумма" in (extraction.note or "")


def test_ordinary_photo_does_not_ask_for_a_human():
    extraction = as_extraction(
        _recognition(_reading(doc_class=DocClass.PHOTO, total=None, lines=[], stage="плитка"))
    )

    assert not extraction.needs_human


# --- вызов модели ---


async def test_image_is_passed_to_the_model():
    router = FakeRouter(_reading().model_dump(mode="json"))
    agent = DocumentAgent(router)

    await agent.recognize(b"\x89PNG", media_type="image/png", object_code="obj_17")

    request = router.requests[0]
    assert len(request.media) == 1
    assert request.media[0].media_type == "image/png"
    assert request.media[0].data == b"\x89PNG"
    assert "obj_17" in request.volatile_system


async def test_caption_is_offered_as_a_hint_not_as_truth():
    """Подпись «грунтовка на кухню» помогает, но суммы берутся с картинки."""
    router = FakeRouter(_reading().model_dump(mode="json"))

    await DocumentAgent(router).recognize(
        b"img", media_type="image/jpeg", message_text="Чек за грунтовку, 12 450"
    )

    context = router.requests[0].volatile_system
    assert "Чек за грунтовку" in context
    assert "подсказка" in context.lower()


async def test_oversized_file_is_refused_before_the_call():
    router = FakeRouter(_reading().model_dump(mode="json"))
    agent = DocumentAgent(router, max_bytes=10)

    with pytest.raises(LlmInvalidOutput, match="предела распознавания"):
        await agent.recognize(b"x" * 100, media_type="image/jpeg")

    assert router.requests == []


async def test_unsupported_format_is_refused():
    agent = DocumentAgent(FakeRouter({}))

    with pytest.raises(LlmInvalidOutput, match="не распознаётся"):
        await agent.recognize(b"x", media_type="video/mp4")


# --- определение формата ---


@pytest.mark.parametrize(
    ("mime", "filename", "kind", "expected"),
    [
        ("image/png", None, "image", "image/png"),
        (None, "чек.PDF", "file", "application/pdf"),
        (None, "фото.jpeg", "file", "image/jpeg"),
        # Фотография из мессенджера приходит без имени и без mime.
        (None, None, "image", "image/jpeg"),
        (None, "смета.xlsx", "file", None),
        ("video/mp4", None, "video", None),
    ],
)
def test_media_type_is_guessed_from_what_is_available(mime, filename, kind, expected):
    assert guess_media_type(mime, filename, kind) == expected


def test_document_agent_can_use_its_own_model():
    """Цена ошибки у распознавания другая, чем у разбора сообщений.

    Неверно прочитанная сумма попадает в бюджет объекта; невнятный разбор
    сообщения просто уйдёт человеку. Поэтому модель для документов можно
    поднять отдельно, не переплачивая на потоке в полторы тысячи
    сообщений в сутки.
    """
    router = FakeRouter(_reading().model_dump(mode="json"))
    agent = DocumentAgent(router, model="anthropic/claude-opus-5")

    import asyncio

    asyncio.run(agent.recognize(b"img", media_type="image/jpeg"))

    assert router.requests[0].model == "anthropic/claude-opus-5"


def test_without_an_override_the_shared_model_is_used():
    router = FakeRouter(_reading().model_dump(mode="json"))

    import asyncio

    asyncio.run(DocumentAgent(router).recognize(b"img", media_type="image/jpeg"))

    assert router.requests[0].model is None


# --- какой моделью читать ---


def _seen(**kw):
    from repairbot.agents.documents import DocumentReading

    payload = {
        "doc_class": DocClass.PHOTO,
        "confidence": 0.95,
        "summary": "Фото санузла",
        "unreadable": False,
    }
    payload.update(kw)
    return DocumentReading.model_validate(payload)


def test_photo_report_is_not_worth_the_expensive_model():
    """Ошибка в слове «санузел» стоит недоразумения, а не денег."""
    from repairbot.agents.documents import needs_careful_reading

    assert not needs_careful_reading(_seen(doc_class=DocClass.PHOTO, confidence=0.6))


def test_confidently_read_receipt_is_not_reread():
    """Переплата за уверенно прочитанный чек ничего не покупает."""
    from repairbot.agents.documents import needs_careful_reading

    assert not needs_careful_reading(_seen(doc_class=DocClass.RECEIPT, confidence=0.95))


def test_uncertain_receipt_is_reread_carefully():
    """Неверная сумма попадёт в бюджет объекта — шесть центов дешевле."""
    from repairbot.agents.documents import needs_careful_reading

    assert needs_careful_reading(_seen(doc_class=DocClass.RECEIPT, confidence=0.7))


def test_unreadable_receipt_is_reread_even_if_confident():
    """«Уверенно не разобрал» — повод дать документ модели получше."""
    from repairbot.agents.documents import needs_careful_reading

    assert needs_careful_reading(
        _seen(doc_class=DocClass.RECEIPT, confidence=0.99, unreadable=True)
    )


def test_contract_and_act_are_treated_as_financial():
    from repairbot.agents.documents import needs_careful_reading

    assert needs_careful_reading(_seen(doc_class=DocClass.CONTRACT, confidence=0.5))
    assert needs_careful_reading(_seen(doc_class=DocClass.ACT, confidence=0.5))


# --- формат по содержимому ---


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF89a", "image/gif"),
        (b"%PDF-1.7", "application/pdf"),
    ],
)
def test_format_is_read_from_the_file_itself(head, expected):
    from repairbot.agents.documents import sniff_media_type

    assert sniff_media_type(head) == expected


def test_webp_is_recognized_by_two_markers():
    """MAX присылает фото в webp без имени и без mime.

    Подставленный наугад «image/jpeg» модель отвергает: «указан
    image/jpeg, но файл похож на image/webp» — и документ не читается.
    """
    from repairbot.agents.documents import sniff_media_type

    assert sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_riff_that_is_not_webp_is_not_claimed():
    """RIFF — это ещё и звук. Назвать его картинкой значит соврать."""
    from repairbot.agents.documents import sniff_media_type

    assert sniff_media_type(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None


def test_unknown_content_is_left_to_the_guess():
    from repairbot.agents.documents import sniff_media_type

    assert sniff_media_type(b"zzzz") is None
    assert sniff_media_type(b"") is None
