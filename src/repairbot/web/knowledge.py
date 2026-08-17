"""Знания о компании: то, что человек рассказал системе руками.

Модель не знает, что «касса» — это наличные в офисе, что подотчёт
выдают под чеки, а «ростовка» и «Ростовское шоссе» — одно место. Из
переписки это не выводится: там об этом не пишут, потому что все и так
знают. Здесь такие вещи записываются словами и уходят помощнику в
контекст.

Заметки не проверяются и ни с чем не сопоставляются — это не
справочник, а пояснения. Отсюда единственное ограничение: их общий
объём. Контекст не резиновый, и каждый символ в нём оплачивается при
каждом вопросе, поэтому предел есть и о нём говорится прямо на
странице, а не выясняется по счёту от провайдера.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import KnowledgeNote
from repairbot.observability import get_logger

log = get_logger(__name__)

MAX_TOTAL_CHARS = 8000
"""Сколько всего знаний уходит в контекст.

Восемь тысяч символов — примерно две страницы текста. Больше не
запрещаем, но предупреждаем: это цена каждого вопроса к помощнику."""

MAX_BODY_CHARS = 2000


class KnowledgeError(Exception):
    """Запись сохранить нельзя. Текст показывается человеку."""


@dataclass(slots=True)
class NoteCard:
    id: int
    title: str
    body: str
    active: bool
    created_by: str | None
    updated_at: datetime | None


async def load_notes(session: AsyncSession) -> list[NoteCard]:
    rows = await session.execute(select(KnowledgeNote).order_by(KnowledgeNote.id))
    return [
        NoteCard(
            id=n.id,
            title=n.title,
            body=n.body,
            active=n.active,
            created_by=n.created_by,
            updated_at=n.updated_at,
        )
        for n in rows.scalars().all()
    ]


async def create_note(
    session: AsyncSession, *, title: str, body: str, by: str = ""
) -> KnowledgeNote:
    title = title.strip()
    body = body.strip()
    if not title:
        raise KnowledgeError("Заголовок не может быть пустым")
    if not body:
        raise KnowledgeError("Текст не может быть пустым")
    if len(body) > MAX_BODY_CHARS:
        raise KnowledgeError(
            f"Текст длиннее {MAX_BODY_CHARS} символов. Разбейте на несколько записей: "
            "так их проще включать и выключать по отдельности."
        )

    note = KnowledgeNote(title=title, body=body, created_by=by or None)
    session.add(note)
    await session.flush()
    log.info("knowledge.created", note_id=note.id, title=title, by=by)
    return note


async def toggle_note(session: AsyncSession, note_id: int, *, by: str = "") -> KnowledgeNote:
    """Включить или выключить запись.

    Выключенная остаётся в списке: правило часто нужно отключить на
    время и вернуть, а удалённое приходится вспоминать и набирать заново.
    """
    note = await session.get(KnowledgeNote, note_id)
    if note is None:
        raise KnowledgeError("Запись не найдена")
    note.active = not note.active
    await session.flush()
    log.info("knowledge.toggled", note_id=note.id, active=note.active, by=by)
    return note


async def delete_note(session: AsyncSession, note_id: int, *, by: str = "") -> None:
    note = await session.get(KnowledgeNote, note_id)
    if note is None:
        raise KnowledgeError("Запись не найдена")
    await session.delete(note)
    await session.flush()
    log.info("knowledge.deleted", note_id=note_id, by=by)


async def render_for_context(session: AsyncSession, limit_chars: int = MAX_TOTAL_CHARS) -> str:
    """Включённые знания одним куском для контекста помощника.

    Обрезается по границе записи, а не по символу: половина правила
    хуже его отсутствия — модель достроит вторую половину сама.
    """
    rows = await session.execute(
        select(KnowledgeNote)
        .where(KnowledgeNote.active.is_(True))
        .order_by(KnowledgeNote.id)
    )
    parts: list[str] = []
    used = 0
    for note in rows.scalars().all():
        block = f"— {note.title}: {note.body}"
        if used + len(block) > limit_chars:
            break
        parts.append(block)
        used += len(block)

    if not parts:
        return ""
    return "\n\nЧто известно о компании (записано людьми):\n" + "\n".join(parts)


async def total_chars(session: AsyncSession) -> int:
    rows = await session.execute(
        select(KnowledgeNote.title, KnowledgeNote.body).where(KnowledgeNote.active.is_(True))
    )
    return sum(len(t) + len(b) + 4 for t, b in rows.all())
