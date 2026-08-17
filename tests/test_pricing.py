"""Стоимость обращений к модели: расчёт и оценка до запуска.

Считалось по одной ставке для всех моделей, и расход на дорогую
занижался впятеро. Ошибка тихая: цифра выглядит правдоподобной, и
сверять её с провайдером никто не идёт — пока не кончатся деньги.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from repairbot.db.models import LlmCall
from repairbot.llm import pricing


async def _call(session, *, purpose="document", model="anthropic/claude-opus-4.1", **kw):
    session.add(
        LlmCall(
            purpose=purpose,
            provider="openrouter",
            model=model,
            input_tokens=kw.get("input_tokens", 3000),
            cached_input_tokens=kw.get("cached_input_tokens", 0),
            output_tokens=kw.get("output_tokens", 150),
        )
    )
    await session.flush()


# --- ставки ---


@pytest.mark.parametrize(
    ("model", "input_usd"),
    [
        ("anthropic/claude-opus-4.1", 15.0),
        ("claude-opus-4-1-20260805", 15.0),
        ("anthropic/claude-sonnet-4.5", 3.0),
        ("anthropic/claude-haiku-4.5", 1.0),
    ],
)
def test_model_is_recognized_by_a_fragment_of_its_name(model, input_usd):
    """Имя приходит в разных видах, точное совпадение промахнётся."""
    assert pricing.price_for(model).input_usd == input_usd


def test_unknown_model_is_not_priced_as_free():
    """Занизить страшнее: заниженная оценка не остановит дорогую операцию."""
    price = pricing.price_for("что-то-новое")

    assert price.input_usd > 0
    assert price.output_usd > 0


def test_opus_costs_five_times_sonnet():
    tokens = {"input_tokens": 1_000_000, "cached_tokens": 0, "output_tokens": 0}

    opus = pricing.cost_usd("claude-opus-4.1", **tokens)
    sonnet = pricing.cost_usd("claude-sonnet-4.5", **tokens)

    assert opus == pytest.approx(sonnet * 5)


def test_real_numbers_match_the_provider():
    """Сверено со счётом OpenRouter: 494 тыс. на вход, 26 тыс. на выход."""
    usd = pricing.cost_usd(
        "anthropic/claude-opus-4.1",
        input_tokens=493_740,
        cached_tokens=0,
        output_tokens=25_569,
    )

    assert usd == pytest.approx(9.32, abs=0.01)


# --- оценка до запуска ---


async def test_estimate_uses_our_own_history(db_session):
    """Своя история точнее любого предположения."""
    for _ in range(12):
        await _call(db_session)

    estimate = await pricing.estimate_for(db_session, "document", items=10)

    assert estimate.based_on_history
    assert estimate.per_item_usd == pytest.approx(0.0562, abs=0.001)
    assert estimate.total_usd == pytest.approx(0.562, abs=0.01)


async def test_short_history_falls_back_to_an_average(db_session):
    """На трёх вызовах среднее неустойчиво и обманет сильнее, чем оценка."""
    for _ in range(3):
        await _call(db_session)

    estimate = await pricing.estimate_for(db_session, "document", items=100)

    assert not estimate.based_on_history
    assert estimate.total_usd == pytest.approx(6.0)


async def test_estimate_without_any_history(db_session):
    estimate = await pricing.estimate_for(db_session, "document", items=50)

    assert not estimate.based_on_history
    assert estimate.total_usd > 0


async def test_estimate_reads_as_a_sentence(db_session):
    estimate = await pricing.estimate_for(db_session, "document", items=89)

    assert "89 шт." in estimate.render()
    assert "$" in estimate.render()


# --- сколько уже потрачено ---


async def test_spend_counts_each_model_by_its_own_rate(db_session):
    await _call(db_session, model="anthropic/claude-opus-4.1", input_tokens=1_000_000)
    await _call(db_session, model="anthropic/claude-sonnet-4.5", input_tokens=1_000_000)

    total = await pricing.spent_usd(db_session)

    # 15 + 3 за вход плюс выход обеих моделей.
    assert total == pytest.approx(15.0 + 3.0 + 0.01125 + 0.00225, abs=0.001)


async def test_spend_can_be_limited_to_a_window(db_session):
    await _call(db_session)
    old = LlmCall(
        purpose="document",
        provider="openrouter",
        model="anthropic/claude-opus-4.1",
        input_tokens=1_000_000,
        cached_input_tokens=0,
        output_tokens=0,
        created_at=datetime.now(tz=UTC) - timedelta(days=40),
    )
    db_session.add(old)
    await db_session.flush()

    recent = await pricing.spent_usd(db_session, since=datetime.now(tz=UTC) - timedelta(days=1))
    everything = await pricing.spent_usd(db_session)

    assert recent < 1
    assert everything > 15


async def test_spend_can_be_limited_to_a_purpose(db_session):
    await _call(db_session, purpose="document", input_tokens=1_000_000)
    await _call(db_session, purpose="assistant", input_tokens=1_000_000)

    documents = await pricing.spent_usd(db_session, purpose="document")

    assert documents == pytest.approx(15.0 + 0.01125, abs=0.001)
