"""Файловый архив объектов на Диске (этап 3, раздел 5 ТЗ).

Фотоотчёты, чеки и замеры приходят вложениями в мессенджер и живут там
ровно столько, сколько платформа считает нужным. Архив перекладывает их на
Диск заказчика, где они переживут и переписку, и самого бота.

Что здесь важно понимать про идемпотентность. Признак «файл уже на Диске» —
это `drive_file_id` в базе, а не факт наличия файла на Диске. Порядок
поэтому такой: загрузили, записали идентификатор, зафиксировали. Обрыв
между загрузкой и записью оставит на Диске лишний файл, и это осознанный
выбор — лучше дубликат, чем потерянный чек. Метка `attachment_id` в
свойствах файла позволяет такие дубликаты потом найти.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import AttachmentRecord, ChannelIdentity, Message, RepairObject
from repairbot.domain.events import AttachmentKind
from repairbot.integrations.drive.client import DriveClient, DriveError, DriveUnavailable
from repairbot.observability import get_logger

log = get_logger(__name__)

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
"""Предел на одно вложение. Видео с объекта бывает большим, но всё, что
крупнее сотни мегабайт, — почти наверняка не отчёт о работах."""

SKIPPED_KINDS: frozenset[str] = frozenset({
    AttachmentKind.STICKER.value,
    AttachmentKind.CONTACT.value,
    AttachmentKind.LOCATION.value,
})
"""Стикеры архивировать незачем, а у контактов и геометок нет файла."""

ARCHIVE_KEY = "archive"
"""Ключ в payload вложения, куда пишется окончательный отказ."""

INBOX_KEY = "inbox"
"""Пометка: файл лёг в общую папку, потому что объект был неизвестен.

Снимается, когда объект появляется и файл переезжает к нему. Без
пометки перекладывать было бы нечего — по одному лишь наличию объекта
не отличить файл, лежащий не там, от файла, лежащего где надо."""

INBOX_FOLDER = "Без объекта"
"""Куда складывать файлы, пока объект сообщения не известен.

