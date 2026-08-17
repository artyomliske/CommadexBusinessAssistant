"""Проход распознавания документов (этап 3).

Отдельно от архива, и это не случайно. Архив должен быть быстрым и
безотказным: он раскладывает файлы и не зависит ни от модели, ни от её
доступности. Распознавание идёт следом, читает уже сохранённую копию с
Диска и может падать сколько угодно — файл при этом уже у заказчика.

Читаем с Диска, а не из мессенджера: ссылка на CDN живёт неизвестно
сколько, а копия постоянна. Это же позволяет перечитать документы заново
после доработки промптов — достаточно сбросить `doc_class`.

Порция мелкая. Каждый документ — это вызов модели со зрением, самый
дорогой из всех, что мы делаем, и укладываться надо в отведённое задаче
время, а не пытаться разобрать весь архив за один заход.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents import journal
from repairbot.agents.documents import (
    DocumentAgent,
    Recognition,
    as_extraction,
    guess_media_type,
    needs_careful_reading,
    sniff_media_type,
)
from repairbot.agents.object_state import rebuild_object_state
from repairbot.db.models import (
    AttachmentRecord,
    ChannelIdentity,
    Event,
    LlmCall,
    Message,
    RepairObject,
)
from repairbot.integrations.drive.archive import build_filename
from repairbot.llm.base import LlmError, LlmMediaUnsupported, LlmRefusal
from repairbot.observability import get_logger

log = get_logger(__name__)

UNRECOGNIZABLE = "unrecognizable"
"""Значение `doc_class` для того, что распознать невозможно в принципе.

