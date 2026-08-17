"""Чтения для веб-интерфейса.

Отдельный модуль, а не запросы внутри обработчиков: их надо будет
переиспользовать в отчётах этапа 5, и держать их в одном месте дешевле,
чем растаскивать по страницам.

Все запросы только читают. Единственные записи интерфейса — вход и
подтверждение фактов, они в [security.py] и [review.py].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, Select, cast, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents.document_pass import FAILED as DOC_FAILED
from repairbot.db.models import (
    AttachmentRecord,
    ChannelIdentity,
    ChatRecord,
    Event,
    LlmCall,
    Message,
    OutboundMessage,
    RecurringPayment,
    RepairObject,
)
from repairbot.domain.events import (
    FACT_CONFIRMED_EVENT,
    FACT_EVENT_PREFIX,
    FACT_REJECTED_EVENT,
    MANUAL_EDIT_EVENT,
    NEEDS_HUMAN_EVENT,
)
from repairbot.integrations.drive.archive import ARCHIVE_KEY, SKIPPED_KINDS
from repairbot.web.authors import author_column, join_author


@dataclass(slots=True)
class ObjectCard:
    id: int
    code: str
    address: str
    status: str
    current_stage: str | None
    pending_confirmations: int
    open_issues: int
    events_24h: int
    last_event_at: datetime | None
    chats: int
    spreadsheet_id: str | None

    @property
    def is_stale(self) -> bool:
        """Больше двух суток без событий — повод спросить, что с объектом."""
        if self.last_event_at is None:
            return True
        return datetime.now(tz=UTC) - self.last_event_at > timedelta(days=2)


@dataclass(slots=True)
class PendingFact:
    event_id: int
    object_id: int | None
    object_code: str | None
    fact_type: str
    summary: str
    confidence: float | None
    threshold: float
    payload: dict[str, Any]
    occurred_at: datetime | None
    source_text: str | None
    source_message_id: str | None


@dataclass(slots=True)
class FeedItem:
    event_id: int
    occurred_at: datetime | None
    event_type: str
    object_code: str | None
    description: str
    confidence: float | None
    applied: bool
    needs_human: bool
    channel: str
    author: str | None = None
    """Кто написал. None — у события нет автора: так приходят
    системные события вроде «бота добавили в чат»."""

    @property
    def is_fact(self) -> bool:
        return self.event_type.startswith(FACT_EVENT_PREFIX)


@dataclass(slots=True)
class ChatCard:
    id: int
    channel: str
    channel_chat_id: str
    title: str | None
    kind: str
    object_code: str | None
    bot_is_member: bool
    can_read_all_messages: bool | None
    history_backfilled_at: datetime | None
    last_event_at: datetime | None
    messages: int

    @property
    def problems(self) -> list[str]:
        """Чек-лист подключения объекта (раздел 2 ТЗ)."""
        issues: list[str] = []
        if not self.bot_is_member:
            issues.append("бот удалён из чата")
        if self.object_code is None:
            issues.append("не привязан к объекту")
        if self.can_read_all_messages is None:
            issues.append("права не проверены")
        elif not self.can_read_all_messages:
            issues.append("нет права read_all_messages")
        if self.history_backfilled_at is None:
            issues.append("история не загружена")
        return issues

    @property
    def healthy(self) -> bool:
        return not self.problems


@dataclass(slots=True)
class SpendWindow:
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    degraded_calls: int = 0
    facts: int = 0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.input_tokens + self.cached_input_tokens
        return self.cached_input_tokens / total if total else 0.0

    @property
    def degraded_ratio(self) -> float:
        return self.degraded_calls / self.calls if self.calls else 0.0


@dataclass(slots=True)
class Overview:
    objects: list[ObjectCard] = field(default_factory=list)
    pending_total: int = 0
    orphan_chats: int = 0
    unhealthy_chats: int = 0
    events_24h: int = 0
    manual_edits_24h: int = 0
    archive_pending: int = 0
    archive_failed: int = 0
    documents_failed: int = 0
    drafts_waiting: int = 0
    """Черновики, ждущие решения менеджера. Это то, ради чего он сюда зашёл."""
    payments_soon: int = 0
    payments_overdue: int = 0
    people_unassigned: int = 0
    spend_24h: SpendWindow = field(default_factory=SpendWindow)
    spend_30d: SpendWindow = field(default_factory=SpendWindow)

    @property
    def needs_you(self) -> int:
        """Сколько дел требует человека прямо сейчас.

        Одно число вместо пяти счётчиков: если оно ноль, страницу можно
        закрыть и заниматься своими делами — а ровно за этим на обзор
        и заходят.
        """
        return self.pending_total + self.drafts_waiting + self.payments_overdue


# --- обзор ---


async def load_overview(session: AsyncSession) -> Overview:
    since = datetime.now(tz=UTC) - timedelta(hours=24)

    objects = await load_object_cards(session)
    overview = Overview(objects=objects)
    overview.pending_total = sum(o.pending_confirmations for o in objects)

    chats = await load_chats(session)
    overview.orphan_chats = sum(1 for c in chats if c.object_code is None)
    overview.unhealthy_chats = sum(1 for c in chats if not c.healthy)

    overview.events_24h = int(
        (
            await session.execute(
                select(func.count(Event.id)).where(Event.created_at >= since)
            )
        ).scalar_one()
    )
    overview.manual_edits_24h = int(
        (
            await session.execute(
                select(func.count(Event.id)).where(
                    Event.event_type == MANUAL_EDIT_EVENT, Event.created_at >= since
                )
            )
        ).scalar_one()
    )

    overview.drafts_waiting = int(
        (
            await session.execute(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.verdict == "hold",
                    OutboundMessage.decision.is_(None),
                )
            )
        ).scalar_one()
    )

    today = datetime.now(tz=UTC).date()
    payments = (
        await session.execute(
            select(RecurringPayment.next_due_on, RecurringPayment.notify_days_before).where(
                RecurringPayment.active.is_(True)
            )
        )
    ).all()
    for due_on, notify_days in payments:
        left = (due_on - today).days
        if left < 0:
            overview.payments_overdue += 1
        elif left <= notify_days:
            overview.payments_soon += 1

    overview.people_unassigned = int(
        (
            await session.execute(
                select(func.count(ChannelIdentity.id)).where(
                    ChannelIdentity.person_id.is_(None),
                    ChannelIdentity.is_bot.is_(False),
                )
            )
        ).scalar_one()
    )

    overview.archive_pending, overview.archive_failed = await load_archive_counts(session)
    overview.documents_failed = int(
        (
            await session.execute(
                select(func.count(AttachmentRecord.id)).where(
                    AttachmentRecord.doc_class == DOC_FAILED
                )
            )
        ).scalar_one()
    )

    overview.spend_24h = await load_spend(session, hours=24)
    overview.spend_30d = await load_spend(session, hours=24 * 30)
    return overview


async def load_archive_counts(session: AsyncSession) -> tuple[int, int]:
    """Сколько вложений ждёт Диска и по скольким вынесен отказ.

    Отказы важнее очереди: очередь рассосётся сама следующим заходом,
    а отказ — это чек, который не переложится уже никогда, пока человек
    не вмешается.
    """
    failed = (
        await session.execute(
            select(func.count(AttachmentRecord.id)).where(
                AttachmentRecord.payload.has_key(ARCHIVE_KEY)
            )
        )
    ).scalar_one()
    pending = (
        await session.execute(
            select(func.count(AttachmentRecord.id)).where(
                AttachmentRecord.drive_file_id.is_(None),
                AttachmentRecord.source_url.isnot(None),
                AttachmentRecord.kind.notin_(SKIPPED_KINDS),
                not_(AttachmentRecord.payload.has_key(ARCHIVE_KEY)),
            )
        )
    ).scalar_one()
    return int(pending), int(failed)


async def load_object_cards(session: AsyncSession) -> list[ObjectCard]:
    since = datetime.now(tz=UTC) - timedelta(hours=24)

    pending = (
        select(Event.object_id, func.count(Event.id).label("n"))
        .where(Event.needs_human.is_(True), Event.applied.is_(False))
        .group_by(Event.object_id)
        .subquery()
    )
    issues = (
        select(Event.object_id, func.count(Event.id).label("n"))
        .where(Event.event_type.in_([f"{FACT_EVENT_PREFIX}issue", NEEDS_HUMAN_EVENT]))
        .group_by(Event.object_id)
        .subquery()
    )
    recent = (
        select(Event.object_id, func.count(Event.id).label("n"))
        .where(Event.created_at >= since)
        .group_by(Event.object_id)
        .subquery()
    )
    last_seen = (
        select(Event.object_id, func.max(Event.created_at).label("at"))
        .group_by(Event.object_id)
        .subquery()
    )
    chat_counts = (
        select(ChatRecord.object_id, func.count(ChatRecord.id).label("n"))
        .group_by(ChatRecord.object_id)
        .subquery()
    )

    stmt = (
        select(
            RepairObject,
            func.coalesce(pending.c.n, 0),
            func.coalesce(issues.c.n, 0),
            func.coalesce(recent.c.n, 0),
            last_seen.c.at,
            func.coalesce(chat_counts.c.n, 0),
        )
        .outerjoin(pending, pending.c.object_id == RepairObject.id)
        .outerjoin(issues, issues.c.object_id == RepairObject.id)
        .outerjoin(recent, recent.c.object_id == RepairObject.id)
        .outerjoin(last_seen, last_seen.c.object_id == RepairObject.id)
        .outerjoin(chat_counts, chat_counts.c.object_id == RepairObject.id)
        .order_by(RepairObject.code)
    )

    cards: list[ObjectCard] = []
    for obj, pending_n, issues_n, recent_n, seen_at, chats_n in await session.execute(stmt):
        cards.append(
            ObjectCard(
                id=obj.id,
                code=obj.code,
                address=obj.address,
                status=obj.status,
                current_stage=(obj.state or {}).get("current_stage"),
                pending_confirmations=int(pending_n),
                open_issues=int(issues_n),
                events_24h=int(recent_n),
                last_event_at=seen_at,
                chats=int(chats_n),
                spreadsheet_id=obj.spreadsheet_id,
            )
        )
    return cards


async def load_object(session: AsyncSession, object_id: int) -> RepairObject | None:
    return (
        await session.execute(select(RepairObject).where(RepairObject.id == object_id))
    ).scalar_one_or_none()


# --- очередь подтверждения ---


async def load_pending_facts(
    session: AsyncSession, *, object_id: int | None = None, limit: int = 100
) -> list[PendingFact]:
    """Факты ниже порога достоверности и пометки «нужен человек»."""
    from repairbot.domain.facts import FactType, threshold_for

    stmt = (
        select(Event, RepairObject.code, Message.text)
        .outerjoin(RepairObject, RepairObject.id == Event.object_id)
        .outerjoin(Message, Message.id == Event.source_message_id)
        .where(Event.needs_human.is_(True), Event.applied.is_(False))
        .order_by(Event.occurred_at.desc().nullslast(), Event.id.desc())
        .limit(limit)
    )
    if object_id is not None:
        stmt = stmt.where(Event.object_id == object_id)

    pending: list[PendingFact] = []
    for event, object_code, source_text in await session.execute(stmt):
        payload = event.payload or {}
        raw_type = event.event_type.removeprefix(FACT_EVENT_PREFIX)
        try:
            threshold = threshold_for(FactType(raw_type))
        except ValueError:
            threshold = 0.0

        pending.append(
            PendingFact(
                event_id=event.id,
                object_id=event.object_id,
                object_code=object_code,
                fact_type=raw_type,
                summary=payload.get("summary") or payload.get("reason") or event.event_type,
                confidence=float(event.confidence) if event.confidence is not None else None,
                threshold=threshold,
                payload=payload,
                occurred_at=event.occurred_at,
                source_text=source_text,
                source_message_id=event.channel_message_id,
            )
        )
    return pending


# --- лента событий ---


async def load_feed(
    session: AsyncSession,
    *,
    object_id: int | None = None,
    event_type: str | None = None,
    only_needs_human: bool = False,
    only_facts: bool = False,
    limit: int = 50,
    before_id: int | None = None,
) -> list[FeedItem]:
    # Автор берётся через сообщение, а не из Event.actor_id: то поле
    # указывает на карточку человека и ингестом не заполняется.
    stmt: Select[Any] = join_author(
        select(Event, RepairObject.code, author_column())
        .outerjoin(RepairObject, RepairObject.id == Event.object_id)
        .outerjoin(Message, Message.id == Event.source_message_id)
    ).order_by(Event.id.desc()).limit(limit)
    if object_id is not None:
        stmt = stmt.where(Event.object_id == object_id)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    if only_needs_human:
        stmt = stmt.where(Event.needs_human.is_(True))
    if only_facts:
        stmt = stmt.where(Event.event_type.startswith(FACT_EVENT_PREFIX))
    if before_id is not None:
        stmt = stmt.where(Event.id < before_id)

    items: list[FeedItem] = []
    for event, object_code, author in await session.execute(stmt):
        items.append(
            FeedItem(
                event_id=event.id,
                occurred_at=event.occurred_at or event.created_at,
                event_type=event.event_type,
                object_code=object_code,
                description=_describe(event),
                confidence=float(event.confidence) if event.confidence is not None else None,
                applied=event.applied,
                needs_human=event.needs_human,
                channel=event.channel,
                author=author,
            )
        )
    return items


async def load_event_types(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        select(Event.event_type, func.count(Event.id))
        .group_by(Event.event_type)
        .order_by(func.count(Event.id).desc())
    )
    return [event_type for event_type, _ in rows]


# --- чаты ---


async def load_chats(session: AsyncSession) -> list[ChatCard]:
    message_counts = (
        select(Message.chat_id, func.count(Message.id).label("n"))
        .group_by(Message.chat_id)
        .subquery()
    )
    stmt = (
        select(ChatRecord, RepairObject.code, func.coalesce(message_counts.c.n, 0))
        .outerjoin(RepairObject, RepairObject.id == ChatRecord.object_id)
        .outerjoin(message_counts, message_counts.c.chat_id == ChatRecord.id)
        .order_by(ChatRecord.object_id.nullsfirst(), ChatRecord.id)
    )

    cards: list[ChatCard] = []
    for chat, object_code, messages in await session.execute(stmt):
        cards.append(
            ChatCard(
                id=chat.id,
                channel=chat.channel,
                channel_chat_id=chat.channel_chat_id,
                title=chat.title,
                kind=chat.kind,
                object_code=object_code,
                bot_is_member=chat.bot_is_member,
                can_read_all_messages=chat.can_read_all_messages,
                history_backfilled_at=chat.history_backfilled_at,
                last_event_at=chat.last_event_at,
                messages=int(messages),
            )
        )
    return cards


# --- расход ---


async def load_spend(session: AsyncSession, *, hours: int) -> SpendWindow:
    since = datetime.now(tz=UTC) - timedelta(hours=hours)
    row = (
        await session.execute(
            select(
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.cached_input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
                func.coalesce(func.sum(cast(LlmCall.degraded, Integer)), 0),
                func.coalesce(func.sum(LlmCall.facts_extracted), 0),
            ).where(LlmCall.created_at >= since)
        )
    ).one()

    return SpendWindow(
        calls=int(row[0]),
        input_tokens=int(row[1]),
        cached_input_tokens=int(row[2]),
        output_tokens=int(row[3]),
        degraded_calls=int(row[4]),
        facts=int(row[5]),
    )


def estimate_cost_rub(
    spend: SpendWindow,
    *,
    input_usd_per_mtok: float,
    cached_input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    usd_rub: float,
) -> float:
    """Оценка стоимости в рублях.

    Оценка приблизительная: цены заданы для основного провайдера, а вызовы
    резервного посчитаны по тем же ставкам. Для сверки с разделом 9 ТЗ
    этого достаточно, для выставления счёта — нет.
    """
    usd = (
        spend.input_tokens * input_usd_per_mtok
        + spend.cached_input_tokens * cached_input_usd_per_mtok
        + spend.output_tokens * output_usd_per_mtok
    ) / 1_000_000
    return usd * usd_rub


def _describe(event: Event) -> str:
    payload = event.payload or {}
    if event.event_type.startswith(FACT_EVENT_PREFIX):
        return payload.get("summary") or event.event_type
    if event.event_type == NEEDS_HUMAN_EVENT:
        return payload.get("reason", "требует внимания менеджера")

    # Решения менеджера. Без этого в журнале стояло «fact_confirmed» и
    # в колонке типа, и в колонке описания — строка ничего не сообщала.
    if event.event_type == FACT_CONFIRMED_EVENT:
        who = payload.get("reviewed_by", "менеджер")
        correction = payload.get("correction")
        applied = f"{who} подтвердил факт"
        return f"{applied}, уточнение: {correction}" if correction else applied
    if event.event_type == FACT_REJECTED_EVENT:
        who = payload.get("reviewed_by", "менеджер")
        reason = payload.get("reason")
        rejected = f"{who} отклонил факт"
        return f"{rejected}: {reason}" if reason else rejected
    if event.event_type == MANUAL_EDIT_EVENT:
        columns = ", ".join(payload.get("columns") or [])
        return f"правка в «{payload.get('sheet', '?')}»: {columns}"
    message = payload.get("message") or {}
    body = message.get("body") or {}
    return body.get("text") or event.event_type


@dataclass(slots=True)
class ModelSpend:
    """Расход по одной модели. Стоимость — по её собственной ставке."""

    model: str
    title: str
    calls: int
    input_tokens: int
    output_tokens: int
    usd: float


async def load_spend_by_model(session: AsyncSession, *, hours: int) -> list[ModelSpend]:
    """Разбивка расхода по моделям.

    Общей цифры мало: пока не видно, что дорогая модель съедает почти
    всё, менять нечего. Именно эта разбивка показала, что 96 % расхода
    приходится на разбор документов.
    """
    from repairbot.llm import pricing

    since = datetime.now(tz=UTC) - timedelta(hours=hours)
    rows = await session.execute(
        select(
            LlmCall.model,
            func.count(LlmCall.id),
            func.sum(LlmCall.input_tokens),
            func.sum(LlmCall.cached_input_tokens),
            func.sum(LlmCall.output_tokens),
        )
        .where(LlmCall.created_at >= since)
        .group_by(LlmCall.model)
    )

    spend = [
        ModelSpend(
            model=model or "—",
            title=pricing.price_for(model).title,
            calls=int(count or 0),
            input_tokens=int(inp or 0),
            output_tokens=int(out or 0),
            usd=pricing.cost_usd(
                model,
                input_tokens=int(inp or 0),
                cached_tokens=int(cached or 0),
                output_tokens=int(out or 0),
            ),
        )
        for model, count, inp, cached, out in rows.all()
    ]
    spend.sort(key=lambda s: s.usd, reverse=True)
    return spend