Чаты у заказчика общие, объект узнаётся из текста, и часть сообщений
остаётся неразобранной. Раньше вложения таких сообщений не попадали на
Диск вообще: запрос требовал объекта. Ссылка на файл в мессенджере
живёт недолго, поэтому теперь складываем сразу, а разложим потом."""


class Downloader(Protocol):
    async def download(self, url: str, *, max_bytes: int) -> bytes: ...


@dataclass(slots=True)
class ArchiveResult:
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors[:10],
        }


class FileArchive:
    def __init__(
        self,
        drive: DriveClient,
        downloader: Downloader,
        *,
        root_folder_id: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._drive = drive
        self._downloader = downloader
        self._root = root_folder_id
        self._max_bytes = max_bytes
        self._inbox: str | None = None

    async def archive_pending(
        self, session: AsyncSession, *, limit: int = 50
    ) -> ArchiveResult:
        """Переложить на Диск вложения, которых там ещё нет."""
        result = ArchiveResult()
        if not self._root:
            raise DriveError(
                "Не задан GOOGLE_DRIVE_FOLDER_ID — некуда складывать архив объектов."
            )

        rows = await session.execute(
            select(AttachmentRecord, Message, RepairObject)
            .join(Message, Message.id == AttachmentRecord.message_id)
            # Внешнее соединение: объект у сообщения может быть неизвестен,
            # и раньше такие вложения не архивировались вовсе. При общих
            # чатах это большинство файлов, а ссылка на них в мессенджере
            # живёт недолго — собираем всё, разложим потом.
            .join(RepairObject, RepairObject.id == Message.object_id, isouter=True)
            .where(
                AttachmentRecord.drive_file_id.is_(None),
                AttachmentRecord.source_url.isnot(None),
                AttachmentRecord.kind.notin_(SKIPPED_KINDS),
                # Вложения, по которым уже вынесен окончательный отказ,
                # второй раз не трогаем: иначе каждая задача снова
                # упиралась бы в тот же неподъёмный файл.
                not_(AttachmentRecord.payload.has_key(ARCHIVE_KEY)),
            )
            .order_by(AttachmentRecord.id)
            .limit(limit)
        )

        for attachment, message, obj in rows.all():
            try:
                stored = await self._archive_one(session, attachment, message, obj)
            except DriveUnavailable as exc:
                # Временная беда: не помечаем, повторим в следующий заход.
                result.failed += 1
                result.errors.append(str(exc))
                log.warning("archive.postponed", attachment_id=attachment.id, error=str(exc))
                break
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"вложение {attachment.id}: {exc}")
                await self._mark_failed(session, attachment, exc)
                log.error("archive.failed", attachment_id=attachment.id, error=str(exc))
                continue

            if stored:
                result.uploaded += 1
            else:
                result.skipped += 1

        log.info("archive.run", **result.as_dict())
        return result

    async def _archive_one(
        self,
        session: AsyncSession,
        attachment: AttachmentRecord,
        message: Message,
        obj: RepairObject | None,
    ) -> bool:
        content = await self._downloader.download(
            str(attachment.source_url), max_bytes=self._max_bytes
        )
        if not content:
            raise DriveError("пустой файл")

        folder_id = await self._ensure_object_folder(session, obj)
        author = await self._author_name(session, message)

        properties = {"attachment_id": str(attachment.id)}
        if obj is not None:
            properties["object_code"] = obj.code

        uploaded = await self._drive.upload(
            name=build_filename(attachment, message, author),
            content=content,
            folder_id=folder_id,
            mime_type=attachment.mime_type,
            app_properties=properties,
        )

        attachment.drive_file_id = uploaded.file_id
        attachment.stored_at = datetime.now(tz=UTC)
        if attachment.size_bytes is None:
            attachment.size_bytes = uploaded.size_bytes
        if obj is None:
            # Помечаем, что файл лежит в общей папке: когда объект
            # появится, его надо будет переложить.
            payload = dict(attachment.payload or {})
            payload[INBOX_KEY] = True
            attachment.payload = payload
        # Запись должна дойти до базы до того, как задача сочтётся
        # выполненной: иначе следующий заход загрузит файл повторно.
        await session.flush()
        return True

    async def _ensure_object_folder(
        self, session: AsyncSession, obj: RepairObject | None
    ) -> str:
        if obj is None:
            return await self._inbox_folder()
        if obj.drive_folder_id:
            return obj.drive_folder_id

        folder_id = await self._drive.ensure_folder(folder_name(obj), self._root)
        obj.drive_folder_id = folder_id
        await session.flush()
        return folder_id

    async def _inbox_folder(self) -> str:
        """Общая папка. Ищется один раз за проход, а не на каждый файл."""
        if self._inbox is None:
            self._inbox = await self._drive.ensure_folder(INBOX_FOLDER, self._root)
        return self._inbox

    async def refile_pending(self, session: AsyncSession, *, limit: int = 50) -> int:
        """Переложить в папки объектов то, что легло в общую.

        Объект у сообщения появляется позже — разбором в панели или
        новым названием в справочнике. Без этого шага файл остался бы в
        общей папке навсегда, и папка объекта была бы неполной.
        """
        rows = await session.execute(
            select(AttachmentRecord, Message, RepairObject)
            .join(Message, Message.id == AttachmentRecord.message_id)
            .join(RepairObject, RepairObject.id == Message.object_id)
            .where(
                AttachmentRecord.drive_file_id.isnot(None),
                AttachmentRecord.payload.has_key(INBOX_KEY),
            )
            .order_by(AttachmentRecord.id)
            .limit(limit)
        )

        moved = 0
        inbox = None
        for attachment, message, obj in rows.all():
            if inbox is None:
                inbox = await self._inbox_folder()
            target = await self._ensure_object_folder(session, obj)
            author = await self._author_name(session, message)
            description = _reading_summary(attachment)
            try:
                await self._drive.rename(
                    str(attachment.drive_file_id),
                    build_filename(attachment, message, author, description=description),
                    move_to=target,
                    move_from=inbox,
                )
            except DriveError as exc:
                log.warning("archive.refile_failed", attachment_id=attachment.id, error=str(exc))
                continue

            payload = dict(attachment.payload or {})
            payload.pop(INBOX_KEY, None)
            attachment.payload = payload
            await session.flush()
            moved += 1

        if moved:
            log.info("archive.refiled", moved=moved)
        return moved

    async def _author_name(self, session: AsyncSession, message: Message) -> str | None:
        if message.author_identity_id is None:
            return None
        return (
            await session.execute(
                select(ChannelIdentity.display_name).where(
                    ChannelIdentity.id == message.author_identity_id
                )
            )
        ).scalar_one_or_none()

    async def _mark_failed(
        self, session: AsyncSession, attachment: AttachmentRecord, exc: Exception
    ) -> None:
        """Записать окончательный отказ в payload вложения.

        Не в отдельную колонку: причин отказа немного, они разнородны, и
        менять схему ради них незачем. Запись видна в панели вместе с
        остальными сведениями о вложении.

        Сбрасываем сразу: пометка, оставшаяся только в памяти, пропадёт
        при откате сессии, и следующий заход снова упрётся в тот же файл.
        """
        payload = dict(attachment.payload or {})
        payload[ARCHIVE_KEY] = {
            "error": f"{exc.__class__.__name__}: {exc}"[:300],
            "at": datetime.now(tz=UTC).isoformat(),
        }
        attachment.payload = payload
        await session.flush()


def _reading_summary(attachment: AttachmentRecord) -> str | None:
    """Что распознавание сказало о документе. Одной строкой.

    Появляется только после разбора, поэтому при первой загрузке имя
    строится по подписи, а уточняется потом.
    """
    reading = (attachment.payload or {}).get("reading")
    if not isinstance(reading, dict):
        return None
    summary = (reading.get("summary") or "").strip()
    return summary or None


def folder_name(obj: RepairObject) -> str:
    """Имя папки объекта: код и адрес.

    Код первым, чтобы папки сортировались предсказуемо, а адрес рядом —
    иначе заказчик не поймёт, что такое obj_17, открыв Диск через полгода.
    """
    return _sanitize(f"{obj.code} — {obj.address}")[:120]


CAPTION_LIMIT = 70
"""Сколько символов подписи попадает в имя файла.

