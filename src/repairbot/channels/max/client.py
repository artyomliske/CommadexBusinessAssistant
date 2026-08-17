"""Клиент MAX Bot API.

Особенности платформы, учтённые здесь (раздел 2 ТЗ):
  * домен `platform-api2.max.ru`, требуется сертификат Минцифры в доверенных;
  * лимит 30 запросов в секунду — соблюдается токен-бакетом;
  * `GET /chats` (список чатов) отключён с июня 2026 — метода здесь нет,
    реестр чатов ведётся на своей стороне;
  * вебхук принимается только по HTTPS с сертификатом доверенного УЦ.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from pathlib import Path
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from repairbot.config import Settings
from repairbot.domain.events import InboundEvent, NormalizationError
from repairbot.observability import get_logger

log = get_logger(__name__)

DEFAULT_UPDATE_TYPES = [
    "message_created",
    "message_edited",
    "message_removed",
    "message_callback",
    "bot_added",
    "bot_removed",
    "bot_started",
    "user_added",
    "user_removed",
    "chat_title_changed",
]


class MaxApiError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"MAX API {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class RetryableApiError(MaxApiError):
    """429 и 5xx — имеет смысл повторить."""


class AttachmentTooLarge(MaxApiError):
    """Вложение крупнее допустимого. Повторять бессмысленно."""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            0, f"вложение {size_bytes // 1024} КБ больше предела {limit_bytes // 1024} КБ"
        )
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


class RateLimiter:
    """Токен-бакет на `rps` запросов в секунду."""

    def __init__(self, rps: int) -> None:
        self._capacity = float(rps)
        self._tokens = float(rps)
        self._rps = float(rps)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated_at) * self._rps
                )
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rps)


CERTS_DIR = Path(__file__).resolve().parents[3].parent / "certs"
"""Каталог с дополнительными корневыми сертификатами."""


def trust_context(certs_dir: Path | None = None) -> ssl.SSLContext | None:
    """Контекст TLS с корневым сертификатом Минцифры вдобавок к системным.

    Домен `platform-api2.max.ru` подписан удостоверяющим центром, которого
    нет в системном хранилище: без этого сертификата соединение не
    устанавливается, и выглядит это как поломка нашего кода.

    Системные центры **сохраняем**: вложения раздаются с обычного CDN,
    и подменить весь набор одним корнем значило бы сломать их скачивание.
    Возвращаем None, если добавлять нечего, — тогда httpx берёт умолчания.
    """
    directory = certs_dir or CERTS_DIR
    extra = sorted(directory.glob("*.crt")) if directory.is_dir() else []
    if not extra:
        return None

    context = ssl.create_default_context()
    for path in extra:
        try:
            context.load_verify_locations(cafile=str(path))
        except ssl.SSLError as exc:
            log.error("max.bad_certificate", path=str(path), error=str(exc))
    log.info("max.extra_certificates", files=[p.name for p in extra])
    return context


class MaxClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._token = settings.max_access_token.get_secret_value()
        self._base_url = settings.max_api_base.rstrip("/")
        self._limiter = RateLimiter(settings.max_rate_limit_rps)
        verify = trust_context()
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0, read=30.0),
            limits=httpx.Limits(max_connections=20),
            **({"verify": verify} if verify is not None else {}),
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

    @retry(
        retry=retry_if_exception_type((RetryableApiError, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._limiter.acquire()
        # Токен идёт заголовком: параметр запроса `access_token` платформа
        # объявила устаревшим и отвечает на него 401. Заголовок и лучше —
        # адрес попадает в журналы обратного прокси, а заголовок нет.
        #
        # Без схемы `Bearer`: на неё платформа отвечает «Malformed access
        # token». Ожидается голое значение, вопреки привычному виду
        # заголовка Authorization.
        query = {k: v for k, v in (params or {}).items() if v is not None}
        response = await self._client.request(
            method,
            path,
            params=query,
            json=json,
            headers={"Authorization": self._token},
        )

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableApiError(response.status_code, response.text[:500])
        if response.status_code >= 400:
            raise MaxApiError(response.status_code, response.text[:500])

        if not response.content:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"result": payload}

    # --- методы API ---

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def subscribe(self, webhook_url: str, update_types: list[str] | None = None) -> None:
        """Подписка на вебхуки — единственный допустимый способ для production."""
        await self._request(
            "POST",
            "/subscriptions",
            json={"url": webhook_url, "update_types": update_types or DEFAULT_UPDATE_TYPES},
        )
        log.info("max.subscribed", url=webhook_url)

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/subscriptions")
        subscriptions = payload.get("subscriptions", [])
        return subscriptions if isinstance(subscriptions, list) else []

    async def unsubscribe(self, webhook_url: str) -> None:
        await self._request("DELETE", "/subscriptions", params={"url": webhook_url})

    async def get_updates(
        self, *, marker: int | None = None, wait_seconds: int = 30, limit: int = 100
    ) -> tuple[list[InboundEvent], int | None]:
        """Забрать накопившиеся апдейты опросом (long polling).

        В работе используются вебхуки: платформа сама доставляет события,
        и на них построен весь приём. Опрос нужен для **настройки** —
        вебхук требует публичного адреса с сертификатом доверенного УЦ,
        а узнать идентификатор чата надо раньше, чем такой адрес появится.

        Возвращает события и новый маркер. Маркер — позиция в очереди
        платформы: без него следующий запрос вернёт то же самое.
        """
        payload = await self._request(
            "GET",
            "/updates",
            params={"marker": marker, "timeout": wait_seconds, "limit": limit},
        )
        from repairbot.channels.max import normalizer

        events: list[InboundEvent] = []
        for update in payload.get("updates") or []:
            try:
                events.extend(normalizer.normalize(update))
            except NormalizationError as exc:
                # Незнакомый апдейт не должен прерывать настройку.
                log.warning("max.update_skipped", error=str(exc))
        next_marker = payload.get("marker")
        return events, int(next_marker) if next_marker is not None else None

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Карточка одного чата. Список чатов (`GET /chats`) платформой отключён."""
        return await self._request("GET", f"/chats/{chat_id}")

    async def get_chat_members(
        self, chat_id: str, *, marker: int | None = None, count: int = 100
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"/chats/{chat_id}/members", params={"marker": marker, "count": count}
        )

    async def get_chat_membership(self, chat_id: str) -> dict[str, Any]:
        """Права бота в чате — для проверки `read_all_messages`."""
        return await self._request("GET", f"/chats/{chat_id}/members/me")

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        notify: bool = True,
    ) -> str | None:
        body: dict[str, Any] = {"text": text, "notify": notify}
        if reply_to_message_id:
            body["link"] = {"type": "reply", "mid": reply_to_message_id}
        payload = await self._request("POST", "/messages", params={"chat_id": chat_id}, json=body)
        message = payload.get("message") or {}
        return (message.get("body") or {}).get("mid")

    async def get_messages(
        self,
        chat_id: str,
        *,
        count: int = 100,
        from_time: int | None = None,
        to_time: int | None = None,
        message_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Догрузка истории чата.

        `from_time`/`to_time` — секунды эпохи, как ожидает платформа.
        """
        return await self._request(
            "GET",
            "/messages",
            params={
                "chat_id": chat_id,
                "count": min(count, 100),
                "from": from_time,
                "to": to_time,
                "message_ids": ",".join(message_ids) if message_ids else None,
            },
        )

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Скачать вложение по ссылке на CDN.

        Отдельно от `_request`: адрес абсолютный и на другом хосте, токен
        доступа туда не передаётся, а ответ — байты, а не JSON. Счётчик
        частоты тоже не нужен: ограничение в 30 запросов в секунду
        относится к Bot API, а не к раздаче файлов.

        Размер проверяется дважды — по заголовку и по факту. Заголовку
        одному верить нельзя: он необязателен и может соврать, а мы
        держим в памяти всё, что скачали.
        """
        timeout = httpx.Timeout(10.0, read=120.0)
        try:
            async with self._client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 400:
                    raise MaxApiError(response.status_code, f"скачивание {url}")

                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise AttachmentTooLarge(int(declared), max_bytes)

                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise AttachmentTooLarge(received, max_bytes)
                    chunks.append(chunk)
        except httpx.TransportError as exc:
            raise RetryableApiError(0, f"не удалось скачать вложение: {exc}") from exc

        return b"".join(chunks)

    async def iter_history(
        self, chat_id: str, *, page_size: int = 100, max_messages: int = 1000
    ) -> list[InboundEvent]:
        """Постранично собрать историю чата и вернуть нормализованные события.

        Идём назад по времени: `to` следующей страницы — время самого
        старого сообщения предыдущей.
        """
        from repairbot.channels.max.normalizer import normalize_update
        from repairbot.channels.max.schemas import MaxMessageListResponse, MaxUpdate

        collected: list[InboundEvent] = []
        # Курсор ведём в миллисекундах — как приходит от платформы; в секунды
        # переводим только на границе запроса, где их ожидает API.
        cursor_ms: int | None = None

        while len(collected) < max_messages:
            to_time = None if cursor_ms is None else cursor_ms // 1000 - 1
            payload = await self.get_messages(chat_id, count=page_size, to_time=to_time)
            page = MaxMessageListResponse.model_validate(payload).messages
            if not page:
                break

            for message in page:
                update = MaxUpdate(
                    update_type="message_created",
                    timestamp=message.timestamp,
                    message=message,
                )
                collected.append(normalize_update(update))

            oldest_ms = min((m.timestamp for m in page if m.timestamp), default=None)
            if oldest_ms is None or (cursor_ms is not None and oldest_ms >= cursor_ms):
                break  # платформа перестала отдавать более старые сообщения
            cursor_ms = oldest_ms
            if len(page) < page_size:
                break

        log.info("max.history_loaded", chat_id=chat_id, messages=len(collected))
        return collected
