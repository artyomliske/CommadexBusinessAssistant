from repairbot.llm.base import (
    LlmError,
    LlmInvalidOutput,
    LlmProvider,
    LlmRefusal,
    LlmUnavailable,
    StructuredRequest,
    StructuredResponse,
)
from repairbot.llm.router import LlmRouter

__all__ = [
    "LlmError",
    "LlmInvalidOutput",
    "LlmProvider",
    "LlmRefusal",
    "LlmRouter",
    "LlmUnavailable",
    "StructuredRequest",
    "StructuredResponse",
]


def build_router(settings) -> LlmRouter:  # type: ignore[no-untyped-def]
    """Собрать маршрутизатор из конфигурации.

    Резервный провайдер подключается только если задан его ключ: в dev
    его обычно нет, и это не должно мешать работе.
    """
    from repairbot.llm.claude import ClaudeProvider
    from repairbot.llm.reserve import OpenAiCompatibleProvider

    if settings.llm_provider == "openai":
        # OpenRouter и подобные шлюзы: тот же Claude, но по совместимому
        # с OpenAI протоколу. Деградацией это не считаем — это штатный
        # режим, а не запасной.
        primary = OpenAiCompatibleProvider(
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            name="primary",
            supports_media=settings.llm_supports_media,
            degraded=False,
        )
    else:
        # Пустой ключ передаём как None: тогда SDK сам разрешит учётные
        # данные из окружения, а не уйдёт в запрос с заведомо пустым ключом.
        primary = ClaudeProvider(
            api_key=settings.llm_api_key.get_secret_value() or None,
            model=settings.llm_model,
            base_url=settings.llm_base_url or None,
            effort=settings.llm_effort,
        )

    reserve = None
    if settings.llm_reserve_api_key.get_secret_value() and settings.llm_reserve_base_url:
        reserve = OpenAiCompatibleProvider(
            api_key=settings.llm_reserve_api_key.get_secret_value(),
            model=settings.llm_reserve_model,
            base_url=settings.llm_reserve_base_url,
            supports_media=settings.llm_reserve_supports_media,
        )

    return LlmRouter(primary, reserve, cooldown_seconds=settings.llm_cooldown_seconds)
