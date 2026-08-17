"""Адаптер канала MAX: нормализация + исходящие вызовы."""

from __future__ import annotations

from typing import Any

from repairbot.channels.max import normalizer
from repairbot.channels.max.client import MaxClient
from repairbot.config import Settings
from repairbot.domain.events import Channel, InboundEvent, OutboundText


class MaxAdapter:
    channel = Channel.MAX

    def __init__(self, settings: Settings, client: MaxClient | None = None) -> None:
        self._settings = settings
        self._client = client or MaxClient(settings)

    @property
    def client(self) -> MaxClient:
        return self._client

    def normalize(self, payload: dict[str, Any]) -> list[InboundEvent]:
        return normalizer.normalize(payload)

    async def send_text(self, message: OutboundText) -> str | None:
        return await self._client.send_text(
            message.channel_chat_id,
            message.text,
            reply_to_message_id=message.reply_to_message_id,
            notify=message.notify,
        )

    async def fetch_history(
        self, channel_chat_id: str, *, limit: int = 100, before_message_id: str | None = None
    ) -> list[InboundEvent]:
        return await self._client.iter_history(channel_chat_id, max_messages=limit)

    async def ensure_subscription(self) -> None:
        """Зарегистрировать вебхук, если он ещё не зарегистрирован."""
        url = self._settings.public_base_url.rstrip("/") + self._settings.max_webhook_path
        existing = {s.get("url") for s in await self._client.list_subscriptions()}
        if url not in existing:
            await self._client.subscribe(url)

    async def aclose(self) -> None:
        await self._client.aclose()
