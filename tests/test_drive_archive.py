"""Файловый архив на Диске заказчика (этап 3).

Имена файлов и папок проверяются отдельно от базы — это чистые функции.
Перекладывание требует Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from repairbot.db.models import (
    AttachmentRecord,
    ChannelIdentity,
    Message,
    RepairObject,
)
from repairbot.integrations.drive.archive import (
    ARCHIVE_KEY,
    INBOX_FOLDER,
    INBOX_KEY,
    FileArchive,
    build_filename,
    caption,
    folder_name,
)
from repairbot.integrations.drive.client import DriveError, DriveUnavailable, UploadedFile


class FakeDrive:
    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.folders: dict[tuple[str, str], str] = {}
        self.folder_calls = 0
        self.renames: list[dict[str, Any]] = []
        self._fail_with = fail_with

    async def rename(self, file_id, name, *, move_to=None, move_from=None) -> None:
        self.renames.append(
            {"file_id": file_id, "name": name, "move_to": move_to, "move_from": move_from}
        )

    async def ensure_folder(self, name: str, parent_id: str) -> str:
        self.folder_calls += 1
        key = (name, parent_id)
        self.folders.setdefault(key, f"folder-{len(self.folders) + 1}")
        return self.folders[key]

    async def upload(self, **kw: Any) -> UploadedFile:
        if self._fail_with is not None:
            raise self._fail_with
        self.uploads.append(kw)
        return UploadedFile(
            file_id=f"file-{len(self.uploads)}",
            name=kw["name"],
            size_bytes=len(kw["content"]),
        )


class FakeDownloader:
    def __init__(self, content: bytes = b"binary", fail_with: Exception | None = None) -> None:
        self.content = content
        self.urls: list[str] = []
        self._fail_with = fail_with

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        if self._fail_with is not None:
            raise self._fail_with
        return self.content


async def _object_with_attachment(
    session,
    *,
    kind: str = "image",
    filename: str | None = "чек.pdf",
    url: str | None = "https://cdn.max.ru/1.jpg",
    author: str | None = "Иван Петров",
    linked: bool = True,
) -> tuple[RepairObject, AttachmentRecord]:
    obj = RepairObject(code="obj_17", address="Ленина 5, кв. 12")
    session.add(obj)
    await session.flush()

    identity_id = None
    if author:
        identity = ChannelIdentity(
            channel="max", channel_user_id="777", display_name=author
        )
        session.add(identity)
        await session.flush()
        identity_id = identity.id

    message = Message(
        channel="max",
        channel_chat_id="-100500",
        channel_message_id="mid.1",
        object_id=obj.id if linked else None,
        author_identity_id=identity_id,
        text="Чек за грунтовку",
        sent_at=datetime(2026, 3, 14, 9, 5, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()

    attachment = AttachmentRecord(
        message_id=message.id, kind=kind, filename=filename, source_url=url
    )
    session.add(attachment)
    await session.flush()
    return obj, attachment


# --- имена ---


def test_folder_name_puts_the_code_first():
    """Код первым — папки сортируются предсказуемо.

    Адрес рядом, иначе заказчик не поймёт, что такое obj_17, открыв Диск
    через полгода.
    """
    obj = RepairObject(code="obj_17", address="Ленина 5, кв. 12")

    assert folder_name(obj) == "obj_17 — Ленина 5, кв. 12"


def test_folder_name_survives_a_slash_in_the_address():
    """Косая черта при скачивании папки превращается в подкаталог."""
    obj = RepairObject(code="obj_3", address="Мира 7/2, кв. 4")

    assert "/" not in folder_name(obj)


def test_filename_starts_with_a_sortable_date():
    """Содержимое папки выстраивается по ходу работ само."""
    message = Message(
        channel="max",
        channel_chat_id="-1",
        channel_message_id="mid.1",
        sent_at=datetime(2026, 3, 14, 9, 5, tzinfo=UTC),
    )
    attachment = AttachmentRecord(message_id=1, kind="file", filename="смета.pdf")

    name = build_filename(attachment, message, "Иван Петров")

    assert name.startswith("2026-03-14 0905")
    assert "Иван Петров" in name
    assert name.endswith("смета.pdf")


def test_filename_invents_an_extension_when_there_is_none():
    """Фотографии из мессенджера приходят без имени файла."""
    message = Message(
        channel="max",
        channel_chat_id="-1",
        channel_message_id="mid.1",
        sent_at=datetime(2026, 3, 14, 9, 5, tzinfo=UTC),
    )
    attachment = AttachmentRecord(message_id=1, kind="image", filename=None)

    assert build_filename(attachment, message, None).endswith("image.jpg")


def _message(text: str | None = None) -> Message:
    return Message(
        channel="max",
        channel_chat_id="-1",
        channel_message_id="mid.1",
        text=text,
        sent_at=datetime(2026, 3, 14, 9, 5, tzinfo=UTC),
    )


def test_caption_becomes_the_filename():
    """«IMG_20260314_090500.jpg» через полгода не ищется, «Чек» — ищется."""
    attachment = AttachmentRecord(message_id=1, kind="image", filename="IMG_0042.jpg")

    name = build_filename(attachment, _message("Чек за грунтовку"), None)

    assert "Чек за грунтовку" in name
    assert name.endswith(".jpg")


def test_only_the_first_line_of_the_caption_is_used():
    """Под фото пишут абзац, и он весь в имя файла не помещается."""
    text = "Санузел готов\nЗавтра приедет плиточник, нужен клей"

    assert caption(_message(text)) == "Санузел готов"


def test_a_long_caption_is_cut_on_a_word():
    long = "Привезли " + "плитку " * 20

    short = caption(_message(long))

    assert len(short) < 80
    assert short.endswith("…")
    assert "плитк" in short


def test_recognized_description_wins_over_the_caption():
    """Прочитанное с документа точнее, чем подпись «вот чек»."""
    attachment = AttachmentRecord(message_id=1, kind="file", filename="scan.pdf")

    name = build_filename(
        attachment, _message("вот чек"), None, description="Чек, Леруа Мерлен, 12 480 ₽"
    )

    assert "Леруа Мерлен" in name
    assert "вот чек" not in name


def test_extension_is_not_doubled():
    """Подпись «смета.pdf» не должна дать «смета.pdf.pdf»."""
    attachment = AttachmentRecord(message_id=1, kind="file", filename="смета.pdf")

    name = build_filename(attachment, _message("смета.pdf"), None)

    assert name.count(".pdf") == 1


def test_original_extension_survives_a_caption():
    """Без расширения заказчик не поймёт, чем открывать файл."""
    attachment = AttachmentRecord(message_id=1, kind="file", filename="scan_001.pdf")

    name = build_filename(attachment, _message("Договор с бригадой"), None)

    assert name.endswith("Договор с бригадой.pdf")


def test_caption_of_an_empty_message_is_nothing():
    assert caption(_message(None)) is None
    assert caption(_message("   ")) is None


# --- перекладывание ---


async def test_attachment_lands_in_the_object_folder(db_session):
    obj, attachment = await _object_with_attachment(db_session)
    drive, downloader = FakeDrive(), FakeDownloader("содержимое".encode())

    result = await FileArchive(drive, downloader, root_folder_id="root").archive_pending(
        db_session
    )

    assert result.uploaded == 1
    upload = drive.uploads[0]
    assert upload["folder_id"] == "folder-1"
    assert upload["content"] == "содержимое".encode()
    assert upload["app_properties"]["attachment_id"] == str(attachment.id)

    await db_session.refresh(attachment)
    assert attachment.drive_file_id == "file-1"
    assert attachment.stored_at is not None
    await db_session.refresh(obj)
    assert obj.drive_folder_id == "folder-1"


async def test_folder_is_created_once_per_object(db_session):
    """Повторный поиск папки при каждом вложении — лишние запросы к Диску."""
    await _object_with_attachment(db_session)
    message = (await db_session.execute(select(Message))).scalar_one()
    for i in range(3):
        db_session.add(
            AttachmentRecord(
                message_id=message.id,
                kind="image",
                source_url=f"https://cdn.max.ru/{i}.jpg",
            )
        )
    await db_session.flush()

    drive = FakeDrive()
    await FileArchive(drive, FakeDownloader(), root_folder_id="root").archive_pending(
        db_session
    )

    assert len(drive.uploads) == 4
    assert drive.folder_calls == 1


async def test_already_archived_attachment_is_not_uploaded_twice(db_session):
    """Признак — drive_file_id в базе. Повторный заход должен ничего не найти."""
    await _object_with_attachment(db_session)
    archive = FileArchive(FakeDrive(), FakeDownloader(), root_folder_id="root")

    first = await archive.archive_pending(db_session)
    second = await archive.archive_pending(db_session)

    assert first.uploaded == 1
    assert second.uploaded == 0


async def test_attachment_without_an_object_goes_to_the_shared_folder(db_session):
    """Объект неизвестен — но файл всё равно надо забрать.

    Чаты у заказчика общие, и часть сообщений остаётся неразобранной.
    Раньше такие вложения не попадали на Диск вовсе, а ссылка на файл в
    мессенджере живёт недолго — то есть файл терялся навсегда.
    """
    _, attachment = await _object_with_attachment(db_session, linked=False)
    drive = FakeDrive()

    result = await FileArchive(drive, FakeDownloader(), root_folder_id="root").archive_pending(
        db_session
    )

    assert result.uploaded == 1
    assert drive.uploads[0]["folder_id"] == drive.folders[(INBOX_FOLDER, "root")]
    assert attachment.payload[INBOX_KEY] is True


async def test_shared_folder_file_has_no_object_property(db_session):
    """Свойство `object_code` — признак разложенного файла. Врать нельзя."""
    await _object_with_attachment(db_session, linked=False)
    drive = FakeDrive()

    await FileArchive(drive, FakeDownloader(), root_folder_id="root").archive_pending(db_session)

    assert "object_code" not in drive.uploads[0]["app_properties"]


async def test_file_moves_to_the_object_once_it_is_known(db_session):
    """Объект появляется позже — разбором в панели или новым названием."""
    obj, attachment = await _object_with_attachment(db_session, linked=False)
    drive = FakeDrive()
    archive = FileArchive(drive, FakeDownloader(), root_folder_id="root")
    await archive.archive_pending(db_session)

    message = await db_session.get(Message, attachment.message_id)
    message.object_id = obj.id
    await db_session.flush()

    moved = await archive.refile_pending(db_session)

    assert moved == 1
    assert drive.renames[0]["move_to"] == drive.folders[(folder_name(obj), "root")]
    assert drive.renames[0]["move_from"] == drive.folders[(INBOX_FOLDER, "root")]
    assert INBOX_KEY not in (attachment.payload or {})


async def test_a_filed_attachment_is_not_moved_again(db_session):
    """Иначе каждый заход трогал бы на Диске все файлы подряд."""
    obj, attachment = await _object_with_attachment(db_session, linked=False)
    drive = FakeDrive()
    archive = FileArchive(drive, FakeDownloader(), root_folder_id="root")
    await archive.archive_pending(db_session)
    message = await db_session.get(Message, attachment.message_id)
    message.object_id = obj.id
    await db_session.flush()
    await archive.refile_pending(db_session)

    assert await archive.refile_pending(db_session) == 0


async def test_stickers_are_not_archived(db_session):
    await _object_with_attachment(db_session, kind="sticker", filename=None)
    drive = FakeDrive()

    await FileArchive(drive, FakeDownloader(), root_folder_id="root").archive_pending(
        db_session
    )

    assert drive.uploads == []


async def test_permanent_failure_is_recorded_and_not_retried(db_session):
    """Иначе каждый заход упирался бы в тот же неподъёмный файл."""
    _, attachment = await _object_with_attachment(db_session)
    downloader = FakeDownloader(fail_with=ValueError("вложение слишком большое"))
    archive = FileArchive(FakeDrive(), downloader, root_folder_id="root")

    first = await archive.archive_pending(db_session)
    assert first.failed == 1

    await db_session.refresh(attachment)
    assert ARCHIVE_KEY in attachment.payload
    assert "слишком большое" in attachment.payload[ARCHIVE_KEY]["error"]

    # Второй заход его уже не берёт.
    second = await archive.archive_pending(db_session)
    assert second.failed == 0
    assert len(downloader.urls) == 1


async def test_temporary_drive_failure_stops_the_run_without_marking(db_session):
    """Квота или 5xx — не повод хоронить вложение.

    Проход прерывается целиком: если Диск отвечает отказом, следующие
    файлы упрутся в то же самое, а пометить их отказом было бы враньём.
    """
    _, attachment = await _object_with_attachment(db_session)
    drive = FakeDrive(fail_with=DriveUnavailable("Диск вернул 503"))

    result = await FileArchive(drive, FakeDownloader(), root_folder_id="root").archive_pending(
        db_session
    )

    assert result.failed == 1
    await db_session.refresh(attachment)
    assert ARCHIVE_KEY not in attachment.payload
    assert attachment.drive_file_id is None


async def test_archive_without_a_root_folder_refuses(db_session):
    await _object_with_attachment(db_session)

    with pytest.raises(DriveError, match="GOOGLE_DRIVE_FOLDER_ID"):
        await FileArchive(FakeDrive(), FakeDownloader(), root_folder_id="").archive_pending(
            db_session
        )
