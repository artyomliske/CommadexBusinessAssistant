"""К какому объекту относится сообщение.

Нужно там, где чат не привязан к объекту: у заказчика чаты
функциональные («Заказ материала», «Тех.группа»), и работа по всем
адресам идёт вперемешку в одном чате.

Три способа, от самого надёжного к самому шаткому:

1. **Адрес в тексте.** Прямое указание, спорить не с чем.
2. **Ответ на сообщение.** Отвечающий имеет в виду то же место.
3. **Недавний разговор.** «Ленина 5» — «сколько плитки?» — «два
   поддона»: адрес звучит один раз, дальше подразумевается. Без этого
   способа до объекта доходила бы одна реплика из трёх.

Третий способ — единственный, который догадывается, поэтому он и самый
ограниченный: только тот же чат, только тот же автор, только внутри
окна времени. Разговор в общем чате перебивают, и «недавно» без
ограничения по автору означало бы приписывать чужие реплики.

Неузнанное остаётся неузнанным. Сообщение без объекта видно человеку и
ждёт его решения — это дешевле, чем тихо приписать работу чужой
квартире и потом искать расхождение в отчёте.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import Message, ObjectAlias
from repairbot.domain.addresses import match_objects
from repairbot.observability import get_logger

log = get_logger(__name__)

CONTEXT_WINDOW = timedelta(minutes=30)
"""Сколько сообщение остаётся «про тот же объект» без повторного адреса."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """Кому принадлежит сообщение и почему так решено."""

    object_id: int | None
    source: str | None
    """chat, text, reply, context — или None, если объект не узнан."""
    ambiguous: bool = False
    """В тексте названы сразу несколько объектов. Гадать не стали."""

    @property
    def resolved(self) -> bool:
        return self.object_id is not None


UNRESOLVED = Resolution(object_id=None, source=None)


class ObjectResolver:
    """Определяет объект сообщения. Справочник названий кэшируется.

    Кэш живёт столько же, сколько экземпляр: на один разбор пачки
    сообщений это один запрос вместо запроса на сообщение, а свежесть
    обеспечивается тем, что экземпляр создаётся заново на каждую пачку.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._aliases: list[tuple[str, int]] | None = None

    async def aliases(self) -> list[tuple[str, int]]:
        if self._aliases is None:
            rows = await self._session.execute(select(ObjectAlias.alias, ObjectAlias.object_id))
            self._aliases = [(alias, object_id) for alias, object_id in rows.all()]
        return self._aliases

    async def resolve(
        self,
        *,
        text: str | None,
        chat_id: int | None,
        chat_object_id: int | None = None,
        author_identity_id: int | None = None,
        reply_to_message_id: str | None = None,
        channel: str | None = None,
        channel_chat_id: str | None = None,
        sent_at: datetime | None = None,
    ) -> Resolution:
        # Привязанный чат отвечает на вопрос сам. Разбирать текст в нём
        # незачем: упоминание соседнего адреса не должно переносить
        # сообщение с объекта, к которому чат приписан руками.
        if chat_object_id is not None:
            return Resolution(object_id=chat_object_id, source="chat")

        aliases = await self.aliases()
        if aliases:
            matches = match_objects(text, aliases)
            if len(matches) == 1:
                return Resolution(object_id=matches[0].object_id, source="text")
            if len(matches) > 1:
                log.info(
                    "resolve.ambiguous",
                    chat_id=chat_id,
                    objects=[m.object_id for m in matches],
                )
                return Resolution(object_id=None, source=None, ambiguous=True)

        if reply_to_message_id and channel and channel_chat_id:
            parent = await self._object_of(channel, channel_chat_id, reply_to_message_id)
            if parent is not None:
                return Resolution(object_id=parent, source="reply")

        if chat_id is not None and author_identity_id is not None and sent_at is not None:
            recent = await self._recent_object(chat_id, author_identity_id, sent_at)
            if recent is not None:
                return Resolution(object_id=recent, source="context")

        return UNRESOLVED

    async def _object_of(
        self, channel: str, channel_chat_id: str, channel_message_id: str
    ) -> int | None:
        return (
            await self._session.execute(
                select(Message.object_id).where(
                    Message.channel == channel,
                    Message.channel_chat_id == channel_chat_id,
                    Message.channel_message_id == channel_message_id,
                )
            )
        ).scalar_one_or_none()

    async def _recent_object(
        self, chat_id: int, author_identity_id: int, sent_at: datetime
    ) -> int | None:
        """Объект последнего сообщения того же автора в том же чате.

        Только сообщения с прямым указанием: наследовать наследованное
        значит тянуть одну догадку через весь день переписки.
        """
        return (
            await self._session.execute(
                select(Message.object_id)
                .where(
                    Message.chat_id == chat_id,
                    Message.author_identity_id == author_identity_id,
                    Message.object_id.is_not(None),
                    Message.object_source.in_(("text", "manual")),
                    Message.sent_at <= sent_at,
                    Message.sent_at >= sent_at - CONTEXT_WINDOW,
                )
                .order_by(Message.sent_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
