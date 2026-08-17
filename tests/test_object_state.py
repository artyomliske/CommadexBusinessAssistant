"""Свёртка журнала в состояние объекта.

Проекция — чистая функция, поэтому проверяется без базы: собираем список
событий, сворачиваем, смотрим результат.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from repairbot.agents import stages
from repairbot.agents.object_state import project
from repairbot.db.models import Event
from repairbot.domain.events import MANUAL_EDIT_EVENT, NEEDS_HUMAN_EVENT

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _event(
    event_type: str,
    payload: dict | None = None,
    *,
    at: datetime | None = None,
    applied: bool = True,
    needs_human: bool = False,
    event_id: int = 0,
    mid: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        object_id=1,
        channel="max",
        channel_chat_id="-100500",
        channel_message_id=mid or f"mid.{event_id}",
        event_type=event_type,
        payload=payload or {},
        applied=applied,
        needs_human=needs_human,
        dedup_key=f"k{event_id}",
        occurred_at=at or NOW,
    )


def _progress(stage: str, status: str, *, room: str | None = None, **kw) -> Event:
    payload = {"type": "work_progress", "stage": stage, "status": status,
               "summary": f"{stage}: {status}"}
    if room:
        payload["room"] = room
    return _event("fact:work_progress", payload, **kw)


# --- этапы ---


def test_empty_journal_gives_empty_state():
    state = project([], now=NOW).to_json(now=NOW)

    assert state["current_stage"] is None
    assert state["status"] == "не начато"
    assert state["open_questions_count"] == 0


def test_stage_in_progress_becomes_current():
    state = project([_progress("штукатурка", "в работе", event_id=1)], now=NOW).to_json(now=NOW)

    assert state["current_stage"] == "штукатурка"
    assert state["status"] == "в работе"


def test_finished_stage_shows_as_last_done():
    state = project([_progress("штукатурка", "завершено", event_id=1)], now=NOW).to_json(now=NOW)

    assert state["current_stage"] == "штукатурка"
    # Один закрытый этап из тринадцати — объект не «завершён», а между этапами.
    assert state["status"] == "в работе"


def test_object_is_finished_only_after_the_last_stage():
    """Судить о завершении по упомянутым этапам нельзя: в переписке
    фигурирует один-два этапа из тринадцати."""
    state = project(
        [
            _progress("штукатурка", "завершено", event_id=1),
            _progress("уборка", "завершено", event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["status"] == "работы завершены"


def test_earliest_in_progress_stage_wins():
    """Если в работе несколько этапов, показываем узкое место — самый ранний."""
    state = project(
        [
            _progress("плитка", "в работе", event_id=1),
            _progress("электрика", "в работе", event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["current_stage"] == "электрика"


def test_stage_synonyms_map_to_one_entry():
    """«штукатурим» и «штукатурка стен» — один этап, не три."""
    state = project(
        [
            _progress("штукатурим", "в работе", event_id=1),
            _progress("штукатурка стен", "завершено", event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert len(state["stages"]) == 1
    assert state["stages"]["plastering"]["status"] == "завершено"


def test_unknown_stage_kept_but_not_ordered():
    """Незнакомый этап не теряется — это повод пополнить справочник."""
    events = [_progress("монтаж аквариума", "в работе", event_id=1)]
    state = project(events, now=NOW).to_json(now=NOW)

    assert state["current_stage"] == "монтаж аквариума"


def test_blocked_stage_makes_object_paused():
    state = project(
        [
            _progress("плитка", "в работе", event_id=1),
            _progress("плитка", "приостановлено", event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["status"] == "приостановлено"


# --- порядок событий ---


def test_events_folded_by_occurrence_not_by_id():
    """Догрузка истории пишет старые сообщения позже новых.

    Сортировка по id дала бы «в работе» вместо «завершено».
    """
    state = project(
        [
            _progress("штукатурка", "завершено", at=NOW, event_id=1),
            _progress("штукатурка", "в работе", at=NOW - timedelta(days=3), event_id=99),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["stages"]["plastering"]["status"] == "завершено"


# --- противоречия ---


def test_regression_from_done_is_recorded():
    """Завершённый этап снова в работе — переделка или ошибка извлечения.

    Молча перезаписать нельзя: показатель изменится, и никто не узнает почему.
    """
    state = project(
        [
            _progress("штукатурка", "завершено", at=NOW - timedelta(days=2), event_id=1),
            _progress("штукатурка", "в работе", at=NOW, event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert len(state["contradictions"]) == 1
    contradiction = state["contradictions"][0]
    assert contradiction["kind"] == "stage_regression"
    assert contradiction["was"] == "завершено"
    assert contradiction["became"] == "в работе"
    assert state["stages"]["plastering"]["status"] == "в работе"


def test_regression_restarts_the_clock():
    """Переделка идёт свой срок, а не продолжает старый.

    Этап закрыли давно и переоткрыли вчера: считать надо со вчера, иначе
    переделка навсегда останется «просроченной» на всю прошлую историю.
    """
    state = project(
        [
            _progress("штукатурка", "в работе", at=NOW - timedelta(days=150), event_id=1),
            _progress("штукатурка", "завершено", at=NOW - timedelta(days=140), event_id=2),
            _progress("штукатурка", "в работе", at=NOW - timedelta(days=1), event_id=3),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["stages"]["plastering"]["finished_at"] is None
    # 1 день против норматива 10 — отклонения быть не должно.
    assert [d for d in state["deviations"] if d["kind"] == "stage_overrun"] == []


def test_normal_progression_records_no_contradiction():
    state = project(
        [
            _progress("штукатурка", "в работе", at=NOW - timedelta(days=2), event_id=1),
            _progress("штукатурка", "завершено", at=NOW, event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["contradictions"] == []


# --- пороги достоверности ---


def test_unconfirmed_fact_does_not_change_state():
    """Раздел 6 ТЗ: ниже порога — не применяется автоматически."""
    state = project(
        [
            _event(
                "fact:purchase",
                {"type": "purchase", "amount": 3400, "summary": "грунтовка"},
                applied=False,
                needs_human=True,
                event_id=1,
            )
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["spent"] == 0
    assert state["pending_confirmations"] == 1


def test_rejected_fact_is_excluded():
    state = project(
        [
            _event(
                "fact:purchase",
                {"type": "purchase", "amount": 3400},
                applied=False,
                needs_human=False,
                event_id=1,
            )
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["spent"] == 0
    assert state["pending_confirmations"] == 0


def test_applied_fact_counts():
    state = project(
        [
            _event("fact:purchase", {"type": "purchase", "amount": 3400}, event_id=1),
            _event("fact:purchase", {"type": "purchase", "amount": 1100.50}, event_id=2),
            _event("fact:payment", {"type": "payment", "amount": 50000}, event_id=3),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["spent"] == 4500.5
    assert state["purchases"] == 2
    assert state["paid"] == 50000


def test_broken_amount_does_not_break_projection():
    state = project(
        [_event("fact:purchase", {"type": "purchase", "amount": "три тысячи"}, event_id=1)],
        now=NOW,
    ).to_json(now=NOW)

    assert state["spent"] == 0
    assert state["purchases"] == 1


# --- открытые вопросы ---


def test_issue_becomes_open_question():
    state = project(
        [
            _event(
                "fact:issue",
                {"type": "issue", "summary": "Трещина в санузле"},
                at=NOW - timedelta(days=5),
                event_id=1,
            )
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["open_questions_count"] == 1
    question = state["open_questions"][0]
    assert question["summary"] == "Трещина в санузле"
    assert question["age_days"] == 5
    assert question["stale"] is True


def test_fresh_question_is_not_stale():
    state = project(
        [_event("fact:issue", {"type": "issue", "summary": "мелочь"}, at=NOW, event_id=1)],
        now=NOW,
    ).to_json(now=NOW)

    assert state["open_questions"][0]["stale"] is False
    assert state["stale_questions_count"] == 0


def test_needs_human_event_counts_as_pending_and_question():
    state = project(
        [_event(NEEDS_HUMAN_EVENT, {"reason": "претензия"}, needs_human=True,
                applied=False, event_id=1)],
        now=NOW,
    ).to_json(now=NOW)

    assert state["pending_confirmations"] == 1
    assert state["open_questions_count"] == 1


# --- отклонения ---


def test_stage_overrun_is_flagged():
    """Штукатурка по нормативу 10 дней; идёт 20 — отклонение."""
    started = NOW - timedelta(days=20)
    events = [_progress("штукатурка", "в работе", at=started, event_id=1)]
    state = project(events, now=NOW).to_json(now=NOW)

    overruns = [d for d in state["deviations"] if d["kind"] == "stage_overrun"]
    assert len(overruns) == 1
    assert overruns[0]["stage"] == "штукатурка"
    assert overruns[0]["normative_days"] == stages.BY_KEY["plastering"].normative_days
    assert overruns[0]["over_by_days"] == 10


def test_stage_within_norm_is_not_flagged():
    state = project(
        [_progress("штукатурка", "в работе", at=NOW - timedelta(days=3), event_id=1)],
        now=NOW,
    ).to_json(now=NOW)

    assert [d for d in state["deviations"] if d["kind"] == "stage_overrun"] == []


def test_finished_stage_is_not_flagged_as_overrun():
    state = project(
        [
            _progress("штукатурка", "в работе", at=NOW - timedelta(days=40), event_id=1),
            _progress("штукатурка", "завершено", at=NOW, event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert [d for d in state["deviations"] if d["kind"] == "stage_overrun"] == []


def test_out_of_order_stage_is_flagged():
    """Начали покраску, не закрыв штукатурку."""
    state = project(
        [
            _progress("штукатурка", "в работе", event_id=1),
            _progress("покраска", "в работе", event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    out_of_order = [d for d in state["deviations"] if d["kind"] == "out_of_order"]
    assert len(out_of_order) == 1
    assert out_of_order[0]["stage"] == "покраска"
    assert "штукатурка" in out_of_order[0]["blocked_by"]


# --- прочее ---


def test_rooms_are_tracked():
    state = project(
        [
            _progress("плитка", "завершено", room="санузел", event_id=1),
            _progress("штукатурка", "в работе", room="кухня", event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["rooms"]["санузел"]["плитка"] == "завершено"
    assert state["rooms"]["кухня"]["штукатурка"] == "в работе"


def test_schedule_shift_updates_planned_finish():
    state = project(
        [
            _event("fact:schedule_change",
                   {"type": "schedule_change", "due_date": "2026-09-01", "summary": "перенос"},
                   at=NOW - timedelta(days=1), event_id=1),
            _event("fact:schedule_change",
                   {"type": "schedule_change", "due_date": "2026-09-15", "summary": "ещё перенос"},
                   at=NOW, event_id=2),
        ],
        now=NOW,
    ).to_json(now=NOW)

    assert state["planned_finish_on"] == "2026-09-15"
    assert len(state["schedule_shifts"]) == 2


def test_manual_edits_are_counted():
    state = project(
        [_event(MANUAL_EDIT_EVENT, {"sheet": "Смета"}, event_id=1)], now=NOW
    ).to_json(now=NOW)

    assert state["manual_edits"] == 1


def test_transport_events_do_not_affect_state():
    """Само сообщение состояния не меняет — меняют извлечённые из него факты."""
    state = project(
        [_event("message_created", {"message": {"body": {"text": "привет"}}}, event_id=1)],
        now=NOW,
    ).to_json(now=NOW)

    assert state["current_stage"] is None
    assert state["stages"] == {}


def test_projection_is_deterministic():
    """Одни и те же события всегда дают одно и то же состояние."""
    events = [
        _progress("штукатурка", "завершено", event_id=1),
        _event("fact:purchase", {"type": "purchase", "amount": 3400}, event_id=2),
        _event("fact:issue", {"type": "issue", "summary": "трещина"}, event_id=3),
    ]

    first = project(events, now=NOW).to_json(now=NOW)
    second = project(list(reversed(events)), now=NOW).to_json(now=NOW)

    first.pop("projection")
    second.pop("projection")
    assert first == second


def test_traceability_to_source_message():
    """Раздел 4 ТЗ: каждый показатель прослеживается до исходного сообщения."""
    state = project(
        [_progress("штукатурка", "завершено", event_id=1, mid="mid.777")], now=NOW
    ).to_json(now=NOW)

    assert state["stages"]["plastering"]["source"] == "mid.777"


def test_projection_metadata_recorded():
    state = project(
        [_progress("штукатурка", "в работе", event_id=42)], now=NOW
    ).to_json(now=NOW)

    assert state["projection"]["events_folded"] == 1
    assert state["projection"]["last_event_id"] == 42


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("штукатурка", "plastering"),
        ("Штукатурим", "plastering"),
        ("шпаклевка", "puttying"),
        ("шпаклёвка", "puttying"),
        ("ЭЛЕКТРИКА", "electrical"),
        ("укладка плитки", "tiling"),
        ("монтаж аквариума", None),
        (None, None),
    ],
)
def test_stage_resolution(raw, expected):
    resolved = stages.resolve(raw)
    assert (resolved.key if resolved else None) == expected
