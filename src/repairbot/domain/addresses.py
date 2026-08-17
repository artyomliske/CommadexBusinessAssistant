"""Узнавание адреса объекта в тексте сообщения.

Чаты у заказчика функциональные — «Заказ материала», «Тех.группа», —
а не по объектам: в одном чате идёт работа сразу по всем адресам.
Привязка «чат = объект» там не работает, и объект приходится узнавать
из самого сообщения.

Произвольный адрес мы не разбираем: сопоставляем текст с адресами,
которые уже заведены. Такой выбор ошибается только в одну сторону —
незнакомый адрес останется неузнанным и попадёт человеку, а не
припишется чужому объекту. Обратная ошибка хуже: она тихая.

Сравнение идёт по словам, а не по строкам. «ул. Ленина, д. 5»,
«Ленина 5» и «на ленина-5 завезли» — одно и то же место, написанное
тремя способами, и различаться они не должны.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Слова, которые в адресе ничего не различают: «ул.», «д.», «корп.».
#: Выбрасываются с обеих сторон сравнения, поэтому лишнее слово в списке
#: безвредно — оно исчезает и из образца, и из текста.
STOP_WORDS = frozenset(
    {
        "г",
        "город",
        "гор",
        "ул",
        "улица",
        "д",
        "дом",
        "кв",
        "квартира",
        "к",
        "корп",
        "корпус",
        "с",
        "стр",
        "строение",
        "пр",
        "просп",
        "проспект",
        "пер",
        "переулок",
        "б",
        "бул",
        "бульвар",
        "ш",
        "шоссе",
        "наб",
        "набережная",
        "пл",
        "площадь",
        "мкр",
        "микрорайон",
        "лит",
        "литера",
        "вл",
        "владение",
        "оф",
        "офис",
        "под",
        "подъезд",
    }
)

#: Буквенные и цифровые куски разделяются: «5к2» — это дом 5 корпус 2,
#: а не одно слово. Без разделения такая запись не совпала бы с «5 корп. 2».
_CHUNK = re.compile(r"[a-zа-я]+|\d+")


def normalize(value: str) -> tuple[str, ...]:
    """Адрес или текст → сравнимая последовательность слов."""
    lowered = value.lower().replace("ё", "е")
    return tuple(w for w in _CHUNK.findall(lowered) if w not in STOP_WORDS)


def is_usable_alias(alias: str) -> bool:
    """Годится ли запись как образец для поиска.

    Из одних цифр образец делать нельзя: «5» встретится в любом
    сообщении про пять мешков и припишет их случайному объекту.
    """
    words = normalize(alias)
    return bool(words) and any(not w.isdigit() for w in words)


@dataclass(frozen=True, slots=True)
class Match:
    object_id: int
    alias: str
    words: int
    """Длина совпадения. Чем длиннее, тем точнее указан объект."""


def match_objects(text: str | None, aliases: list[tuple[str, int]]) -> list[Match]:
    """Какие объекты упомянуты в тексте.

    Совпадением считается непрерывная цепочка слов: «Ленина 5» есть в
    «на Ленина 5 закончили», но не в «Ленина закончили, 5 мешков».

    Возвращаются только самые длинные совпадения. Если у объекта есть
    и «Ленина 5», и «Ленина 5 корпус 2», то в тексте со вторым выиграет
    второй — более точное указание вытесняет менее точное.
    """
    if not text:
        return []
    words = normalize(text)
    if not words:
        return []

    found: list[Match] = []
    for alias, object_id in aliases:
        pattern = normalize(alias)
        if not pattern or len(pattern) > len(words):
            continue
        if _contains(words, pattern):
            found.append(Match(object_id=object_id, alias=alias, words=len(pattern)))

    if not found:
        return []

    longest = max(m.words for m in found)
    best: dict[int, Match] = {}
    for match in found:
        if match.words == longest:
            best.setdefault(match.object_id, match)
    return list(best.values())


def _contains(words: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    span = len(pattern)
    return any(words[i : i + span] == pattern for i in range(len(words) - span + 1))
