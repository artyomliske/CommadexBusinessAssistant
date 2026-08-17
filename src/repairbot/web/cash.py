"""Касса, займы и подотчёт — свёрнутые из журнала.

Ничего не хранится отдельно: остаток считается сложением записей
журнала, как и состояние объекта. Хранить остаток числом значило бы
завести вторую правду, которая рано или поздно разойдётся с первой —
и выяснится это на пересчёте наличных в конце месяца.

Что считается и по каким правилам:

* **Остаток кассы** — только явные наличные. Записи с безналом и с
  неуказанным способом остаток не двигают. Посчитать неизвестное как
  наличные — значит показать деньги, которых нет.
* **Вложено в объект** — выданные ему займы минус приходы с него.
  Положительное число означает «объект должен».
* **Подотчёт** — выдано человеку минус возвращено. Кто сколько не
  закрыл чеками.

В расчёт идут только применённые факты. Ждущие подтверждения не
считаются: их достоверность ниже порога, и складывать в остаток то,
что человек ещё не подтвердил, — верный способ получить цифру, которой
никто не верит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import Event, RepairObject
from repairbot.domain.events import FACT_EVENT_PREFIX
from repairbot.domain.facts import FactType
from repairbot.domain.money import (
    ACCOUNTS,
    DIRECTIONS,
    KINDS,
    Account,
    Direction,
    MovementKind,
    affects_cash,
    signed,
)
from repairbot.observability import get_logger

log = get_logger(__name__)

MOVEMENT_EVENT = f"{FACT_EVENT_PREFIX}{FactType.MONEY_MOVEMENT.value}"
"""Тип события в журнале. Префикс берём из одного места: написанный
руками «fact.» вместо «fact:» не находит ничего и выглядит как «записей
пока нет» — ошибка, которую по пустой странице не отличить от правды."""


@dataclass(slots=True)
class Movement:
    """Одна запись журнала, прочитанная как денежная."""

    event_id: int
    at: datetime | None
    direction: str | None
    account: str | None
    kind: str | None
    amount: float | None
    summary: str
    object_code: str | None
    counterparty: str | None
    applied: bool

    @property
    def direction_title(self) -> str:
        return DIRECTIONS.get(self.direction or "", "—")

    @property
    def account_title(self) -> str:
        return ACCOUNTS.get(self.account or Account.UNKNOWN, "не указано")

    @property
    def kind_title(self) -> str:
        return KINDS.get(self.kind or MovementKind.OTHER, "прочее")

    @property
    def in_cash(self) -> bool:
        return affects_cash(self.account)


@dataclass(slots=True)
class Balances:
    cash: float = 0.0
    """Остаток наличных: только записи с явной кассой."""
    unassigned_amount: float = 0.0
    """Сколько прошло мимо кассы и безнала — способ не указан."""
    by_object: dict[str, float] = field(default_factory=dict)
    """Вложено в объект: займы минус приходы. Плюс — объект должен."""
    by_person: dict[str, float] = field(default_factory=dict)
    """Подотчёт: выдано минус возвращено."""
    movements: list[Movement] = field(default_factory=list)
    pending: int = 0
    """Записей ждёт подтверждения. В суммы они не входят."""


async def load_balances(
    session: AsyncSession, *, since: date | None = None, limit: int = 300
) -> Balances:
    """Свернуть денежные записи журнала в остатки."""
    stmt = (
        select(Event, RepairObject.code)
        .outerjoin(RepairObject, RepairObject.id == Event.object_id)
        .where(Event.event_type == MOVEMENT_EVENT)
        .order_by(Event.occurred_at.desc().nullslast(), Event.id.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(Event.occurred_at >= datetime.combine(since, datetime.min.time()))

    balances = Balances()
    for event, object_code in (await session.execute(stmt)).all():
        movement = _read(event, object_code)
        balances.movements.append(movement)

        if not movement.applied:
            balances.pending += 1
            continue

        delta = signed(movement.direction, movement.amount)
        if movement.in_cash:
            balances.cash += delta
        elif movement.account in (None, Account.UNKNOWN):
            balances.unassigned_amount += abs(delta)

        _apply_object(balances, movement, delta)
        _apply_person(balances, movement, delta)

    return balances


def _apply_object(balances: Balances, movement: Movement, delta: float) -> None:
    """Вложено в объект: заём увеличивает долг, приход уменьшает."""
    if movement.object_code is None:
        return
    if movement.kind == MovementKind.LOAN and movement.direction == Direction.OUT:
        balances.by_object[movement.object_code] = (
            balances.by_object.get(movement.object_code, 0.0) + abs(delta)
        )
    elif movement.kind in (MovementKind.INCOME, MovementKind.RETURN):
        balances.by_object[movement.object_code] = (
            balances.by_object.get(movement.object_code, 0.0) - abs(delta)
        )


def _apply_person(balances: Balances, movement: Movement, delta: float) -> None:
    """Подотчёт: выдали человеку — должен, вернул — не должен."""
    who = (movement.counterparty or "").strip()
    if not who or movement.kind != MovementKind.ADVANCE:
        return
    sign = 1.0 if movement.direction == Direction.OUT else -1.0
    balances.by_person[who] = balances.by_person.get(who, 0.0) + sign * abs(delta)


def _read(event: Event, object_code: str | None) -> Movement:
    payload = event.payload or {}
    return Movement(
        event_id=event.id,
        at=event.occurred_at or event.created_at,
        direction=payload.get("direction"),
        account=payload.get("account"),
        kind=payload.get("movement_kind"),
        amount=_as_float(payload.get("amount")),
        summary=str(payload.get("summary") or "").strip() or "без описания",
        object_code=object_code,
        counterparty=payload.get("counterparty") or payload.get("person"),
        applied=bool(event.applied),
    )


def _as_float(value: object) -> float | None:
    """Сумма из журнала. Там лежит JSON, и число может оказаться строкой."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def default_since(days: int = 30) -> date:
    return (datetime.now(tz=UTC) - timedelta(days=days)).date()