Отличается от отказа модели: формат не тот, файла нет. Второй раз такое
не берём."""

FAILED = "failed"
"""Модель не смогла прочесть документ. Виден человеку в панели."""


class DriveReader(Protocol):
    async def download(self, file_id: str, *, max_bytes: int | None = None) -> bytes: ...


class ChannelReader(Protocol):
    """Скачивание прямо из мессенджера, по ссылке из вложения."""

    async def download(self, url: str, *, max_bytes: int) -> bytes: ...


@dataclass(slots=True)
class RecognitionResult:
    recognized: int = 0
    facts: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recognized": self.recognized,
            "facts": self.facts,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors[:5],
        }


class DocumentPass:
    def __init__(
        self,
        agent: DocumentAgent,
        drive: DriveReader | None = None,
        *,
        max_bytes: int,
        batch_size: int = 10,
        channel: ChannelReader | None = None,
        fast_model: str | None = None,
    ) -> None:
        self._agent = agent
        self._fast_model = fast_model
        """Дешёвая модель для первого прохода.

        Фотоотчётов больше половины, а читать их дорогой моделью — то
        же, что нанимать бухгалтера подписывать фотографии. Дорогая
        включается только там, где на кону деньги и дешёвая не уверена;
        решает это `needs_careful_reading`."""
        self._drive = drive
        self._max_bytes = max_bytes
        self._batch = batch_size
        self._channel = channel
        """Запасной источник файла: сам мессенджер.

        Основной — копия на Диске: она постоянна, и по ней документ
        можно перечитать заново после доработки промптов. Но пока Диск
        не подключён, копий нет вовсе, и без запасного пути ни один
        документ не разбирается — счета и чеки лежат в мессенджере
        нетронутыми. Ссылка там живёт недолго, поэтому запасной путь
        именно запасной: читаем, пока файл ещё доступен."""

    async def run(self, session: AsyncSession, *, limit: int | None = None) -> RecognitionResult:
        """Разобрать очередную порцию сохранённых документов."""
        result = RecognitionResult()
        rows = await session.execute(
            select(AttachmentRecord, Message, RepairObject)
            .join(Message, Message.id == AttachmentRecord.message_id)
            # Внешнее соединение: документ без объекта тоже надо прочесть.
            # Он уже лежит на Диске в общей папке, и именно распознавание
            # даёт ему осмысленное имя.
            .join(RepairObject, RepairObject.id == Message.object_id, isouter=True)
            .where(
                # Читать можно с Диска либо, если его нет, прямо из
                # мессенджера. Требовать копию на Диске значило бы не
                # разбирать ничего, пока Google не подключён.
                or_(
                    AttachmentRecord.drive_file_id.isnot(None),
                    AttachmentRecord.source_url.isnot(None),
                ),
                # Разобранное второй раз не берём. Чтобы перечитать заново
                # после доработки промптов, достаточно сбросить doc_class.
                AttachmentRecord.doc_class.is_(None),
            )
            .order_by(AttachmentRecord.id)
            .limit(limit or self._batch)
        )

        touched_objects: set[int] = set()

        for attachment, message, obj in rows.all():
            media_type = guess_media_type(
                attachment.mime_type, attachment.filename, attachment.kind
            )
            if media_type is None:
                attachment.doc_class = UNRECOGNIZABLE
                await session.flush()
                result.skipped += 1
                continue

            try:
                recognition = await self._recognize(attachment, message, obj, media_type)
            except LlmMediaUnsupported as exc:
                # Резерв не читает изображения. Не помечаем: основной
                # провайдер вернётся, и документ разберётся сам.
                log.info("documents.postponed", attachment_id=attachment.id, reason=str(exc))
                result.skipped += 1
                break
            except LlmRefusal as exc:
                await self._mark_failed(
                    session, attachment, message, f"модель отклонила разбор: {exc.category}"
                )
                result.failed += 1
                continue
            except LlmError as exc:
                await self._mark_failed(session, attachment, message, str(exc))
                result.failed += 1
                result.errors.append(f"вложение {attachment.id}: {exc}")
                continue

            stored = await self._store(session, attachment, message, recognition)
            await self._rename_by_reading(session, attachment, message, recognition)
            result.recognized += 1
            result.facts += stored
            if stored and message.object_id is not None:
                touched_objects.add(message.object_id)

        # Состояние пересобираем по разу на объект, а не на каждый факт:
        # свёртка читает весь журнал, и десять пересборок подряд означали
        # бы десять полных проходов ради одной и той же картины.
        for object_id in touched_objects:
            await rebuild_object_state(session, object_id)

        log.info("documents.pass", **result.as_dict())
        return result

    async def _recognize(
        self,
        attachment: AttachmentRecord,
        message: Message,
        obj: RepairObject | None,
        media_type: str,
    ) -> Recognition:
        content = await self._fetch(attachment)

        # Тип по содержимому важнее догадки по виду вложения: MAX шлёт
        # фотографии без имени и без mime, а подставленный наугад
        # `image/jpeg` модель отвергает — «файл похож на image/webp».
        sniffed = sniff_media_type(content)
        if sniffed is not None and sniffed != media_type:
            log.info(
                "documents.media_type_corrected",
                attachment_id=attachment.id,
                guessed=media_type,
                actual=sniffed,
            )
            media_type = sniffed

        async def read(model: str | None) -> Recognition:
            return await self._agent.recognize(
                content,
                media_type=media_type,
                filename=attachment.filename,
                message_text=message.text,
                object_code=obj.code if obj is not None else None,
                model=model,
            )

        if self._fast_model is None:
            return await read(None)

        first = await read(self._fast_model)
        if not needs_careful_reading(first.reading):
            return first

        # Перечитываем дорогой моделью. Лишние шесть центов дешевле, чем
        # неверная сумма, попавшая в бюджет объекта.
        log.info(
            "documents.reread",
            attachment_id=attachment.id,
            doc_class=first.reading.doc_class,
            confidence=first.reading.confidence,
        )
        careful = await read(None)
        careful.first_pass = first.response
        return careful

    async def _fetch(self, attachment: AttachmentRecord) -> bytes:
        """Достать файл: сначала с Диска, потом из мессенджера."""
        if self._drive is not None and attachment.drive_file_id:
            return await self._drive.download(
                str(attachment.drive_file_id), max_bytes=self._max_bytes
            )
        if self._channel is not None and attachment.source_url:
            return await self._channel.download(
                str(attachment.source_url), max_bytes=self._max_bytes
            )
        raise LlmError("файл недоступен: нет ни копии на Диске, ни ссылки в мессенджере")

    async def _store(
        self,
        session: AsyncSession,
        attachment: AttachmentRecord,
        message: Message,
        recognition: Recognition,
    ) -> int:
        source = await self._source_event(session, message)
        extraction = as_extraction(recognition)

        stored = 0
        if source is not None:
            for index, fact in enumerate(extraction.facts):
                payload = fact.model_dump(mode="json", exclude_none=True)
                payload["source"] = {
                    "kind": "document",
                    "attachment_id": attachment.id,
                    "drive_file_id": attachment.drive_file_id,
                    "doc_class": recognition.reading.doc_class,
                    "model": recognition.response.model,
                    "schema": self._agent.schema_fingerprint,
                    "degraded": recognition.degraded,
                }
                if recognition.reading.vendor:
                    payload["vendor"] = recognition.reading.vendor
                stored += int(
                    await journal.append_fact(
                        session,
                        source=source,
                        fact=fact,
                        payload=payload,
                        # Ключ по вложению, а не по событию: у одного
                        # сообщения бывает несколько файлов.
                        dedup_key=f"doc:{attachment.id}:{index}",
                    )
                )

            if extraction.needs_human:
                await journal.append_needs_human(
                    session,
                    source=source,
                    reason=extraction.note or "документ требует внимания менеджера",
                    dedup_key=f"doc_needs_human:{attachment.id}",
                )

        attachment.doc_class = recognition.reading.doc_class
        payload = dict(attachment.payload or {})
        payload["reading"] = recognition.reading.model_dump(mode="json", exclude_none=True)
        attachment.payload = payload

        if recognition.first_pass is not None:
            # Платим за оба прохода. Не записать первый значит занизить
            # расход ровно там, где его и смотрят.
            session.add(
                LlmCall(
                    object_id=message.object_id,
                    source_event_id=source.id if source is not None else None,
                    purpose="document",
                    provider=recognition.first_pass.provider,
                    model=recognition.first_pass.model[:64],
                    input_tokens=recognition.first_pass.input_tokens,
                    cached_input_tokens=recognition.first_pass.cached_input_tokens,
                    output_tokens=recognition.first_pass.output_tokens,
                    degraded=recognition.first_pass.degraded,
                )
            )

        session.add(
            LlmCall(
                object_id=message.object_id,
                source_event_id=source.id if source is not None else None,
                purpose="document",
                provider=recognition.response.provider,
                model=recognition.response.model[:64],
                input_tokens=recognition.response.input_tokens,
                cached_input_tokens=recognition.response.cached_input_tokens,
                output_tokens=recognition.response.output_tokens,
                degraded=recognition.degraded,
                facts_extracted=len(extraction.facts),
            )
        )
        await session.flush()
        return stored

    async def _rename_by_reading(
        self,
        session: AsyncSession,
        attachment: AttachmentRecord,
        message: Message,
        recognition: Recognition,
    ) -> None:
        """Уточнить имя файла на Диске по прочитанному.

        В момент загрузки известна только подпись под файлом, а её часто
        нет вовсе: фотографию присылают молча. После разбора известно,
        что это за документ, — и «IMG_20260814_121314.jpg» превращается в
        «Чек, Леруа Мерлен, 12 480 ₽».

        Переименование необязательное. Отказ Диска не должен отменять
        разбор: факты уже записаны, а имя — украшение поверх них.
        """
        reading = recognition.reading
        if reading.unreadable or not (reading.summary or "").strip():
            return
        if not attachment.drive_file_id:
            # Копии на Диске нет — переименовывать нечего. Документ при
            # этом прочитан: файл брали прямо из мессенджера.
            return
        renamer = getattr(self._drive, "rename", None)
        if renamer is None:  # pragma: no cover - у читателя может не быть записи
            return

        author = await self._author_name(session, message)
        name = build_filename(attachment, message, author, description=reading.summary.strip())
        try:
            await renamer(str(attachment.drive_file_id), name)
        except Exception as exc:
            log.warning("documents.rename_failed", attachment_id=attachment.id, error=str(exc))

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
        self,
        session: AsyncSession,
        attachment: AttachmentRecord,
        message: Message,
        reason: str,
    ) -> None:
        """Пометить документ неразобранным и показать его человеку.

        Помечаем окончательно: повторять один и тот же неудачный разбор
        каждые несколько минут значило бы платить за модель без надежды
        на другой ответ. Снять пометку можно, сбросив `doc_class`.
        """
        attachment.doc_class = FAILED
        payload = dict(attachment.payload or {})
        payload["reading_error"] = reason[:300]
        attachment.payload = payload

        source = await self._source_event(session, message)
        if source is not None:
            await journal.append_needs_human(
                session,
                source=source,
                reason=f"Не удалось разобрать вложение: {reason}"[:500],
                dedup_key=f"doc_failed:{attachment.id}",
            )
        await session.flush()
        log.warning("documents.failed", attachment_id=attachment.id, reason=reason)

    async def _source_event(self, session: AsyncSession, message: Message) -> Event | None:
        """Событие, к которому привязать факты документа.

        Берём самое раннее событие сообщения: правки приходят следом, а
        факт относится к моменту, когда файл появился.
        """
        return (
            await session.execute(
                select(Event)
                .where(Event.source_message_id == message.id)
                .order_by(Event.id)
                .limit(1)
            )
        ).scalar_one_or_none()
