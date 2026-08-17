"""Кто написал сообщение.

Отдельным модулем, потому что цепочка неочевидная и однажды её уже
прошли неверно. У события есть поле `actor_id`, и оно выглядит ровно
тем, что нужно, — но указывает на карточку человека и ингестом не
заполняется. Автор берётся только так:

    событие → сообщение → учётная запись в канале → карточка человека

Имя выбирается по убыванию точности: имя из карточки (его назначил
человек), затем имя из мессенджера, затем `@username`. Пусто — значит
автора нет вовсе: так приходят системные события вроде «бота добавили
в чат».
"""

from __future__ import annotations

from sqlalchemy import Select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import coalesce

from repairbot.db.models import ChannelIdentity, Message, Person


def author_column() -> ColumnElement[str | None]:
    """Выражение с именем автора. Подставляется в select как колонка."""
    return coalesce(
        Person.display_name,
        ChannelIdentity.display_name,
        ChannelIdentity.username,
    ).label("author")


def join_author(stmt: Select, source: type[Message] = Message) -> Select:
    """Присоединить учётную запись автора и его карточку.

    Только внешними соединениями: сообщение без автора, автор без
    карточки и карточка без имени — все три случая обычные, и ни один
    не должен убирать строку из выдачи.
    """
    return stmt.join(
        ChannelIdentity, ChannelIdentity.id == source.author_identity_id, isouter=True
    ).join(Person, Person.id == ChannelIdentity.person_id, isouter=True)
