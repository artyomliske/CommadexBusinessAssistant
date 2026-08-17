"""Доставка сводок (этап 5).

Два адресата, и это не дублирование. В сводную книгу уходят все цифры,
включая суммы: её открывает тот, у кого есть к ней доступ. В мессенджер
уходит короткое сообщение с тем, что требует внимания, — оно приходит
само, и денежных сведений в нём нет по разделу 6.

Почему не почта. Раздел 5 ТЗ предполагает рассылку через Gmail, но у
заказчика личный аккаунт Google, а `gmail.send` — область ограниченного
доступа: без проверки приложения Google её не выдаст, а для одного клиента
такая проверка несоразмерна. Раз основной канал всё равно MAX, почта в нём
лишнее звено. Решение обратимо: если заказчик переедет на свой домен,
делегирование закроет и отправку писем.

Сводка руководителю разрешена без подтверждения (раздел 6), но идёт она
всё равно через контролёр — иначе появился бы путь наружу мимо журнала
аудита, а его быть не должно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents.reports import (
    DIGEST_HEADERS,
    DIGEST_SHEET,
    Digest,
    Period,
    build_digest,
    digest_rows,
    render_message,
)
from repairbot.integrations.sheets.book import a1_range
from repairbot.observability import get_logger
from repairbot.outbound.controller import Controller, OutboundRequest
from repairbot.outbound.policy import Audience, Intent

log = get_logger(__name__)


class SheetsWriter(Protocol):
    async def add_sheets(self, spreadsheet_id: str, titles: list[str]) -> dict[str, Any]: ...

    async def values_append(
        self, spreadsheet_id: str, range_: str, rows: list[list[Any]]
    ) -> dict[str, Any]: ...

    async def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class DeliveryResult:
    period: str
    objects: int
    attention: int
    rows_written: int = 0
    outbound_id: int | None = None
    verdict: str | None = None
    sheet_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "objects": self.objects,
            "attention": self.attention,
            "rows_written": self.rows_written,
            "outbound_id": self.outbound_id,
            "verdict": self.verdict,
            "sheet_error": self.sheet_error,
        }


class ReportDelivery:
    def __init__(
        self,
        *,
        sheets: SheetsWriter | None = None,
        rollup_spreadsheet_id: str = "",
        manager_channel: str = "max",
        manager_chat_id: str = "",
        pricing: dict[str, float] | None = None,
    ) -> None:
        self._sheets = sheets
        self._rollup = rollup_spreadsheet_id
        self._channel = manager_channel
        self._chat_id = manager_chat_id
        self._pricing = pricing or {}

    async def deliver(
        self,
        session: AsyncSession,
        period: Period,
        *,
        controller: Controller | None = None,
    ) -> DeliveryResult:
        digest = await build_digest(session, period, **self._pricing)
        result = DeliveryResult(
            period=period.value,
            objects=len(digest.objects),
            attention=len(digest.attention),
        )

        # Сначала таблица, потом сообщение. Порядок важен: сообщение
        # обещает, что цифры лежат в книге, и обещание должно быть
        # выполнено к моменту его отправки.
        if self._sheets is not None and self._rollup:
            try:
                result.rows_written = await self._write_sheet(digest)
            except Exception as exc:
                # Сводка руководителю ценнее строчек в таблице: если
                # книга недоступна, сообщение всё равно уходит.
                result.sheet_error = str(exc)
                log.warning("reports.sheet_failed", error=str(exc))

        if controller is not None and self._chat_id:
            outcome = await controller.review(
                OutboundRequest(
                    channel=self._channel,
                    channel_chat_id=self._chat_id,
                    text=render_message(digest),
                    intent=Intent.MANAGER_DIGEST,
                    audience=Audience.MANAGER,
                    idempotency_key=digest.key,
                )
            )
            result.verdict = outcome.verdict.value
            if outcome.may_send:
                result.outbound_id = outcome.outbound_id

        log.info("reports.delivered", **result.as_dict())
        return result

    async def _write_sheet(self, digest: Digest) -> int:
        rows = digest_rows(digest)
        if not rows:
            return 0

        assert self._sheets is not None
        await self._ensure_sheet()
        await self._sheets.values_append(self._rollup, a1_range(DIGEST_SHEET), rows)
        return len(rows)

    async def _ensure_sheet(self) -> None:
        """Завести лист сводок, если его ещё нет.

        Проверяем состав книги, а не ловим ошибку добавления: заказчик
        мог переименовать или удалить лист руками, и падение на этом
        месте лишило бы его сводок молча.
        """
        assert self._sheets is not None
        meta = await self._sheets.get_spreadsheet(self._rollup)
        titles = {
            (sheet.get("properties") or {}).get("title")
            for sheet in meta.get("sheets") or []
        }
        if DIGEST_SHEET in titles:
            return

        await self._sheets.add_sheets(self._rollup, [DIGEST_SHEET])
        await self._sheets.values_append(
            self._rollup, a1_range(DIGEST_SHEET), [DIGEST_HEADERS]
        )
