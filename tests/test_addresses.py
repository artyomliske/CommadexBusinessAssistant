"""Узнавание адреса в тексте сообщения.

Цена ошибки несимметрична: неузнанный адрес попадёт человеку на
разбор, а адрес, приписанный чужому объекту, разойдётся по сводкам
и отчётам молча. Тесты закрепляют эту несимметричность.
"""

from __future__ import annotations

import pytest

from repairbot.domain.addresses import (
    is_usable_alias,
    match_objects,
    normalize,
)

ALIASES = [("Ленина 5", 1), ("Мира 12", 2)]


# --- приведение к сравнимому виду ---


@pytest.mark.parametrize(
    "written",
    ["Ленина 5", "ул. Ленина, д. 5", "УЛИЦА ЛЕНИНА ДОМ 5", "ленина-5"],
)
def test_the_same_place_written_differently_gives_one_form(written):
    assert normalize(written) == ("ленина", "5")


def test_corpus_written_together_and_apart_is_the_same():
    """«5к2» и «5 корп. 2» пишут вперемешку в одном и том же чате."""
    assert normalize("Ленина 5к2") == normalize("ул. Ленина, д. 5, корп. 2")


def test_house_number_is_not_glued_to_the_corpus():
    """Дом 52 и дом 5 корпус 2 — разные объекты, и путать их нельзя."""
    assert normalize("Ленина 52") != normalize("Ленина 5к2")


def test_yo_does_not_split_the_same_street():
    assert normalize("Гвардейцев Ёлкина 3") == normalize("Гвардейцев Елкина 3")


# --- какие образцы вообще годятся ---


def test_digits_only_alias_is_refused():
    """«5» встретится в любом сообщении про пять мешков."""
    assert not is_usable_alias("5")
    assert not is_usable_alias("д. 5")


def test_alias_with_a_word_is_accepted():
    assert is_usable_alias("Ленина 5")
    assert is_usable_alias("ЖК Форум")


def test_empty_alias_is_refused():
    assert not is_usable_alias("   ")


# --- поиск в тексте ---


def test_address_inside_a_sentence_is_found():
    matches = match_objects("на Ленина 5 закончили стяжку", ALIASES)

    assert [m.object_id for m in matches] == [1]


def test_words_must_stand_together():
    """Иначе «Ленина закончили, 5 мешков» приписалось бы объекту."""
    assert match_objects("Ленина закончили, 5 мешков осталось", ALIASES) == []


def test_text_without_an_address_matches_nothing():
    assert match_objects("привезут завтра после обеда", ALIASES) == []


def test_empty_text_matches_nothing():
    assert match_objects(None, ALIASES) == []
    assert match_objects("", ALIASES) == []


def test_two_addresses_in_one_message_are_both_returned():
    """Решать, что с этим делать, — не дело разбора текста."""
    matches = match_objects("с Ленина 5 перекинуть на Мира 12", ALIASES)

    assert {m.object_id for m in matches} == {1, 2}


def test_unknown_address_is_not_guessed():
    """Незаведённый объект остаётся неузнанным, а не ближайшим похожим."""
    assert match_objects("на Гагарина 7 нужен плиточник", ALIASES) == []


# --- точность указания ---


def test_the_more_precise_alias_wins():
    aliases = [("Ленина 5", 1), ("Ленина 5 корпус 2", 2)]

    matches = match_objects("завезли на Ленина 5к2", aliases)

    assert [m.object_id for m in matches] == [2]


def test_the_shorter_alias_still_works_on_its_own():
    aliases = [("Ленина 5", 1), ("Ленина 5 корпус 2", 2)]

    matches = match_objects("завезли на Ленина 5", aliases)

    assert [m.object_id for m in matches] == [1]


def test_one_object_is_reported_once_even_with_several_aliases():
    aliases = [("Ленина 5", 1), ("ул. Ленина, д. 5", 1)]

    matches = match_objects("на Ленина 5 закончили", aliases)

    assert len(matches) == 1
