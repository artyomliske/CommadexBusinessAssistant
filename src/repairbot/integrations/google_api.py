"""Общая основа клиентов Google API.

Здесь живёт то, что одинаково у Таблиц и Диска: получение токена, счёт
запросов и разбор ответов об ошибках. Дублировать это в двух местах нельзя —
разойдётся при первой же правке, и разойдётся молча.

Счётчик квоты у каждого клиента **свой**, и это не небрежность: Таблицы
ограничены 60 запросами в минуту на пользователя, Диск — на порядок мягче.
Общий счётчик заставил бы загрузку фотографий ждать очереди за витриной.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest

from repairbot.integrations.google_auth import Credentials, CredentialsSource
from repairbot.observability import get_logger

log = get_logger(__name__)


class GoogleApiError(RuntimeError):
    """Запрос отклонён. Повторять бессмысленно."""


class GoogleApiUnavailable(GoogleApiError):
    """Временная ошибка: квота, 5xx, сеть. Имеет смысл повторить позже."""


class QuotaLimiter:
    """Счётчик запросов в скользящем окне.

    Токен-бакет для минутной квоты не подходит: Google считает запросы
    именно за последние 60 секунд, и ровный расход по одному в секунду
    квоту не спасёт, если предыдущая минута была плотной.
    """

    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self._window:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                sleep_for = self._window - (now - self._timestamps[0])
                log.debug("google.quota_wait", seconds=round(sleep_for, 2))
                await asyncio.sleep(max(sleep_for, 0.05))


class GoogleApiClient:
    """Клиент одного из API Google с общей аутентификацией.

    Загрузка учётных данных и обновление токена в google-auth блокирующие,
    поэтому выполняются в отдельном потоке.
    """

    #: Класс ошибки, которым говорит наследник. Позволяет вызывающему коду
    #: ловить `SheetsError`, не зная про общую основу.
    error_class: type[GoogleApiError] = GoogleApiError
    unavailable_class: type[GoogleApiUnavailable] = GoogleApiUnavailable
    service_name = "Google"

    def __init__(
        self,
        source: CredentialsSource,
        *,
        base_url: str,
        requests_per_minute: int,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._source = source
        self._credentials: Credentials | None = None
        self._auth_lock = asyncio.Lock()
        self._limiter = QuotaLimiter(requests_per_minute)
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=timeout or httpx.Timeout(10.0, read=60.0)
        )

    @property
    def source(self) -> CredentialsSource:
        return self._source

    async def _token(self) -> str:
        """Токен доступа.

        Под замком: без него два параллельных запроса на старте загрузят
        учётные данные дважды и дважды сходят к Google за токеном.
        """
        async with self._auth_lock:
            if self._credentials is None:
                self._credentials = await asyncio.to_thread(self._source.load)

            credentials = self._credentials
            if not credentials.valid:
                await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())
            return str(credentials.token)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Запрос к API с учётом квоты и разбором ошибок.

        `path` относительный уходит по базовому адресу клиента,
        абсолютный — как есть: так вызовы к соседнему API живут в том же
        счётчике квоты, а не в обход него.
        """
        await self._limiter.acquire()
        token = await self._token()

        request_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)

        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                content=content,
                headers=request_headers,
            )
        except httpx.TransportError as exc:
            raise self.unavailable_class(f"{self.service_name} недоступен: {exc}") from exc

        self._raise_for_status(response)
        return response

    async def _request_json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        response = await self._request(method, path, **kw)
        return response.json() if response.content else {}

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (429, 403) or response.status_code >= 500:
            # 403 означает и «нет прав», и «превышена квота»; различаем
            # по тексту, чтобы не повторять безнадёжное.
            body = response.text[:300]
            if response.status_code == 403 and "quota" not in body.lower():
                raise self.error_class(f"Нет доступа: {body}")
            raise self.unavailable_class(
                f"{self.service_name} вернул {response.status_code}: {body}"
            )
        if response.status_code >= 400:
            raise self.error_class(
                f"{self.service_name} отклонил запрос: "
                f"{response.status_code} {response.text[:300]}"
            )

    async def aclose(self) -> None:
        await self._client.aclose()
