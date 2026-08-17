"""Скачивание вложений из MAX: пределы размера и ошибки."""

from __future__ import annotations

import httpx
import pytest

from repairbot.channels.max.client import AttachmentTooLarge, MaxApiError, MaxClient


def _client(handler, settings) -> MaxClient:
    transport = httpx.MockTransport(handler)
    return MaxClient(
        settings, client=httpx.AsyncClient(transport=transport, base_url="https://api.test")
    )


async def test_file_is_returned_as_bytes(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n")

    content = await _client(handler, settings).download(
        "https://cdn.max.ru/1.jpg", max_bytes=1024
    )

    assert content == b"\x89PNG\r\n"


async def test_declared_size_over_the_limit_is_refused_before_download(settings):
    """Заголовок позволяет отказаться, не выкачивая файл целиком."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * 10, headers={"Content-Length": "999999"}
        )

    with pytest.raises(AttachmentTooLarge):
        await _client(handler, settings).download("https://cdn.max.ru/big.mp4", max_bytes=100)


async def test_actual_size_is_checked_too(settings):
    """Заголовок необязателен и может соврать.

    Проверка только по нему означала бы, что файл без Content-Length
    выкачивается в память целиком, каким бы он ни был.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    with pytest.raises(AttachmentTooLarge):
        await _client(handler, settings).download("https://cdn.max.ru/big.mp4", max_bytes=100)


async def test_too_large_is_not_retriable(settings):
    """Повторять бессмысленно: файл меньше не станет."""
    from repairbot.channels.max.client import RetryableApiError

    error = AttachmentTooLarge(5000, 100)

    assert isinstance(error, MaxApiError)
    assert not isinstance(error, RetryableApiError)


async def test_missing_file_reports_the_status(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(MaxApiError) as exc_info:
        await _client(handler, settings).download(
            "https://cdn.max.ru/gone.jpg", max_bytes=1024
        )
    assert exc_info.value.status_code == 404


async def test_access_token_is_not_sent_to_the_cdn(settings):
    """Ссылка ведёт на другой хост — токен бота ему знать незачем."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"ok")

    await _client(handler, settings).download("https://cdn.max.ru/1.jpg", max_bytes=1024)

    assert "access_token" not in seen["url"]


# --- как передаётся токен ---


async def test_token_goes_in_the_authorization_header(settings):
    """Параметр запроса `access_token` платформа объявила устаревшим.

    На него приходит 401 с указанием использовать заголовок. Значение
    голое, без схемы `Bearer`: на неё ответ — «Malformed access token».
    Проверено на живой платформе, поэтому тест закрепляет именно такой
    вид — при следующей правке легко машинально дописать `Bearer`.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"user_id": 1, "name": "Бот"})

    await _client(handler, settings).get_me()

    assert seen["auth"] == "test-token"
    assert "access_token" not in seen["url"]


async def test_other_query_parameters_survive(settings):
    """Токен ушёл из строки запроса, остальное должно остаться."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"messages": []})

    await _client(handler, settings).get_messages("-100500", count=50)

    assert seen["params"]["chat_id"] == "-100500"
    assert seen["params"]["count"] == "50"
