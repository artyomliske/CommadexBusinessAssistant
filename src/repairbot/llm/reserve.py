"""Резервный провайдер — российская модель.

Раздел 7 ТЗ: «При недоступности основного провайдера моделей предусмотрено
переключение на резервный с допустимым снижением качества обработки».
Снижение качества здесь не абстрактное, а конкретное: у российских
провайдеров нет гарантированного соответствия схеме, поэтому схема
передаётся в промпте, а соответствие проверяется на нашей стороне.

Реализован обмен по совместимому с OpenAI протоколу `/chat/completions` —
его поддерживают и прокси, и большинство отечественных предложений.
Конкретный провайдер в ТЗ не определён (пункт 11 «Исходные данные»), поэтому
аутентификация вынесена в конфигурацию: если выбранный провайдер потребует
собственный протокол (как GigaChat с обменом ключа на токен), меняется
только этот класс.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from repairbot.llm.base import (
    LlmInvalidOutput,
    LlmMediaUnsupported,
    LlmUnavailable,
    StructuredRequest,
    StructuredResponse,
)
from repairbot.observability import get_logger

log = get_logger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class OpenAiCompatibleProvider:
    """Провайдер, говорящий по протоколу `/chat/completions`.

    Годится и резервным, и основным. Через него подключается OpenRouter:
    он раздаёт те же модели Claude, но по совместимому с OpenAI протоколу,
    а не по родному протоколу Anthropic.

    Что при этом теряется по сравнению с прямым обращением к Anthropic:
    гарантия соответствия схеме (здесь схема уходит в промпт и проверяется
    на нашей стороне) и серверный fallback при отказе модели. Что не
    теряется — зрение: формат передачи картинок в этом протоколе
    стандартный, и распознавание чеков работает.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        name: str = "reserve",
        supports_media: bool = False,
        degraded: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.supports_media = supports_media
        """Умеет ли **выбранная модель** читать изображения.

        Свойство модели, а не протокола: передать картинку можно всегда,
        но текстовая модель ответит, что на чеке ничего нет, — а это хуже
        ошибки. Поэтому по умолчанию выключено и включается осознанно."""
        self._degraded = degraded
        """Помечать ли ответы как полученные с понижением качества.

        Для резерва — да, это его смысл. Для основного провайдера — нет,
        иначе панель показывала бы деградацию при штатной работе."""
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(10.0, read=90.0),
        )

    async def complete_structured(self, request: StructuredRequest) -> StructuredResponse:
        if request.media and not self.supports_media:
            raise LlmMediaUnsupported(
                f"Модель {self.model} не читает изображения — распознавание "
                "документов отложено"
            )

        # Схема уходит в промпт: соответствие на стороне провайдера не гарантировано.
        system_parts = [request.stable_system]
        if request.volatile_system:
            system_parts.append(request.volatile_system)
        system_parts.append(
            "Ответь строго одним объектом JSON по схеме ниже, без пояснений и "
            "без обрамляющих кавычек кода.\n"
            + json.dumps(request.json_schema, ensure_ascii=False)
        )

        body = {
            "model": request.model or self.model,
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "\n\n".join(system_parts)},
                {"role": "user", "content": _user_content(request)},
            ],
        }

        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TransportError as exc:
            # Класс исключения обязателен. У обрыва соединения и у
            # таймаута `str()` пустой, и без имени класса в журнале
            # остаётся «недоступен: » — строка, по которой нельзя
            # отличить упавший прокси от неверного адреса.
            raise LlmUnavailable(
                f"Провайдер {self.name} недоступен: "
                f"{exc.__class__.__name__}: {str(exc) or 'без подробностей'} "
                f"(адрес {self._client.base_url})"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise LlmUnavailable(
                f"Провайдер {self.name} вернул {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            # Двести символов обрезали ответ ровно на том месте, где
            # начиналась причина: «...image.source.base64: Th». Провайдер
            # оборачивает исходную ошибку в два слоя JSON, и полезное
            # оказывается в самом конце.
            raise LlmInvalidOutput(
                f"Провайдер {self.name} отклонил запрос: "
                f"{response.status_code} {response.text[:800]}"
            )

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmInvalidOutput(f"Неожиданная структура ответа: {exc}") from exc

        usage = data.get("usage") or {}
        return StructuredResponse(
            provider=self.name,
            model=data.get("model", self.model),
            payload=_parse_json_loosely(content),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            degraded=self._degraded,
            meta={"finish_reason": (data["choices"][0] or {}).get("finish_reason")},
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_json_loosely(content: str) -> dict[str, Any]:
    """Разобрать JSON, допуская обрамление текстом.

    Без гарантий соответствия схеме модель нередко добавляет пояснение или
    обрамление ```json. Сначала пробуем строгий разбор, затем — первый
    объект в тексте.
    """
    text = content.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if match is None:
            raise LlmInvalidOutput("В ответе провайдера нет JSON") from None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LlmInvalidOutput(f"JSON провайдера повреждён: {exc}") from exc

    if not isinstance(payload, dict):
        raise LlmInvalidOutput(f"Ожидался объект JSON, получен {type(payload).__name__}")
    return payload


def _user_content(request: StructuredRequest) -> str | list[dict[str, Any]]:
    """Пользовательское сообщение: строка либо список блоков с вложениями.

    Без вложений отдаём строку — так запрос остаётся ровно таким, каким
    был до появления распознавания, и ничего не ломается у провайдеров,
    которые списки блоков не принимают.

    Вложения идут перед текстом: модель отвечает точнее, когда инструкция
    читается после документа.

    PDF и картинка передаются **по-разному**. Картинка — блоком
    `image_url`, документ — блоком `file`. Отправить PDF как картинку
    нельзя: шлюз такой запрос отклоняет, и распознавание смет и счетов
    не работало бы, хотя формат числится поддерживаемым.
    """
    if not request.media:
        return request.user_content

    blocks: list[dict[str, Any]] = []
    for index, part in enumerate(request.media, start=1):
        if not part.supported:
            raise LlmInvalidOutput(f"Формат {part.media_type} не поддерживается")
        encoded = base64.b64encode(part.data).decode("ascii")
        data_url = f"data:{part.media_type};base64,{encoded}"
        if part.is_pdf:
            blocks.append(
                {
                    "type": "file",
                    # Имя обязательно, содержимого не касается: без него
                    # шлюз отвечает отказом на разбор вложения.
                    "file": {"filename": part.filename or f"документ-{index}.pdf",
                             "file_data": data_url},
                }
            )
        else:
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    blocks.append({"type": "text", "text": request.user_content})
    return blocks


ReserveProvider = OpenAiCompatibleProvider
"""Прежнее имя. Класс стал общим, когда через тот же протокол пошёл
основной провайдер, — переименовывать вызовы ради этого не стали."""
