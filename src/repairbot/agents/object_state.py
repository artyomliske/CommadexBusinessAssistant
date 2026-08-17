"""Агент объекта: свёртка журнала в состояние (раздел 3.2 ТЗ).

Состояние объекта — **производная от журнала**, а не отдельно хранимая
правда (раздел 4 ТЗ). Отсюда всё остальное:

* свёртка детерминирована и не обращается к модели — одни и те же события
  всегда дают одно и то же состояние;
* пересборка с нуля возможна в любой момент, поэтому расхождения не
  накапливаются, а исправление промпта задним числом меняет и состояние;
* каждый показатель хранит ссылку на событие, из которого получен, — это
  и есть требуемая прослеживаемость.

Свёртка идёт по времени наступления события, а не по порядку записи:
догрузка истории приносит старые сообщения после новых, и сортировка по
`id` дала бы неверную картину.

Применяются только факты с `applied = true`. Значения ниже порога
достоверности ждут подтверждения менеджера и на состояние не влияют —
ровно как требует раздел 6.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents import stages
from repairbot.db.models import Event, RepairObject
from repairbot.domain.events import (
    FACT_EVENT_PREFIX,
    MANUAL_EDIT_EVENT,
    NEEDS_HUMAN_EVENT,
)
from repairbot.domain.facts import FactType, WorkStatus
from repairbot.observability import get_logger

log = get_logger(__name__)

STALE_QUESTION_DAYS = 3
"""Открытый вопрос старше этого срока — повод показать его отдельно."""


@dataclass(slots=True)
class StageState:
    key: str | None
    title: str
    status: str = WorkStatus.PLANNED.value
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rooms: dict[str, str] = field(default_factory=dict)
    source: str | None = None

    @property
    def order(self) -> int:
        return stages.BY_KEY[self.key].order if self.key else 999

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "rooms": dict(self.rooms),
            "source": self.source,
        }


@dataclass(slots=True)
class OpenQuestion:
    summary: str
    since: datetime | None
    source: str
    kind: str

    def as_dict(self, *, now: datetime) -> dict[str, Any]:
        age = (now - self.since).days if self.since else None
        return {
            "summary": self.summary,
            "since": _iso(self.since),
            "age_days": age,
            "stale": bool(age is not None and age >= STALE_QUESTION_DAYS),
            "source": self.source,
            "kind": self.kind,
        }


@dataclass(slots=True)
class ObjectState:
    """Свёрнутое состояние. Сериализуется в `objects.state`."""

    stages: dict[str, StageState] = field(default_factory=dict)
    rooms: dict[str, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))
    open_questions: list[OpenQuestion] = field(default_factory=list)
    participants: dict[str, str] = field(default_factory=dict)

    spent: float = 0.0
    paid: float = 0.0
    estimate_delta: float = 0.0
    purchases: int = 0

    planned_finish_on: date | None = None
    schedule_shifts: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    deviations: list[dict[str, Any]] = field(default_factory=list)

    pending_confirmations: int = 0
    manual_edits: int = 0
    events_folded: int = 0
    last_event_id: int | None = None
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None

    # --- производные показатели ---

    @property
    def current_stage(self) -> str | None:
        """Этап, который идёт сейчас.

        Если в работе несколько — берём самый ранний по справочнику: он
        и есть узкое место. Если ни один не в работе — последний
        завершённый, иначе ничего.
        """
        in_progress = [s for s in self.stages.values() if s.status == WorkStatus.IN_PROGRESS.value]
        if in_progress:
            return min(in_progress, key=lambda s: s.order).title

        done = [s for s in self.stages.values() if s.status == WorkStatus.DONE.value]
        if done:
            return max(done, key=lambda s: s.order).title
        return None

    @property
    def status(self) -> str:
        """Состояние объекта в целом.

        «Завершены» объявляем только по завершении последнего этапа
        справочника. Судить по тому, что все *упомянутые* этапы закрыты,
        нельзя: в переписке обычно фигурирует один-два этапа из тринадцати,
        и объект после сданной штукатурки оказался бы «завершённым».
        """
        if any(s.status == WorkStatus.BLOCKED.value for s in self.stages.values()):
            return "приостановлено"
        if any(s.status == WorkStatus.IN_PROGRESS.value for s in self.stages.values()):
            return "в работе"
        if not self.stages:
            return "не начато"

        final = stages.STAGES[-1]
        if self.stages.get(final.key, None) and (
            self.stages[final.key].status == WorkStatus.DONE.value
        ):
            return "работы завершены"

        # Этапы есть, но ни один не в работе: между этапами.
        return "в работе"

    def to_json(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Представление для БД, витрины и панели.

        Ключи верхнего уровня совпадают с теми, что читают лист «Сводка»
        и карточка объекта: менять их нельзя, не поправив оба места.
        """
        now = now or datetime.now(tz=UTC)
        questions = [q.as_dict(now=now) for q in self.open_questions]

        return {
            "current_stage": self.current_stage,
            "status": self.status,
            "started_on": _iso(self.first_event_at),
            "planned_finish_on": self.planned_finish_on.isoformat()
            if self.planned_finish_on
            else None,
            "spent": round(self.spent, 2),
            "paid": round(self.paid, 2),
            "estimate_delta": round(self.estimate_delta, 2),
            "purchases": self.purchases,
            "open_questions_count": len(questions),
            "stale_questions_count": sum(1 for q in questions if q["stale"]),
            "pending_confirmations": self.pending_confirmations,
            "manual_edits": self.manual_edits,
            "stages": {key: s.as_dict() for key, s in sorted(
                self.stages.items(), key=lambda kv: kv[1].order
            )},
            "rooms": {room: dict(v) for room, v in self.rooms.items()},
            "open_questions": questions,
            "participants": dict(self.participants),
            "schedule_shifts": self.schedule_shifts,
            "contradictions": self.contradictions,
            "deviations": self.deviations,
            "projection": {
                "events_folded": self.events_folded,
                "last_event_id": self.last_event_id,
                "last_event_at": _iso(self.last_event_at),
                "rebuilt_at": now.isoformat(),
            },
        }


