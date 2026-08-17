"""Отправка и решения менеджера по черновикам. Требуют Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from repairbot.channels import registry
from repairbot.db.models import DialogState, OutboundMessage, User
from repairbot.domain.events import Channel
from repairbot.outbound.controller import Controller
from repairbot.outbound.sender import SendRefused, send_approved
from repairbot.web import drafts
from repairbot.web.security import hash_password


class FakeAdapter:
    channel = Channel.MAX

    def __init__(self) -> None:
        self.sent: list[str] = []

    def normalize(self, payload):  # pragma: no cover — не используется здесь
        return []

    async def send_text(self, message) -> str:
        self.sent.append(message.text)
        return f"mid.out.{len(self.sent)}"

    async def fetch_history(self, *a, **kw):  # pragma: no cover
        return []

    async def aclose(self) -> None:
        pass


@pytest.fixture
def adapter():
    fake = FakeAdapter()
    registry.register(fake)
    yield fake
    registry.clear()


async def _manager(session) -> User:
    user = User(
        login="mgr",
        display_name="Менеджер",
        password_hash=hash_password("пароль-достаточной-длины"),
        role="manager",
    )
    session.add(user)
    await session.flush()
    return user


async def _outbound(session, **kw) -> OutboundMessage:
    defaults = {
        "channel": "max",
        "channel_chat_id": "-100500",
        "intent": "status_update",
        "audience": "client",
        "text": "Штукатурка завершена",
        "verdict": "allow",
        "reasons": {},
        "idempotency_key": kw.pop("key", "out-1"),
    }
    defaults.update(kw)
    message = OutboundMessage(**defaults)
    session.add(message)
    await session.flush()
    return message


# --- отправка ---


async def test_approved_message_is_sent(db_session, adapter):
    message = await _outbound(db_session)

    result = await send_approved(db_session, message.id, controller=Controller(db_session))

    assert result.sent is True
    assert adapter.sent == ["Штукатурка завершена"]

    await db_session.refresh(message)
    assert message.sent_at is not None
    assert message.channel_message_id == "mid.out.1"


async def test_unapproved_message_is_refused(db_session, adapter):
    """Агент не может обойти контролёр: отправляется только одобренное."""
    message = await _outbound(db_session, verdict="hold", key="out-hold")

    with pytest.raises(SendRefused, match="не одобрена"):
        await send_approved(db_session, message.id, controller=Controller(db_session))

    assert adapter.sent == []


async def test_blocked_message_is_refused(db_session, adapter):
    message = await _outbound(db_session, verdict="block", key="out-block")

    with pytest.raises(SendRefused):
        await send_approved(db_session, message.id, controller=Controller(db_session))

    assert adapter.sent == []


async def test_already_sent_is_not_sent_twice(db_session, adapter):
    """Повтор задачи не должен привести ко второй отправке."""
    message = await _outbound(db_session, sent_at=datetime.now(tz=UTC), key="out-sent")

    result = await send_approved(db_session, message.id, controller=Controller(db_session))

    assert result.sent is False
    assert adapter.sent == []


async def test_pause_after_approval_stops_the_send(db_session, adapter):
    """Заказчик попросил человека уже после того, как черновик одобрили."""
    message = await _outbound(db_session, key="out-paused")
    db_session.add(
        DialogState(
            channel="max",
            channel_chat_id="-100500",
            autoreply_paused_until=datetime.now(tz=UTC) + timedelta(hours=5),
            paused_reason="стоп-слово",
        )
    )
    await db_session.flush()

    result = await send_approved(db_session, message.id, controller=Controller(db_session))

    assert result.sent is False
    assert result.skipped_reason == "диалог на паузе"
    assert adapter.sent == []


async def test_first_send_to_client_marks_disclosure(db_session, adapter):
    """После первого сообщения предохранитель первого контакта не срабатывает."""
    message = await _outbound(db_session, key="out-first")

    await send_approved(db_session, message.id, controller=Controller(db_session))

    state = (
        await db_session.execute(
            select(DialogState).where(DialogState.channel_chat_id == "-100500")
        )
    ).scalar_one()
    assert state.automation_disclosed_at is not None


async def test_edited_text_is_sent_instead_of_original(db_session, adapter):
    message = await _outbound(
        db_session, verdict="hold", decision="sent", edited_text="Поправленный текст",
        key="out-edited",
    )

    await send_approved(db_session, message.id, controller=Controller(db_session))

    assert adapter.sent == ["Поправленный текст"]


# --- решения менеджера ---


async def test_approve_marks_ready_and_keeps_original(db_session):
    """Правка менеджера не затирает то, что предложила модель."""
    user = await _manager(db_session)
    message = await _outbound(db_session, verdict="hold", key="d-1")

    decision = await drafts.approve(
        db_session, message.id, user=user, edited_text="Мой вариант"
    )

    assert decision.decision == "edited"
    assert decision.ready_to_send is True

    await db_session.refresh(message)
    assert message.text == "Штукатурка завершена"
    assert message.edited_text == "Мой вариант"
    assert message.decided_by == user.id


async def test_approve_without_edit_is_plain_send(db_session):
    user = await _manager(db_session)
    message = await _outbound(db_session, verdict="hold", key="d-2")

    decision = await drafts.approve(db_session, message.id, user=user)

    assert decision.decision == "sent"
    await db_session.refresh(message)
    assert message.edited_text is None


async def test_identical_edit_is_not_treated_as_edit(db_session):
    user = await _manager(db_session)
    message = await _outbound(db_session, verdict="hold", key="d-same")

    decision = await drafts.approve(
        db_session, message.id, user=user, edited_text="Штукатурка завершена"
    )

    assert decision.decision == "sent"


async def test_rejected_draft_is_kept_for_prompt_tuning(db_session):
    """Раздел 6: отклонённые накапливаются для доработки промптов."""
    user = await _manager(db_session)
    message = await _outbound(db_session, verdict="hold", key="d-3")

    await drafts.reject(db_session, message.id, user=user, reason="выдумал срок")
    await db_session.commit()

    rejected = await drafts.rejected_for_prompt_tuning(db_session)

    assert len(rejected) == 1
    assert rejected[0].reasons["rejected_by_manager"] == "выдумал срок"
    assert rejected[0].text == "Штукатурка завершена"


async def test_second_decision_is_refused(db_session):
    user = await _manager(db_session)
    message = await _outbound(db_session, verdict="hold", key="d-4")

    await drafts.approve(db_session, message.id, user=user)

    with pytest.raises(drafts.DraftError, match="уже принято"):
        await drafts.reject(db_session, message.id, user=user)


async def test_drafts_queue_shows_only_undecided(db_session):
    user = await _manager(db_session)
    pending = await _outbound(db_session, verdict="hold", key="q-1")
    decided = await _outbound(db_session, verdict="hold", key="q-2")
    await drafts.approve(db_session, decided.id, user=user)
    await _outbound(db_session, verdict="allow", key="q-3")

    queue = await drafts.load_drafts(db_session)

    assert [d.id for d in queue] == [pending.id]


async def test_audit_includes_blocked(db_session):
    """Журнал аудита показывает и то, что не ушло."""
    await _outbound(db_session, verdict="block", key="a-1")
    await _outbound(db_session, verdict="allow", key="a-2")

    audit = await drafts.load_audit(db_session)

    assert {m.verdict for m in audit} == {"block", "allow"}
