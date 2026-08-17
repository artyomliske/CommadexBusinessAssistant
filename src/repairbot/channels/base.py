"""Контракт адаптера канала.

Адаптер изолирует API конкретного мессенджера (раздел 10 ТЗ: «изоляция API
адаптером»). Всё, что выше шлюза, работает только с `InboundEvent`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from repairbot.domain.events import Channel, InboundEvent, OutboundText


@runtime_checkable
class ChannelAdapter(Protocol):
    channel: Channel

    def normalize(self, payload: dict[str, Any]) -> list[InboundEvent]:
        """Привести payload вебхука к списку внутренних событий.

        Возвращает список, потому что часть каналов доставляет
        несколько апдейтов в одном запросе.
        """
        ...

    async def send_text(self, message: OutboundText) -> str | None:
        """Отправить текст. Возвращает id сообщения в канале, если он известен."""
        ...

    async def fetch_history(
        self, channel_chat_id: str, *, limit: int = 100, before_message_id: str | None = None
    ) -> list[InboundEvent]:
        """Догрузить историю чата (этап 1 ТЗ: загрузка истории)."""
        ...
