"""Переключение между провайдерами моделей.

Правило простое: на недоступность основного провайдера переключаемся на
резервный, на отказ модели по соображениям безопасности — нет. Отказ
означает, что запрос не следует обслуживать вообще, и переигрывать его на
другой модели было бы обходом предохранителя, а не отказоустойчивостью.

Резервный провайдер работает с деградацией качества, поэтому после
восстановления основного мы возвращаемся к нему: «залипания» на резерве нет,
но и на каждый запрос основной не дёргаем — держим паузу.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from repairbot.llm.base import (
    LlmError,
    LlmProvider,
    LlmRefusal,
    LlmUnavailable,
    StructuredRequest,
    StructuredResponse,
)
from repairbot.observability import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class _Breaker:
    """Пауза перед следующей попыткой обратиться к основному провайдеру."""

    cooldown_seconds: float
    failures_before_trip: int = 3
    _failures: int = 0
    _tripped_at: float | None = None

    def allow(self) -> bool:
        if self._tripped_at is None:
            return True
        if time.monotonic() - self._tripped_at >= self.cooldown_seconds:
            # Пробная попытка: успех сбросит счётчик, неудача продлит паузу.
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._tripped_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failures_before_trip:
            self._tripped_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        return self._tripped_at is not None


class LlmRouter:
    def __init__(
        self,
        primary: LlmProvider,
        reserve: LlmProvider | None = None,
        *,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._primary = primary
        self._reserve = reserve
        self._breaker = _Breaker(cooldown_seconds=cooldown_seconds)

    @property
    def degraded(self) -> bool:
        return self._breaker.is_open

    def _reserve_can_serve(self, request: StructuredRequest) -> bool:
        """Может ли резерв обслужить именно этот запрос.

        Запрос с изображением резерву без зрения передавать нельзя. Он не
        откажется, а ответит по одному тексту — то есть сообщит, что на
        чеке ничего нет. Такой ответ хуже ошибки: ошибку видно, а пустой
        разбор чека молча уедет в журнал как факт.
        """
        if self._reserve is None:
            return False
        if request.media and not getattr(self._reserve, "supports_media", False):
            return False
        return True

    async def complete_structured(self, request: StructuredRequest) -> StructuredResponse:
        reserve_available = self._reserve_can_serve(request)

        if self._breaker.allow():
            try:
                response = await self._primary.complete_structured(request)
                self._breaker.record_success()
                return response
            except LlmRefusal:
                # Не переключаемся: отказ — это решение по содержанию запроса.
                raise
            except LlmUnavailable as exc:
                self._breaker.record_failure()
                log.warning(
                    "llm.primary_unavailable",
                    provider=self._primary.name,
                    error=str(exc),
                    reserve_configured=self._reserve is not None,
                    reserve_available=reserve_available,
                )
                if not reserve_available:
                    raise
            except LlmError:
                # Ошибка схемы или запроса — резерв её не исправит.
                raise
        elif not reserve_available:
            raise LlmUnavailable(
                f"Основной провайдер {self._primary.name} недоступен, "
                "а резервный этот запрос обслужить не может"
            )
        else:
            log.info("llm.using_reserve", reason="cooldown")

        assert self._reserve is not None
        response = await self._reserve.complete_structured(request)
        log.info("llm.reserve_served", provider=self._reserve.name, model=response.model)
        return response

    async def aclose(self) -> None:
        await self._primary.aclose()
        if self._reserve is not None:
            await self._reserve.aclose()
