"""Эндпоинты вебхуков.

Платформа MAX не подписывает вебхуки, поэтому подлинность источника
подтверждается секретом в пути URL. Сравнение — постоянного времени.
Проверка объявлена зависимостью раньше сессии БД: запрос с неверным
секретом не должен приводить к обращению к базе.

Порядок обработки: принять → нормализовать → записать в журнал →
поставить задачу в очередь → ответить 200. Ответ отдаётся только после
фиксации в БД: иначе при падении воркера событие потеряется, а платформа
повтор уже не пришлёт.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.config import Settings, get_settings
from repairbot.db.session import db_session
from repairbot.domain.events import NormalizationError
from repairbot.ingest.service import IngestService
from repairbot.memory.working import WorkingMemory
from repairbot.observability import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["webhooks"])


def secrets_match(provided: str, expected: str) -> bool:
    """Сравнение секретов постоянного времени.

    Сравниваем байты, а не строки. `compare_digest` со строками требует
    только ASCII и бросает `UnicodeEncodeError` на всём остальном: запрос
    на `/webhooks/max/тест` или с кириллицей в заголовке возвращал бы 500
    вместо 404 — то есть неаутентифицированный клиент мог бы вызвать
    ошибку сервера и заодно понять, что эндпоинт существует.
    """
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def verify_max_secret(
    secret: Annotated[str, Path()],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    expected = settings.max_webhook_secret.get_secret_value()
    if not secrets_match(secret, expected):
        log.warning("webhook.bad_secret")
        # 404, а не 403: наличие эндпоинта не подтверждаем.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def verify_telegram_secret(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Telegram присылает секрет заголовком, а не в пути URL.

    Это лучше, чем у MAX: адрес попадает в журналы обратного прокси и в
    историю браузера, а заголовок — нет. Поэтому и путь здесь постоянный.
    """
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    expected = settings.telegram_webhook_secret.get_secret_value()
    if not secrets_match(provided, expected):
        log.warning("webhook.bad_secret", channel="telegram")
        # 404, а не 403: наличие эндпоинта не подтверждаем.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.post(
    "/webhooks/max/{secret}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_max_secret)],
)
async def max_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(db_session)],
) -> dict[str, Any]:
    return await _accept(request, session, request.app.state.max_adapter)


@router.post(
    "/webhooks/telegram",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_telegram_secret)],
)
async def telegram_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(db_session)],
) -> dict[str, Any]:
    adapter = getattr(request.app.state, "telegram_adapter", None)
    if adapter is None:
        # Канал не подключён. 200, чтобы платформа не копила повторы.
        log.warning("webhook.channel_disabled", channel="telegram")
        return {"accepted": 0, "duplicates": 0, "error": "channel_disabled"}
    return await _accept(request, session, adapter)


async def _accept(
    request: Request, session: AsyncSession, adapter: Any
) -> dict[str, Any]:
    """Общий приём для всех каналов.

    Разница между мессенджерами заканчивается на адаптере: дальше идут
    одни и те же события, один журнал и одна очередь. Ради этого шлюз
    и строился (раздел 3.1 ТЗ).
    """
    payload = await request.json()
    working_memory: WorkingMemory = request.app.state.working_memory

    try:
        events = adapter.normalize(payload)
    except NormalizationError as exc:
        # 200, чтобы платформа не повторяла заведомо неразбираемый апдейт.
        log.warning("webhook.normalization_failed", error=str(exc))
        return {"accepted": 0, "duplicates": 0, "error": "normalization_failed"}

    ingest = IngestService(session, working_memory)
    queued: list[int] = []
    duplicates = 0

    for event in events:
        result = await ingest.ingest(event)
        if result.duplicate:
            duplicates += 1
        elif result.event_id is not None:
            queued.append(result.event_id)

    await session.commit()

    # Задачи ставим после коммита: воркер должен видеть запись в БД.
    arq = request.app.state.arq
    if arq is not None:
        for event_id in queued:
            await arq.enqueue_job("process_event", event_id)

    return {"accepted": len(queued), "duplicates": duplicates}
