"""Справочник людей (раздел 4 ТЗ, карточки сущностей).

До этого модуля карточка человека читалась в трёх местах и не создавалась
ни в одном. Последствия были не косметические:

* проверка «автор не заказчик» в клиентском агенте никогда не срабатывала —
  роль брать было неоткуда, и прораб, написавший боту в личку, получал бы
  автоответ как заказчик;
* псевдоним человека выводился из идентификатора учётной записи, поэтому
  один и тот же прораб в MAX и в Telegram (этап 6) стал бы для модели
  двумя разными людьми.

Роль назначает человек, а не эвристика. Соблазн «кто пишет в групповом
чате — тот сотрудник» велик, но именно роль решает, уйдёт ли заказчику
автоответ, и ошибка здесь стоит дороже, чем минута работы менеджера.
Подсказки в интерфейсе есть, решение — за ним.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import ChannelIdentity, ChatRecord, Message, Person, RepairObject
from repairbot.observability import get_logger

log = get_logger(__name__)

ROLES: tuple[str, ...] = ("staff", "client", "supplier", "unknown")

ROLE_TITLES: dict[str, str] = {
    "staff": "сотрудник",
    "client": "заказчик",
    "supplier": "поставщик",
    "unknown": "не определена",
}


class PeopleError(Exception):
    """Действие применить нельзя."""


@dataclass(slots=True)
class IdentityCard:
    """Учётная запись в мессенджере и то, что о ней известно."""

    id: int
    channel: str
    channel_user_id: str
    display_name: str | None
    username: str | None
    is_bot: bool
    person_id: int | None
    person_name: str | None
    role: str
    messages: int
    last_seen_at: datetime | None
    objects: list[str]
    dialogs: int
    """В скольких личных диалогах с ботом писал. Подсказка: заказчики
    пишут в личку, бригада — в групповые чаты."""

    @property
    def is_linked(self) -> bool:
        return self.person_id is not None

    @property
    def hint(self) -> str:
        """Подсказка менеджеру, а не решение за него."""
        if self.is_bot:
            return "это бот"
        if self.dialogs and not self.objects:
            return "пишет только в личку — похоже на заказчика"
        if self.objects and not self.dialogs:
            return "пишет в рабочих чатах — похоже на сотрудника"
        if self.objects and self.dialogs:
            return "пишет и в рабочих чатах, и в личку"
        return "сообщений пока нет"


async def load_identities(session: AsyncSession, *, limit: int = 200) -> list[IdentityCard]:
    """Учётные записи, отсортированные по активности.

    Неразобранные идут первыми: страница нужна для того, чтобы их
    разобрать, а не любоваться уже разобранными.
    """
    stats = (
        select(
            Message.author_identity_id.label("identity_id"),
            func.count(Message.id).label("messages"),
            func.max(Message.sent_at).label("last_seen_at"),
        )
        .where(Message.author_identity_id.isnot(None))
        .group_by(Message.author_identity_id)
        .subquery()
    )

    rows = await session.execute(
        select(
            ChannelIdentity,
            Person,
            func.coalesce(stats.c.messages, 0),
            stats.c.last_seen_at,
        )
        .outerjoin(Person, Person.id == ChannelIdentity.person_id)
        .outerjoin(stats, stats.c.identity_id == ChannelIdentity.id)
        .order_by(
            ChannelIdentity.person_id.isnot(None),
            func.coalesce(stats.c.messages, 0).desc(),
            ChannelIdentity.id,
        )
        .limit(limit)
    )

    identities = list(rows.all())
    where = await _where_they_write(session, [i[0].id for i in identities])

    cards: list[IdentityCard] = []
    for identity, person, messages, last_seen in identities:
        objects, dialogs = where.get(identity.id, ([], 0))
        cards.append(
            IdentityCard(
                id=identity.id,
                channel=identity.channel,
                channel_user_id=identity.channel_user_id,
                display_name=identity.display_name,
                username=identity.username,
                is_bot=identity.is_bot,
                person_id=identity.person_id,
                person_name=person.display_name if person else None,
                role=person.role if person else "unknown",
                messages=int(messages or 0),
                last_seen_at=last_seen,
                objects=objects,
                dialogs=dialogs,
            )
        )
    return cards


async def _where_they_write(
    session: AsyncSession, identity_ids: list[int]
) -> dict[int, tuple[list[str], int]]:
    """Где человек пишет: коды объектов и число личных диалогов."""
    if not identity_ids:
        return {}

    rows = await session.execute(
        select(
            Message.author_identity_id,
            RepairObject.code,
            ChatRecord.kind,
            func.count(Message.id),
        )
        .join(ChatRecord, ChatRecord.id == Message.chat_id)
        .outerjoin(RepairObject, RepairObject.id == ChatRecord.object_id)
        .where(Message.author_identity_id.in_(identity_ids))
        .group_by(Message.author_identity_id, RepairObject.code, ChatRecord.kind)
    )

    where: dict[int, tuple[list[str], int]] = {}
    for identity_id, code, kind, count in rows:
        objects, dialogs = where.get(identity_id, ([], 0))
        if kind == "dialog":
            dialogs += int(count)
        elif code and code not in objects:
            objects = [*objects, code]
        where[identity_id] = (objects, dialogs)
    return where


async def assign(
    session: AsyncSession,
    identity_id: int,
    *,
    role: str,
    display_name: str | None = None,
    by: str | None = None,
) -> Person:
    """Назначить учётной записи человека с ролью.

    Карточка заводится при первом назначении: отдельный экран «создать
    человека, потом связать» удвоил бы работу менеджера без пользы.
    Повторное назначение меняет роль у той же карточки, а не плодит новую.
    """
    if role not in ROLES:
        raise PeopleError(f"Неизвестная роль: {role}")

    identity = (
        await session.execute(
            select(ChannelIdentity).where(ChannelIdentity.id == identity_id)
        )
    ).scalar_one_or_none()
    if identity is None:
        raise PeopleError(f"Учётная запись {identity_id} не найдена")

    name = (display_name or "").strip() or identity.display_name or identity.username
    if not name:
        raise PeopleError("Не из чего собрать имя — укажите его вручную")

    person: Person | None = None
    if identity.person_id is not None:
        person = (
            await session.execute(select(Person).where(Person.id == identity.person_id))
        ).scalar_one_or_none()

    if person is None:
        person = Person(display_name=name, role=role)
        session.add(person)
        await session.flush()
        # Псевдоним привязан к карточке, а не к учётной записи: один и тот
        # же человек в разных мессенджерах должен быть для модели одним.
        person.pseudonym = f"[NAME_P{person.id}]"
        identity.person_id = person.id
    else:
        person.role = role
        if display_name:
            person.display_name = name

    await session.flush()
    log.info(
        "people.assigned",
        identity_id=identity_id,
        person_id=person.id,
        role=role,
        by=by,
    )
    return person


async def unlink(session: AsyncSession, identity_id: int, *, by: str | None = None) -> None:
    """Отвязать учётную запись от карточки.

    Саму карточку не удаляем: на неё могут ссылаться события журнала,
    а журнал неизменяем.
    """
    identity = (
        await session.execute(
            select(ChannelIdentity).where(ChannelIdentity.id == identity_id)
        )
    ).scalar_one_or_none()
    if identity is None:
        raise PeopleError(f"Учётная запись {identity_id} не найдена")

    identity.person_id = None
    await session.flush()
    log.info("people.unlinked", identity_id=identity_id, by=by)


async def unassigned_count(session: AsyncSession) -> int:
    """Сколько учётных записей без роли.

    Пока их много, проверка «автор не заказчик» работает вслепую.
    """
    return int(
        (
            await session.execute(
                select(func.count(ChannelIdentity.id)).where(
                    ChannelIdentity.person_id.is_(None),
                    ChannelIdentity.is_bot.is_(False),
                )
            )
        ).scalar_one()
    )
