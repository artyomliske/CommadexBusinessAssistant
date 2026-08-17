"""Экстрактор фактов (раздел 3.2 ТЗ).

Преобразует свободный текст рабочей переписки в структурированные записи.
Каждый факт получает оценку достоверности и ссылку на исходное сообщение;
значения ниже порога не применяются автоматически.

Инструкция и схема собираются один раз при создании экстрактора и дальше
не меняются — на этом держится кэширование промпта. Всё, что меняется от
сообщения к сообщению (объект, участники, недавняя переписка), передаётся
переменной частью, после точки кэширования.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError

from repairbot.domain.facts import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FINANCIAL_CONFIDENCE_THRESHOLD,
    ExtractionResult,
    FactType,
    api_json_schema,
)
from repairbot.llm.base import LlmInvalidOutput, StructuredRequest, StructuredResponse
from repairbot.llm.router import LlmRouter
from repairbot.observability import get_logger
from repairbot.privacy.pseudonymizer import Pseudonymizer

log = get_logger(__name__)


def _build_instruction() -> str:
    """Стабильная часть промпта. Меняется только вместе с версией схемы."""
    types = "\n".join(f"  - {t.value}: {_TYPE_HINTS[t]}" for t in FactType)
    return f"""\
Ты извлекаешь структурированные факты из рабочей переписки бригад, \
прорабов и заказчиков компании по ремонту квартир. Сообщения написаны \
разговорным русским языком, с сокращениями, опечатками и без знаков препинания.

Твоя задача — только извлечение. Ты не даёшь советов, не пишешь ответов \
заказчику и не делаешь выводов, которых нет в тексте.

Типы фактов:
{types}

Правила:
1. Извлекай только то, что прямо сказано в сообщении. Не достраивай \
предположения: если не указано помещение — оставь room пустым, если не \
указана сумма — оставь amount пустым.
2. Одно сообщение может содержать несколько фактов. Разбивай их по смыслу: \
«купил грунтовку за 3400 и завтра начинаем шпаклевать» — это две записи.
3. Если фактов нет, верни пустой список facts. Пустой ответ — нормальный \
результат, придумывать факт не нужно.
4. confidence — твоя оценка того, насколько однозначно факт прочитан из текста:
   - 0.9-1.0: сказано прямо и полно («штукатурка в кухне закончена»);
   - 0.7-0.9: сказано, но с неоднозначностью в деталях;
   - 0.4-0.7: приходится догадываться о предмете или значении;
   - ниже 0.4: похоже на факт, но уверенности нет.
   Для сумм и сроков будь строже: цифра, прочитанная неуверенно, хуже, \
чем отсутствие цифры. Финансовые факты применяются автоматически только \
при confidence не ниже {FINANCIAL_CONFIDENCE_THRESHOLD}, остальные — \
при {DEFAULT_CONFIDENCE_THRESHOLD}.
5. summary — одна строка на русском языке, как её прочитает менеджер \
в таблице. Без вводных слов, по делу.
6. Поставь needs_human = true, если сообщение содержит претензию, \
конфликт, юридический вопрос или требует решения человека — независимо \
от того, извлеклись ли факты.
7. В тексте встречаются условные идентификаторы вида [PHONE_1], [ADDR_2], \
[NAME_3] — это заменённые персональные данные. Переноси их как есть, \
не пытайся раскрыть или заменить.
8. Суммы приводи числом без пробелов и символа валюты: «3 400 руб» → \
amount 3400, currency "RUB". «5 тыс» → amount 5000.
9. Даты приводи в формате ГГГГ-ММ-ДД. Относительные («завтра», «в \
пятницу») считай от даты сообщения, которая указана в контексте.
10. Для money_movement заполняй direction, account и movement_kind. Это \
кассовый журнал компании, и записи в нём короткие:
   - «оплата уличной двери 35к взял с кассы -Покерная -займ» → direction out, \
account cash, movement_kind loan, amount 35000, item «уличная дверь»;
   - «795.000₽ приход с Пашковки» → direction in, movement_kind income, \
amount 795000, account unknown;
   - «8.000₽ выдал займ дом ростовка на материалы -трубы» → direction out, \
movement_kind loan, item «трубы», amount 8000;
   - «Аванс 30к плиточник, б/н» → direction out, account bank, \
movement_kind labor, amount 30000, counterparty «плиточник».
   Если про наличные или безнал не сказано — account unknown. Не \
