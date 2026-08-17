"""Клиентский агент: когда он отвечает и что происходит с ответом.

Фильтр «отвечать ли» проверяется отдельно от конвейера — он
детерминированный и базы не требует.
"""

from __future__ import annotations

from typing import Any

import pytest

from repairbot.agents.client_agent import ClientAgent, should_answer
from repairbot.llm.base import LlmError, LlmRefusal, StructuredResponse
from repairbot.outbound.policy import Intent


class FakeRouter:
    """Роутер, отдающий заранее заданный ответ модели."""

    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload
        self.requests: list[Any] = []

    async def complete_structured(self, request) -> StructuredResponse:
        self.requests.append(request)
        if isinstance(self._payload, Exception):
            raise self._payload
        return StructuredResponse(
            provider="claude",
            model="claude-opus-5",
            payload=self._payload,
            input_tokens=100,
            output_tokens=40,
        )


def _reply(**overrides: Any) -> dict[str, Any]:
    payload = {
        "answer": "Сейчас идёт штукатурка стен, кухня уже готова.",
        "intent": "status_update",
        "grounded": True,
        "missing": None,
    }
    payload.update(overrides)
    return payload


# --- фильтр: отвечать ли вообще ---


@pytest.mark.parametrize(
    "text",
    [
        "Когда закончите с кухней?",
        "А что там по плитке",
        "Подскажите, на каком этапе работы",
        "Всё готово?",
    ],
)
def test_questions_in_a_private_dialog_are_answered(text: str):
    assert should_answer(text=text, chat_kind="dialog").answer


def test_group_chats_are_never_answered():
    """В групповом чате переписывается бригада.

    Бот, вклинивающийся туда с ответом на риторический вопрос, — ровно то,
    из-за чего такие системы отключают на второй день.
    """
    decision = should_answer(text="Когда закончите с кухней?", chat_kind="group")

    assert not decision.answer
    assert "диалог" in decision.reason


def test_staff_writing_privately_is_not_a_client():
    decision = should_answer(
        text="Когда закончите с кухней?", chat_kind="dialog", author_role="staff"
    )

    assert not decision.answer


def test_unknown_role_is_treated_as_a_client():
    """В личном диалоге с ботом неизвестный человек — почти наверняка заказчик."""
    assert should_answer(text="Когда закончите?", chat_kind="dialog", author_role=None).answer
    assert should_answer(
        text="Когда закончите?", chat_kind="dialog", author_role="unknown"
    ).answer


def test_bots_are_not_answered():
    """Иначе два бота в диалоге переписываются друг с другом до упора."""
    decision = should_answer(
        text="Когда закончите?", chat_kind="dialog", author_is_bot=True
    )

    assert not decision.answer


@pytest.mark.parametrize(
    "text",
    [
        "Спасибо!",
        "Хорошо, ждём",
        "Завтра приеду посмотреть",
        "ок",
    ],
)
def test_statements_are_left_alone(text: str):
    """Сомнение решается против ответа.

    Пропущенный вопрос заказчик задаст ещё раз; лишнее сообщение уже
    отправлено и обратно не забирается.
    """
    assert not should_answer(text=text, chat_kind="dialog").answer


def test_complaints_pass_even_without_a_question_mark():
    """«У вас брак по всей стене» — не вопрос, но молчать здесь нельзя.

    Черновик составляется, чтобы претензия дошла до менеджера. Уйти сама
    она не может: раздел 6 требует подтверждения для ответов на претензии.
    """
    decision = should_answer(
        text="Это отвратительно, у вас брак по всей стене", chat_kind="dialog"
    )

    assert decision.answer
    assert "претензия" in decision.reason


def test_complaint_in_a_group_chat_still_gets_no_reply():
    """Претензия не отменяет главного правила: в бригадный чат не пишем."""
    assert not should_answer(
        text="Это отвратительно, у вас брак по всей стене", chat_kind="group"
    ).answer


def test_question_word_only_counts_at_the_start():
    """«Как» в середине рассказа вопроса не делает."""
    assert not should_answer(
        text="Сделали всё как договаривались, стены ровные", chat_kind="dialog"
    ).answer


# --- составление ответа ---


async def test_draft_carries_intent_and_usage():
    agent = ClientAgent(FakeRouter(_reply()))

    draft = await agent.draft("Когда закончите?", object_state={"current_stage": "штукатурка"})

    assert draft.intent is Intent.STATUS_UPDATE
    assert draft.answerable
    assert draft.input_tokens == 100
    assert draft.output_tokens == 40


async def test_model_never_sees_the_chat_history():
    """Раздел 3.2: агент отвечает исключительно по базе состояния.

    Из чего нельзя составить ответ, того агент и не знает — выдумать сумму,
    которой нет в состоянии, ему просто неоткуда.
    """
    router = FakeRouter(_reply())
    agent = ClientAgent(router)

    await agent.draft(
        "Сколько стоит?",
        object_state={"current_stage": "штукатурка", "spent": 240000},
        object_code="obj_17",
    )

    request = router.requests[0]
    context = f"{request.stable_system}\n{request.volatile_system}"
    # Свёрнутое состояние — да, суммы и переписка — нет.
    assert "штукатурка" in context
    assert "240000" not in context
    assert "obj_17" in context


async def test_ungrounded_answer_is_not_answerable():
    """Честное «не могу ответить» переадресует вопрос менеджеру."""
    agent = ClientAgent(
        FakeRouter(_reply(grounded=False, missing="в состоянии нет сроков", answer="—"))
    )

    draft = await agent.draft("Когда сдадите?", object_state={})

    assert not draft.answerable
    assert draft.missing == "в состоянии нет сроков"


async def test_complaint_raises_the_intent_declared_by_the_model():
    """Модель может назвать ответ на претензию обычным статусом.

    Претензию распознаём сами, детерминированно, и повышаем намерение —
    вниз оно никогда не идёт.
    """
    agent = ClientAgent(FakeRouter(_reply(intent="status_update")))

    draft = await agent.draft(
        "Это отвратительно, у вас брак по всей стене!", object_state={}
    )

    assert draft.intent is Intent.COMPLAINT_RESPONSE


async def test_unknown_intent_falls_back_to_the_most_cautious():
    agent = ClientAgent(FakeRouter(_reply(intent="что-то новое")))

    draft = await agent.draft("Когда закончите?", object_state={})

    assert draft.intent is Intent.COMPLAINT_RESPONSE


async def test_errors_propagate_to_the_caller():
    """Конвейер решает, что делать с отказом, — не агент."""
    agent = ClientAgent(FakeRouter(LlmRefusal("policy")))
    with pytest.raises(LlmRefusal):
        await agent.draft("Когда закончите?", object_state={})

    agent = ClientAgent(FakeRouter(LlmError("провайдер недоступен")))
    with pytest.raises(LlmError):
        await agent.draft("Когда закончите?", object_state={})


async def test_state_without_data_says_so_plainly():
    router = FakeRouter(_reply())
    await ClientAgent(router).draft("Когда закончите?", object_state={})

    assert "данных пока нет" in router.requests[0].volatile_system
