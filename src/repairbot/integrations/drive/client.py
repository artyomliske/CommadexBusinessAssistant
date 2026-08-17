"""Клиент Google Drive: папки объектов и загрузка файлов.

Два способа загрузки, и выбор между ними не стилистический. Google
принимает multipart только для файлов до 5 МБ; всё, что крупнее, — через
resumable-сессию. Фотография с телефона легко перешагивает эту границу,
поэтому поддержаны оба.

Файлы принадлежат тому, от чьего имени идёт вызов, — при OAuth это сам
заказчик (см. `repairbot.integrations.google_auth`).
"""

from __future__ import annotations

import json
import mimetypes
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from repairbot.integrations.google_api import (
    GoogleApiClient,
    GoogleApiError,
    GoogleApiUnavailable,
)
from repairbot.integrations.google_auth import CredentialsSource
from repairbot.observability import get_logger

log = get_logger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

MULTIPART_LIMIT = 5 * 1024 * 1024
"""Граница Google: до 5 МБ — multipart, крупнее — resumable."""

DEFAULT_REQUESTS_PER_MINUTE = 300
"""Квота Диска мягче, чем у Таблиц. Значение с большим запасом вниз:
архив не срочен, а конкурировать с витриной за квоту ему незачем."""


class DriveError(GoogleApiError):
    pass


class DriveUnavailable(DriveError, GoogleApiUnavailable):
    """Временная ошибка: квота, 5xx, сеть. Имеет смысл повторить позже."""


@dataclass(frozen=True, slots=True)
class UploadedFile:
    file_id: str
    name: str
    size_bytes: int
    web_link: str | None = None


