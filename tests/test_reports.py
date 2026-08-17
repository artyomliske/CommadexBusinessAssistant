"""Сводки руководителю (этап 5). Требуют Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from repairbot.agents.report_delivery import ReportDelivery
from repairbot.agents.reports import (
    DIGEST_HEADERS,
    DIGEST_SHEET,
    Period,
    build_digest,
    digest_rows,
    next_period_for,
    render_message,
)
from repairbot.db.models import Event, LlmCall, OutboundMessage, RepairObject
from repairbot.outbound.controller import Controller
from repairbot.outbound.policy import Verdict

NOW = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)


class FakeSheets:
    def __init__(self, *, existing_sheets: list[str] | None = None) -> None:
        self.appended: list[tuple[str, list[list[Any]]]] = []
        self.added: list[list[str]] = []
        self._sheets = existing_sheets or []

    async def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        return {"sheets": [{"properties": {"title": t}} for t in self._sheets]}

    async def add_sheets(self, spreadsheet_id: str, titles: list[str]) -> dict[str, Any]:
        self.added.append(titles)
        self._sheets.extend(titles)
        return {}

    async def values_append(
        self, spreadsheet_id: str, range_: str, rows: list[list[Any]]
    ) -> dict[str, Any]:
        self.appended.append((range_, rows))
        return {}


async def _object(
    session,
    *,
    code: str = "obj_17",
    state: dict[str, Any] | None = None,
    last_event_ago: timedelta | None = timedelta(hours=2),
    pending: int = 0,
) -> RepairObject:
    obj = RepairObject(
        code=code,
        address=f"Ленина 5, кв. {code[-2:]}",
        state=state
        or {
            "current_stage": "штукатурка",
            "status": "в работе",
            "spent": 240000.0,
            "open_questions_count": 1,
            "stale_questions_count": 0,
            "deviations": [],
        },
    )
    session.add(obj)
    await session.flush()

    if last_event_ago is not None:
        session.add(
            Event(
                channel="max",
                channel_chat_id="-100500",
                object_id=obj.id,
                event_type="message_created",
                payload={},
                dedup_key=f"ev.{code}",
                occurred_at=NOW - last_event_ago,
            )
        )
    for i in range(pending):
        session.add(
            Event(
                channel="max",
                channel_chat_id="-100500",
                object_id=obj.id,
                event_type="fact:purchase",
                payload={},
                dedup_key=f"pending.{code}.{i}",
                occurred_at=NOW - timedelta(hours=1),
                applied=False,
                needs_human=True,
            )
        )
    await session.flush()
    return obj


# --- сборка ---


async def test_digest_lists_active_objects(db_session):
    await _object(db_session, code="obj_17")
    await _object(db_session, code="obj_18")

    digest = await build_digest(db_session, Period.DAY, now=NOW)

    assert [o.code for o in digest.objects] == ["obj_17", "obj_18"]
    assert digest.objects[0].current_stage == "штукатурка"
    assert digest.objects[0].spent == 240000.0


async def test_finished_objects_are_not_in_the_digest(db_session):
    obj = await _object(db_session)
    obj.status = "done"
    await db_session.flush()

    digest = await build_digest(db_session, Period.DAY, now=NOW)

    assert digest.objects == []


async def test_silent_object_needs_attention(db_session):
    """Больше двух суток без событий — повод спросить, что с объектом."""
    await _object(db_session, last_event_ago=timedelta(days=5))

    digest = await build_digest(db_session, Period.DAY, now=NOW)

    line = digest.objects[0]
    assert line.silent_days == 5
    assert line.needs_attention


async def test_recent_activity_is_not_attention(db_session):
    await _object(db_session, last_event_ago=timedelta(hours=3))

    digest = await build_digest(db_session, Period.DAY, now=NOW)

    assert digest.objects[0].silent_days is None
    assert not digest.objects[0].needs_attention


async def test_stage_overrun_becomes_a_readable_line(db_session):
    await _object(
        db_session,
        state={
            "current_stage": "штукатурка",
            "status": "в работе",
            "deviations": [
                {
                    "kind": "stage_overrun",
                    "stage": "штукатурка",
                    "elapsed_days": 25,
                    "normative_days": 10,
                    "over_by_days": 15,
                }
            ],
        },
    )

    digest = await build_digest(db_session, Period.DAY, now=NOW)

    assert "идёт 25 дн. при нормативе 10" in digest.objects[0].deviations[0]
    assert digest.attention


async def test_pending_confirmations_are_counted_per_object(db_session):
    await _object(db_session, code="obj_17", pending=3)
    await _object(db_session, code="obj_18", pending=0)

    digest = await build_digest(db_session, Period.DAY, now=NOW)

    by_code = {o.code: o for o in digest.objects}
    assert by_code["obj_17"].pending == 3
    assert by_code["obj_18"].pending == 0
    assert digest.pending_total == 3


async def test_spend_is_counted_for_the_period_only(db_session):
    await _object(db_session)
    db_session.add(
        LlmCall(purpose="extraction", provider="claude", model="opus", input_tokens=1_000_000)
    )
    await db_session.flush()

    digest = await build_digest(
        db_session, Period.DAY, now=datetime.now(tz=UTC), price_input_usd=5.0, usd_rub=100.0
    )

    assert digest.llm_calls == 1
    assert digest.llm_cost_rub == 500.0


# --- текст сообщения ---


def _digest_with(**kw: Any):
    from repairbot.agents.reports import Digest, ObjectLine

    digest = Digest(period=Period.DAY, generated_at=NOW, since=NOW - timedelta(days=1))
    digest.objects = [
        ObjectLine(
            code=kw.get("code", "obj_17"),
            address="Ленина 5",
            current_stage="штукатурка",
            status="в работе",
            spent=kw.get("spent", 240000.0),
            open_questions=kw.get("open_questions", 0),
            stale_questions=kw.get("stale_questions", 0),
            pending=kw.get("pending", 0),
            deviations=kw.get("deviations", []),
            silent_days=kw.get("silent_days"),
        )
    ]
    digest.events = kw.get("events", 12)
    digest.facts = kw.get("facts", 4)
    digest.pending_total = kw.get("pending", 0)
    return digest


def test_message_leads_with_what_needs_attention():
    """До просроченного этапа руководитель доскроллит не всегда."""
    text = render_message(_digest_with(silent_days=4))
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[1] == "Требует внимания:"
    assert "молчит 4 дн." in lines[2]


def test_message_says_plainly_when_all_is_well():
    text = render_message(_digest_with())

    assert "Отклонений нет." in text


def test_message_carries_no_money():
    """Сводка уходит без подтверждения человеком.

    Раздел 6 требует подтверждения для всего, что содержит денежные
    сведения, — значит, сумм в тексте быть не должно, иначе контролёр
    придержит собственную сводку системы.
    """
    text = render_message(_digest_with(spent=240000.0))

    assert "240000" not in text
    assert "₽" not in text


def test_sheet_rows_do_carry_money():
    """В таблицу суммы идут: её открывает тот, у кого есть доступ."""
    rows = digest_rows(_digest_with(spent=240000.0))

    assert rows[0][5] == 240000.0
    assert len(rows[0]) == len(DIGEST_HEADERS)


def test_weekly_digest_on_mondays():
    from datetime import date

    assert next_period_for(date(2026, 8, 3)) is Period.WEEK
    assert next_period_for(date(2026, 8, 4)) is Period.DAY


# --- доставка ---


async def test_digest_goes_to_the_sheet_and_through_the_controller(db_session):
    await _object(db_session)
    sheets = FakeSheets()
    delivery = ReportDelivery(
        sheets=sheets,
        rollup_spreadsheet_id="rollup-1",
        manager_chat_id="-100777",
    )

    result = await delivery.deliver(
        db_session, Period.DAY, controller=Controller(db_session)
    )

    assert result.rows_written == 1
    assert sheets.added == [[DIGEST_SHEET]]
    # Первой строкой уходит заголовок нового листа, затем данные.
    assert sheets.appended[0][1] == [DIGEST_HEADERS]

    message = (await db_session.execute(select(OutboundMessage))).scalar_one()
    assert message.intent == "manager_digest"
    assert message.audience == "manager"
    assert message.verdict == Verdict.ALLOW.value
    assert result.outbound_id == message.id


async def test_existing_sheet_is_not_recreated(db_session):
    await _object(db_session)
    sheets = FakeSheets(existing_sheets=[DIGEST_SHEET])

    await ReportDelivery(
        sheets=sheets, rollup_spreadsheet_id="rollup-1"
    ).deliver(db_session, Period.DAY)

    assert sheets.added == []
    assert len(sheets.appended) == 1


async def test_unavailable_sheet_does_not_stop_the_message(db_session):
    """Сводка руководителю ценнее строчек в таблице."""

    class BrokenSheets(FakeSheets):
        async def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
            raise RuntimeError("книга недоступна")

    await _object(db_session)
    delivery = ReportDelivery(
        sheets=BrokenSheets(),
        rollup_spreadsheet_id="rollup-1",
        manager_chat_id="-100777",
    )

    result = await delivery.deliver(
        db_session, Period.DAY, controller=Controller(db_session)
    )

    assert result.sheet_error is not None
    assert result.outbound_id is not None


async def test_second_run_on_the_same_day_does_not_resend(db_session):
    """Перезапуск воркера не должен присылать вторую такую же сводку."""
    await _object(db_session)
    delivery = ReportDelivery(manager_chat_id="-100777")

    first = await delivery.deliver(db_session, Period.DAY, controller=Controller(db_session))
    second = await delivery.deliver(db_session, Period.DAY, controller=Controller(db_session))

    assert first.outbound_id is not None
    assert second.outbound_id is None

    messages = (await db_session.execute(select(OutboundMessage))).scalars().all()
    assert len(messages) == 1


async def test_without_a_manager_chat_nothing_is_sent(db_session):
    await _object(db_session)

    result = await ReportDelivery().deliver(
        db_session, Period.DAY, controller=Controller(db_session)
    )

    assert result.outbound_id is None
    assert (await db_session.execute(select(OutboundMessage))).first() is None
