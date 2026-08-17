"""Схема извлекаемых фактов (раздел 3.2 ТЗ).

Каждый факт содержит оценку достоверности и ссылку на исходное сообщение.
Значения ниже порога не применяются автоматически и направляются менеджеру
на подтверждение: 0,8 для финансовых данных, 0,6 для остальных.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FactType(StrEnum):
    WORK_PROGRESS = "work_progress"
    """Ход работ: этап, помещение, состояние."""
    PURCHASE = "purchase"
    """Закупка материалов с суммой."""
    PAYMENT = "payment"
    """Платёж заказчика или поставщику."""
    ESTIMATE_CHANGE = "estimate_change"
    """Изменение сметы: доплата, скидка, пересчёт."""
    SCHEDULE_CHANGE = "schedule_change"
    """Сдвиг сроков."""
    ISSUE = "issue"
    """Проблема, дефект, задержка."""
    MATERIAL_REQUEST = "material_request"
    """Запрос материалов без суммы."""
    MEASUREMENT = "measurement"
    """Замер, размеры, площади."""
    CLIENT_REQUEST = "client_request"
    """Обращение заказчика: вопрос, пожелание, претензия."""
    STAFF_ASSIGNMENT = "staff_assignment"
    """Назначение или смена исполнителя."""
    MONEY_MOVEMENT = "money_movement"
    """Движение денег: касса, заём объекту, подотчёт, приход.

    Отдельно от `payment`. Тот описывает «заплатили столько-то», а здесь
    важно ещё откуда взяли и в каком качестве отдали: «взял с кассы 35к
    и выдал займом на Покерную» — это минус в кассе и плюс к вложенному
    в объект одновременно."""


FINANCIAL_FACT_TYPES: frozenset[FactType] = frozenset(
    {
        FactType.PURCHASE,
        FactType.PAYMENT,
        FactType.ESTIMATE_CHANGE,
        FactType.MONEY_MOVEMENT,
    }
)
"""Финансовые типы — к ним применяется повышенный порог достоверности."""

FINANCIAL_CONFIDENCE_THRESHOLD = 0.8
DEFAULT_CONFIDENCE_THRESHOLD = 0.6


def threshold_for(fact_type: FactType) -> float:
    return (
        FINANCIAL_CONFIDENCE_THRESHOLD
        if fact_type in FINANCIAL_FACT_TYPES
        else DEFAULT_CONFIDENCE_THRESHOLD
    )


class WorkStatus(StrEnum):
    PLANNED = "запланировано"
    IN_PROGRESS = "в работе"
    DONE = "завершено"
    BLOCKED = "приостановлено"


class Fact(BaseModel):
    """Один извлечённый факт.

    Поля намеренно плоские: так их проще проверять глазами в таблице
    и сопоставлять с исходным сообщением.
    """

    model_config = ConfigDict(extra="forbid")

    type: FactType
    confidence: float = Field(
        ge=0.0, le=1.0, description="Оценка достоверности от 0 до 1"
    )
    """Диапазон проверяется на нашей стороне: из схемы для API числовые
    ограничения вырезаются (`api_json_schema`), потому что провайдер их
    не поддерживает."""
    summary: str = Field(description="Формулировка факта одной строкой, на русском языке")

    stage: str | None = Field(default=None, description="Этап работ, например «штукатурка»")
    room: str | None = Field(default=None, description="Помещение, например «кухня»")
    status: WorkStatus | None = None

    item: str | None = Field(default=None, description="Материал или предмет закупки")
    qty: float | None = Field(default=None, description="Количество")
    unit: str | None = Field(default=None, description="Единица измерения")
    amount: float | None = Field(default=None, description="Сумма")
    currency: str | None = Field(default=None, description="Код валюты, обычно RUB")

    due_date: date | None = Field(default=None, description="Дата, о которой идёт речь")
    person: str | None = Field(default=None, description="Условный идентификатор человека")

    # --- только для money_movement ---
    direction: str | None = Field(
        default=None, description="Направление денег: in — приход, out — расход"
    )
    account: str | None = Field(
        default=None,
        description="Откуда или куда: cash — наличные из кассы, bank — безнал (б/н), "
        "unknown — не сказано",
    )
    movement_kind: str | None = Field(
        default=None,
        description="Вид движения: loan — заём объекту, advance — подотчёт, "
        "material — материалы, labor — работы, income — приход от заказчика, "
        "return — возврат, other — прочее",
    )
    counterparty: str | None = Field(
        default=None, description="Кому или от кого: поставщик, работник, заказчик"
    )

    @property
    def is_financial(self) -> bool:
        return self.type in FINANCIAL_FACT_TYPES

    @property
    def threshold(self) -> float:
        return threshold_for(self.type)

    @property
    def below_threshold(self) -> bool:
        return self.confidence < self.threshold


class ExtractionResult(BaseModel):
    """Результат работы экстрактора по одному сообщению.

    Соответствует формату из раздела 3.2 ТЗ.
    """

    model_config = ConfigDict(extra="forbid")

    facts: list[Fact] = Field(default_factory=list)
    needs_human: bool = Field(
        default=False,
        description="Сообщение требует внимания менеджера независимо от достоверности фактов",
    )
    note: str | None = Field(
        default=None, description="Краткое пояснение, если извлечение неоднозначно"
    )

    def facts_requiring_confirmation(self) -> list[Fact]:
        return [f for f in self.facts if f.below_threshold]

    def applicable_facts(self) -> list[Fact]:
        return [f for f in self.facts if not f.below_threshold]

    @property
    def requires_human(self) -> bool:
        return self.needs_human or bool(self.facts_requiring_confirmation())


_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)


def api_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema для structured outputs.

    Anthropic не поддерживает числовые и строковые ограничения и требует
    `additionalProperties: false` со полным перечислением `required`.
    Ограничения остаются в pydantic-модели и проверяются на нашей стороне,
    а из схемы для API убираются.
    """
    return _clean(model.model_json_schema())


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(item) for item in node]
    if not isinstance(node, dict):
        return node

    result = {k: _clean(v) for k, v in node.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}

    if result.get("type") == "object" and "properties" in result:
        result["additionalProperties"] = False
        # Все свойства обязательны: необязательные объявлены как nullable.
        result["required"] = list(result["properties"])
    return result
