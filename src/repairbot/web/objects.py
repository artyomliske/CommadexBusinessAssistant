"""Объекты, их названия в переписке и разбор неузнанного.

Чаты у заказчика функциональные — «Заказ материала», «Тех.группа», — и
объект узнаётся из текста сообщения по справочнику названий. Из этого
следуют две ежедневные работы, которых не было бы при схеме «чат =
объект»:

* пополнять справочник, когда объект начинают звать по-новому;
* разбирать сообщения, где адрес не назван и по разговору не выводится.

Обе делаются здесь, а не командами на сервере: разбирать очередь через
SSH владелец бизнеса не станет, а неразобранная очередь означает работу,
не попавшую ни в один отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import (
    AttachmentRecord,
    ChatRecord,
    Event,
    Message,
    ObjectAlias,
    RepairObject,
)
from repairbot.domain import addresses
from repairbot.observability import get_logger
from repairbot.web.authors import author_column, join_author

log = get_logger(__name__)

UNASSIGNED_LIMIT = 100

DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders/{id}"
DRIVE_FILE_URL = "https://drive.google.com/file/d/{id}/view"
"""Адреса Диска. Собираются из идентификатора, который у нас и так есть,
— просить у Google ещё и ссылку значило бы лишний запрос ради строки,
формат которой не менялся годами."""


def folder_url(folder_id: str | None) -> str | None:
    return DRIVE_FOLDER_URL.format(id=folder_id) if folder_id else None


def file_url(file_id: str | None) -> str | None:
    return DRIVE_FILE_URL.format(id=file_id) if file_id else None


class ObjectsError(Exception):
    """Действие применить нельзя. Текст показывается человеку."""


@dataclass(slots=True)
class AliasCard:
    id: int
    alias: str


@dataclass(slots=True)
class ObjectCard:
    id: int
    code: str
    address: str
    status: str
    messages: int
    chats: int
    drive_folder_id: str | None = None
    files: int = 0
    aliases: list[AliasCard] = field(default_factory=list)

    @property
    def drive_url(self) -> str | None:
        """Папка объекта на Диске. None — пока туда ничего не клали."""
        return folder_url(self.drive_folder_id)


@dataclass(slots=True)
class UnassignedMessage:
    id: int
    text: str
    chat_title: str | None
    author: str | None
    sent_at: datetime


# --- справочник объектов ---


async def load_objects(session: AsyncSession) -> list[ObjectCard]:
    counts = dict(
        (
            await session.execute(
                select(Message.object_id, func.count(Message.id))
                .where(Message.object_id.is_not(None))
                .group_by(Message.object_id)
            )
        ).all()
    )
    chats = dict(
        (
            await session.execute(
                select(ChatRecord.object_id, func.count(ChatRecord.id))
                .where(ChatRecord.object_id.is_not(None))
                .group_by(ChatRecord.object_id)
            )
        ).all()
    )
    files = dict(
        (
            await session.execute(
                select(Message.object_id, func.count(AttachmentRecord.id))
                .join(Message, Message.id == AttachmentRecord.message_id)
                .where(
                    AttachmentRecord.drive_file_id.is_not(None),
                    Message.object_id.is_not(None),
                )
                .group_by(Message.object_id)
            )
        ).all()
    )
    aliases: dict[int, list[AliasCard]] = {}
    for row in (await session.execute(select(ObjectAlias).order_by(ObjectAlias.id))).scalars():
        aliases.setdefault(row.object_id, []).append(AliasCard(id=row.id, alias=row.alias))

    rows = await session.execute(select(RepairObject).order_by(RepairObject.id))
    return [
        ObjectCard(
            id=o.id,
            code=o.code,
            address=o.address,
            status=o.status,
            messages=counts.get(o.id, 0),
            chats=chats.get(o.id, 0),
            drive_folder_id=o.drive_folder_id,
            files=files.get(o.id, 0),
            aliases=aliases.get(o.id, []),
        )
        for o in rows.scalars().all()
    ]


async def create_object(
    session: AsyncSession, *, address: str, code: str | None = None, by: str = ""
) -> RepairObject:
    """Завести объект. Адрес сразу становится названием для поиска.

    Заводить название отдельным действием значило бы однажды забыть — и
    получить объект, которого не видно ни в одном сообщении.
    """
    address = address.strip()
    if not address:
        raise ObjectsError("Адрес не может быть пустым")

    code = (code or "").strip() or await _next_code(session)
    taken = (
        await session.execute(select(RepairObject).where(RepairObject.code == code))
    ).scalar_one_or_none()
    if taken is not None:
        raise ObjectsError(f"Код {code} уже занят объектом «{taken.address}»")

    obj = RepairObject(code=code, address=address)
    session.add(obj)
    await session.flush()
    await add_alias(session, obj.id, address)

    log.info("objects.created", object_id=obj.id, code=obj.code, by=by)
    return obj


async def _next_code(session: AsyncSession) -> str:
    """Следующий свободный `obj_N`.

    Считаем от наибольшего существующего, а не от количества: после
    удаления объекта счёт по количеству выдал бы занятый код.
    """
    last = (await session.execute(select(func.max(RepairObject.id)))).scalar_one() or 0
    return f"obj_{last + 1}"


async def add_alias(
    session: AsyncSession, object_id: int, alias: str, *, by: str = ""
) -> ObjectAlias:
    """Завести название объекта для поиска в переписке."""
    alias = alias.strip()
    if not addresses.is_usable_alias(alias):
        raise ObjectsError(
            f"«{alias}» не годится как название: нужно хотя бы одно слово буквами. "
            "Из одних цифр название совпало бы с любым сообщением про эти цифры."
        )

    normalized = " ".join(addresses.normalize(alias))
    taken = (
        await session.execute(select(ObjectAlias).where(ObjectAlias.normalized == normalized))
    ).scalar_one_or_none()
    if taken is not None:
        if taken.object_id == object_id:
            return taken
        other = await session.get(RepairObject, taken.object_id)
        raise ObjectsError(
            f"«{alias}» уже означает объект «{other.address if other else taken.object_id}». "
            "Одна запись не может указывать на два объекта — иначе узнавание "
            "адреса превратится в гадание."
        )

    record = ObjectAlias(object_id=object_id, alias=alias, normalized=normalized)
    session.add(record)
    await session.flush()
    log.info("objects.alias_added", object_id=object_id, alias=alias, by=by)
    return record


async def remove_alias(session: AsyncSession, alias_id: int, *, by: str = "") -> None:
    record = await session.get(ObjectAlias, alias_id)
    if record is None:
        raise ObjectsError("Название не найдено")
    await session.delete(record)
    await session.flush()
    log.info("objects.alias_removed", object_id=record.object_id, alias=record.alias, by=by)


# --- очередь разбора ---


async def load_unassigned(
    session: AsyncSession, limit: int = UNASSIGNED_LIMIT
) -> list[UnassignedMessage]:
    """Сообщения, для которых объект не узнан.

    Только входящие и только с текстом: свои отправленные разбирать
    незачем, а вложение без подписи человек всё равно не отнесёт.
    """
    rows = await session.execute(
        join_author(
            select(Message, ChatRecord.title, author_column()).join(
                ChatRecord, Message.chat_id == ChatRecord.id, isouter=True
            )
        )
        .where(
            Message.object_id.is_(None),
            Message.is_outbound.is_(False),
            Message.text.is_not(None),
            Message.text != "",
        )
        .order_by(Message.sent_at.desc())
        .limit(limit)
    )
    return [
        UnassignedMessage(
            id=m.id,
            text=m.text or "",
            chat_title=title,
            author=author,
            sent_at=m.sent_at,
        )
        for m, title, author in rows.all()
    ]


async def assign_message(
    session: AsyncSession, message_id: int, object_id: int, *, by: str = ""
) -> RepairObject:
    """Отнести сообщение к объекту руками.

    Пометка `manual` не для отчётности: следующие сообщения того же
    автора наследуют объект только от прямых указаний и решений
    человека, но не от таких же догадок.
    """
    obj = await session.get(RepairObject, object_id)
    if obj is None:
        raise ObjectsError("Объект не найден")

    updated = (
        await session.execute(
            sql_update(Message)
            .where(Message.id == message_id)
            .values(object_id=object_id, object_source="manual")
            .returning(Message.id)
        )
    ).first()
    if updated is None:
        raise ObjectsError("Сообщение не найдено")

    # Журнал — источник состояния объекта. Без этой строки сообщение бы
    # «переехало», а событие осталось бы ничьим, и работа не попала бы
    # ни в состояние объекта, ни в сводку.
    await session.execute(
        sql_update(Event).where(Event.source_message_id == message_id).values(object_id=object_id)
    )
    log.info("objects.message_assigned", message_id=message_id, object_id=object_id, by=by)
    return obj


# --- файлы на Диске ---


@dataclass(slots=True)
class FileCard:
    """Файл в архиве. Ссылка ведёт на Диск заказчика, а не к нам.

    Копии у себя мы не держим намеренно: архив принадлежит заказчику и
    должен пережить и переписку, и саму систему.
    """

    id: int
    name: str | None
    kind: str
    object_address: str | None
    object_id: int | None
    doc_class: str | None
    summary: str | None
    stored_at: datetime | None
    drive_file_id: str
    in_inbox: bool

    @property
    def url(self) -> str | None:
        return file_url(self.drive_file_id)


DOC_TITLES: dict[str, str] = {
    "receipt": "чек",
    "measurement": "замер",
    "contract": "договор",
    "act": "акт",
    "photo": "фото работ",
    "other": "прочее",
    "failed": "не прочитан",
    "unrecognizable": "не читается",
}


async def load_files(session: AsyncSession, limit: int = 200) -> list[FileCard]:
    """Что уже лежит на Диске. Свежие сверху."""
    rows = await session.execute(
        select(AttachmentRecord, Message, RepairObject)
        .join(Message, Message.id == AttachmentRecord.message_id)
        .join(RepairObject, RepairObject.id == Message.object_id, isouter=True)
        .where(AttachmentRecord.drive_file_id.is_not(None))
        .order_by(AttachmentRecord.stored_at.desc().nullslast(), AttachmentRecord.id.desc())
        .limit(limit)
    )
    cards: list[FileCard] = []
    for attachment, _message, obj in rows.all():
        payload = attachment.payload or {}
        reading = payload.get("reading") if isinstance(payload.get("reading"), dict) else {}
        cards.append(
            FileCard(
                id=attachment.id,
                name=attachment.filename,
                kind=attachment.kind,
                object_address=obj.address if obj is not None else None,
                object_id=obj.id if obj is not None else None,
                doc_class=attachment.doc_class,
                summary=(reading or {}).get("summary"),
                stored_at=attachment.stored_at,
                drive_file_id=str(attachment.drive_file_id),
                in_inbox=bool(payload.get("inbox")),
            )
        )
    return cards
