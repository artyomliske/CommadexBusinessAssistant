"""Схема фактов, пороги достоверности и JSON Schema для structured outputs."""

from __future__ import annotations

from repairbot.domain.facts import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FINANCIAL_CONFIDENCE_THRESHOLD,
    ExtractionResult,
    Fact,
    FactType,
    api_json_schema,
    threshold_for,
)


def test_financial_facts_have_stricter_threshold():
    assert threshold_for(FactType.PURCHASE) == FINANCIAL_CONFIDENCE_THRESHOLD
    assert threshold_for(FactType.PAYMENT) == FINANCIAL_CONFIDENCE_THRESHOLD
    assert threshold_for(FactType.ESTIMATE_CHANGE) == FINANCIAL_CONFIDENCE_THRESHOLD
    assert threshold_for(FactType.WORK_PROGRESS) == DEFAULT_CONFIDENCE_THRESHOLD


def test_purchase_at_071_is_below_threshold():
    """Пример из ТЗ: закупка с confidence 0.71 не применяется автоматически."""
    fact = Fact(type=FactType.PURCHASE, confidence=0.71, summary="грунтовка, 2 шт, 3400 ₽")

    assert fact.is_financial
    assert fact.below_threshold


def test_work_progress_at_071_is_applied():
    fact = Fact(type=FactType.WORK_PROGRESS, confidence=0.71, summary="штукатурка кухни завершена")

    assert not fact.is_financial
    assert not fact.below_threshold


def test_result_splits_facts_by_threshold():
    result = ExtractionResult(
        facts=[
            Fact(type=FactType.WORK_PROGRESS, confidence=0.92, summary="штукатурка завершена"),
            Fact(type=FactType.PURCHASE, confidence=0.71, summary="грунтовка 3400"),
        ]
    )

    assert len(result.applicable_facts()) == 1
    assert len(result.facts_requiring_confirmation()) == 1
    assert result.requires_human is True


def test_needs_human_flag_alone_requires_human():
    result = ExtractionResult(facts=[], needs_human=True)

    assert result.requires_human is True


def test_clean_result_does_not_require_human():
    result = ExtractionResult(
        facts=[Fact(type=FactType.WORK_PROGRESS, confidence=0.95, summary="готово")]
    )

    assert result.requires_human is False


def test_empty_extraction_is_valid():
    """Пустой ответ — нормальный результат, а не ошибка."""
    result = ExtractionResult.model_validate({"facts": [], "needs_human": False})

    assert result.facts == []


def test_tz_example_payload_validates():
    """Формат из раздела 3.2 ТЗ разбирается схемой."""
    result = ExtractionResult.model_validate(
        {
            "facts": [
                {
                    "type": "work_progress",
                    "stage": "штукатурка",
                    "room": "кухня",
                    "status": "завершено",
                    "confidence": 0.92,
                    "summary": "штукатурка на кухне завершена",
                },
                {
                    "type": "purchase",
                    "item": "грунтовка",
                    "qty": 2,
                    "amount": 3400,
                    "currency": "RUB",
                    "confidence": 0.71,
                    "summary": "закуплена грунтовка, 2 шт, 3400 ₽",
                },
            ],
            "needs_human": False,
        }
    )

    assert [f.type for f in result.facts] == [FactType.WORK_PROGRESS, FactType.PURCHASE]
    assert result.facts[1].amount == 3400


def test_api_schema_has_no_unsupported_keywords():
    """Anthropic не поддерживает числовые и строковые ограничения."""
    schema = api_json_schema(ExtractionResult)
    flat = repr(schema)

    for keyword in ("minimum", "maximum", "minLength", "maxLength", "multipleOf", "pattern"):
        assert keyword not in flat, keyword


def test_api_schema_closes_objects_and_lists_all_required():
    schema = api_json_schema(ExtractionResult)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    fact_schema = schema["$defs"]["Fact"]
    assert fact_schema["additionalProperties"] is False
    assert set(fact_schema["required"]) == set(fact_schema["properties"])


def test_api_schema_is_deterministic():
    """Иначе кэш промпта обнулялся бы на каждом запросе."""
    assert api_json_schema(ExtractionResult) == api_json_schema(ExtractionResult)
