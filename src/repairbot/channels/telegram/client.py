"""Клиент Telegram Bot API.

Ограничения платформы, определившие устройство (раздел 10 ТЗ):

* **истории чата нет.** Bot API не отдаёт сообщения, отправленные до того,
  как бота добавили, и вообще не имеет метода чтения истории. Догрузка,
  которая у MAX закрывает переписку «до подключения», здесь невозможна —
  `fetch_history` возвращает пустой список честно, а не притворяется;
* **режим приватности.** По умолчанию бот в группе получает только команды
  и ответы себе. Отключается в @BotFather (`/setprivacy` → Disable) и
  проверяется методом прав в чате;
* **токен в адресе.** Скачивание файла идёт по URL с токеном внутри,
  поэтому такие адреса не логируются и не сохраняются в журнал.
"""

from __future__ import annotations

from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from repairbot.channels.max.client import AttachmentTooLarge, RateLimiter
from repairbot.config import Settings
from repairbot.observability import get_logger

log = get_logger(__name__)

API_BASE = "https://api.telegram.org"

DEFAULT_UPDATE_TYPES = [
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "my_chat_member",
]

READER_STATUSES = frozenset({"administrator", "creator"})
"""Статусы, при которых бот видит всю переписку независимо от приватности.

Администратор группы получает все сообщения даже с включённым режимом
приватности — это самый надёжный способ выполнить требование раздела 2
о чтении рабочих чатов."""


class TelegramApiError(RuntimeError):
    def __init__(self, status_code: int, description: str) -> None:
        super().__init__(f"Telegram API {status_code}: {description}")
        self.status_code = status_code
        self.description = description


class RetryableApiError(TelegramApiError):
    """429 и 5xx — имеет смысл повторить."""


class TelegramClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._token = settings.telegram_bot_token.get_secret_value()
        self._base = settings.telegram_api_base.rstrip("/")
        self._limiter = RateLimiter(settings.telegram_rate_limit_rps)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=30.0),
            limits=httpx.Limits(max_connections=20),
        )
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- транспорт ---

    @property
    def _method_base(self) -> str:
        return f"{self._base}/bot{self._token}"

    @retry(
        retry=retry_if_exception_type((RetryableApiError, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _call(self, method: str, **params: Any) -> Any:
        """Вызов метода API.

        Telegram отвечает 200 даже на отказ, а признак успеха лежит в теле
        (`ok`). Проверять только код ответа здесь недостаточно.
        """
        await self._limiter.acquire()
        body = {k: v for k, v in params.items() if v is not None}
        response = await self._client.post(f"{self._method_base}/{method}", json=body)

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableApiError(response.status_code, response.text[:300])

        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramApiError(response.status_code, response.text[:300]) from exc

        if not payload.get("ok"):
            description = str(payload.get("description") or "")
            # 429 приходит и с кодом 200 — с указанием, сколько ждать.
            if payload.get("error_code") == 429:
                raise RetryableApiError(429, description)
            raise TelegramApiError(int(payload.get("error_code") or 0), description)

        return payload.get("result")

    # --- методы API ---

    async def get_me(self) -> dict[str, Any]:
        return dict(await self._call("getMe"))

    async def set_webhook(
        self, url: str, *, secret_token: str, update_types: list[str] | None = None
    ) -> None:
        """Зарегистрировать вебхук.

        `secret_token` Telegram возвращает в заголовке каждого запроса —
        это и есть подтверждение подлинности источника. У MAX секрет
        приходится класть в путь URL, потому что заголовка там нет.
        """
        await self._call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=update_types or DEFAULT_UPDATE_TYPES,
            drop_pending_updates=False,
        )
        log.info("telegram.webhook_set", url=url)

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook")

    async def get_webhook_info(self) -> dict[str, Any]:
        return dict(await self._call("getWebhookInfo"))

    async def get_updates(
        self, *, offset: int | None = None, wait_seconds: int = 25, limit: int = 100
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Опрос апдейтов — для настройки, как и у MAX.

        Возвращает апдейты и смещение для следующего запроса. Работает,
        только пока вебхук не зарегистрирован: Telegram не отдаёт апдейты
        обоими способами сразу.
        """
        result = await self._call(
            "getUpdates", offset=offset, timeout=wait_seconds, limit=limit
        )
        updates = list(result or [])
        next_offset = (updates[-1]["update_id"] + 1) if updates else offset
        return updates, next_offset

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        notify: bool = True,
    ) -> str | None:
        result = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
            disable_notification=not notify,
        )
        message_id = (result or {}).get("message_id")
        return str(message_id) if message_id is not None else None

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return dict(await self._call("getChat", chat_id=chat_id))

    async def get_chat_membership(self, chat_id: str) -> dict[str, Any]:
        """Права бота в чате.

        Заодно отвечает на вопрос, увидит ли он переписку: администратор
        получает все сообщения независимо от режима приватности.
        """
        me = await self.get_me()
        member = await self._call("getChatMember", chat_id=chat_id, user_id=me["id"])
        status = str((member or {}).get("status") or "")
        return {
            "status": status,
            "reads_all_messages": status in READER_STATUSES,
            "raw": member,
        }

    async def download(self, file_id: str, *, max_bytes: int) -> bytes:
        """Скачать файл: сначала путь, потом содержимое.

        Адрес скачивания содержит токен бота, поэтому наружу он не
        отдаётся и в журнал не пишется — в отличие от MAX, где ссылка
        ведёт на открытый CDN.
        """
        info = await self._call("getFile", file_id=file_id)
        file_path = (info or {}).get("file_path")
        if not file_path:
            raise TelegramApiError(0, f"Telegram не вернул путь к файлу {file_id}")

        declared = (info or {}).get("file_size")
        if declared and int(declared) > max_bytes:
            raise AttachmentTooLarge(int(declared), max_bytes)

        url = f"{self._base}/file/bot{self._token}/{file_path}"
        timeout = httpx.Timeout(10.0, read=120.0)
        try:
            async with self._client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 400:
                    # Токен в адрес сообщения не попадает.
                    raise TelegramApiError(
                        response.status_code, f"скачивание файла {file_id}"
                    )
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise AttachmentTooLarge(received, max_bytes)
                    chunks.append(chunk)
        except httpx.TransportError as exc:
            raise RetryableApiError(0, f"не удалось скачать файл: {exc}") from exc

        return b"".join(chunks)
