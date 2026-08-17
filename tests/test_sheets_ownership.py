"""Владение файлами в Google-аккаунте заказчика.

Созданные файлы должны принадлежать заказчику, а не нам: по окончании работ
его данные не должны остаться на нашей стороне.

У заказчика личный аккаунт Google, а не Workspace, поэтому владение
обеспечивается входом от его имени по OAuth. Делегирования в домене там нет
как механизма — оно проверяется отдельно, на случай переезда на домен.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from repairbot.db.models import RepairObject
from repairbot.integrations import google_auth
from repairbot.integrations.google_auth import (
    SCOPES,
    CredentialsSource,
    GoogleAuthError,
)
from repairbot.integrations.sheets import SheetsError, SheetsSync
from repairbot.integrations.sheets.client import DRIVE_API, SPREADSHEET_MIME


class FakeClient:
    """Записывает вызовы вместо обращения к Google."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.created_id = "sheet-new"

    async def create_spreadsheet_in_folder(
        self, title: str, folder_id: str, sheet_titles: list[str]
    ) -> str:
        self.calls.append(
            ("create", {"title": title, "folder_id": folder_id, "sheets": sheet_titles})
        )
        return self.created_id

    async def values_batch_update(self, spreadsheet_id: str, data: list[dict]) -> dict:
        self.calls.append(("headers", {"spreadsheet_id": spreadsheet_id, "ranges": len(data)}))
        return {}


def _object(**kw: Any) -> RepairObject:
    return RepairObject(code=kw.pop("code", "obj_12"), address=kw.pop("address", "Ленина 5"), **kw)


def _token_file(tmp_path, **overrides: Any) -> str:
    payload = {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "shh",
        "refresh_token": "1//refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": list(SCOPES),
    }
    payload.update(overrides)
    path = tmp_path / "token.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# --- размещение книги ---


async def test_book_is_created_in_the_customer_folder():
    client = FakeClient()
    sync = SheetsSync(client, drive_folder_id="folder-of-customer")

    spreadsheet_id = await sync.ensure_book(None, _object())

    assert spreadsheet_id == "sheet-new"
    create = next(payload for name, payload in client.calls if name == "create")
    assert create["folder_id"] == "folder-of-customer"
    assert "obj_12" in create["title"]


async def test_without_folder_we_refuse_instead_of_creating_an_orphan():
    """Отказ здесь сознательный.

    Молча создать книгу «где получится» значит бросить её в корень Диска
    заказчика, где она потеряется среди личных файлов.
    """
    sync = SheetsSync(FakeClient(), drive_folder_id="")

    with pytest.raises(SheetsError, match="GOOGLE_DRIVE_FOLDER_ID"):
        await sync.ensure_book(None, _object())


async def test_existing_book_is_reused_without_folder():
    """Готовую книгу заказчика можно прописать напрямую — папка не нужна."""
    client = FakeClient()
    sync = SheetsSync(client, drive_folder_id="")

    spreadsheet_id = await sync.ensure_book(None, _object(spreadsheet_id="их-книга"))

    assert spreadsheet_id == "их-книга"
    assert client.calls == []


def test_scopes_do_not_request_full_drive():
    """На том же аккаунте лежит вся личная почта и документы заказчика.

    drive.file даёт доступ только к тому, что создали мы или чем с нами
    поделились явно, — полный drive запрашивать не за что.
    """
    assert "https://www.googleapis.com/auth/drive.file" in SCOPES
    assert "https://www.googleapis.com/auth/drive" not in SCOPES


def test_spreadsheet_is_created_through_drive_api():
    """Sheets API не умеет класть файл в папку — только Drive API."""
    assert DRIVE_API.endswith("/drive/v3")
    assert SPREADSHEET_MIME == "application/vnd.google-apps.spreadsheet"


# --- кто владеет созданным ---


def test_oauth_makes_the_customer_the_owner(tmp_path):
    """Вход от имени заказчика — файлы сразу его."""
    source = CredentialsSource(oauth_token_path=_token_file(tmp_path))

    assert source.kind == "oauth"
    assert source.owns_created_files


