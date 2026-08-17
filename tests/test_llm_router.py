"""Переключение на резервный провайдер (раздел 7 ТЗ, отказоустойчивость)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from repairbot.llm.base import (
    LlmInvalidOutput,
    LlmRefusal,
    LlmUnavailable,
    MediaPart,
    StructuredRequest,
    StructuredResponse,
)
from repairbot.llm.router import LlmRouter


@dataclass
class FakeProvider:
    name: str
    model: str = "fake-model"
    fail_with: Exception | None = None
    degraded: bool = False
    supports_media: bool = True
    calls: list[str] = field(default_factory=list)
    closed: bool = False

    async def complete_structured(self, request: StructuredRequest) -> StructuredResponse:
        self.calls.append(request.user_content)
        if self.fail_with is not None:
            raise self.fail_with
        return StructuredResponse(
            provider=self.name,
            model=self.model,
            payload={"ok": True},
            degraded=self.degraded,
        )

    async def aclose(self) -> None:
        self.closed = True


def _request(text: str = "тест") -> StructuredRequest:
    return StructuredRequest(
        stable_system="инструкция",
        user_content=text,
        json_schema={"type": "object"},
        schema_name="test",
    )


async def test_primary_serves_when_healthy():
    primary = FakeProvider("claude")
    reserve = FakeProvider("reserve", degraded=True)

    response = await LlmRouter(primary, reserve).complete_structured(_request())

    assert response.provider == "claude"
    assert response.degraded is False
    assert reserve.calls == []


async def test_switches_to_reserve_when_primary_unavailable():
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True)

    response = await LlmRouter(primary, reserve).complete_structured(_request())

    assert response.provider == "reserve"
    assert response.degraded is True


async def test_refusal_is_not_retried_on_reserve():
    """Отказ модели — решение по содержанию запроса, а не сбой.

    Переигрывать его на другой модели значило бы обходить предохранитель.
    """
    primary = FakeProvider("claude", fail_with=LlmRefusal("cyber"))
    reserve = FakeProvider("reserve")

    with pytest.raises(LlmRefusal):
        await LlmRouter(primary, reserve).complete_structured(_request())

    assert reserve.calls == []


async def test_schema_error_is_not_retried_on_reserve():
    """Резерв не исправит несоответствие схемы — только потратит токены."""
    primary = FakeProvider("claude", fail_with=LlmInvalidOutput("плохая схема"))
    reserve = FakeProvider("reserve")

    with pytest.raises(LlmInvalidOutput):
        await LlmRouter(primary, reserve).complete_structured(_request())

    assert reserve.calls == []


async def test_raises_when_primary_down_and_no_reserve():
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))

    with pytest.raises(LlmUnavailable):
        await LlmRouter(primary, None).complete_structured(_request())


async def test_primary_not_hammered_after_repeated_failures():
    """После серии сбоев держим паузу, а не дёргаем основной на каждый запрос."""
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True)
    router = LlmRouter(primary, reserve, cooldown_seconds=300)

    for i in range(6):
        await router.complete_structured(_request(f"сообщение {i}"))

    # Порог — три сбоя; дальше основной в паузе.
    assert len(primary.calls) == 3
    assert len(reserve.calls) == 6
    assert router.degraded is True


async def test_recovers_to_primary_after_success():
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True)
    router = LlmRouter(primary, reserve, cooldown_seconds=0)

    for _ in range(4):
        await router.complete_structured(_request())

    primary.fail_with = None
    response = await router.complete_structured(_request())

    assert response.provider == "claude"
    assert router.degraded is False


async def test_aclose_closes_both_providers():
    primary = FakeProvider("claude")
    reserve = FakeProvider("reserve")

    await LlmRouter(primary, reserve).aclose()

    assert primary.closed and reserve.closed


# --- запросы с изображениями ---


def _media_request() -> StructuredRequest:
    request = _request("разбери чек")
    request.media = (MediaPart(media_type="image/jpeg", data=b"\xff\xd8"),)
    return request


async def test_blind_reserve_does_not_get_a_picture():
    """Резерв без зрения не откажется, а ответит по одному тексту.

    То есть сообщит, что на чеке ничего нет. Такой ответ хуже ошибки:
    ошибку видно, а пустой разбор чека молча уедет в журнал как факт.
    """
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True, supports_media=False)

    with pytest.raises(LlmUnavailable):
        await LlmRouter(primary, reserve).complete_structured(_media_request())

    assert reserve.calls == []


async def test_seeing_reserve_does_get_a_picture():
    """Если резерв читает изображения, отказоустойчивость работает как обычно."""
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True, supports_media=True)

    response = await LlmRouter(primary, reserve).complete_structured(_media_request())

    assert response.provider == "reserve"


async def test_blind_reserve_still_serves_text():
    """Ограничение касается только запросов с картинкой."""
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True, supports_media=False)

    response = await LlmRouter(primary, reserve).complete_structured(_request())

    assert response.provider == "reserve"


async def test_open_breaker_does_not_route_pictures_to_a_blind_reserve():
    """Пауза после отказов не должна открывать обходной путь.

    Основной провайдер на паузе, и обычные запросы идут в резерв. Запрос
    с картинкой в этот момент обязан упасть, а не разобраться вслепую.
    """
    primary = FakeProvider("claude", fail_with=LlmUnavailable("503"))
    reserve = FakeProvider("reserve", degraded=True, supports_media=False)
    router = LlmRouter(primary, reserve, cooldown_seconds=60.0)

    for _ in range(3):
        await router.complete_structured(_request())
    assert router.degraded

    with pytest.raises(LlmUnavailable):
        await router.complete_structured(_media_request())
    assert len(reserve.calls) == 3


# --- совместимый с OpenAI протокол: OpenRouter ---


def _openai_request(with_image: bool = False) -> StructuredRequest:
    request = _request("разбери")
    if with_image:
        request.media = (MediaPart(media_type="image/jpeg", data=b"\xff\xd8\xff"),)
    return request


async def test_image_goes_as_a_data_uri():
    """Формат передачи картинок в этом протоколе стандартный.

    Именно поэтому распознавание чеков работает и через OpenRouter, а не
    только через родной протокол Anthropic.
    """
    import base64
    import json

    import httpx

    from repairbot.llm.reserve import OpenAiCompatibleProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "anthropic/claude-opus-4",
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    provider = OpenAiCompatibleProvider(
        api_key="k",
        model="anthropic/claude-opus-4",
        base_url="https://openrouter.test/api/v1",
        supports_media=True,
        degraded=False,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )

    response = await provider.complete_structured(_openai_request(with_image=True))

    content = seen["body"]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    expected = base64.b64encode(b"\xff\xd8\xff").decode()
    assert content[0]["image_url"]["url"] == f"data:image/jpeg;base64,{expected}"
    # Текст идёт после картинки: так модель отвечает точнее.
    assert content[1]["type"] == "text"
    assert response.degraded is False


async def test_plain_text_request_stays_a_plain_string():
    """Списки блоков принимают не все шлюзы — без картинок их не шлём."""
    import json

    import httpx

    from repairbot.llm.reserve import OpenAiCompatibleProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}}
        )

    provider = OpenAiCompatibleProvider(
        api_key="k",
        model="m",
        base_url="https://openrouter.test/api/v1",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )

    await provider.complete_structured(_openai_request())

    assert seen["body"]["messages"][1]["content"] == "разбери"


async def test_text_only_model_refuses_pictures():
    """Текстовая модель ответила бы «на чеке ничего нет» — это хуже ошибки."""
    from repairbot.llm.base import LlmMediaUnsupported
    from repairbot.llm.reserve import OpenAiCompatibleProvider

    provider = OpenAiCompatibleProvider(
        api_key="k", model="текстовая", base_url="https://x", supports_media=False
    )

    with pytest.raises(LlmMediaUnsupported):
        await provider.complete_structured(_openai_request(with_image=True))


def test_reserve_marks_degradation_but_primary_does_not():
    """Панель не должна показывать деградацию при штатной работе."""
    from repairbot.llm.reserve import OpenAiCompatibleProvider

    reserve = OpenAiCompatibleProvider(api_key="k", model="m", base_url="https://x")
    primary = OpenAiCompatibleProvider(
        api_key="k", model="m", base_url="https://x", name="primary", degraded=False
    )

    assert reserve._degraded is True
    assert primary._degraded is False


# --- сообщения об отказе ---


async def test_transport_failure_names_the_exception_class(settings):
    """Пустое «недоступен: » в журнале не позволяет понять причину.

    У обрыва соединения и у таймаута `str()` пустой, и без имени класса
    упавший прокси неотличим от неверного адреса.
    """
    import httpx
    import pytest

    from repairbot.llm.base import LlmUnavailable
    from repairbot.llm.reserve import OpenAiCompatibleProvider

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("")

    provider = OpenAiCompatibleProvider(
        api_key="k",
        model="m",
        base_url="https://openrouter.ai/api/v1",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.ai/api/v1"
        ),
    )

    with pytest.raises(LlmUnavailable) as exc_info:
        await provider.complete_structured(
            StructuredRequest(
                stable_system="s", user_content="u", json_schema={}, schema_name="t"
            )
        )

    message = str(exc_info.value)
    assert "ConnectError" in message
    assert "без подробностей" in message
    assert "openrouter.ai" in message
