"""Клиент Google Drive: выбор способа загрузки, папки, ошибки."""

from __future__ import annotations

import json

import httpx
import pytest

from repairbot.integrations.drive.client import (
    FOLDER_MIME,
    MULTIPART_LIMIT,
    DriveClient,
    DriveError,
    DriveUnavailable,
)
from repairbot.integrations.google_auth import CredentialsSource


class FakeCredentials:
    valid = True
    token = "tok"

    def refresh(self, request) -> None:  # pragma: no cover — токен уже годен
        pass


def _client(handler) -> DriveClient:
    transport = httpx.MockTransport(handler)
    drive = DriveClient(
        CredentialsSource(oauth_token_path="/не-читается.json"),
        client=httpx.AsyncClient(transport=transport, base_url="https://drive.test"),
    )
    # Учётные данные подставляем напрямую: сеть и файловая система здесь ни при чём.
    drive._credentials = FakeCredentials()
    return drive


async def test_small_file_goes_as_multipart():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["type"] = request.headers.get("Content-Type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "file-1", "name": "чек.pdf"})

    drive = _client(handler)
    uploaded = await drive.upload(
        name="чек.pdf", content=b"x" * 100, folder_id="folder-1", mime_type="application/pdf"
    )

    assert uploaded.file_id == "file-1"
    assert "uploadType=multipart" in seen["url"]
    assert seen["type"].startswith("multipart/related; boundary=")
    # Метаданные и тело идут одним запросом.
    assert b"application/pdf" in seen["body"]
    assert b"folder-1" in seen["body"]


async def test_large_file_switches_to_a_resumable_session():
    """Google принимает multipart только до 5 МБ."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(
                200, headers={"Location": "https://drive.test/upload/session-42"}
            )
        return httpx.Response(200, json={"id": "file-big"})

    drive = _client(handler)
    uploaded = await drive.upload(
        name="видео.mp4", content=b"x" * (MULTIPART_LIMIT + 1), folder_id="folder-1"
    )

    assert uploaded.file_id == "file-big"
    assert calls[0][0] == "POST"
    assert "uploadType=resumable" in calls[0][1]
    assert calls[1] == ("PUT", "https://drive.test/upload/session-42")


async def test_resumable_session_without_a_location_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    drive = _client(handler)

    with pytest.raises(DriveError, match="адрес сессии"):
        await drive.upload(
            name="видео.mp4", content=b"x" * (MULTIPART_LIMIT + 1), folder_id="folder-1"
        )


async def test_existing_folder_is_reused():
    """Диск разрешает две папки с одним именем: без поиска они бы плодились."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"files": [{"id": "folder-7", "name": "obj_17"}]})

    drive = _client(handler)

    assert await drive.ensure_folder("obj_17", "root") == "folder-7"
    assert calls == ["GET"]


async def test_missing_folder_is_created():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        body = json.loads(request.content)
        assert body["mimeType"] == FOLDER_MIME
        assert body["parents"] == ["root"]
        return httpx.Response(200, json={"id": "folder-new"})

    drive = _client(handler)

    assert await drive.ensure_folder("obj_17", "root") == "folder-new"


async def test_apostrophe_in_a_folder_name_does_not_break_the_query():
    """Имя папки берётся из адреса объекта, то есть приходит извне."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["q"] = request.url.params.get("q")
        return httpx.Response(200, json={"files": []})

    drive = _client(handler)
    await drive.find_folder("Квартира О'Коннора", "root")

    assert "\\'" in seen["q"]


async def test_quota_error_is_retriable_but_forbidden_is_not():
    def quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Rate Limit Exceeded: user quota")

    with pytest.raises(DriveUnavailable):
        await _client(quota).find_folder("obj_17", "root")

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="The user does not have permission")

    with pytest.raises(DriveError) as exc_info:
        await _client(denied).find_folder("obj_17", "root")
    assert not isinstance(exc_info.value, DriveUnavailable)


async def test_network_failure_is_temporary():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сеть недоступна")

    with pytest.raises(DriveUnavailable):
        await _client(handler).find_folder("obj_17", "root")
