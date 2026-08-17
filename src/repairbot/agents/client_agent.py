"""Клиентский агент (раздел 3.2 ТЗ).

«Диалог с заказчиком. Отвечает исключительно на основании данных из базы
состояния».

Слово «исключительно» здесь определяет конструкцию. Модели передаётся
**только свёрнутое состояние объекта** — ни переписки, ни журнала, ни
сырых сообщений. Из чего нельзя составить ответ, того агент и не знает:
выдумать сумму, которой нет в состоянии, ему просто неоткуда.

Ответ агента — не отправка. Это черновик, который дальше идёт в контролёр
и может быть остановлен независимо от того, что решила модель.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repairbot.domain.facts import api_json_schema
from repairbot.llm.base import LlmInvalidOutput, StructuredRequest
from repairbot.llm.router import LlmRouter
from repairbot.observability import get_logger
from repairbot.outbound.policy import Audience, Intent, looks_like_complaint

log = get_logger(__name__)

DISCLOSURE = (
    "Это автоматический помощник компании. Я отвечаю по данным о вашем объекте; "
    "если нужен человек — напишите «менеджер»."
)
"""Явное представление системы при первом контакте (раздел 6 ТЗ)."""

NON_CLIENT_ROLES: frozenset[str] = frozenset({"staff", "supplier"})
"""Роли, которым клиентский агент не отвечает.

Неизвестная роль считается заказчиком: в личном диалоге с ботом это
наиболее вероятно, а всё написанное всё равно проходит контролёр."""

_QUESTION_WORDS: frozenset[str] = frozenset({
    "когда", "сколько", "что", "чего", "где", "куда", "откуда",
    "почему", "зачем", "как", "какой", "какая", "какое", "какие",
    "кто", "можно", "будет", "будут", "успеете", "успеваете",
    "скажите", "подскажите", "уточните", "напомните",
})


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Отвечать ли на это сообщение вообще."""

    answer: bool
    reason: str


def should_answer(
    *,
    text: str,
    chat_kind: str,
    author_role: str | None = None,
    author_is_bot: bool = False,
) -> GateDecision:
    """Решить, уместен ли автоответ заказчику.

    Проверка детерминированная и намеренно узкая. Сомнение решается
    **против** ответа — в отличие от префильтра экстрактора, где сомнение
    решается в пользу разбора. Разница в цене ошибки: пропущенный факт
    можно достать из журнала позже, а лишнее сообщение заказчику уже
    отправлено.

    Групповые чаты исключены целиком. Там переписывается бригада, и бот,
    вклинивающийся в рабочий чат с ответом на риторический вопрос, — это
    ровно то, из-за чего такие системы отключают на второй день.
    """
    if author_is_bot:
        return GateDecision(False, "автор — бот")

    # ChatKind.DIALOG: личная переписка заказчика с ботом.
    if chat_kind != "dialog":
        return GateDecision(False, "не личный диалог")

    if author_role in NON_CLIENT_ROLES:
        return GateDecision(False, f"автор не заказчик (роль «{author_role}»)")

    clean = (text or "").strip()
    if len(clean) < 3:
        return GateDecision(False, "сообщение слишком короткое")

    # Претензия обычно не вопрос: «у вас брак по всей стене» вопросительного
    # знака не содержит. Молча её пропустить — худший исход из возможных,
    # поэтому черновик составляется. Автоматически он не уйдёт: раздел 6
    # относит ответ на претензию к требующим подтверждения, и контролёр
    # придержит его для менеджера.
    if looks_like_complaint(clean):
        return GateDecision(True, "претензия заказчика")

    if not _looks_like_question(clean):
        return GateDecision(False, "сообщение не похоже на вопрос")

    return GateDecision(True, "вопрос заказчика в личном диалоге")


def _looks_like_question(text: str) -> bool:
    """Вопросительный знак либо вопросительное слово в начале фразы.

    Смотрим только на два первых слова. Третьего уже достаточно, чтобы
    поймать «сделали всё как договаривались» — повествование, а не вопрос.
    Двух хватает на «а что там по плитке» и на вежливые просьбы
    («подскажите», «уточните»), которые по сути те же вопросы.
    """
    if "?" in text:
        return True
    words = [w.strip(".,!;:()«»\"'—-").casefold() for w in text.split()[:2]]
    return any(word in _QUESTION_WORDS for word in words)


class ClientReply(BaseModel):
    """Ответ модели. Схема намеренно узкая."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(description="Текст ответа заказчику на русском языке")
    intent: str = Field(
        description=(
            "Одно из: status_update, schedule_digest, organizational, "
            "financial, deadline_to_client, complaint_response, legal"
        )
    )
    grounded: bool = Field(
        description="True, только если каждое утверждение ответа опирается на переданные данные"
    )
    missing: str | None = Field(
        default=None,
        description="Каких данных не хватило, если ответить по состоянию нельзя",
    )


@dataclass(slots=True)
class ClientDraft:
    text: str
    intent: Intent
    audience: Audience = Audience.CLIENT
    grounded: bool = True
    missing: str | None = None
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    degraded: bool = False

    @property
    def answerable(self) -> bool:
        return bool(self.text.strip()) and self.grounded


_INSTRUCTION = """\
Ты отвечаешь заказчику ремонта квартиры от лица компании.

