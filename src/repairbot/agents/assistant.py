"""Внутренний помощник: ответы и действия в личном диалоге.

Отличается от клиентского агента ровно тем, кому отвечает. Клиенту
нельзя называть суммы и сроки, которых не подтвердил человек, — здесь
адресат сам эти суммы и утверждает, поэтому картина отдаётся как есть.

Из этого следует главное ограничение: **доступ строго по списку**. Не
«кто написал в личку», а «кто в списке». Личный диалог с ботом может
завести и заказчик, и подрядчик, и случайный человек, нашедший бота в
каталоге, — и любой из них получил бы сводку по деньгам компании.
Проверка идёт по идентификатору учётной записи в канале: имя и
отображаемое название подделываются, идентификатор — нет.

Про действия. Помощник умеет не только отвечать, но и делать: завести
объект, добавить ему название, отметить платёж оплаченным. Каждое
действие сначала называется вслух и выполняется только после явного
согласия следующим сообщением. Согласие распознаётся списком слов, а не
моделью: подтверждение, которое само является догадкой, — не
подтверждение.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents import payment_calendar as pc
from repairbot.db.models import (
    AttachmentRecord,
    ChannelIdentity,
    ChatRecord,
    Event,
    LlmCall,
    Message,
    Person,
    RecurringPayment,
    RepairObject,
)
from repairbot.domain import addresses
from repairbot.domain.events import FACT_EVENT_PREFIX
from repairbot.llm.base import LlmInvalidOutput, StructuredRequest, StructuredResponse
from repairbot.llm.router import LlmRouter
from repairbot.observability import get_logger
from repairbot.web import knowledge
from repairbot.web import objects as objects_service

log = get_logger(__name__)

ROLE_TITLES: dict[str, str] = {
    "staff": "сотрудник",
    "client": "заказчик",
    "supplier": "поставщик",
}
"""Роль подставляется, только если её назначил человек. Роли `unknown`
в справочнике нет намеренно: «должность не определена» — это не
должность, и сообщать её модели незачем."""

MESSAGES_IN_CONTEXT = 80
"""Сколько последних сообщений уходит в контекст.

Больше — дороже каждый вопрос, меньше — помощник не помнит вчерашнего
дня. Восемьдесят покрывают примерно сутки переписки бригад."""

CONFIRMATION_TTL = timedelta(minutes=15)
"""Сколько ждёт предложенное действие.

Без срока «да», сказанное назавтра совсем по другому поводу, завело бы
объект, о котором давно забыли."""

YES = frozenset(
    {"да", "ага", "угу", "давай", "давайте", "ок", "окей", "подтверждаю", "верно", "делай", "+"}
)
NO = frozenset({"нет", "не", "отмена", "отменить", "не надо", "стоп", "-"})


class AssistantError(Exception):
    """Действие выполнить нельзя. Текст уходит человеку как есть."""


# --- что модель возвращает ---


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="Одно из: create_object, add_alias, mark_paid")
    address: str | None = Field(default=None, description="Адрес объекта")
    object_code: str | None = Field(default=None, description="Код объекта, если известен")
    alias: str | None = Field(default=None, description="Новое название объекта")
    payment_title: str | None = Field(default=None, description="Название платежа")


class AssistantReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(description="Ответ человеку на русском, коротко и по делу")
    action: ProposedAction | None = Field(
        default=None, description="Действие, если человек просил что-то сделать"
    )


_INSTRUCTION = """Ты — помощник внутри компании, которая делает ремонт квартир.
Отвечаешь своим — людям, которые и так знают все цифры. Говори прямо,
без осторожных оговорок.

Кто именно спрашивает, сказано в контексте. Его должность там же, если
она известна. Не додумывай ни того, ни другого: «владелец» вместо
«прораба» — такая же выдумка, как выдуманная сумма.

Правила:
* Отвечай только по данным из контекста. Не знаешь — так и скажи и
  подскажи, где это посмотреть. Выдуманная цифра здесь хуже молчания:
  по ней примут решение.
* В контексте есть и разобранные факты, и сырая переписка. Если в
  фактах ответа нет, а в переписке есть — отвечай по переписке и
  указывай, кто и когда это написал.
* Отвечай коротко. Это переписка в мессенджере, а не отчёт.
* Числа бери из контекста дословно, не пересчитывай.
* Если просят что-то сделать — заполни action и в answer спроси
  подтверждение одной фразой. Сам действие не выполняешь.