def project(events: list[Event], *, now: datetime | None = None) -> ObjectState:
    """Свернуть события объекта в состояние.

    Чистая функция: ни базы, ни модели, ни текущего времени внутри логики —
    поэтому её легко проверять и невозможно «испортить» порядком вызовов.
    """
    now = now or datetime.now(tz=UTC)
    state = ObjectState()

    # По времени наступления: догрузка истории пишет старые сообщения
    # позже новых, и сортировка по id перевернула бы картину.
    ordered = sorted(events, key=lambda e: (e.occurred_at or e.created_at, e.id))

    for event in ordered:
        state.events_folded += 1
        state.last_event_id = max(state.last_event_id or 0, event.id)
        at = event.occurred_at or event.created_at
        if at is not None:
            state.first_event_at = min(state.first_event_at or at, at)
            state.last_event_at = max(state.last_event_at or at, at)

        if event.event_type == MANUAL_EDIT_EVENT:
            state.manual_edits += 1
            continue

        if event.event_type == NEEDS_HUMAN_EVENT and event.needs_human:
            state.pending_confirmations += 1
            _add_question(state, event, kind="needs_human")
            continue

        if not event.event_type.startswith(FACT_EVENT_PREFIX):
            continue

        # Факт ниже порога ждёт менеджера и на состояние не влияет.
        if event.needs_human and not event.applied:
            state.pending_confirmations += 1
            continue
        if not event.applied:
            continue  # отклонён менеджером

        _apply_fact(state, event, at)

    _detect_deviations(state, now=now)
    return state


def _apply_fact(state: ObjectState, event: Event, at: datetime | None) -> None:
    raw_type = event.event_type.removeprefix(FACT_EVENT_PREFIX)
    payload = event.payload or {}
    source = event.channel_message_id or f"event:{event.id}"

    try:
        fact_type = FactType(raw_type)
    except ValueError:
        return  # неизвестный тип факта: молча пропускаем, журнал его сохранил

    if fact_type is FactType.WORK_PROGRESS:
        _apply_progress(state, payload, at, source)
    elif fact_type is FactType.PURCHASE:
        state.spent += _amount(payload)
        state.purchases += 1
    elif fact_type is FactType.PAYMENT:
        state.paid += _amount(payload)
    elif fact_type is FactType.ESTIMATE_CHANGE:
        state.estimate_delta += _amount(payload)
    elif fact_type is FactType.SCHEDULE_CHANGE:
        _apply_schedule(state, payload, at, source)
    elif fact_type in (FactType.ISSUE, FactType.CLIENT_REQUEST, FactType.MATERIAL_REQUEST):
        _add_question(state, event, kind=fact_type.value)
    elif fact_type is FactType.STAFF_ASSIGNMENT:
        person = payload.get("person") or payload.get("summary")
        if person:
            state.participants[str(person)] = payload.get("stage") or ""


def _apply_progress(
    state: ObjectState, payload: dict[str, Any], at: datetime | None, source: str
) -> None:
    """Обновить этап и помещение.

    Здесь же разрешаются противоречия: если завершённый этап снова
    объявляют незавершённым, это либо переделка, либо ошибка извлечения.
    Молча перезаписывать нельзя — иначе показатель изменится, и никто
    не узнает почему.
    """
    raw_stage = payload.get("stage")
    reference = stages.resolve(raw_stage)
    title = reference.title if reference else (raw_stage or "без этапа")
    key = reference.key if reference else f"custom:{title}"

    current = state.stages.get(key)
    if current is None:
        current = StageState(key=reference.key if reference else None, title=title)
        state.stages[key] = current

    new_status = payload.get("status")
    if new_status:
        regressed = (
            current.status == WorkStatus.DONE.value and new_status != WorkStatus.DONE.value
        )
        if regressed:
            state.contradictions.append({
                "kind": "stage_regression",
                "stage": title,
                "was": current.status,
                "became": new_status,
                "at": _iso(at),
                "source": source,
                "started_over_at": _iso(at),
                "note": "Завершённый этап снова в работе: переделка или ошибка извлечения",
            })
            # Отсчёт срока начинается заново. Иначе этап, закрытый в феврале
            # и переоткрытый в июле, навсегда останется «просроченным на
            # полгода», хотя переделка идёт неделю. История не теряется —
            # она осталась в записи о противоречии и в самом журнале.
            current.started_at = at
            current.finished_at = None

        current.status = str(new_status)
        current.source = source

        if new_status == WorkStatus.IN_PROGRESS.value and current.started_at is None:
            current.started_at = at
        if new_status == WorkStatus.DONE.value:
            current.finished_at = at
            if current.started_at is None:
                current.started_at = at

    room = payload.get("room")
    if room:
        room_name = str(room)
        current.rooms[room_name] = str(new_status or current.status)
        state.rooms[room_name][title] = str(new_status or current.status)


