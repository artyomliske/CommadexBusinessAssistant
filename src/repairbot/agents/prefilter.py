"""Предварительный классификатор.

Раздел 9 ТЗ: «Основная часть сообщений отсеивается предварительным
классификатором и не передаётся в ресурсоёмкую модель». Это единственный
механизм, который держит расход на модель в заявленных пределах при
1500 сообщениях в сутки, поэтому он детерминированный: никаких обращений
к модели, чтобы решить, обращаться ли к модели.

Правило разрешения сомнений — в пользу разбора. Пропущенный факт дороже
лишнего вызова модели: факт придётся вводить руками, а лишний вызов стоит
копейки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from repairbot.domain.events import AttachmentKind, InboundEvent


class Verdict(StrEnum):
    EXTRACT = "extract"
    """Передать экстрактору."""
    SKIP = "skip"
    """Записать в журнал, но модели не показывать."""
    DOCUMENT_ONLY = "document_only"
    """Текста нет, но есть вложение: работа для агента документов."""


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str

    @property
    def needs_model(self) -> bool:
        return self.verdict is Verdict.EXTRACT


MIN_MEANINGFUL_LENGTH = 12
"""Короче этого сообщение обычно не несёт извлекаемого факта — при условии,
что в нём нет цифр, дат и ключевых слов (проверяется отдельно)."""

_ACKNOWLEDGEMENTS = frozenset(
    {
        "ок", "ок.", "окей", "хорошо", "хорошо.", "принято", "принял", "приняла",
        "понял", "поняла", "понятно", "ясно", "да", "да.", "нет", "нет.", "ага",
        "угу", "спасибо", "спасибо!", "спс", "благодарю", "пожалуйста", "не за что",
        "привет", "привет!", "здравствуйте", "здравствуйте!", "добрый день",
        "доброе утро", "добрый вечер", "до связи", "пока", "всё", "все", "готово",
        "+", "++", "-", "ok", "okay", "thanks", "?", "??", "!",
    }
)
"""Подтверждения и приветствия. «Готово» без указания этапа тоже сюда:
без предмета факт всё равно не собрать."""

_SIGNIFICANT_KEYWORDS = (
    # деньги
    "руб", "₽", "тысяч", "оплат", "плат", "счёт", "счет", "чек", "смет", "стоим",
    "цен", "доплат", "скидк", "аванс", "предоплат", "долг",
    # сроки
    "срок", "дедлайн", "успе", "задерж", "перенес", "перенос", "график",
    "завтра", "сегодня", "послезавтра", "неделя", "недел", "числа",
    # ход работ
    "закончил", "законч", "сделал", "сделан", "начал", "начат", "заверш",
    "приступ", "остал", "этап", "работ",
    # материалы
    "куп", "привез", "привёз", "доставк", "заказал", "закуп", "материал",
    "не хватает", "нужно", "надо", "закончилась", "остаток",
    # проблемы
    "проблем", "не работает", "сломал", "брак", "дефект", "трещин", "протеч",
    "передел", "жалоб", "претенз", "ошибк",
    # люди и организация
    "бригад", "мастер", "прораб", "выход", "приед", "приех",
)

_DIGIT_RE = re.compile(r"\d")
_DATE_RE = re.compile(
    r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"
    r"|\b\d{1,2}\s+(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)",
    re.IGNORECASE,
)

_MEDIA_ONLY_KINDS = frozenset(
    {
        AttachmentKind.IMAGE,
        AttachmentKind.VIDEO,
        AttachmentKind.FILE,
        AttachmentKind.AUDIO,
    }
)


def classify(event: InboundEvent) -> Decision:
    """Решить, нужен ли модели разбор этого сообщения."""
    if event.actor is not None and event.actor.is_bot:
        return Decision(Verdict.SKIP, "сообщение от бота")

    text = (event.text or "").strip()
    has_media = any(a.kind in _MEDIA_ONLY_KINDS for a in event.attachments)

    if not text:
        if has_media:
            return Decision(Verdict.DOCUMENT_ONLY, "вложение без текста")
        return Decision(Verdict.SKIP, "пустое сообщение без вложений")

    normalized = _normalize(text)

    if normalized in _ACKNOWLEDGEMENTS:
        return Decision(Verdict.SKIP, "подтверждение или приветствие")

    # Дальше — признаки значимости. Любого достаточно.
    if _DATE_RE.search(text):
        return Decision(Verdict.EXTRACT, "содержит дату")

    keyword = _find_keyword(normalized)
    if keyword is not None:
        return Decision(Verdict.EXTRACT, f"ключевое слово: {keyword}")

    if _DIGIT_RE.search(text):
        return Decision(Verdict.EXTRACT, "содержит числа")

    if has_media:
        return Decision(Verdict.EXTRACT, "текст с вложением")

    if len(text) < MIN_MEANINGFUL_LENGTH:
        return Decision(Verdict.SKIP, "слишком короткое без значимых признаков")

    if event.reply_to_message_id:
        # Ответ на чужое сообщение почти всегда продолжает содержательный
        # разговор, даже если сам по себе выглядит бессодержательно.
        return Decision(Verdict.EXTRACT, "ответ в переписке")

    # Развёрнутая фраза без явных признаков: сомнение — в пользу разбора.
    if len(text.split()) >= 5:
        return Decision(Verdict.EXTRACT, "развёрнутая фраза")

    return Decision(Verdict.SKIP, "нет признаков извлекаемого факта")


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _find_keyword(normalized: str) -> str | None:
    for keyword in _SIGNIFICANT_KEYWORDS:
        if keyword in normalized:
            return keyword
    return None
