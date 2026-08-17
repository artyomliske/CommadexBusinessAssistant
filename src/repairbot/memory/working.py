"""Рабочая память чата (раздел 4 ТЗ).

Последние 20–50 сообщений чата в Redis с TTL 24 часа. Нужна для разрешения
контекстных ссылок в тексте («там же», «как договаривались»). Источником
истины не является — при потере Redis восстанавливается из журнала.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from repairbot.config import Settings
from repairbot.domain.events import InboundEvent


class WorkingMemory:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._max_messages = settings.working_memory_max_messages
        self._ttl = settings.working_memory_ttl_seconds

    @staticmethod
    def _key(channel: str, chat_id: str) -> str:
        return f"wm:{channel}:{chat_id}"

    async def append(self, event: InboundEvent) -> None:
        if not event.has_text and not event.attachments:
            return

        key = self._key(event.channel.value, event.chat.channel_chat_id)
        entry = json.dumps(
            {
                "message_id": event.message_id,
                "author": event.actor.display_name if event.actor else None,
                "author_id": event.actor.channel_user_id if event.actor else None,
                "text": event.text,
                "attachments": [a.kind.value for a in event.attachments],
                "at": event.occurred_at.isoformat(),
            },
            ensure_ascii=False,
        )

        pipe = self._redis.pipeline()
        pipe.rpush(key, entry)
        pipe.ltrim(key, -self._max_messages, -1)
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def recent(
        self, channel: str, chat_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        count = limit or self._max_messages
        raw = await self._redis.lrange(self._key(channel, chat_id), -count, -1)
        return [json.loads(item) for item in raw]

    async def clear(self, channel: str, chat_id: str) -> None:
        await self._redis.delete(self._key(channel, chat_id))
