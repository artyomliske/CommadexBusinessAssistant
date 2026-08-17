"""Синхронизация журнала событий с витриной в Google Sheets.

Направление одно: PostgreSQL → таблица. Источник истины — база, таблица
показывает её состояние. Из этого следуют два свойства:

* синхронизация идёт от водяного знака (последнего перенесённого события),
  поэтому повторный запуск не дублирует строки и не требует блокировок;
* ручные правки менеджера считываются **до** записи и регистрируются в
  журнале отдельным типом события, после чего система их не перезаписывает
  (раздел 5 ТЗ).

Ошибка записи в таблицу не должна ронять обработку событий: витрина
отстанет и догонит на следующем проходе.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import Event, RepairObject
from repairbot.domain.events import (
    FACT_EVENT_PREFIX,
    MANUAL_EDIT_EVENT,
    NEEDS_HUMAN_EVENT,
)
from repairbot.domain.facts import Fact
from repairbot.integrations.sheets import book
from repairbot.integrations.sheets.book import ROLLUP_HEADERS, ROLLUP_SHEET, Sheet
from repairbot.integrations.sheets.client import SheetsClient, SheetsError, SheetsUnavailable
from repairbot.observability import get_logger

log = get_logger(__name__)

STATE_KEY = "sheets"
"""Ключ в `objects.state`, где хранится состояние синхронизации."""

MAX_EVENTS_PER_FLUSH = 500
"""Ограничение на проход: догоняем историю постепенно, не упираясь в квоту."""


@dataclass(slots=True)
class SyncOutcome:
    object_id: int
    rows_written: int = 0
    manual_edits: int = 0
    last_event_id: int | None = None
    error: str | None = None
    lagging: bool = False
    """True, если события кончились не потому, что всё перенесено, а по лимиту."""


@dataclass(slots=True)
class _PendingRows:
    by_sheet: dict[Sheet, list[list[Any]]] = field(default_factory=dict)

    def add(self, sheet: Sheet, row: list[Any]) -> None:
        self.by_sheet.setdefault(sheet, []).append(row)

    @property
    def total(self) -> int:
        return sum(len(rows) for rows in self.by_sheet.values())


class SheetsSync:
    def __init__(self, client: SheetsClient, *, drive_folder_id: str = "") -> None:
        self._client = client
        self._drive_folder_id = drive_folder_id
        """Папка, где живут книги объектов. Пусто — книги не создаём."""

    # --- подготовка книги ---

    async def ensure_book(self, session: AsyncSession, obj: RepairObject) -> str:
        """Найти или создать книгу объекта.

        Книга создаётся **в папке заказчика**. Если папка не настроена,
        книгу не создаём вовсе: файл, созданный сервисным аккаунтом «где
        получится», принадлежал бы нам, а не заказчику, — и по окончании
        работ его данные остались бы на нашей стороне.
        """
        if obj.spreadsheet_id:
            return obj.spreadsheet_id

        if not self._drive_folder_id:
            raise SheetsError(
                f"Книга для {obj.code} не создана: не задан GOOGLE_DRIVE_FOLDER_ID. "
                "Нужна папка в Workspace заказчика — на общем диске либо в Диске "
                "ящика бота при делегировании в домене. Либо пропишите готовую "
                "книгу в objects.spreadsheet_id."
            )

        spreadsheet_id = await self._client.create_spreadsheet_in_folder(
            title=f"{obj.code} — {obj.address}",
            folder_id=self._drive_folder_id,
            sheet_titles=[s.value for s in book.OBJECT_SHEETS],
        )

        await self._client.values_batch_update(
            spreadsheet_id,
            [
                {
                    "range": book.a1_range(sheet, "A1"),
                    "values": [book.HEADERS[sheet]],
                }
                for sheet in book.OBJECT_SHEETS
            ],
        )

        obj.spreadsheet_id = spreadsheet_id
        log.info("sheets.book_created", object=obj.code, spreadsheet_id=spreadsheet_id)
        return spreadsheet_id

    # --- основной проход ---

    async def sync_object(self, session: AsyncSession, obj: RepairObject) -> SyncOutcome:
        outcome = SyncOutcome(object_id=obj.id)
        try:
            spreadsheet_id = await self.ensure_book(session, obj)

            # Сначала читаем правки: иначе запись затрёт их своей версией.
            outcome.manual_edits = await self._register_manual_edits(
                session, obj, spreadsheet_id
            )

            events, lagging = await self._events_to_sync(session, obj)
            outcome.lagging = lagging
            if events:
                pending = _render(events)
                await self._write(spreadsheet_id, pending)
                outcome.rows_written = pending.total
                outcome.last_event_id = events[-1].id
                self._advance_watermark(obj, events[-1].id)

            await self._write_summary(spreadsheet_id, obj)

        except SheetsUnavailable as exc:
            # Витрина отстанет и догонит: водяной знак не сдвинут.
            log.warning("sheets.unavailable", object=obj.code, error=str(exc))
            outcome.error = str(exc)
        except SheetsError as exc:
            log.error("sheets.failed", object=obj.code, error=str(exc))
            outcome.error = str(exc)

        return outcome

    async def _events_to_sync(
        self, session: AsyncSession, obj: RepairObject
    ) -> tuple[list[Event], bool]:
        watermark = self._watermark(obj)
        rows = await session.execute(
            select(Event)
            .where(
                Event.object_id == obj.id,
                Event.id > watermark,
                Event.event_type != MANUAL_EDIT_EVENT,
            )
            .order_by(Event.id)
            .limit(MAX_EVENTS_PER_FLUSH + 1)
        )
        events = list(rows.scalars().all())
        lagging = len(events) > MAX_EVENTS_PER_FLUSH
        return events[:MAX_EVENTS_PER_FLUSH], lagging

    async def _write(self, spreadsheet_id: str, pending: _PendingRows) -> None:
        """Добавить строки.

        `values:append` работает с одним диапазоном за вызов, поэтому здесь
        по одному запросу на лист — не по одному на строку. При шести листах
        это шесть запросов на проход, что помещается в квоту 60/мин.
        """
        for sheet, rows in pending.by_sheet.items():
            await self._client.values_append(spreadsheet_id, book.a1_range(sheet, "A1"), rows)

    async def _write_summary(self, spreadsheet_id: str, obj: RepairObject) -> None:
        """Сводка — производная от журнала, поэтому перезаписывается целиком."""
        rows = book.summary_rows(obj.state or {}, updated_at=datetime.now(tz=UTC))
        await self._client.values_batch_update(
            spreadsheet_id,
            [
                {
                    "range": book.a1_range(Sheet.SUMMARY, f"A2:C{len(rows) + 1}"),
                    "values": rows,
                }
            ],
        )

    # --- ручные правки ---

    async def _register_manual_edits(
        self, session: AsyncSession, obj: RepairObject, spreadsheet_id: str
    ) -> int:
        """Считать правки менеджера и записать их в журнал.

        Сравниваются только те колонки, которые правит человек. Ключ строки —
        колонка «Источник»: она стабильна и не редактируется.
        """
        sheets = sorted(book.EDITABLE_COLUMNS, key=lambda s: s.value)
        ranges = [book.a1_range(s, "A2:Z") for s in sheets]
        values = await self._client.values_batch_get(spreadsheet_id, ranges)

        # batchGet возвращает диапазоны в порядке запроса, но с нормализованными
        # именами — сопоставляем по позиции, а не по строке диапазона.
        ordered = list(values.values())
        snapshot = self._snapshot(obj)
        registered = 0

        for position, sheet in enumerate(sheets):
            rows = ordered[position] if position < len(ordered) else []
            spec = book.EDITABLE_COLUMNS[sheet]
            sheet_snapshot: dict[str, list[str]] = snapshot.setdefault(sheet.value, {})

            for row in rows:
                key = _cell(row, spec.key_index)
                if not key:
                    continue
                current = [_cell(row, i) for i in spec.editable_indices]
                previous = sheet_snapshot.get(key)

                if previous == current:
                    continue
                if previous is not None:
                    await self._append_manual_edit(
                        session,
                        obj,
                        sheet=sheet,
                        key=key,
                        before=previous,
                        after=current,
                        columns=[book.HEADERS[sheet][i] for i in spec.editable_indices],
                    )
                    registered += 1
                sheet_snapshot[key] = current

        if registered:
            log.info("sheets.manual_edits", object=obj.code, count=registered)
        self._store_snapshot(obj, snapshot)
        return registered

    async def _append_manual_edit(
        self,
        session: AsyncSession,
        obj: RepairObject,
        *,
        sheet: Sheet,
        key: str,
        before: list[str],
        after: list[str],
        columns: list[str],
    ) -> None:
        payload = {
            "sheet": sheet.value,
            "row_key": key,
            "columns": columns,
            "before": before,
            "after": after,
        }
        stmt = (
            pg_insert(Event)
            .values(
                object_id=obj.id,
                channel="sheets",
                event_type=MANUAL_EDIT_EVENT,
                payload=payload,
                applied=True,
                needs_human=False,
                # Ключ включает новое значение: следующая правка той же
                # строки — отдельное событие, а повтор того же чтения — нет.
                dedup_key=f"manual:{sheet.value}:{key}:{'|'.join(after)}"[:160],
                occurred_at=datetime.now(tz=UTC),
            )
            .on_conflict_do_nothing(constraint="uq_event_dedup")
        )
        await session.execute(stmt)

    # --- сводная книга по всем объектам ---

    async def sync_rollup(self, session: AsyncSession, spreadsheet_id: str) -> int:
        """Перезаписать сводную книгу (раздел 5 ТЗ).

        Полная перезапись, а не добавление: это срез текущего состояния всех
        объектов, а не история.
        """
        objects = list(
            (
                await session.execute(
                    select(RepairObject)
                    .where(RepairObject.status == "active")
                    .order_by(RepairObject.code)
                )
            )
            .scalars()
            .all()
        )

        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
        rows = [
            [
                obj.code,
                obj.address,
                (obj.state or {}).get("current_stage", ""),
                obj.status,
                (obj.state or {}).get("open_questions_count", ""),
                (obj.state or {}).get("pending_confirmations", ""),
                stamp,
            ]
            for obj in objects
        ]

        last_column = book.column_label(len(ROLLUP_HEADERS) - 1)
        await self._client.values_batch_update(
            spreadsheet_id,
            [
                {
                    "range": book.a1_range(ROLLUP_SHEET, f"A1:{last_column}1"),
                    "values": [ROLLUP_HEADERS],
                },
                {
                    "range": book.a1_range(
                        ROLLUP_SHEET, f"A2:{last_column}{max(len(rows) + 1, 2)}"
                    ),
                    # Пустая строка очищает лист, если активных объектов не осталось.
                    "values": rows or [[""] * len(ROLLUP_HEADERS)],
                },
            ],
        )
        log.info("sheets.rollup_synced", objects=len(rows))
        return len(rows)

    # --- состояние синхронизации ---

    @staticmethod
    def _watermark(obj: RepairObject) -> int:
        return int(((obj.state or {}).get(STATE_KEY) or {}).get("last_event_id") or 0)

    @staticmethod
    def _advance_watermark(obj: RepairObject, event_id: int) -> None:
        state = dict(obj.state or {})
        sheets_state = dict(state.get(STATE_KEY) or {})
        sheets_state["last_event_id"] = event_id
        sheets_state["synced_at"] = datetime.now(tz=UTC).isoformat()
        state[STATE_KEY] = sheets_state
        # Присваиваем новый словарь: JSONB не отслеживает изменения на месте.
        obj.state = state

    @staticmethod
    def _snapshot(obj: RepairObject) -> dict[str, dict[str, list[str]]]:
        stored = ((obj.state or {}).get(STATE_KEY) or {}).get("snapshot") or {}
        return {sheet: dict(rows) for sheet, rows in stored.items()}

    @staticmethod
    def _store_snapshot(obj: RepairObject, snapshot: dict[str, Any]) -> None:
        state = dict(obj.state or {})
        sheets_state = dict(state.get(STATE_KEY) or {})
        sheets_state["snapshot"] = snapshot
        state[STATE_KEY] = sheets_state
        obj.state = state


def _render(events: list[Event]) -> _PendingRows:
    """Разложить события журнала по листам."""
    pending = _PendingRows()

    for event in events:
        occurred_at = event.occurred_at or event.created_at
        source = book.source_link(event.channel_message_id, event.id)

        if event.event_type.startswith(FACT_EVENT_PREFIX):
            fact = _fact_from_payload(event)
            if fact is not None:
                pending.add(book.sheet_for(fact.type), book.fact_row(
                    fact, occurred_at=occurred_at, source=source
                ))
                # Факт попадает и в журнал: он ведётся по всем событиям объекта.
                pending.add(
                    Sheet.JOURNAL,
                    book.journal_row(
                        occurred_at=occurred_at,
                        event_type=event.event_type,
                        description=fact.summary,
                        confidence=float(event.confidence) if event.confidence else None,
                        applied=event.applied,
                        source=source,
                    ),
                )
                continue

        pending.add(
            Sheet.JOURNAL,
            book.journal_row(
                occurred_at=occurred_at,
                event_type=event.event_type,
                description=_describe(event),
                confidence=float(event.confidence) if event.confidence else None,
                applied=event.applied,
                source=source,
            ),
        )
    return pending


def _fact_from_payload(event: Event) -> Fact | None:
    from pydantic import ValidationError

    try:
        return Fact.model_validate(event.payload or {})
    except ValidationError:
        log.warning("sheets.unparseable_fact", event_id=event.id, event_type=event.event_type)
        return None


def _describe(event: Event) -> str:
    payload = event.payload or {}
    if event.event_type == NEEDS_HUMAN_EVENT:
        return f"Требует внимания менеджера: {payload.get('reason', '')}"
    # Транспортные события описываем текстом сообщения, если он есть.
    message = payload.get("message") or {}
    body = message.get("body") or {}
    text = body.get("text")
    return text or event.event_type


def _cell(row: list[Any], index: int) -> str:
    """Значение ячейки. Sheets обрезает пустые хвосты, поэтому строка короче схемы."""
    if index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value).strip()