* Если не просят делать — action оставь пустым."""


@dataclass(slots=True)
class AssistantOutcome:
    text: str
    action_taken: str | None = None
    pending: dict[str, Any] | None = None
    facts_used: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_taken": self.action_taken,
            "pending": (self.pending or {}).get("kind"),
            "facts_used": self.facts_used,
        }


# --- допуск ---


def allowed_user_ids(raw: str) -> frozenset[str]:
    """Разобрать список из настроек. Пусто — помощник выключен."""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


async def is_allowed(
    session: AsyncSession, identity_id: int | None, allowed: frozenset[str]
) -> bool:
    """Пускать ли этого человека к помощнику.

    Проверяем идентификатор учётной записи в канале: имя и отображаемое
    название подделываются, идентификатор — нет.
    """
    if not allowed or identity_id is None:
        return False
    user_id = (
        await session.execute(
            select(ChannelIdentity.channel_user_id).where(ChannelIdentity.id == identity_id)
        )
    ).scalar_one_or_none()
    return bool(user_id) and str(user_id) in allowed


# --- подтверждения ---


def reads_as_yes(text: str) -> bool:
    return _first_word(text) in YES


def reads_as_no(text: str) -> bool:
    return _first_word(text) in NO


def _first_word(text: str) -> str:
    cleaned = (text or "").strip().lower().replace("ё", "е")
    for ch in ".,!?;:":
        cleaned = cleaned.replace(ch, " ")
    words = cleaned.split()
    return words[0] if words else ""


def is_fresh(pending: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """Не протухло ли предложенное действие."""
    if not pending or not pending.get("at"):
        return False
    try:
        proposed = datetime.fromisoformat(str(pending["at"]))
    except ValueError:
        return False
    return (now or datetime.now(tz=UTC)) - proposed <= CONFIRMATION_TTL


class Assistant:
    def __init__(self, router: LlmRouter, *, model: str | None = None) -> None:
        self._router = router
        self._model = model
        self._schema = AssistantReply.model_json_schema()
        self._schema_json = json.dumps(self._schema, ensure_ascii=False, sort_keys=True)

    async def handle(
        self,
        session: AsyncSession,
        text: str,
        *,
        pending: dict[str, Any] | None = None,
        by: str = "",
        identity_id: int | None = None,
    ) -> AssistantOutcome:
        """Ответить на сообщение и, если нужно, предложить или выполнить действие."""
        if is_fresh(pending):
            if reads_as_yes(text):
                return await self._perform(session, pending or {}, by=by)
            if reads_as_no(text):
                return AssistantOutcome(text="Отменил.", pending=None)
            # Не «да» и не «нет» — значит человек перешёл к другому
            # вопросу. Предложение снимаем: висящее подтверждение,
            # про которое забыли, однажды сработает не вовремя.

        context = await build_context(
            session, text, asked_by=await author_name(session, identity_id)
        )
        reply, response = await self._ask(text, context)
        # Учитываем расход: без записи вызовы помощника не попадали ни в
        # страницу «Расход», ни в подсчёт трат — на них тихо уходило
        # больше доллара, и увидеть это можно было только у провайдера.
        session.add(
            LlmCall(
                purpose="assistant",
                provider=response.provider,
                model=response.model[:64],
                input_tokens=response.input_tokens,
                cached_input_tokens=response.cached_input_tokens,
                output_tokens=response.output_tokens,
                degraded=response.degraded,
            )
        )
        await session.flush()

        if reply.action is None:
            return AssistantOutcome(text=reply.answer, facts_used=context.facts)

        proposal = reply.action.model_dump(exclude_none=True)
        proposal["at"] = datetime.now(tz=UTC).isoformat()
        return AssistantOutcome(text=reply.answer, pending=proposal, facts_used=context.facts)

    async def _ask(
        self, text: str, context: CompanyContext
    ) -> tuple[AssistantReply, StructuredResponse]:
        response = await self._router.complete_structured(
            StructuredRequest(
                stable_system=_INSTRUCTION,
                volatile_system=context.render(),
                user_content=text,
                json_schema=self._schema,
                schema_name="assistant_reply",
                max_output_tokens=1500,
                model=self._model,
            )
        )
        try:
            return AssistantReply.model_validate(response.payload), response
        except ValidationError as exc:
            raise LlmInvalidOutput(f"Ответ не соответствует схеме: {exc.error_count()}") from exc

    async def _perform(
        self, session: AsyncSession, pending: dict[str, Any], *, by: str
    ) -> AssistantOutcome:
        kind = pending.get("kind")
        try:
            if kind == "create_object":
                obj = await objects_service.create_object(
                    session, address=str(pending.get("address") or ""), by=by
                )
                text = f"Завёл объект «{obj.address}», код {obj.code}."
            elif kind == "add_alias":
                obj = await _find_object(session, pending)
                record = await objects_service.add_alias(
                    session, obj.id, str(pending.get("alias") or ""), by=by
                )
                text = f"«{record.alias}» теперь означает {obj.address}."
            elif kind == "mark_paid":
                payment = await _find_payment(session, str(pending.get("payment_title") or ""))
                await pc.mark_paid(session, payment.id, by=by)
                text = (
                    f"Отметил «{payment.title}» оплаченным. "
                    f"Следующий срок — {payment.next_due_on:%d.%m}."
                )
            else:
                raise AssistantError(f"Не умею такое: {kind}")
        except (objects_service.ObjectsError, pc.PaymentError, AssistantError) as exc:
            return AssistantOutcome(text=f"Не получилось: {exc}", pending=None)

        log.info("assistant.action", kind=kind, by=by)
        return AssistantOutcome(text=text, action_taken=str(kind), pending=None)


async def _find_object(session: AsyncSession, pending: dict[str, Any]) -> RepairObject:
    code = pending.get("object_code")
    if code:
        obj = (
            await session.execute(select(RepairObject).where(RepairObject.code == str(code)))
        ).scalar_one_or_none()
        if obj is not None:
            return obj

    address = str(pending.get("address") or "")
    if address:
        aliases = await _alias_pairs(session)
        matches = addresses.match_objects(address, aliases)
        if len(matches) == 1:
            obj = await session.get(RepairObject, matches[0].object_id)
            if obj is not None:
                return obj
    raise AssistantError(f"не нашёл объект: {code or address or 'не указан'}")


async def _find_payment(session: AsyncSession, title: str) -> RecurringPayment:
    """Найти платёж по части названия.

    Сравнение в Python, а не в SQL: `ILIKE` сворачивает регистр по
    правилам сортировки базы, и при сортировке C кириллица под неё не
    попадает — «связь» не нашла бы «Связь МТС». Платежей десятки, так
    что выбрать их все и сравнить здесь дешевле, чем зависеть от того,
    как настроена база.
    """
    needle = title.strip().casefold().replace("ё", "е")
    if not needle:
        raise AssistantError("не понял, какой платёж")

    rows = await session.execute(
        select(RecurringPayment).where(RecurringPayment.active.is_(True))
    )
    found = [
        p for p in rows.scalars().all() if needle in p.title.casefold().replace("ё", "е")
    ]
    if not found:
        raise AssistantError(f"не нашёл платёж «{title}»")
    if len(found) > 1:
        names = ", ".join(p.title for p in found[:5])
        raise AssistantError(f"под «{title}» подходит несколько: {names}. Уточните")
    return found[0]


async def _alias_pairs(session: AsyncSession) -> list[tuple[str, int]]:
    from repairbot.db.models import ObjectAlias

    rows = await session.execute(select(ObjectAlias.alias, ObjectAlias.object_id))
    return [(alias, object_id) for alias, object_id in rows.all()]


# --- картина компании ---


@dataclass(slots=True)
class CompanyContext:
    """То, что помощник знает на момент вопроса."""

    lines: list[str] = field(default_factory=list)
    facts: int = 0

    def render(self) -> str:
        return "\n".join(self.lines)


async def author_name(session: AsyncSession, identity_id: int | None) -> str | None:
    """Как зовут того, кто спрашивает.

    Помощник, не знающий собеседника, на «ты знаешь, кто я» отвечает
    «нет» — и выглядит это как незнание системы, хотя учётная запись ей
    прекрасно известна.
    """
    if identity_id is None:
        return None
    row = (
        await session.execute(
            select(Person.display_name, Person.role, ChannelIdentity.display_name)
            .select_from(ChannelIdentity)
            .join(Person, Person.id == ChannelIdentity.person_id, isouter=True)
            .where(ChannelIdentity.id == identity_id)
        )
    ).first()
    if row is None:
        return None

    name = row[0] or row[2]
    if not name:
        return None
    # Должность приписываем только назначенную человеком на странице
    # «Люди». Неназначенную не выдумываем и не подставляем по умолчанию.
    title = ROLE_TITLES.get(row[1] or "")
    return f"{name} ({title})" if title else name


async def build_context(
    session: AsyncSession,
    question: str,
    *,
    asked_by: str | None = None,
    messages_limit: int = MESSAGES_IN_CONTEXT,
) -> CompanyContext:
    """Собрать картину: общую, а по упомянутому объекту — подробную.

    Подбирается по вопросу, а не отдаётся целиком: вся переписка в
    контекст не влезет, а платим мы за каждый вложенный в него символ.
    """
    context = CompanyContext()
    today = datetime.now(tz=UTC).date()
    context.lines.append(f"Сегодня {today:%d.%m.%Y}.")
    if asked_by:
        context.lines.append(f"Спрашивает: {asked_by}.")

    # Знания идут первыми: это правила, по которым читается всё
    # остальное. «Касса — наличные в офисе» должно стоять до денежных
    # сообщений, а не после них.
    context.lines.append(await knowledge.render_for_context(session))

    aliases = await _alias_pairs(session)
    mentioned = addresses.match_objects(question, aliases) if aliases else []

    rows = await session.execute(select(RepairObject).order_by(RepairObject.id))
    all_objects = list(rows.scalars().all())
    if not all_objects:
        context.lines.append("Объектов в системе нет.")
    else:
        context.lines.append(f"\nОбъекты ({len(all_objects)}):")
        for obj in all_objects:
            state = obj.state or {}
            stage = state.get("stage") or "этап не указан"
            context.lines.append(f"— {obj.code}: {obj.address}, {stage}")

    for match in mentioned:
        obj = await session.get(RepairObject, match.object_id)
        if obj is not None:
            context.lines.append(await _object_detail(session, obj))
            context.facts += 1

    context.lines.append(await _payments_block(session))
    context.lines.append(await _attention_block(session))
    context.lines.append(await _recent_messages(session, messages_limit))
    return context


async def _recent_messages(session: AsyncSession, limit: int) -> str:
    """Свежая переписка как есть.

    Без неё помощник отвечает только по разобранным фактам, а разбор
    отстаёт: пока объект не заведён, сообщение «23к взял оплата
    материала» никуда не попадает, и на вопрос «на что ушли 23 тысячи»
    система отвечает «не вижу» — при том что сообщение у неё есть.

    Сырые строки, без пересказа: помощник должен видеть то же, что
    видел бы человек, открывший чат.
    """
    rows = await session.execute(
        select(Message.sent_at, Message.text, ChatRecord.title, ChannelIdentity.display_name)
        .join(ChatRecord, Message.chat_id == ChatRecord.id, isouter=True)
        .join(ChannelIdentity, ChannelIdentity.id == Message.author_identity_id, isouter=True)
        .where(
            Message.is_outbound.is_(False),
            Message.text.is_not(None),
            Message.text != "",
        )
        .order_by(Message.sent_at.desc())
        .limit(limit)
    )
    lines = []
    for sent_at, text, chat_title, author in reversed(rows.all()):
        when = sent_at.strftime("%d.%m %H:%M") if sent_at else "?"
        who = author or "неизвестно кто"
        where = chat_title or "чат без названия"
        lines.append(f"  {when} · {where} · {who}: {(text or '')[:400]}")

    if not lines:
        return "\nСообщений в системе нет."
    return "\nПоследние сообщения из чатов (свежие внизу):\n" + "\n".join(lines)


async def _object_detail(session: AsyncSession, obj: RepairObject) -> str:
    state = obj.state or {}
    lines = [
        f"\nПодробно про {obj.address} ({obj.code}):",
        f"состояние: {json.dumps(state, ensure_ascii=False)}",
    ]

    files = (
        await session.execute(
            select(func.count(AttachmentRecord.id))
            .join(Message, Message.id == AttachmentRecord.message_id)
            .where(Message.object_id == obj.id, AttachmentRecord.drive_file_id.is_not(None))
        )
    ).scalar_one()
    lines.append(f"файлов в архиве: {files}")

    rows = await session.execute(
        select(Event.event_type, Event.payload, Event.occurred_at)
        .where(Event.object_id == obj.id, Event.event_type.startswith(FACT_EVENT_PREFIX))
        .order_by(Event.id.desc())
        .limit(15)
    )
    facts = rows.all()
    if facts:
        lines.append("последние факты:")
        for event_type, payload, occurred_at in facts:
            when = occurred_at.strftime("%d.%m") if occurred_at else "?"
            lines.append(f"  {when} {event_type}: {json.dumps(payload, ensure_ascii=False)[:300]}")
    return "\n".join(lines)


async def _payments_block(session: AsyncSession) -> str:
    upcoming, overdue = await pc.due_for_reminder(session)
    if not upcoming and not overdue:
        return "\nПлатежи: ничего срочного."
    return "\nПлатежи:\n" + pc.render_reminder(upcoming, overdue, datetime.now(tz=UTC).date())


async def _attention_block(session: AsyncSession) -> str:
    needs_human = (
        await session.execute(
            select(func.count(Event.id)).where(
                Event.needs_human.is_(True), Event.applied.is_(False)
            )
        )
    ).scalar_one()
    unassigned = (
        await session.execute(
            select(func.count(Message.id)).where(
                Message.object_id.is_(None),
                Message.is_outbound.is_(False),
                Message.text.is_not(None),
            )
        )
    ).scalar_one()
    return (
        f"\nТребует внимания: {needs_human} записей на подтверждении, "
        f"{unassigned} сообщений без объекта."
    )