def _apply_schedule(
    state: ObjectState, payload: dict[str, Any], at: datetime | None, source: str
) -> None:
    due = _parse_date(payload.get("due_date"))
    state.schedule_shifts.append({
        "summary": payload.get("summary", ""),
        "stage": payload.get("stage"),
        "due_date": due.isoformat() if due else None,
        "at": _iso(at),
        "source": source,
    })
    # Последний по времени срок считаем действующим.
    if due:
        state.planned_finish_on = due


def _add_question(state: ObjectState, event: Event, *, kind: str) -> None:
    payload = event.payload or {}
    summary = payload.get("summary") or payload.get("reason") or event.event_type
    state.open_questions.append(
        OpenQuestion(
            summary=str(summary),
            since=event.occurred_at or event.created_at,
            source=event.channel_message_id or f"event:{event.id}",
            kind=kind,
        )
    )


def _detect_deviations(state: ObjectState, *, now: datetime) -> None:
    """Отклонения от плана: этап идёт дольше норматива.

    Нормативы ориентировочные (см. `stages.py`), поэтому отклонение — это
    повод посмотреть, а не приговор.
    """
    for stage_state in state.stages.values():
        if stage_state.status != WorkStatus.IN_PROGRESS.value or stage_state.started_at is None:
            continue
        norm = stages.BY_KEY[stage_state.key].normative_days if stage_state.key else None
        if norm is None:
            continue

        elapsed = (now - stage_state.started_at).days
        if elapsed > norm:
            state.deviations.append({
                "kind": "stage_overrun",
                "stage": stage_state.title,
                "elapsed_days": elapsed,
                "normative_days": norm,
                "over_by_days": elapsed - norm,
                "since": _iso(stage_state.started_at),
            })

    # Нарушение порядка: начали поздний этап, не закрыв ранний.
    started = [s for s in state.stages.values() if s.key and s.status != WorkStatus.PLANNED.value]
    for stage_state in started:
        unfinished_earlier = [
            other.title
            for other in started
            if other.order < stage_state.order and other.status != WorkStatus.DONE.value
        ]
        if unfinished_earlier and stage_state.status != WorkStatus.PLANNED.value:
            state.deviations.append({
                "kind": "out_of_order",
                "stage": stage_state.title,
                "blocked_by": unfinished_earlier,
                "note": "Этап начат, хотя предыдущие не завершены",
            })


# --- работа с базой ---


async def rebuild_object_state(session: AsyncSession, object_id: int) -> dict[str, Any] | None:
    """Пересобрать состояние объекта с нуля и записать в `objects.state`.

    Полная пересборка, а не досчёт: события могут приходить задним числом
    при догрузке истории, а решение менеджера меняет флаги у старого
    факта. Досчёт пропустил бы и то, и другое.
    """
    obj = (
        await session.execute(select(RepairObject).where(RepairObject.id == object_id))
    ).scalar_one_or_none()
    if obj is None:
        return None

    events = list(
        (await session.execute(select(Event).where(Event.object_id == object_id)))
        .scalars()
        .all()
    )
    state = project(events)
    payload = state.to_json()

    # Ключ синхронизации с витриной принадлежит не нам — сохраняем.
    previous = obj.state or {}
    if "sheets" in previous:
        payload["sheets"] = previous["sheets"]

    obj.state = payload
    log.info(
        "object_state.rebuilt",
        object=obj.code,
        events=state.events_folded,
        stage=payload["current_stage"],
        open_questions=payload["open_questions_count"],
        pending=payload["pending_confirmations"],
        contradictions=len(state.contradictions),
        deviations=len(state.deviations),
    )
    return payload


async def rebuild_all(session: AsyncSession) -> dict[str, int]:
    """Пересобрать состояние всех активных объектов."""
    ids = list(
        (
            await session.execute(
                select(RepairObject.id).where(RepairObject.status == "active")
            )
        )
        .scalars()
        .all()
    )
    for object_id in ids:
        await rebuild_object_state(session, object_id)
    return {"objects": len(ids)}


# --- вспомогательное ---


def _amount(payload: dict[str, Any]) -> float:
    value = payload.get("amount")
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
