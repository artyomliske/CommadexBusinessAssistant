"""Контракт провайдера языковой модели.

Экстрактор и клиентский агент работают через этот интерфейс и не знают,
какой провайдер обслуживает запрос. Это то, что делает возможным
переключение на резервный провайдер (раздел 7 ТЗ, отказоустойчивость).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

IMAGE_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
PDF_MEDIA_TYPE = "application/pdf"
SUPPORTED_MEDIA_TYPES: frozenset[str] = IMAGE_MEDIA_TYPES | {PDF_MEDIA_TYPE}


@dataclass(frozen=True, slots=True)
class MediaPart:
    """Изображение или PDF, приложенные к запросу.

    Байты держим сырыми, а в base64 переводим у провайдера: кодирование
    привязано к его формату запроса, и делать его заранее значило бы
    таскать по коду строку втрое тяжелее исходника.
    """

    media_type: str
    data: bytes
    filename: str | None = None
    """Имя файла. Нужно только PDF: шлюз, совместимый с OpenAI, требует
    его в блоке документа и без него отвечает отказом."""

    @property
    def is_pdf(self) -> bool:
        return self.media_type == PDF_MEDIA_TYPE

    @property
    def supported(self) -> bool:
        return self.media_type in SUPPORTED_MEDIA_TYPES


@dataclass(slots=True)
class StructuredRequest:
    """Запрос на структурированный ответ по схеме.

    `system` разделён на стабильную и переменную части: стабильная
    кэшируется провайдером, переменная меняется от запроса к запросу.
    Смешивать их нельзя — любое изменение в начале промпта обнуляет кэш.
    """

    stable_system: str
    """Инструкция и схема. Байт в байт одинаковы между запросами."""

    user_content: str
    """Текст, который нужно разобрать. Уже псевдонимизированный."""

    json_schema: dict[str, Any]
    schema_name: str

    volatile_system: str | None = None
    """Контекст запроса: объект, участники, справочник этапов."""

    max_output_tokens: int = 8192

    media: tuple[MediaPart, ...] = ()
    """Изображения и PDF для распознавания. Пусто — обычный текстовый запрос.

    Провайдер, не умеющий работать с изображениями, обязан отказать, а не
    ответить по одному тексту: молчаливая деградация здесь означала бы
    «на чеке ничего нет», то есть ответ хуже ошибки."""

    model: str | None = None
    """Модель для этого запроса. None — та, что настроена у провайдера.

    Нужно ради разной цены ошибки. Разбор короткого сообщения ошибается
    дёшево: факт ниже порога уйдёт человеку. Неверно прочитанная сумма
    с чека попадает в бюджет объекта. Поэтому распознавание документов
    можно поднять на модель посильнее, не переплачивая на потоке
    сообщений, которых в сутки полторы тысячи."""


@dataclass(slots=True)
class StructuredResponse:
    provider: str
    model: str
    payload: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    degraded: bool = False
    """True, если ответ получен от резервного провайдера."""
    meta: dict[str, Any] = field(default_factory=dict)


class LlmError(RuntimeError):
    """Провайдер не смог обслужить запрос."""


class LlmRefusal(LlmError):
    """Модель отказалась отвечать по соображениям безопасности.

    Повторять с тем же промптом бессмысленно — нужен человек.
    """

    def __init__(self, category: str | None, explanation: str | None = None) -> None:
        super().__init__(f"Модель отклонила запрос (категория: {category or 'не указана'})")
        self.category = category
        self.explanation = explanation


class LlmUnavailable(LlmError):
    """Провайдер недоступен: сеть, лимиты, 5xx. Имеет смысл переключиться."""


class LlmInvalidOutput(LlmError):
    """Ответ не соответствует схеме."""


class LlmMediaUnsupported(LlmError):
    """Провайдер не умеет читать изображения. Резерв здесь не поможет."""


@runtime_checkable
class LlmProvider(Protocol):
    name: str
    model: str
    supports_media: bool
    """Умеет ли провайдер читать изображения и PDF."""

    async def complete_structured(self, request: StructuredRequest) -> StructuredResponse:
        """Получить ответ, соответствующий `request.json_schema`."""
        ...

    async def aclose(self) -> None: ...
