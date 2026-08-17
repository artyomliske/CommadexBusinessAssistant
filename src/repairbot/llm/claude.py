"""Основной провайдер: Claude через прокси.

Используются три возможности API, каждая по делу:

* **structured outputs** (`output_config.format`) — ответ гарантированно
  соответствует схеме, не нужно вылавливать JSON из текста;
* **кэширование промпта** — инструкция и схема одинаковы для всех сообщений,
  поэтому кэшируются; переменный контекст идёт после точки кэширования,
  иначе кэш обнуляется на каждом запросе;
* **серверный fallback при отказе** — у модели есть предохранители
  безопасности, и запрос может быть отклонён с `stop_reason: "refusal"`;
  `fallbacks="default"` заставляет API переиграть такой запрос на другой
  модели в рамках того же вызова.

`effort: "low"` выбран сознательно: извлечение фактов из короткого сообщения
не требует глубокого рассуждения, а расход токенов — статья затрат из
раздела 9 ТЗ. Уровень стоит перепроверить на реальной переписке заказчика.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import anthropic

from repairbot.llm.base import (
    LlmInvalidOutput,
    LlmRefusal,
    LlmUnavailable,
    StructuredRequest,
    StructuredResponse,
)
from repairbot.observability import get_logger

log = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    name = "claude"
    supports_media = True

    def __init__(
        self,
        api_key: str | None,
        model: str = "claude-opus-5",
        *,
        base_url: str | None = None,
        effort: str = "low",
        enable_refusal_fallback: bool = True,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._effort = effort
        self._enable_refusal_fallback = enable_refusal_fallback
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=2,
            timeout=60.0,
        )

    async def complete_structured(self, request: StructuredRequest) -> StructuredResponse:
        # Стабильный блок — с точкой кэширования, переменный — после неё.
        system: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": request.stable_system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if request.volatile_system:
            system.append({"type": "text", "text": request.volatile_system})

        params: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": _user_content(request)}],
            "output_config": {
                "effort": self._effort,
                "format": {
                    "type": "json_schema",
                    "schema": request.json_schema,
                },
            },
        }
        if self._enable_refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        try:
            response = await self._client.beta.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise LlmUnavailable(f"Превышен лимит запросов: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LlmUnavailable(f"Провайдер вернул {exc.status_code}") from exc
            raise LlmInvalidOutput(f"Запрос отклонён: {exc.status_code} {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LlmUnavailable(f"Сеть недоступна: {exc}") from exc

        if response.stop_reason == "refusal":
            details = response.stop_details
            raise LlmRefusal(
                getattr(details, "category", None),
                getattr(details, "explanation", None),
            )

        payload = _extract_json(response)

        usage = response.usage
        return StructuredResponse(
            provider=self.name,
            model=response.model,
            payload=payload,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            degraded=False,
            meta={"stop_reason": response.stop_reason},
        )

    async def aclose(self) -> None:
        await self._client.close()


def _user_content(request: StructuredRequest) -> str | list[dict[str, Any]]:
    """Содержимое пользовательского сообщения.

    Без вложений — обычная строка: так запрос остаётся ровно таким, каким
    был до появления распознавания, и кэш промпта не сбивается.

    Изображения идут **перед** текстом: модель лучше отвечает, когда
    инструкция читается после картинки, а не до неё.
    """
    if not request.media:
        return request.user_content

    blocks: list[dict[str, Any]] = []
    for part in request.media:
        if not part.supported:
            raise LlmInvalidOutput(f"Формат {part.media_type} не поддерживается")
        blocks.append(
            {
                "type": "document" if part.is_pdf else "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": base64.b64encode(part.data).decode("ascii"),
                },
            }
        )
    blocks.append({"type": "text", "text": request.user_content})
    return blocks


def _extract_json(response: Any) -> dict[str, Any]:
    """Достать JSON из ответа.

    При `output_config.format` содержимое приходит текстовым блоком с
    валидным JSON. Блоков может быть несколько (например, thinking идёт
    первым), поэтому ищем первый текстовый.
    """
    if response.stop_reason == "max_tokens":
        raise LlmInvalidOutput(
            "Ответ обрезан по max_tokens: JSON неполный. Увеличьте max_output_tokens."
        )

    for block in response.content:
        if getattr(block, "type", None) != "text":
            continue
        text = block.text.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LlmInvalidOutput(f"Ответ не является JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LlmInvalidOutput(f"Ожидался объект JSON, получен {type(payload).__name__}")
        return payload

    raise LlmInvalidOutput("В ответе нет текстового блока")
