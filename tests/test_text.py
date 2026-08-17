"""Русские числительные и форматы дат в панели.

Мелочь, из-за которой интерфейс выглядит недоделанным: «1 учётных
записей», «1 платёж(ей)», «превышение на 15» без единицы.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from repairbot import text
from repairbot.web.routes import _dt, _iso_dt


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "0 платежей"),
        (1, "1 платёж"),
        (2, "2 платежа"),
        (4, "4 платежа"),
        (5, "5 платежей"),
        (10, "10 платежей"),
        (21, "21 платёж"),
        (22, "22 платежа"),
        (25, "25 платежей"),
        (101, "101 платёж"),
    ],
)
def test_plural_by_last_digit(number, expected):
    assert text.count(number, "платёж") == expected


@pytest.mark.parametrize("number", [11, 12, 13, 14, 111, 112])
def test_second_ten_always_takes_the_many_form(number):
    """Одиннадцать — не «один»: «11 платежей», а не «11 платёж»."""
    assert text.count(number, "платёж") == f"{number} платежей"


def test_negative_numbers_use_the_absolute_value():
    """Просрочка считается отрицательными днями и переворачивается в шаблоне."""
    assert text.plural_form(-1, "день", "дня", "дней") == "день"
    assert text.plural_form(-5, "день", "дня", "дней") == "дней"


def test_unknown_word_does_not_break_the_page():
    """Опечатка в шаблоне не должна ронять страницу."""
    assert text.count(3, "виджет") == "3 виджет"


def test_words_used_in_templates_are_known():
    """Все слова, которыми пользуются шаблоны, должны быть в справочнике.

    Иначе опечатка проявится только на странице и только при нужном
    числе — то есть, скорее всего, у заказчика.
    """
    import re
    from pathlib import Path

    templates = Path("src/repairbot/web/templates")
    used = set()
    for path in templates.rglob("*.html"):
        used |= set(re.findall(r'count\("([^"]+)"\)', path.read_text(encoding="utf-8")))

    assert used, "фильтр склонения нигде не используется — тест устарел"
    assert used <= set(text.WORDS), f"нет форм для: {used - set(text.WORDS)}"


# --- даты ---


def test_current_year_is_shown_without_the_year():
    now = datetime.now(tz=UTC)
    value = now.replace(month=1, day=5, hour=17, minute=17)

    assert _dt(value) == "05.01 17:17"


def test_other_years_carry_the_year():
    """Событие прошлого года иначе читается как позавчерашнее."""
    now = datetime.now(tz=UTC)
    value = datetime(now.year - 1, 7, 5, 17, 17, tzinfo=UTC)

    assert _dt(value) == f"05.07.{now.year - 1} 17:17"


def test_empty_date_is_a_dash():
    assert _dt(None) == "—"


def test_iso_string_from_json_state_is_formatted_the_same_way():
    """В objects.state время лежит строками — это JSON."""
    now = datetime.now(tz=UTC)

    assert _iso_dt(f"{now.year - 1}-07-05T17:17:38+00:00").endswith(f"{now.year - 1} 17:17")


def test_broken_iso_string_is_shown_as_is():
    assert _iso_dt("вчера") == "вчера"
