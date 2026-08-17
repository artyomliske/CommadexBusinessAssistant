"""Псевонимизация персональных данных перед отправкой в модель."""

from __future__ import annotations

from repairbot.privacy import Pseudonymizer


def test_phone_masked_and_restored():
    p = Pseudonymizer()

    masked = p.mask("Позвони заказчику +7 999 000-11-22, он ждёт")

    assert "+7 999 000-11-22" not in masked
    assert "[PHONE_1]" in masked
    assert p.unmask(masked) == "Позвони заказчику +7 999 000-11-22, он ждёт"


def test_same_phone_different_spelling_gets_one_alias():
    """Модель должна видеть, что это один и тот же человек."""
    p = Pseudonymizer()

    masked = p.mask("звонил 8(999)000-11-22 и потом +7 999 000 11 22")

    assert masked.count("[PHONE_1]") == 2
    assert "[PHONE_2]" not in masked


def test_different_phones_get_different_aliases():
    p = Pseudonymizer()

    masked = p.mask("+7 999 000-11-22 и +7 916 555-33-44")

    assert "[PHONE_1]" in masked
    assert "[PHONE_2]" in masked


def test_email_masked():
    p = Pseudonymizer()

    masked = p.mask("счёт отправил на ivan.petrov@example.ru")

    assert "ivan.petrov@example.ru" not in masked
    assert "[EMAIL_1]" in masked
    assert "ivan.petrov@example.ru" in p.unmask(masked)


def test_address_masked():
    p = Pseudonymizer()

    masked = p.mask("выезд на ул. Ленина, д. 5, кв. 12 в девять утра")

    assert "Ленина" not in masked
    assert "[ADDR_1]" in masked
    assert p.unmask(masked) == "выезд на ул. Ленина, д. 5, кв. 12 в девять утра"


def test_card_number_masked():
    p = Pseudonymizer()

    masked = p.mask("перевёл на 4276 3800 1234 5678")

    assert "4276" not in masked
    assert "[CARD_1]" in masked


def test_known_names_replaced_case_insensitively():
    p = Pseudonymizer(known_names={"Иван Петров": "[NAME_7]"})

    masked = p.mask("иван петров закончил штукатурку")

    assert "[NAME_7]" in masked
    assert "петров" not in masked.casefold()


def test_unknown_capitalized_words_are_not_touched():
    """Марки материалов и названия помещений не персональные данные.

    Свободный поиск имён по заглавной букве испортил бы смысл сообщения,
    поэтому заменяются только имена из справочника.
    """
    p = Pseudonymizer()

    masked = p.mask("Купил грунтовку Кнауф на Кухню")

    assert masked == "Купил грунтовку Кнауф на Кухню"
    assert len(p) == 0


def test_technical_numbers_survive_masking():
    """Количества и суммы модели нужны — их маскировать нельзя."""
    p = Pseudonymizer()

    masked = p.mask("купил 2 мешка грунтовки за 3400 руб")

    assert masked == "купил 2 мешка грунтовки за 3400 руб"


def test_double_digit_alias_restored_correctly():
    """[ADDR_10] не должен пострадать при подстановке [ADDR_1]."""
    p = Pseudonymizer()
    text = " ; ".join(f"ул. Тестовая{i} д. {i}" for i in range(1, 12))

    masked = p.mask(text)

    assert "[ADDR_11]" in masked
    assert p.unmask(masked) == text


def test_replacements_exposed_for_audit():
    p = Pseudonymizer()
    p.mask("телефон +7 999 000-11-22")

    assert p.replacements["[PHONE_1]"] == "+7 999 000-11-22"


def test_empty_text_is_noop():
    p = Pseudonymizer()

    assert p.mask("") == ""
    assert p.unmask("") == ""
