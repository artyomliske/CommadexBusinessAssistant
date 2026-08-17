"""Касса, займы и подотчёт: свёртка денежных записей журнала.

Цифры отсюда попадают в решения о деньгах, поэтому осторожность важнее
полноты: лучше показать меньше, чем показать остаток, которого нет.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from repairbot.db.models import Event, RepairObject
from repairbot.domain.money import (
    Account,
    Direction,
    MovementKind,
    affects_cash,
    signed,
)
from repairbot.web import cash

WHEN = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


async def _movement(session, *, object_code: str | None = None, applied: bool = True, **payload):
    object_id = None
    if object_code:
        # Один объект на несколько записей — иначе второй заём по тому же
        # адресу упирается в уникальность кода.
        obj = (
            await session.execute(select(RepairObject).where(RepairObject.code == object_code))
        ).scalar_one_or_none()
        if obj is None:
            obj = RepairObject(code=object_code, address=object_code)
            session.add(obj)
            await session.flush()
        object_id = obj.id

    payload.setdefault("summary", "запись")
    event = Event(
        object_id=object_id,
        channel="max",
        channel_chat_id="-1",
        event_type=cash.MOVEMENT_EVENT,
        payload=payload,
        dedup_key=f"m:{id(payload)}:{applied}",
        occurred_at=WHEN,
        applied=applied,
    )
    session.add(event)
    await session.flush()
    return event


# --- знак и счёт ---


def test_income_is_positive_expense_is_negative():
    assert signed(Direction.IN, 100) == 100
    assert signed(Direction.OUT, 100) == -100


def test_no_amount_is_zero():
    assert signed(Direction.IN, None) == 0


@pytest.mark.parametrize(
    ("account", "counts"),
    [(Account.CASH, True), (Account.BANK, False), (Account.UNKNOWN, False), (None, False)],
)
def test_only_explicit_cash_moves_the_balance(account, counts):
    """Приписать неизвестное к наличным — показать деньги, которых нет."""
    assert affects_cash(account) is counts


# --- остаток кассы ---


async def test_cash_balance_adds_up(db_session):
    await _movement(
        db_session, direction=Direction.IN, account=Account.CASH, amount=100000
    )
    await _movement(
        db_session, direction=Direction.OUT, account=Account.CASH, amount=35000
    )

    balances = await cash.load_balances(db_session)

    assert balances.cash == 65000


async def test_bank_does_not_touch_the_cash_balance(db_session):
    await _movement(
        db_session, direction=Direction.OUT, account=Account.BANK, amount=30000
    )

    assert (await cash.load_balances(db_session)).cash == 0


async def test_unspecified_account_is_counted_separately(db_session):
    """«17500 оплата на дом» — способ не назван, и гадать нельзя."""
    await _movement(
        db_session, direction=Direction.OUT, account=Account.UNKNOWN, amount=17500
    )

    balances = await cash.load_balances(db_session)

    assert balances.cash == 0
    assert balances.unassigned_amount == 17500


async def test_unconfirmed_records_are_not_counted(db_session):
    """Ниже порога достоверности — значит человек ещё не подтвердил."""
    await _movement(
        db_session,
        direction=Direction.IN,
        account=Account.CASH,
        amount=999999,
        applied=False,
    )

    balances = await cash.load_balances(db_session)

    assert balances.cash == 0
    assert balances.pending == 1
    assert len(balances.movements) == 1, "в списке они видны, просто не сложены"


# --- вложено в объект ---


async def test_loan_increases_what_the_object_owes(db_session):
    await _movement(
        db_session,
        object_code="Ростовка",
        direction=Direction.OUT,
        account=Account.CASH,
        movement_kind=MovementKind.LOAN,
        amount=8000,
    )

    assert (await cash.load_balances(db_session)).by_object == {"Ростовка": 8000}


async def test_income_reduces_what_the_object_owes(db_session):
    await _movement(
        db_session,
        object_code="Пашковка",
        direction=Direction.OUT,
        movement_kind=MovementKind.LOAN,
        amount=800000,
    )
    await _movement(
        db_session,
        object_code="Пашковка",
        direction=Direction.IN,
        movement_kind=MovementKind.INCOME,
        amount=795000,
    )

    balances = await cash.load_balances(db_session)

    assert balances.by_object["Пашковка"] == 5000


async def test_material_payment_is_not_a_loan(db_session):
    """Оплата материала — расход, но объекту он ничего не добавляет в долг."""
    await _movement(
        db_session,
        object_code="Покерная",
        direction=Direction.OUT,
        account=Account.CASH,
        movement_kind=MovementKind.MATERIAL,
        amount=35000,
    )

    balances = await cash.load_balances(db_session)

    assert balances.by_object == {}
    assert balances.cash == -35000


async def test_movement_without_an_object_does_not_break_the_tally(db_session):
    await _movement(
        db_session, direction=Direction.OUT, movement_kind=MovementKind.LOAN, amount=5000
    )

    assert (await cash.load_balances(db_session)).by_object == {}


# --- подотчёт ---


async def test_advance_is_owed_until_returned(db_session):
    await _movement(
        db_session,
        direction=Direction.OUT,
        account=Account.CASH,
        movement_kind=MovementKind.ADVANCE,
        counterparty="Илья",
        amount=20000,
    )
    await _movement(
        db_session,
        direction=Direction.IN,
        account=Account.CASH,
        movement_kind=MovementKind.ADVANCE,
        counterparty="Илья",
        amount=5000,
    )

    assert (await cash.load_balances(db_session)).by_person == {"Илья": 15000}


async def test_advance_without_a_name_is_not_counted(db_session):
    """Записать долг на «неизвестно кого» — хуже, чем не записать."""
    await _movement(
        db_session,
        direction=Direction.OUT,
        movement_kind=MovementKind.ADVANCE,
        amount=20000,
    )

    assert (await cash.load_balances(db_session)).by_person == {}


# --- чтение записей ---


async def test_a_broken_amount_does_not_break_the_page(db_session):
    """В журнале лежит JSON: сумма может оказаться строкой или мусором."""
    await _movement(
        db_session, direction=Direction.OUT, account=Account.CASH, amount="много"
    )

    balances = await cash.load_balances(db_session)

    assert balances.cash == 0
    assert balances.movements[0].amount is None


async def test_titles_are_human_readable(db_session):
    await _movement(
        db_session,
        direction=Direction.OUT,
        account=Account.CASH,
        movement_kind=MovementKind.LOAN,
        amount=1000,
    )

    movement = (await cash.load_balances(db_session)).movements[0]

    assert movement.direction_title == "расход"
    assert movement.account_title == "касса"
    assert movement.kind_title == "заём объекту"


async def test_unknown_values_are_named_not_blank(db_session):
    await _movement(db_session, amount=1000)

    movement = (await cash.load_balances(db_session)).movements[0]

    assert movement.account_title == "не указано"
    assert movement.kind_title == "прочее"


def test_movement_event_type_matches_the_journal():
    """Префикс пишется в одном месте.

    Написанный руками «fact.» вместо «fact:» не находит ничего, и
    страница выглядит как «записей пока нет» — по пустой странице
    ошибку от правды не отличить.
    """
    from repairbot.domain.events import FACT_EVENT_PREFIX

    assert cash.MOVEMENT_EVENT.startswith(FACT_EVENT_PREFIX)
    assert cash.MOVEMENT_EVENT == "fact:money_movement"