def test_service_account_without_delegation_owns_nothing():
    """У сервисного аккаунта нет своей квоты хранилища.

    Создать файл он не сможет нигде, поэтому такая настройка — не «почти
    работает», а не работает вовсе, и check-drive обязан это показать.
    """
    source = CredentialsSource(service_account_path="/sa.json")

    assert source.kind == "service_account"
    assert not source.owns_created_files


def test_delegation_restores_ownership_on_workspace():
    source = CredentialsSource(
        service_account_path="/sa.json", impersonate_subject="bot@example.ru"
    )

    assert source.owns_created_files


def test_unconfigured_source_refuses_with_instructions():
    source = CredentialsSource()

    assert source.kind == "none"
    assert not source.configured
    with pytest.raises(GoogleAuthError, match="GOOGLE_OAUTH_TOKEN_PATH"):
        source.load()


# --- загрузка токена ---


def test_oauth_token_is_loaded_with_our_scopes(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    class FakeUserCredentials:
        refresh_token = "1//refresh"

        @classmethod
        def from_authorized_user_file(cls, path: str, scopes: list[str]):
            seen["path"] = path
            seen["scopes"] = scopes
            return cls()

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials", FakeUserCredentials, raising=False
    )
    path = _token_file(tmp_path)

    credentials = CredentialsSource(oauth_token_path=path).load()

    assert isinstance(credentials, FakeUserCredentials)
    assert seen["path"] == path
    assert seen["scopes"] == list(SCOPES)


def test_missing_token_file_points_at_the_authorize_command(tmp_path):
    source = CredentialsSource(oauth_token_path=str(tmp_path / "нет.json"))

    with pytest.raises(GoogleAuthError, match="google-authorize"):
        source.load()


def test_token_without_refresh_token_is_rejected_up_front(tmp_path):
    """Токен без refresh проживёт час и встанет посреди рабочего дня.

    Лучше отказать при настройке, чем через час работы.
    """
    path = _token_file(tmp_path)
    payload = json.loads(open(path, encoding="utf-8").read())
    payload.pop("refresh_token")
    open(path, "w", encoding="utf-8").write(json.dumps(payload))

    with pytest.raises(GoogleAuthError, match="refresh_token"):
        CredentialsSource(oauth_token_path=path).load()


def test_corrupt_token_file_says_so(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{не json", encoding="utf-8")

    with pytest.raises(GoogleAuthError, match="испорчен"):
        CredentialsSource(oauth_token_path=str(path)).load()


def test_delegation_is_applied_to_service_account_credentials(monkeypatch):
    """`with_subject` должен вызываться ровно с заданным ящиком."""
    seen: dict[str, Any] = {}

    class FakeCredentials:
        def with_subject(self, subject: str) -> FakeCredentials:
            seen["subject"] = subject
            return self

    class FakeServiceAccount:
        class Credentials:
            @staticmethod
            def from_service_account_file(path: str, scopes: list[str]) -> FakeCredentials:
                seen["path"] = path
                seen["scopes"] = scopes
                return FakeCredentials()

    monkeypatch.setattr("google.oauth2.service_account", FakeServiceAccount, raising=False)

    CredentialsSource(
        service_account_path="/sa.json", impersonate_subject="bot@example.ru"
    ).load()

    assert seen["subject"] == "bot@example.ru"
    assert seen["scopes"] == list(SCOPES)


# --- выбор способа по настройкам ---


def test_settings_prefer_oauth_over_service_account():
    """Если настроено и то, и другое, работаем от имени заказчика.

    Личный аккаунт — то, что у него есть сейчас; сервисный остался от
    Workspace-варианта и молча перебивать OAuth не должен.
    """

    class FakeSettings:
        google_oauth_token_path = "/token.json"
        google_credentials_path = "/sa.json"
        google_impersonate_subject = ""

    source = google_auth.from_settings(FakeSettings())

    assert source.kind == "oauth"