Главное правило: **ты знаешь только то, что написано в блоке «Состояние \
объекта»**. Ничего сверх него ты не знаешь и додумывать не имеешь права.

Если ответа на вопрос в состоянии нет — не выдумывай и не обобщай. Верни \
grounded = false и в поле missing напиши, каких данных не хватило. Честное \
«не могу ответить» переадресует вопрос менеджеру, и это нормальный исход.

Правила ответа:
1. Пиши коротко и по-человечески, без канцелярита и без извинений на \
пустом месте. Два-три предложения.
2. Не называй сумм, которых нет в состоянии. Не называй дат, которых нет \
в состоянии.
3. Не обещай сроков и не давай гарантий — даже если очень просят. Это \
решает человек.
4. Не упоминай внутреннюю кухню: достоверность фактов, названия этапов из \
справочника, номера сообщений.
5. Если вопрос содержит претензию или конфликт, поставь intent = \
complaint_response и ответь нейтрально, не признавая и не отрицая вину.
6. intent выбирай по сути ответа, а не по вопросу: если в ответе есть \
деньги — financial, если даты — deadline_to_client.

Ты формируешь черновик. Он проходит проверку до отправки, поэтому не \
пытайся угадать, что «можно» — просто отвечай честно по данным.
"""


class ClientAgent:
    def __init__(self, router: LlmRouter) -> None:
        self._router = router
        self._schema = api_json_schema(ClientReply)
        self._schema_json = json.dumps(self._schema, ensure_ascii=False, sort_keys=True)

    async def draft(
        self,
        question: str,
        *,
        object_state: dict[str, Any],
        object_code: str | None = None,
    ) -> ClientDraft:
        """Составить черновик ответа заказчику."""
        request = StructuredRequest(
            stable_system=_INSTRUCTION,
            volatile_system=_render_state(object_state, object_code),
            user_content=question,
            json_schema=self._schema,
            schema_name="client_reply",
            max_output_tokens=2048,
        )

        response = await self._router.complete_structured(request)
        try:
            reply = ClientReply.model_validate(response.payload)
        except ValidationError as exc:
            raise LlmInvalidOutput(f"Ответ не соответствует схеме: {exc.error_count()}") from exc

        intent = _intent_from(reply.intent, question)
        draft = ClientDraft(
            text=reply.answer.strip(),
            intent=intent,
            grounded=reply.grounded,
            missing=reply.missing,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            cached_input_tokens=response.cached_input_tokens,
            output_tokens=response.output_tokens,
            degraded=response.degraded,
        )
        log.info(
            "client_agent.drafted",
            intent=intent.value,
            grounded=reply.grounded,
            missing=bool(reply.missing),
            degraded=response.degraded,
        )
        return draft

    @property
    def schema_fingerprint(self) -> str:
        return hashlib.sha256(self._schema_json.encode()).hexdigest()[:12]


def _intent_from(raw: str, question: str) -> Intent:
    """Намерение, объявленное моделью, с поправкой на суть вопроса.

    Модель может назвать ответ на претензию обычным статусом. Претензию мы
    распознаём сами, детерминированно, и повышаем намерение — вниз оно
    никогда не идёт.
    """
    try:
        intent = Intent(raw)
    except ValueError:
        # Незнакомое намерение — самое осторожное из возможных.
        intent = Intent.COMPLAINT_RESPONSE

    if looks_like_complaint(question) and intent in (
        Intent.STATUS_UPDATE,
        Intent.SCHEDULE_DIGEST,
        Intent.ORGANIZATIONAL,
    ):
        return Intent.COMPLAINT_RESPONSE
    return intent


def _render_state(state: dict[str, Any], object_code: str | None) -> str:
    """Состояние объекта в том виде, в каком его видит модель.

    Передаём выжимку, а не состояние целиком: противоречия, отклонения и
    служебные счётчики заказчику знать незачем, а модели они дали бы повод
    о них заговорить.
    """
    lines = ["Состояние объекта:"]
    if object_code:
        lines.append(f"- объект: {object_code}")

    for label, key in (
        ("текущий этап", "current_stage"),
        ("состояние работ", "status"),
        ("плановое завершение", "planned_finish_on"),
    ):
        value = state.get(key)
        if value:
            lines.append(f"- {label}: {value}")

    stages = state.get("stages") or {}
    if stages:
        lines.append("- этапы:")
        for info in stages.values():
            rooms = ", ".join(f"{r}: {s}" for r, s in (info.get("rooms") or {}).items())
            suffix = f" ({rooms})" if rooms else ""
            lines.append(f"    {info.get('title')} — {info.get('status')}{suffix}")

    questions = state.get("open_questions") or []
    if questions:
        lines.append("- открытые вопросы:")
        lines.extend(f"    {q.get('summary')}" for q in questions)

    if len(lines) == 1:
        lines.append("- данных пока нет")

    lines.append(
        "\nЭто всё, что известно. Сведений о суммах и точных сроках здесь нет "
        "намеренно — их сообщает человек."
    )
    return "\n".join(lines)
