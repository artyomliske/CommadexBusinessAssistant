"""Адаптер Telegram к общему шлюзу (этап 6 ТЗ).

Выше шлюза ничего не меняется: конвейер, контролёр и агенты работают с
`InboundEvent` и не знают, из какого мессенджера тот пришёл. Ради этого
шлюз и строился.
"""

from __future__ import annotations

from typing import Any

from repairbot.channels.telegram import normalizer
from repairbot.channels.telegram.client import TelegramClient
from repairbot.config import Settings
from repairbot.domain.events import Channel, InboundEvent, OutboundText
from repairbot.observability import get_logger

log = get_logger(__name__)


class TelegramAdapter:
    channel = Channel.TELEGRAM

    def __init__(self, settings: Settings, client: TelegramClient | None = None) -> None:
        self._settings = settings
        self._client = client or TelegramClient(settings)

    @property
    def client(self) -> TelegramClient:
        return self._client

    def normalize(self, payload: dict[str, Any]) -> list[InboundEvent]:
        return normalizer.normalize(payload)

    async def send_text(self, message: OutboundText) -> str | None:
        return await self._client.send_message(
            message.channel_chat_id,
            message.text,
            reply_to_message_id=message.reply_to_message_id,
            notify=message.notify,
        )

    async def fetch_history(
        self, channel_chat_id: str, *, limit: int = 100, before_message_id: str | None = None
    ) -> list[InboundEvent]:
        """Истории у Telegram нет.

        Bot API не отдаёт сообщения, отправленные до добавления бота, и
        вообще не имеет метода чтения истории. Возвращаем пустой список,
        а не притворяемся: команда догрузки должна сказать об этом прямо,
        а не молча отработать вхолостую.
        """
        log.info(
            "telegram.history_unavailable",
            chat_id=channel_chat_id,
            reason="Bot API не отдаёт историю чата",
        )
        return []

    async def ensure_subscription(self) -> None:
        """Зарегистрировать вебхук, если адрес отличается от текущего."""
        url = (
            self._settings.public_base_url.rstrip("/")
            + self._settings.telegram_webhook_path
        )
        info = await self._client.get_webhook_info()
        if info.get("url") == url:
            return
        await self._client.set_webhook(
            url, secret_token=self._settings.telegram_webhook_secret.get_secret_value()
        )

    async def aclose(self) -> None:
        await self._client.aclose()