Длиннее — и имя перестаёт читаться в списке Диска, а подпись под фото
бывает на абзац."""


def build_filename(
    attachment: AttachmentRecord,
    message: Message,
    author: str | None,
    *,
    description: str | None = None,
) -> str:
    """Имя файла: дата, автор, описание, расширение.

    Дата первой и в сортируемом виде: содержимое папки тогда
    выстраивается по ходу работ само, без сортировки в интерфейсе Диска.

    Описание берётся, по убыванию полезности: то, что вычитано из самого
    документа (появляется после распознавания), затем подпись под файлом
    в чате, затем исходное имя. «IMG_20260814_121314.jpg» не говорит
    ничего — а «Чек за грунтовку» ищется через полгода.
    """
    sent_at = message.sent_at or datetime.now(tz=UTC)
    parts = [sent_at.strftime("%Y-%m-%d %H%M")]
    if author:
        parts.append(author)

    original = (attachment.filename or "").strip()
    label = description or caption(message) or original
    if not label:
        label = attachment.kind

    # Расширение из исходного имени сохраняем: без него Диск и
    # операционная система заказчика перестают понимать, чем открывать.
    suffix = _suffix(original) or _extension(attachment)
    if suffix and label.lower().endswith(suffix.lower()):
        suffix = ""

    return _sanitize(f"{' · '.join(parts)} — {label}{suffix}")[:200]


def caption(message: Message) -> str | None:
    """Подпись под файлом: первая строка сообщения.

    Дальше первой строки в подписи обычно уже не про файл, а про работы.
    """
    text = (message.text or "").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip()
    if len(first) > CAPTION_LIMIT:
        first = first[:CAPTION_LIMIT].rsplit(" ", 1)[0] + "…"
    return first or None


def _suffix(filename: str) -> str:
    """Расширение исходного имени, если оно похоже на расширение."""
    _, dot, tail = filename.rpartition(".")
    if not dot or not tail or len(tail) > 5 or not tail.isalnum():
        return ""
    return f".{tail}"


def _extension(attachment: AttachmentRecord) -> str:
    if attachment.kind == AttachmentKind.IMAGE.value:
        return ".jpg"
    if attachment.kind == AttachmentKind.VIDEO.value:
        return ".mp4"
    if attachment.kind == AttachmentKind.AUDIO.value:
        return ".ogg"
    return ""


_FORBIDDEN = re.compile(r"[/\\\x00-\x1f]+")


def _sanitize(name: str) -> str:
    """Убрать из имени то, что ломает файловые системы и выгрузки.

    Диск косую черту в имени допускает, но при скачивании папки такой
    файл превращается в неожиданный подкаталог.
    """
    return _FORBIDDEN.sub(" ", name).strip() or "файл"
