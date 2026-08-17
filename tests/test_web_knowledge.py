"""Знания о компании: то, что человек объясняет системе словами.

Модель не знает, что «касса» — наличные в офисе: в чатах об этом не
пишут, потому что все и так знают. Здесь такие вещи записываются
руками и уходят помощнику в контекст.
"""

from __future__ import annotations

import pytest

from repairbot.web import knowledge


async def _note(session, title="Касса", body="Наличные в офисе.", **kw):
    return await knowledge.create_note(session, title=title, body=body, **kw)


# --- запись ---


async def test_note_is_saved_and_listed(db_session):
    await _note(db_session, by="artyom")

    cards = await knowledge.load_notes(db_session)

    assert [c.title for c in cards] == ["Касса"]
    assert cards[0].active is True
    assert cards[0].created_by == "artyom"


async def test_empty_title_is_refused(db_session):
    with pytest.raises(knowledge.KnowledgeError, match="Заголовок"):
        await _note(db_session, title="   ")


async def test_empty_body_is_refused(db_session):
    with pytest.raises(knowledge.KnowledgeError, match="Текст"):
        await _note(db_session, body="  ")


async def test_a_very_long_note_is_refused(db_session):
    """Одна запись на десять правил не выключается частично."""
    with pytest.raises(knowledge.KnowledgeError, match="Разбейте"):
        await _note(db_session, body="я" * (knowledge.MAX_BODY_CHARS + 1))


# --- включение и выключение ---


async def test_note_can_be_switched_off_and_back(db_session):
    note = await _note(db_session)

    await knowledge.toggle_note(db_session, note.id)
    assert (await knowledge.load_notes(db_session))[0].active is False

    await knowledge.toggle_note(db_session, note.id)
    assert (await knowledge.load_notes(db_session))[0].active is True


async def test_switched_off_note_stays_in_the_list(db_session):
    """Удалённое правило приходится вспоминать и набирать заново."""
    note = await _note(db_session)
    await knowledge.toggle_note(db_session, note.id)

    assert len(await knowledge.load_notes(db_session)) == 1


async def test_missing_note_is_reported(db_session):
    with pytest.raises(knowledge.KnowledgeError, match="не найдена"):
        await knowledge.toggle_note(db_session, 99999)


async def test_note_can_be_deleted(db_session):
    note = await _note(db_session)

    await knowledge.delete_note(db_session, note.id)

    assert await knowledge.load_notes(db_session) == []


# --- что уходит в контекст ---


async def test_active_notes_reach_the_context(db_session):
    await _note(db_session, title="Касса", body="Наличные в офисе.")
    await _note(db_session, title="Подотчёт", body="Выдаём только под чеки.")

    rendered = await knowledge.render_for_context(db_session)

    assert "Касса: Наличные в офисе." in rendered
    assert "Подотчёт: Выдаём только под чеки." in rendered


async def test_switched_off_note_does_not_reach_the_context(db_session):
    note = await _note(db_session, title="Старое правило", body="Больше не действует.")
    await knowledge.toggle_note(db_session, note.id)

    assert "Старое правило" not in await knowledge.render_for_context(db_session)


async def test_nothing_written_means_nothing_added(db_session):
    """Пустой заголовок раздела в контексте — лишние символы и ничего больше."""
    assert await knowledge.render_for_context(db_session) == ""


async def test_the_limit_cuts_whole_notes(db_session):
    """Половина правила хуже его отсутствия: модель достроит вторую сама."""
    await _note(db_session, title="Первое", body="я" * 200)
    # Наполнитель заведомо не встречается в служебном тексте раздела,
    # иначе проверка «текста нет» срабатывает на заголовке.
    await _note(db_session, title="Второе", body="щ" * 200)

    rendered = await knowledge.render_for_context(db_session, limit_chars=250)

    assert "Первое" in rendered
    assert "Второе" not in rendered
    assert "щ" not in rendered


async def test_total_counts_only_active(db_session):
    await _note(db_session, title="Первое", body="я" * 100)
    note = await _note(db_session, title="Второе", body="ю" * 100)
    await knowledge.toggle_note(db_session, note.id)

    total = await knowledge.total_chars(db_session)

    assert 100 < total < 200
