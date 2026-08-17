"""Агент отчётов (раздел 3.2 ТЗ): периодические сводки.

Сводка собирается **без обращения к модели**. Это не экономия ради
экономии: всё, из чего она состоит, уже посчитано — состояние объектов
свёрнуто из журнала, расход посчитан по вызовам модели. Пересказ этих
чисел языковой моделью не добавил бы ничего, кроме риска, что она их
переформулирует по-своему. Руководителю нужны цифры, а не проза.

Сводка руководителю отнесена разделом 6 к действиям без подтверждения,
поэтому она уходит сама. Именно поэтому здесь нет ни одной строки, которую
не видно в панели: сообщение, которое никто не одобрял, обязано быть
проверяемым до последней цифры.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import Event, LlmCall, RepairObject
from repairbot.domain.events import FACT_EVENT_PREFIX
from repairbot.observability import get_logger

log = get_logger(__name__)


class Period(StrEnum):
    DAY = "day"
    WEEK = "week"

    @property
    def title(self) -> str:
        return "за сутки" if self is Period.DAY else "за неделю"

    @property
    def span(self) -> timedelta:
        return timedelta(days=1) if self is Period.DAY else timedelta(days=7)


@dataclass(slots=True)
class ObjectLine:
    """Строка сводки по одному объекту."""

    code: str
    address: str
    current_stage: str | None
    status: str
    spent: float
    open_questions: int
    stale_questions: int
    pending: int
    """Факты ниже порога, ждущие подтверждения."""
    deviations: list[str] = field(default_factory=list)
    events: int = 0
    silent_days: int | None = None
    """Сколько суток объект молчит. None — события были в период."""

    @property
    def needs_attention(self) -> bool:
        return bool(self.deviations) or self.stale_questions > 0 or self.silent_days is not None


@dataclass(slots=True)
class Digest:
    period: Period
    generated_at: datetime
    since: datetime
    objects: list[ObjectLine] = field(default_factory=list)
    events: int = 0
    facts: int = 0
    pending_total: int = 0
    llm_calls: int = 0
    llm_cost_rub: float = 0.0

    @property
    def key(self) -> str:
        """Ключ идемпотентности: одна сводка на период и дату.

        Повторный запуск задачи после перезапуска воркера не должен
        присылать руководителю вторую такую же сводку.
        """
        return f"digest:{self.period.value}:{self.generated_at:%Y-%m-%d}"

    @property
    def attention(self) -> list[ObjectLine]:
        return [o for o in self.objects if o.needs_attention]


SILENCE_THRESHOLD = timedelta(days=2)
"""Больше двух суток без событий — повод спросить, что с объектом."""


async def build_digest(
    session: AsyncSession,
    period: Period = Period.DAY,
    *,
    now: datetime | None = None,
    price_input_usd: float = 5.0,
    price_cached_usd: float = 0.5,
    price_output_usd: float = 25.0,
    usd_rub: float = 95.0,
) -> Digest:
    """Собрать сводку по активным объектам."""
    now = now or datetime.now(tz=UTC)
    since = now - period.span

    digest = Digest(period=period, generated_at=now, since=since)

    objects = list(
        (
            await session.execute(
                select(RepairObject)
                .where(RepairObject.status == "active")
                .order_by(RepairObject.code)
            )
        )
        .scalars()
        .all()
    )

    events_by_object = dict(
        (
            await session.execute(
                select(Event.object_id, func.count(Event.id))
                .where(Event.created_at >= since)
                .group_by(Event.object_id)
            )
        ).all()
    )
    last_event = dict(
        (
            await session.execute(
                select(Event.object_id, func.max(Event.occurred_at)).group_by(Event.object_id)
            )
        ).all()
    )
    pending_by_object = dict(
        (
            await session.execute(
                select(Event.object_id, func.count(Event.id))
                .where(Event.needs_human.is_(True), Event.applied.is_(False))
                .group_by(Event.object_id)
            )
        ).all()
    )

    for obj in objects:
        state = obj.state or {}
        seen_at = last_event.get(obj.id)
        silent_days = None
        if seen_at is None:
            silent_days = _days_between(obj.created_at, now)
        elif now - seen_at > SILENCE_THRESHOLD:
            silent_days = _days_between(seen_at, now)

        digest.objects.append(
            ObjectLine(
                code=obj.code,
                address=obj.address,
                current_stage=state.get("current_stage"),
                status=state.get("status") or "—",
                spent=float(state.get("spent") or 0.0),
                open_questions=int(state.get("open_questions_count") or 0),
                stale_questions=int(state.get("stale_questions_count") or 0),
                pending=int(pending_by_object.get(obj.id, 0)),
                deviations=[_deviation_text(d) for d in (state.get("deviations") or [])],
                events=int(events_by_object.get(obj.id, 0)),
                silent_days=silent_days,
            )
        )

    digest.events = int(
        (
            await session.execute(
                select(func.count(Event.id)).where(Event.created_at >= since)
            )
        ).scalar_one()
    )
    digest.facts = int(
        (
            await session.execute(
                select(func.count(Event.id)).where(
                    Event.created_at >= since,
                    Event.event_type.startswith(FACT_EVENT_PREFIX),
                )
            )
        ).scalar_one()
    )
    digest.pending_total = sum(o.pending for o in digest.objects)

    spend = (
        await session.execute(
            select(
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.cached_input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
            ).where(LlmCall.created_at >= since)
        )
    ).one()
    calls, input_tokens, cached_tokens, output_tokens = spend
    digest.llm_calls = int(calls)
    digest.llm_cost_rub = round(
        (
            input_tokens * price_input_usd
            + cached_tokens * price_cached_usd
            + output_tokens * price_output_usd
        )
        / 1_000_000
        * usd_rub,
        2,
    )

    log.info(
        "reports.built",
        period=period.value,
        objects=len(digest.objects),
        attention=len(digest.attention),
        events=digest.events,
    )
    return digest


def render_message(digest: Digest) -> str:
    """Короткое сообщение руководителю.

    Сначала то, что требует его внимания, потом цифры. Обратный порядок
    означал бы, что до просроченного этапа он доскроллит не всегда.

    Сумм в рублях по объектам здесь нет намеренно: сообщение уходит без
    подтверждения человеком, а раздел 6 требует подтверждения для всего,
    что содержит денежные сведения. Расход на модель — наш собственный,
    не заказчика, и к его смете отношения не имеет.
    """
    when = digest.generated_at.strftime("%d.%m")
    lines = [f"Сводка {digest.period.title} на {when}"]

    attention = digest.attention
    if attention:
        lines.append("")
        lines.append("Требует внимания:")
        for obj in attention:
            lines.append(f"• {obj.code} — {_attention_text(obj)}")
    else:
        lines.append("")
        lines.append("Отклонений нет.")

    lines.append("")
    lines.append(
        f"Объектов в работе: {len(digest.objects)}. "
        f"Событий: {digest.events}, фактов: {digest.facts}."
    )
    if digest.pending_total:
        lines.append(f"Ждут подтверждения: {digest.pending_total}.")

    return "\n".join(lines)


def _attention_text(obj: ObjectLine) -> str:
    parts: list[str] = []
    if obj.silent_days is not None:
        parts.append(f"молчит {obj.silent_days} дн.")
    parts.extend(obj.deviations)
    if obj.stale_questions:
        parts.append(f"{obj.stale_questions} вопрос(ов) без ответа")
    return "; ".join(parts) if parts else "требует внимания"


def _deviation_text(deviation: dict[str, Any]) -> str:
    if deviation.get("kind") == "stage_overrun":
        return (
            f"«{deviation.get('stage')}» идёт {deviation.get('elapsed_days')} дн. "
            f"при нормативе {deviation.get('normative_days')}"
        )
    blocked = deviation.get("blocked_by") or []
    return f"«{deviation.get('stage')}» начат, не завершены: {', '.join(blocked)}"


DIGEST_SHEET = "Сводки"
DIGEST_HEADERS = [
    "Дата",
    "Период",
    "Объект",
    "Этап",
    "Состояние",
    "Закуплено, ₽",
    "Открытых вопросов",
    "Ждут подтверждения",
    "Событий",
    "Отклонения",
]


def digest_rows(digest: Digest) -> list[list[Any]]:
    """Строки для листа сводок в сводной книге.

    В таблицу суммы идут, в сообщение — нет. Разница не в осторожности,
    а в адресате: таблицу открывает тот, у кого есть к ней доступ, а
    сообщение уходит в мессенджер само.
    """
    when = digest.generated_at.strftime("%d.%m.%Y %H:%M")
    rows: list[list[Any]] = []
    for obj in digest.objects:
        rows.append(
            [
                when,
                digest.period.title,
                obj.code,
                obj.current_stage or "—",
                obj.status,
                round(obj.spent, 2) if obj.spent else "",
                obj.open_questions or "",
                obj.pending or "",
                obj.events,
                "; ".join(obj.deviations),
            ]
        )
    return rows


def _days_between(earlier: datetime | None, now: datetime) -> int:
    if earlier is None:
        return 0
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=UTC)
    return max(0, (now - earlier).days)


def next_period_for(today: date) -> Period:
    """По понедельникам — недельная сводка, в прочие дни — суточная."""
    return Period.WEEK if today.weekday() == 0 else Period.DAY
