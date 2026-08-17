"""Проход распознавания документов. Требуют Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from repairbot.agents.document_pass import FAILED, UNRECOGNIZABLE, DocumentPass
from repairbot.agents.documents import DocClass, DocumentAgent
from repairbot.db.models import AttachmentRecord, Event, LlmCall, Message, RepairObject
from repairbot.domain.events import FACT_EVENT_PREFIX, NEEDS_HUMAN_EVENT
from repairbot.llm.base import (
    LlmMediaUnsupported,
    LlmRefusal,
    LlmUnavailable,
    StructuredResponse,
)


class FakeRouter:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload
        self.calls = 0

    async def complete_structured(self, request) -> StructuredResponse:
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return StructuredResponse(
            provider="claude",
            model="claude-opus-5",
            payload=self._payload,
            input_tokens=1500,
            output_tokens=200,
        )


class FakeDrive:
    def __init__(self, content: bytes = b"\xff\xd8jpeg") -> None:
        self.content = content
        self.downloads: list[str] = []

    async def download(self, file_id: str, *, max_bytes: int | None = None) -> bytes:
        self.downloads.append(file_id)
        return self.content


def _receipt(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "doc_class": DocClass.RECEIPT,
        "confidence": 0.93,
        "summary": "Чек из строительного магазина",
        "vendor": "Леруа",
        "doc_date": "2026-03-14",
        "total": 12450.0,
        "currency": "RUB",
        "lines": [{"item": "Штукатурка", "qty": 20, "unit": "мешок", "amount": 11250.0}],
        "unreadable": False,
    }
    payload.update(overrides)
    return payload


def _pass(payload: dict[str, Any] | Exception, drive: FakeDrive | None = None) -> DocumentPass:
    return DocumentPass(
        DocumentAgent(FakeRouter(payload)),
        drive or FakeDrive(),
        max_bytes=5 * 1024 * 1024,
    )


async def _archived_attachment(
    session,
    *,
    kind: str = "image",
    filename: str | None = None,
    mime: str | None = None,
) -> AttachmentRecord:
    """Вложение, уже переложенное на Диск, — вход для распознавания."""
    obj = RepairObject(code="obj_17", address="Ленина 5, кв. 12")
    session.add(obj)
    await session.flush()

    message = Message(
        channel="max",
        channel_chat_id="-100500",
        channel_message_id="mid.1",
        object_id=obj.id,
        text="Чек за штукатурку",
        sent_at=datetime(2026, 3, 14, 9, 5, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()

    session.add(
        Event(
            channel="max",
            channel_chat_id="-100500",
            channel_message_id="mid.1",
            source_message_id=message.id,
            object_id=obj.id,
            event_type="message_created",
            payload={},
            dedup_key="ev.1",
            occurred_at=message.sent_at,
        )
    )

    attachment = AttachmentRecord(
        message_id=message.id,
        kind=kind,
        filename=filename,
        mime_type=mime,
        drive_file_id="file-1",
        stored_at=datetime.now(tz=UTC),
    )
    session.add(attachment)
    await session.flush()
    return attachment


async def test_receipt_becomes_a_fact_in_the_journal(db_session):
    attachment = await _archived_attachment(db_session)
    drive = FakeDrive()

    result = await _pass(_receipt(), drive).run(db_session)

    assert result.recognized == 1
    assert result.facts == 1
    assert drive.downloads == ["file-1"]

    fact = (
        await db_session.execute(
            select(Event).where(Event.event_type == f"{FACT_EVENT_PREFIX}purchase")
        )
    ).scalar_one()
    assert float(fact.confidence) == 0.93
    assert fact.applied is True
    assert fact.payload["amount"] == 12450.0
    assert fact.payload["source"]["attachment_id"] == attachment.id

    await db_session.refresh(attachment)
    assert attachment.doc_class == DocClass.RECEIPT
    assert attachment.payload["reading"]["vendor"] == "Леруа"


async def test_low_confidence_receipt_waits_for_a_human(db_session):
    """Финансовый порог 0,8 действует и для распознавания."""
    await _archived_attachment(db_session)

    await _pass(_receipt(confidence=0.7)).run(db_session)

    fact = (
        await db_session.execute(
            select(Event).where(Event.event_type == f"{FACT_EVENT_PREFIX}purchase")
        )
    ).scalar_one()
    assert fact.applied is False
    assert fact.needs_human is True


async def test_unreadable_receipt_goes_to_a_human_without_facts(db_session):
    attachment = await _archived_attachment(db_session)

    result = await _pass(_receipt(unreadable=True, total=None)).run(db_session)

    assert result.facts == 0
    needs_human = (
        await db_session.execute(
            select(Event).where(Event.event_type == NEEDS_HUMAN_EVENT)
        )
    ).scalar_one()
    assert "прочесть" in needs_human.payload["reason"]

    # Документ всё равно считается разобранным: второй раз не берём.
    await db_session.refresh(attachment)
    assert attachment.doc_class == DocClass.RECEIPT


async def test_recognized_document_is_not_processed_twice(db_session):
    """Каждый разбор — дорогой вызов модели со зрением."""
    await _archived_attachment(db_session)
    router = FakeRouter(_receipt())
    document_pass = DocumentPass(DocumentAgent(router), FakeDrive(), max_bytes=10**7)

    first = await document_pass.run(db_session)
    second = await document_pass.run(db_session)

    assert first.recognized == 1
    assert second.recognized == 0
    assert router.calls == 1


async def test_object_state_is_rebuilt_after_new_facts(db_session):
    obj_before = await _archived_attachment(db_session)

    await _pass(_receipt()).run(db_session)

    obj = (await db_session.execute(select(RepairObject))).scalar_one()
    assert obj.state.get("projection")
    assert obj_before.doc_class == DocClass.RECEIPT


async def test_unrecognizable_format_is_marked_and_skipped(db_session):
    """Таблица или архив: модель их не читает, и пробовать незачем."""
    attachment = await _archived_attachment(
        db_session, kind="file", filename="смета.xlsx"
    )
    router = FakeRouter(_receipt())

    result = await DocumentPass(
        DocumentAgent(router), FakeDrive(), max_bytes=10**7
    ).run(db_session)

    assert result.skipped == 1
    assert router.calls == 0
    await db_session.refresh(attachment)
    assert attachment.doc_class == UNRECOGNIZABLE


async def test_model_refusal_marks_the_document_and_calls_a_human(db_session):
    attachment = await _archived_attachment(db_session)

    result = await _pass(LlmRefusal("policy")).run(db_session)

    assert result.failed == 1
    await db_session.refresh(attachment)
    assert attachment.doc_class == FAILED

    needs_human = (
        await db_session.execute(
            select(Event).where(Event.event_type == NEEDS_HUMAN_EVENT)
        )
    ).scalar_one()
    assert "разобрать вложение" in needs_human.payload["reason"]


async def test_failed_document_is_not_retried(db_session):
    """Повторять неудачный разбор каждые десять минут — платить впустую."""
    await _archived_attachment(db_session)
    router = FakeRouter(LlmUnavailable("провайдер недоступен"))
    document_pass = DocumentPass(DocumentAgent(router), FakeDrive(), max_bytes=10**7)

    await document_pass.run(db_session)
    await document_pass.run(db_session)

    assert router.calls == 1


async def test_blind_reserve_postpones_instead_of_marking(db_session):
    """Резерв не читает картинки — документ подождёт основного провайдера.

    Пометить его отказом значило бы потерять чек из-за временной беды.
    """
    attachment = await _archived_attachment(db_session)

    result = await _pass(LlmMediaUnsupported("резерв не читает изображения")).run(
        db_session
    )

    assert result.failed == 0
    assert result.skipped == 1
    await db_session.refresh(attachment)
    assert attachment.doc_class is None


async def test_cost_of_recognition_is_recorded(db_session):
    """Распознавание — самый дорогой вызов, и в учёте он отдельной строкой."""
    await _archived_attachment(db_session)

    await _pass(_receipt()).run(db_session)

    call = (await db_session.execute(select(LlmCall))).scalar_one()
    assert call.purpose == "document"
    assert call.input_tokens == 1500


async def test_attachment_without_a_drive_copy_is_not_touched(db_session):
    """Распознавание читает с Диска: нечего читать — нечего разбирать."""
    attachment = await _archived_attachment(db_session)
    attachment.drive_file_id = None
    await db_session.flush()

    router = FakeRouter(_receipt())
    result = await DocumentPass(
        DocumentAgent(router), FakeDrive(), max_bytes=10**7
    ).run(db_session)

    assert result.recognized == 0
    assert router.calls == 0


# --- откуда берётся файл ---


class FakeChannel:
    """Мессенджер: отдаёт файл по ссылке из вложения."""

    def __init__(self, content: bytes = b"\xff\xd8jpeg") -> None:
        self.content = content
        self.urls: list[str] = []

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        return self.content


async def _unarchived_attachment(session) -> AttachmentRecord:
    """Вложение, которого на Диске нет: Google не подключён."""
    attachment = await _archived_attachment(session)
    attachment.drive_file_id = None
    attachment.source_url = "https://cdn.max.ru/1.jpg"
    await session.flush()
    return attachment


async def test_document_is_read_from_the_messenger_when_there_is_no_drive(db_session):
    """Без Google счета и чеки иначе не разбирались бы вовсе.

    Копия на Диске лучше — она постоянна и позволяет перечитать документ
    после доработки промптов. Но её отсутствие не повод не читать ничего.
    """
    await _unarchived_attachment(db_session)
    channel = FakeChannel()

    result = await DocumentPass(
        DocumentAgent(FakeRouter(_receipt())),
        None,
        max_bytes=5 * 1024 * 1024,
        channel=channel,
    ).run(db_session)

    assert result.recognized == 1
    assert channel.urls == ["https://cdn.max.ru/1.jpg"]


async def test_drive_is_preferred_over_the_messenger(db_session):
    """Ссылка в мессенджере живёт недолго, копия на Диске — постоянно."""
    attachment = await _archived_attachment(db_session)
    attachment.source_url = "https://cdn.max.ru/1.jpg"
    await db_session.flush()
    drive, channel = FakeDrive(), FakeChannel()

    await DocumentPass(
        DocumentAgent(FakeRouter(_receipt())),
        drive,
        max_bytes=5 * 1024 * 1024,
        channel=channel,
    ).run(db_session)

    assert drive.downloads == ["file-1"]
    assert channel.urls == [], "мессенджер трогать незачем"


async def test_attachment_without_any_source_is_marked_failed(db_session):
    """Ни копии, ни ссылки — читать нечего, и это надо показать человеку."""
    attachment = await _archived_attachment(db_session)
    attachment.drive_file_id = None
    attachment.source_url = None
    await db_session.flush()

    result = await DocumentPass(
        DocumentAgent(FakeRouter(_receipt())), None, max_bytes=5 * 1024 * 1024
    ).run(db_session)

    assert result.recognized == 0
