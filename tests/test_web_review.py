"""Подтверждение и отклонение фактов.

Требуют Postgres. Без доступной базы пропускаются (см. conftest).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from repairbot.db.models import Event, User
from repairbot.domain.events import FACT_CONFIRMED_EVENT, FACT_REJECTED_EVENT
from repairbot.web import review
from repairbot.web.security import hash_password


async def _manager(session) -> User:
    user = User(
        login="manager",
        display_name="Менеджер",
        password_hash=hash_password("пароль-достаточной-длины"),
        role="manager",
    )
    session.add(user)
    await session.flush()
    return user


async def _pending_fact(session, *, confidence: float = 0.71) -> Event:
    event = Event(
        channel="max",
        channel_chat_id="-100500",
        channel_message_id="mid.1",
        event_type="fact:purchase",
        payload={"type": "purchase", "summary": "грунтовка 3400", "amount": 3400},
        confidence=confidence,  # type: ignore[arg-type]
        applied=False,
        needs_human=True,
        dedup_key="fact:1:0",
    )
    session.add(event)
    await session.flush()
    return event


async def test_confirm_applies_fact_and_writes_decision(db_session):
    user = await _manager(db_session)
    fact = await _pending_fact(db_session)

    result = await review.confirm_fact(db_session, event_id=fact.id, user=user)
    await db_session.commit()

    assert result.applied is True

    await db_session.refresh(fact)
    assert fact.applied is True
    assert fact.needs_human is False

    decision = (
        await db_session.execute(
            select(Event).where(Event.event_type == FACT_CONFIRMED_EVENT)
        )
    ).scalar_one()
    assert decision.payload["source_event_id"] == fact.id
    assert decision.payload["reviewed_by"] == "manager"


async def test_reject_keeps_fact_unapplied(db_session):
    user = await _manager(db_session)
    fact = await _pending_fact(db_session)

    result = await review.reject_fact(
        db_session, event_id=fact.id, user=user, reason="модель выдумала сумму"
    )
    await db_session.commit()

    assert result.applied is False

    await db_session.refresh(fact)
    assert fact.applied is False
    # Решение принято — из очереди факт уходит.
    assert fact.needs_human is False


async def test_rejected_payload_kept_for_prompt_tuning(db_session):
    """Раздел 6 ТЗ: отклонённые накапливаются для доработки промптов."""
    user = await _manager(db_session)
    fact = await _pending_fact(db_session)

    await review.reject_fact(db_session, event_id=fact.id, user=user, reason="неверно")
    await db_session.commit()

    rejected = await review.load_rejected_for_prompt_tuning(db_session)

    assert len(rejected) == 1
    assert rejected[0].payload["rejected_payload"]["amount"] == 3400
    assert rejected[0].payload["reason"] == "неверно"


async def test_original_fact_content_is_never_rewritten(db_session):
    """Журнал неизменяем по содержанию: правятся только флаги обработки."""
    user = await _manager(db_session)
    fact = await _pending_fact(db_session)
    original_payload = dict(fact.payload)

    await review.confirm_fact(
        db_session, event_id=fact.id, user=user, correction="сумма 3450, не 3400"
    )
    await db_session.commit()

    await db_session.refresh(fact)
    assert fact.payload == original_payload

    decision = (
        await db_session.execute(
            select(Event).where(Event.event_type == FACT_CONFIRMED_EVENT)
        )
    ).scalar_one()
    assert decision.payload["correction"] == "сумма 3450, не 3400"


async def test_second_decision_is_refused(db_session):
    """Двойной клик или второй менеджер в той же очереди."""
    user = await _manager(db_session)
    fact = await _pending_fact(db_session)

    await review.confirm_fact(db_session, event_id=fact.id, user=user)
    await db_session.commit()

    with pytest.raises(review.ReviewError, match="уже обработано"):
        await review.reject_fact(db_session, event_id=fact.id, user=user)


async def test_missing_event_is_refused(db_session):
    user = await _manager(db_session)

    with pytest.raises(review.ReviewError, match="не найдено"):
        await review.confirm_fact(db_session, event_id=999_999, user=user)


async def test_decision_count_is_one_per_fact(db_session):
    user = await _manager(db_session)
    fact = await _pending_fact(db_session)

    await review.confirm_fact(db_session, event_id=fact.id, user=user)
    await db_session.commit()

    decisions = (
        await db_session.execute(
            select(func.count(Event.id)).where(
                Event.event_type.in_([FACT_CONFIRMED_EVENT, FACT_REJECTED_EVENT])
            )
        )
    ).scalar_one()
    assert decisions == 1


async def test_viewer_cannot_review():
    """Права проверяются зависимостью, а не шаблоном: скрытая кнопка — не защита."""
    from fastapi import HTTPException

    from repairbot.web.security import reviewer

    viewer = User(
        login="viewer",
        display_name="Наблюдатель",
        password_hash="x",
        role="viewer",
    )
    assert viewer.can_review is False

    with pytest.raises(HTTPException) as exc:
        await reviewer(viewer)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["admin", "manager"])
async def test_reviewer_roles_allowed(role):
    from repairbot.web.security import reviewer

    user = User(login=role, display_name=role, password_hash="x", role=role)

    assert await reviewer(user) is user


# --- аварийная остановка: нажатая кнопка против недоступного Redis ---


def test_unreachable_storage_is_not_a_deliberate_halt():
    """Предохранитель fail-closed: без Redis отправка запрещена.

    Показывать это теми же словами, что и сознательную остановку, нельзя.
    Менеджер нажмёт «Возобновить», ничего не изменится, и он решит, что
    сломана панель, — а чинить надо Redis.
    """
    from repairbot.outbound.controller import SWITCH_UNAVAILABLE
    from repairbot.web.routes import _halt_is_a_failure

    assert _halt_is_a_failure(SWITCH_UNAVAILABLE)


def test_manual_halt_stays_resumable():
    from repairbot.web.routes import _halt_is_a_failure

    assert not _halt_is_a_failure("остановил Артём: проверка")
    assert not _halt_is_a_failure(None)
