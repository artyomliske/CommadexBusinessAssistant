"""Файлы в панели: ссылки на Диск заказчика.

Копий у себя система не держит — архив принадлежит заказчику. Значит,
единственное, что панель может дать, это верную ссылку. Неверная ссылка
хуже её отсутствия: она выглядит рабочей.
"""

from __future__ import annotations

from datetime import UTC, datetime

from repairbot.db.models import AttachmentRecord, Message, RepairObject
from repairbot.web import objects


async def _archived(session, *, linked: bool = True, **kw) -> AttachmentRecord:
    obj = RepairObject(code="obj_1", address="Ленина 5")
    session.add(obj)
    await session.flush()

    message = Message(
        channel="max",
        channel_chat_id="-1",
        channel_message_id=f"mid.{kw.get('drive_file_id', 'x')}",
        object_id=obj.id if linked else None,
        sent_at=datetime(2026, 3, 14, 9, 5, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()

    attachment = AttachmentRecord(
        message_id=message.id,
        kind=kw.pop("kind", "file"),
        filename=kw.pop("filename", "чек.pdf"),
        drive_file_id=kw.pop("drive_file_id", "drive-1"),
        stored_at=datetime(2026, 3, 14, 9, 6, tzinfo=UTC),
        **kw,
    )
    session.add(attachment)
    await session.flush()
    return attachment


# --- адреса ---


def test_folder_url_is_built_from_the_id():
    url = objects.folder_url("1AbC")

    assert url == "https://drive.google.com/drive/folders/1AbC"


def test_file_url_is_built_from_the_id():
    assert objects.file_url("1AbC") == "https://drive.google.com/file/d/1AbC/view"


def test_no_id_means_no_link():
    """Пустая ссылка ведёт на страницу ошибки Google — лучше без неё."""
    assert objects.folder_url(None) is None
    assert objects.folder_url("") is None
    assert objects.file_url(None) is None


# --- список ---


async def test_archived_file_is_listed_with_a_link(db_session):
    await _archived(db_session)

    cards = await objects.load_files(db_session)

    assert len(cards) == 1
    assert cards[0].url == "https://drive.google.com/file/d/drive-1/view"
    assert cards[0].object_address == "Ленина 5"


async def test_file_still_in_the_messenger_is_not_listed(db_session):
    """Пока файла нет на Диске, показывать нечего и ссылка вела бы никуда."""
    await _archived(db_session, drive_file_id=None)

    assert await objects.load_files(db_session) == []


async def test_file_without_an_object_is_marked(db_session):
    """Такой лежит в общей папке и ждёт разбора — это видно в списке."""
    await _archived(db_session, linked=False, payload={"inbox": True})

    card = (await objects.load_files(db_session))[0]

    assert card.in_inbox is True
    assert card.object_address is None


async def test_recognized_summary_is_shown_instead_of_the_filename(db_session):
    """«scan_001.pdf» ни о чём не говорит, «Чек, Леруа Мерлен» — говорит."""
    await _archived(
        db_session,
        filename="scan_001.pdf",
        doc_class="receipt",
        payload={"reading": {"summary": "Чек, Леруа Мерлен, 12 480 ₽"}},
    )

    card = (await objects.load_files(db_session))[0]

    assert card.summary == "Чек, Леруа Мерлен, 12 480 ₽"
    assert card.name == "scan_001.pdf"


async def test_broken_reading_does_not_break_the_page(db_session):
    """В payload может лежать что угодно: он не проверяется схемой."""
    await _archived(db_session, payload={"reading": "не словарь"})

    card = (await objects.load_files(db_session))[0]

    assert card.summary is None


# --- карточка объекта ---


async def test_object_card_links_to_its_drive_folder(db_session):
    obj = RepairObject(code="obj_1", address="Ленина 5", drive_folder_id="folder-1")
    db_session.add(obj)
    await db_session.flush()

    card = (await objects.load_objects(db_session))[0]

    assert card.drive_url == "https://drive.google.com/drive/folders/folder-1"


async def test_object_without_a_folder_has_no_link(db_session):
    """Папка заводится при первом файле. До него ссылке взяться неоткуда."""
    db_session.add(RepairObject(code="obj_1", address="Ленина 5"))
    await db_session.flush()

    card = (await objects.load_objects(db_session))[0]

    assert card.drive_url is None
    assert card.files == 0


async def test_object_card_counts_its_files(db_session):
    await _archived(db_session)

    card = (await objects.load_objects(db_session))[0]

    assert card.files == 1
