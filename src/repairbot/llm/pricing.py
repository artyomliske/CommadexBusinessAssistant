"""Сколько стоит обращение к модели.

Считалось по одной ставке для всех моделей — той, что задана в
настройках. На практике моделей две: Sonnet на поток сообщений и Opus
на документы, а Opus дороже впятеро. Страница расхода показывала около
двух долларов там, где провайдер списал девять. Ошибка тихая: цифра
выглядит правдоподобной, сверять её никто не идёт.

Поэтому ставки — по моделям, а не одна на всех. Имя модели ищется по
вхождению: у провайдеров оно приходит в разных видах
(`anthropic/claude-opus-4.1`, `claude-opus-4-1-20260805`), и точное
совпадение промахнулось бы на первой же смене версии.

Отдельно — оценка **до** запуска. Средняя стоимость вызова берётся из
собственного журнала, а не из головы: у нас уже есть сотни вызовов, и
своя история точнее любого предположения. Пока истории нет, работают
запасные значения.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PRICE = ("прочее", 3.0, 0.3, 15.0)
"""Ставка для незнакомой модели: как у Sonnet.

Занизить страшнее, чем завысить: заниженная оценка не остановит дорогую
операцию, ради которой предупреждение и делалось."""

#: (кусок имени, читаемое имя, вход, вход из кэша, выход) — USD за миллион.
PRICES: tuple[tuple[str, str, float, float, float], ...] = (
    ("opus-4.1", "Claude Opus 4.1", 15.0, 1.5, 75.0),
    ("opus", "Claude Opus", 15.0, 1.5, 75.0),
    ("sonnet", "Claude Sonnet", 3.0, 0.3, 15.0),
    ("haiku", "Claude Haiku", 1.0, 0.1, 5.0),
    ("gpt-4o", "GPT-4o", 2.5, 1.25, 10.0),
)


@dataclass(frozen=True, slots=True)
class Price:
    title: str
    input_usd: float
    cached_usd: float
    output_usd: float

    def cost(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_usd
            + cached_tokens * self.cached_usd
            + output_tokens * self.output_usd
        ) / 1_000_000


def price_for(model: str | None) -> Price:
    """Ставка для модели. Незнакомая считается по запасной."""
    name = (model or "").lower()
    for needle, title, inp, cached, out in PRICES:
        if needle in name:
            return Price(title=title, input_usd=inp, cached_usd=cached, output_usd=out)
    title, inp, cached, out = DEFAULT_PRICE
    return Price(title=title, input_usd=inp, cached_usd=cached, output_usd=out)


def cost_usd(
    model: str | None, *, input_tokens: int, cached_tokens: int, output_tokens: int
) -> float:
    return price_for(model).cost(input_tokens, cached_tokens, output_tokens)


# --- оценка до запуска ---

FALLBACK_PER_CALL_USD: dict[str, float] = {
    "document": 0.06,
    "extraction": 0.02,
    "assistant": 0.03,
    "client_reply": 0.01,
}
"""Во что обходится один вызов, пока своей истории нет.

Числа взяты из первых суток работы: документ на Opus — около шести
центов, разбор сообщения на Sonnet — около двух."""


@dataclass(frozen=True, slots=True)
class Estimate:
    """Во что обойдётся операция. Цифры округляются вверх."""

    items: int
    per_item_usd: float
    total_usd: float
    based_on_history: bool

    def render(self) -> str:
        источник = "по нашей истории" if self.based_on_history else "по средней оценке"
        return (
            f"{self.items} шт. × ${self.per_item_usd:.3f} ≈ "
            f"${self.total_usd:.2f} ({источник})"
        )

    @property
    def rub(self) -> float:
        return self.total_usd * 95.0


async def estimate_for(session, purpose: str, items: int) -> Estimate:
    """Во что обойдётся `items` вызовов такого рода.

    Средняя стоимость берётся из журнала: считаем по фактическим токенам
    и ставке той модели, которой вызов и делался. Меньше десяти вызовов
    в истории — среднее неустойчиво, берём запасное значение.
    """
    from sqlalchemy import func, select

    from repairbot.db.models import LlmCall

    rows = await session.execute(
        select(
            LlmCall.model,
            func.count(LlmCall.id),
            func.sum(LlmCall.input_tokens),
            func.sum(LlmCall.cached_input_tokens),
            func.sum(LlmCall.output_tokens),
        )
        .where(LlmCall.purpose == purpose)
        .group_by(LlmCall.model)
    )

    calls = 0
    total = 0.0
    for model, count, inp, cached, out in rows.all():
        calls += int(count or 0)
        total += cost_usd(
            model,
            input_tokens=int(inp or 0),
            cached_tokens=int(cached or 0),
            output_tokens=int(out or 0),
        )

    if calls >= 10 and total > 0:
        per_item = total / calls
        return Estimate(items, per_item, per_item * items, based_on_history=True)

    per_item = FALLBACK_PER_CALL_USD.get(purpose, 0.05)
    return Estimate(items, per_item, per_item * items, based_on_history=False)


async def spent_usd(session, *, since=None, purpose: str | None = None) -> float:
    """Сколько уже потрачено по журналу, посчитанное по ставкам моделей."""
    from sqlalchemy import func, select

    from repairbot.db.models import LlmCall

    stmt = select(
        LlmCall.model,
        func.sum(LlmCall.input_tokens),
        func.sum(LlmCall.cached_input_tokens),
        func.sum(LlmCall.output_tokens),
    ).group_by(LlmCall.model)
    if since is not None:
        stmt = stmt.where(LlmCall.created_at >= since)
    if purpose is not None:
        stmt = stmt.where(LlmCall.purpose == purpose)

    total = 0.0
    for model, inp, cached, out in (await session.execute(stmt)).all():
        total += cost_usd(
            model,
            input_tokens=int(inp or 0),
            cached_tokens=int(cached or 0),
            output_tokens=int(out or 0),
        )
    return total