додумывай: остаток кассы считается только по явным записям, и \
приписанная касса покажет деньги, которых нет.
"""


_TYPE_HINTS: dict[FactType, str] = {
    FactType.WORK_PROGRESS: "ход работ — этап, помещение, состояние",
    FactType.PURCHASE: "закупка материалов, с суммой или без",
    FactType.PAYMENT: "платёж заказчика или поставщику",
    FactType.ESTIMATE_CHANGE: "изменение сметы: доплата, скидка, пересчёт",
    FactType.SCHEDULE_CHANGE: "сдвиг сроков, перенос даты",
    FactType.ISSUE: "проблема, дефект, задержка, переделка",
    FactType.MATERIAL_REQUEST: "нужны материалы, без указания суммы",
    FactType.MEASUREMENT: "замер, размеры, площади, объёмы",
    FactType.CLIENT_REQUEST: "обращение заказчика: вопрос, пожелание, претензия",
    FactType.STAFF_ASSIGNMENT: "назначение или смена исполнителя, выход бригады",
    FactType.MONEY_MOVEMENT: (
        "движение денег: взяли из кассы, выдали заём объекту, выдали подотчёт, "
        "пришли деньги от заказчика, вернули остаток"
    ),
}


@dataclass(slots=True)
class MessageContext:
    """Переменная часть промпта.

    Идёт после точки кэширования, поэтому может меняться свободно.
    """

    object_code: str | None = None
    object_address: str | None = None
    current_stage: str | None = None
    message_date: date | None = None
    author: str | None = None
    """Условный идентификатор автора, не настоящее имя."""
    recent_messages: list[str] = field(default_factory=list)
    """Последние сообщения чата из рабочей памяти — для контекстных ссылок."""

    def render(self) -> str:
        lines = ["Контекст сообщения:"]
        if self.object_code:
            lines.append(f"- объект: {self.object_code}")
        if self.object_address:
            lines.append(f"- адрес объекта: {self.object_address}")
        if self.current_stage:
            lines.append(f"- текущий этап по плану: {self.current_stage}")
        if self.message_date:
            lines.append(f"- дата сообщения: {self.message_date.isoformat()}")
        if self.author:
            lines.append(f"- автор: {self.author}")
        if self.recent_messages:
            lines.append("- предыдущие сообщения в чате (от старых к новым):")
            lines.extend(f"    {m}" for m in self.recent_messages)
        return "\n".join(lines)


@dataclass(slots=True)
class Extraction:
    """Результат вместе с сопровождающими данными для журнала."""

    result: ExtractionResult
    response: StructuredResponse
    pseudonymizer: Pseudonymizer

    @property
    def degraded(self) -> bool:
        return self.response.degraded


class FactExtractor:
    def __init__(self, router: LlmRouter) -> None:
        self._router = router
        self._instruction = _build_instruction()
        self._schema = api_json_schema(ExtractionResult)
        self._schema_json = json.dumps(self._schema, ensure_ascii=False, sort_keys=True)

    async def extract(
        self,
        text: str,
        *,
        context: MessageContext | None = None,
        known_names: dict[str, str] | None = None,
    ) -> Extraction:
        """Извлечь факты из одного сообщения.

        Псевдонимизация выполняется здесь, а не выше: так исключено, что
        текст уйдёт в модель в обход неё.
        """
        pseudonymizer = Pseudonymizer(known_names=dict(known_names or {}))
        masked = pseudonymizer.mask(text)

        request = StructuredRequest(
            stable_system=self._instruction,
            volatile_system=context.render() if context else None,
            user_content=masked,
            json_schema=self._schema,
            schema_name="extraction_result",
        )

        response = await self._router.complete_structured(request)
        result = self._validate(response)

        log.info(
            "extractor.done",
            provider=response.provider,
            model=response.model,
            facts=len(result.facts),
            needs_human=result.requires_human,
            below_threshold=len(result.facts_requiring_confirmation()),
            degraded=response.degraded,
            input_tokens=response.input_tokens,
            cached_input_tokens=response.cached_input_tokens,
            output_tokens=response.output_tokens,
            masked_values=len(pseudonymizer),
        )
        return Extraction(result=result, response=response, pseudonymizer=pseudonymizer)

    def _validate(self, response: StructuredResponse) -> ExtractionResult:
        """Проверить ответ по схеме.

        Основной провайдер соответствие гарантирует, резервный — нет,
        поэтому проверка выполняется всегда.
        """
        try:
            return ExtractionResult.model_validate(response.payload)
        except ValidationError as exc:
            raise LlmInvalidOutput(
                f"Ответ {response.provider} не соответствует схеме: {exc.error_count()} ошибок"
            ) from exc

    @property
    def schema_fingerprint(self) -> str:
        """Отпечаток схемы — для журнала: по нему видно, какой версией
        экстрактора получен факт."""
        return hashlib.sha256(self._schema_json.encode()).hexdigest()[:12]