class DriveClient(GoogleApiClient):
    error_class = DriveError
    unavailable_class = DriveUnavailable
    service_name = "Google Drive"

    def __init__(
        self,
        source: CredentialsSource,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            source,
            base_url=DRIVE_API,
            requests_per_minute=requests_per_minute,
            client=client,
            # Загрузка файла дольше обычного запроса к API.
            timeout=httpx.Timeout(10.0, read=180.0, write=180.0),
        )

    # --- папки ---

    async def ensure_folder(self, name: str, parent_id: str) -> str:
        """Найти папку по имени внутри родительской или создать её.

        Поиск перед созданием обязателен: Диск разрешает две папки с
        одинаковым именем в одном месте, и без проверки при каждом
        перезапуске появлялась бы новая.
        """
        found = await self.find_folder(name, parent_id)
        if found is not None:
            return found

        created = await self._request_json(
            "POST",
            f"{DRIVE_API}/files",
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        folder_id = str(created["id"])
        log.info("drive.folder_created", name=name, folder_id=folder_id, parent=parent_id)
        return folder_id

    async def find_folder(self, name: str, parent_id: str) -> str | None:
        query = (
            f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' "
            f"and '{_escape(parent_id)}' in parents and trashed = false"
        )
        found = await self._request_json(
            "GET",
            f"{DRIVE_API}/files",
            params={
                "q": query,
                "fields": "files(id,name)",
                "pageSize": 1,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        files = found.get("files") or []
        return str(files[0]["id"]) if files else None

    # --- загрузка ---

    async def upload(
        self,
        *,
        name: str,
        content: bytes,
        folder_id: str,
        mime_type: str | None = None,
        app_properties: dict[str, str] | None = None,
    ) -> UploadedFile:
        """Положить файл в папку.

        `app_properties` — служебные метки, видимые только нашему
        приложению. Кладём туда идентификатор вложения: по нему потом
        можно найти файл на Диске, даже если запись в базе потерялась.
        """
        mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        metadata: dict[str, Any] = {"name": name, "parents": [folder_id]}
        if app_properties:
            metadata["appProperties"] = app_properties

        if len(content) <= MULTIPART_LIMIT:
            payload = await self._upload_multipart(metadata, content, mime)
        else:
            payload = await self._upload_resumable(metadata, content, mime)

        uploaded = UploadedFile(
            file_id=str(payload["id"]),
            name=str(payload.get("name") or name),
            size_bytes=len(content),
            web_link=payload.get("webViewLink"),
        )
        log.info(
            "drive.uploaded",
            file_id=uploaded.file_id,
            name=uploaded.name,
            size=uploaded.size_bytes,
            folder=folder_id,
        )
        return uploaded

    async def _upload_multipart(
        self, metadata: dict[str, Any], content: bytes, mime: str
    ) -> dict[str, Any]:
        boundary = f"repairbot-{secrets.token_hex(16)}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                json.dumps(metadata, ensure_ascii=False).encode(),
                f"\r\n--{boundary}\r\n".encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        return await self._request_json(
            "POST",
            UPLOAD_API,
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": "id,name,webViewLink",
            },
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )

    async def _upload_resumable(
        self, metadata: dict[str, Any], content: bytes, mime: str
    ) -> dict[str, Any]:
        """Загрузка в два шага: сессия, затем тело одним куском.

        На куски тело не режем: докачка имела бы смысл при загрузке из
        ненадёжной сети, а мы отправляем из серверного процесса. При обрыве
        проще повторить всю задачу — она идемпотентна.
        """
        session = await self._request(
            "POST",
            UPLOAD_API,
            params={"uploadType": "resumable", "supportsAllDrives": "true"},
            json=metadata,
            headers={
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(len(content)),
            },
        )
        location = session.headers.get("Location")
        if not location:
            raise DriveError("Диск не вернул адрес сессии загрузки")

        uploaded = await self._request(
            "PUT",
            location,
            content=content,
            headers={"Content-Type": mime},
        )
        return uploaded.json() if uploaded.content else {}

    async def download(self, file_id: str, *, max_bytes: int | None = None) -> bytes:
        """Забрать содержимое файла с Диска.

        Распознавание читает отсюда, а не из мессенджера: ссылки на CDN
        живут неизвестно сколько, а копия на Диске — постоянная. Это же
        позволяет перечитать документ заново после доработки промптов.
        """
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        content = response.content
        if max_bytes is not None and len(content) > max_bytes:
            raise DriveError(
                f"Файл {len(content) // 1024} КБ больше предела {max_bytes // 1024} КБ"
            )
        return content

    async def rename(
        self, file_id: str, name: str, *, move_to: str | None = None, move_from: str | None = None
    ) -> None:
        """Переименовать файл и, если нужно, перенести его в другую папку.

        Имя уточняется после распознавания: в момент загрузки известна
        только подпись под файлом, а что это за документ — уже после
        разбора. Переносим тем же вызовом, потому что второй повод
        трогать файл ровно один: вложение сначала легло в общую папку,
        а объект у сообщения появился позже.

        Диск не переносит файл, а меняет список родителей, поэтому
        старого родителя нужно назвать явно — иначе файл окажется сразу
        в двух папках.
        """
        params: dict[str, str] = {"supportsAllDrives": "true", "fields": "id"}
        if move_to:
            params["addParents"] = move_to
            if move_from:
                params["removeParents"] = move_from

        await self._request_json(
            "PATCH", f"{DRIVE_API}/files/{file_id}", params=params, json={"name": name}
        )
        log.info("drive.file_renamed", file_id=file_id, name=name, moved_to=move_to)

    async def delete(self, file_id: str) -> None:
        """Удалить файл. Используется при откате неудачной загрузки."""
        await self._request(
            "DELETE", f"{DRIVE_API}/files/{file_id}", params={"supportsAllDrives": "true"}
        )


def _escape(value: str) -> str:
    """Экранирование для языка запросов Диска.

    Апостроф в имени папки («Квартира О'Коннора») иначе развалил бы
    запрос — а поиск папки идёт по имени объекта, которое приходит извне.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")
