"""Клиент Google Sheets.

Ограничения, учтённые здесь (раздел 5 ТЗ):

* квота 60 запросов в минуту на пользователя — соблюдается счётчиком
  в скользящем окне, а не паузами наугад;
* запись выполняется пакетно через `values:batchUpdate`, поэтому обновление
  двадцати ячеек стоит один запрос, а не двадцать;
* источник истины — PostgreSQL. Таблица выступает витриной, и ошибка записи
  в неё не должна ронять обработку событий.

Откуда берутся учётные данные — дело `repairbot.integrations.google_auth`.
Здесь важно лишь то, что их загрузка и обновление токена в google-auth
блокирующие, поэтому выполняются в отдельном потоке.
"""

from __future__ import annotations

from typing import Any

import httpx

from repairbot.integrations.google_api import (
    GoogleApiClient,
    GoogleApiError,
    GoogleApiUnavailable,
    QuotaLimiter,
)
from repairbot.integrations.google_auth import SCOPES, CredentialsSource
from repairbot.observability import get_logger

log = get_logger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4"
DRIVE_API = "https://www.googleapis.com/drive/v3"

__all__ = [
    "DRIVE_API",
    "SCOPES",
    "SHEETS_API",
    "SPREADSHEET_MIME",
    "QuotaLimiter",
    "SheetsClient",
    "SheetsError",
    "SheetsUnavailable",
]

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class SheetsError(GoogleApiError):
    pass


class SheetsUnavailable(SheetsError, GoogleApiUnavailable):
    """Временная ошибка: квота, 5xx, сеть. Имеет смысл повторить позже."""


class SheetsClient(GoogleApiClient):
    error_class = SheetsError
    unavailable_class = SheetsUnavailable
    service_name = "Google Sheets"

    def __init__(
        self,
        source: CredentialsSource,
        *,
        requests_per_minute: int = 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            source,
            base_url=SHEETS_API,
            requests_per_minute=requests_per_minute,
            client=client,
        )

    async def _request(  # type: ignore[override]
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Запрос к API. Абсолютный `path` уходит как есть — так вызовы
        к Drive API живут в том же счётчике квоты, а не в обход него."""
        return await self._request_json(method, path, params=params, json=json)

    # --- методы API ---

    async def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/spreadsheets/{spreadsheet_id}",
            params={"fields": "spreadsheetId,properties.title,sheets.properties"},
        )

    async def create_spreadsheet_in_folder(
        self, title: str, folder_id: str, sheet_titles: list[str]
    ) -> str:
        """Создать книгу **в папке заказчика** и вернуть её идентификатор.

        Создаём через Drive API, а не через Sheets API: Sheets кладёт файл
        в корень Диска создателя и не умеет класть в папку.

        Владелец файла — тот, от чьего имени идёт вызов: при OAuth это сам
        заказчик, при сервисном аккаунте с делегированием — указанный ящик
        в его домене. Сервисный аккаунт без делегирования не пройдёт вовсе:
        своей квоты хранилища у него нет.

        `supportsAllDrives` нужен для общих дисков; на личном Диске он
        безвреден, поэтому передаём всегда.
        """
        created = await self._request(
            "POST",
            f"{DRIVE_API}/files",
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"name": title, "mimeType": SPREADSHEET_MIME, "parents": [folder_id]},
        )
        spreadsheet_id = created["id"]

        # Drive создаёт книгу с одним листом по умолчанию: добавляем свои
        # и убираем его, иначе в книге останется пустой «Лист1».
        meta = await self.get_spreadsheet(spreadsheet_id)
        default_sheets = [s["properties"]["sheetId"] for s in meta.get("sheets", [])]

        requests: list[dict[str, Any]] = [
            {"addSheet": {"properties": {"title": title_, "index": i}}}
            for i, title_ in enumerate(sheet_titles)
        ]
        requests += [{"deleteSheet": {"sheetId": sheet_id}} for sheet_id in default_sheets]

        await self._request(
            "POST", f"/spreadsheets/{spreadsheet_id}:batchUpdate", json={"requests": requests}
        )
        log.info("sheets.created_in_folder", spreadsheet_id=spreadsheet_id, folder=folder_id)
        return str(spreadsheet_id)

    async def check_folder(self, folder_id: str) -> dict[str, Any]:
        """Проверить, что папка доступна и в неё можно писать.

        Вызывается при настройке: понятная ошибка сейчас лучше, чем
        отказ на первой синхронизации.
        """
        return await self._request(
            "GET",
            f"{DRIVE_API}/files/{folder_id}",
            params={
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,driveId,owners(emailAddress),"
                "capabilities/canAddChildren",
            },
        )

    async def about(self) -> dict[str, Any]:
        """Чей это аккаунт и сколько на нём места.

        Нужно при настройке: убедиться, что токен принадлежит заказчику,
        а не случайному ящику, и что хранилища хватит под фотоархив.
        """
        return await self._request(
            "GET",
            f"{DRIVE_API}/about",
            params={"fields": "user(emailAddress,displayName),storageQuota"},
        )

    async def add_sheets(self, spreadsheet_id: str, titles: list[str]) -> dict[str, Any]:
        if not titles:
            return {}
        return await self._request(
            "POST",
            f"/spreadsheets/{spreadsheet_id}:batchUpdate",
            json={
                "requests": [
                    {"addSheet": {"properties": {"title": title}}} for title in titles
                ]
            },
        )

    async def values_batch_update(
        self, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Пакетная запись значений. Один запрос на любое число диапазонов."""
        if not data:
            return {}
        return await self._request(
            "POST",
            f"/spreadsheets/{spreadsheet_id}/values:batchUpdate",
            json={"valueInputOption": "USER_ENTERED", "data": data},
        )

    async def values_append(
        self, spreadsheet_id: str, range_: str, rows: list[list[Any]]
    ) -> dict[str, Any]:
        if not rows:
            return {}
        return await self._request(
            "POST",
            f"/spreadsheets/{spreadsheet_id}/values/{range_}:append",
            params={
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"values": rows},
        )

    async def values_batch_get(
        self, spreadsheet_id: str, ranges: list[str]
    ) -> dict[str, list[list[Any]]]:
        """Чтение нескольких диапазонов одним запросом."""
        if not ranges:
            return {}
        payload = await self._request(
            "GET",
            f"/spreadsheets/{spreadsheet_id}/values:batchGet",
            params={"ranges": ranges, "majorDimension": "ROWS"},
        )
        return {
            entry.get("range", ""): entry.get("values", [])
            for entry in payload.get("valueRanges", [])
        }
