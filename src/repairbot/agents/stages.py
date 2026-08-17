"""Справочник этапов работ с нормативными сроками (раздел 4 ТЗ).

Нужен для двух вещей: понимать, какой этап идёт за каким, и замечать
отклонения от плана — этап, который тянется дольше норматива.

⚠️ Сроки здесь **ориентировочные**, взяты из типовой практики отделки
однокомнатной квартиры. Пункт 11 ТЗ требует от заказчика перечень
объектов и форм учёта; когда он поступит, нормативы нужно заменить на
реальные, иначе отклонения будут срабатывать не там, где надо.

Обращение к модели для сопоставления названий сознательно не делается:
названия этапов пишут по-разному («штукатурка», «штукатурим», «штукатурка
стен»), но словарь синонимов дешевле, предсказуемее и не стоит токенов.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Stage:
    key: str
    title: str
    order: int
    normative_days: int
    """Ориентировочная длительность. Требует уточнения у заказчика."""


STAGES: tuple[Stage, ...] = (
    Stage("demolition", "демонтаж", 10, 5),
    Stage("rough_plumbing", "разводка сантехники", 20, 4),
    Stage("electrical", "электрика", 30, 7),
    Stage("plastering", "штукатурка", 40, 10),
    Stage("screed", "стяжка", 50, 7),
    Stage("tiling", "плитка", 60, 10),
    Stage("puttying", "шпаклёвка", 70, 7),
    Stage("painting", "покраска", 80, 5),
    Stage("flooring", "напольные покрытия", 90, 4),
    Stage("doors", "двери", 100, 3),
    Stage("finishing_electrical", "чистовая электрика", 110, 3),
    Stage("finishing_plumbing", "чистовая сантехника", 120, 3),
    Stage("cleanup", "уборка", 130, 2),
)

BY_KEY: dict[str, Stage] = {s.key: s for s in STAGES}

# Как этап называют в переписке → ключ справочника. Список пополняется
# по мере разбора реальной переписки заказчика.
_SYNONYMS: dict[str, str] = {
    "демонтаж": "demolition",
    "снос": "demolition",
    "разводка сантехники": "rough_plumbing",
    "сантехника черновая": "rough_plumbing",
    "черновая сантехника": "rough_plumbing",
    "электрика": "electrical",
    "электрик": "electrical",
    "проводка": "electrical",
    "штукатурка": "plastering",
    "штукатурим": "plastering",
    "штукатурка стен": "plastering",
    "стяжка": "screed",
    "стяжка пола": "screed",
    "плитка": "tiling",
    "укладка плитки": "tiling",
    "кафель": "tiling",
    "шпаклёвка": "puttying",
    "шпаклевка": "puttying",
    "шпатлёвка": "puttying",
    "покраска": "painting",
    "окраска": "painting",
    "обои": "painting",
    "напольные покрытия": "flooring",
    "ламинат": "flooring",
    "паркет": "flooring",
    "линолеум": "flooring",
    "двери": "doors",
    "установка дверей": "doors",
    "чистовая электрика": "finishing_electrical",
    "розетки": "finishing_electrical",
    "чистовая сантехника": "finishing_plumbing",
    "уборка": "cleanup",
    "клининг": "cleanup",
}


def resolve(raw: str | None) -> Stage | None:
    """Сопоставить название этапа из переписки со справочником.

    Возвращает None, если этап незнаком: это не ошибка. Незнакомый этап
    попадёт в состояние как есть, но не будет участвовать в расчёте
    порядка и отклонений — и это повод пополнить словарь.
    """
    if not raw:
        return None

    normalized = " ".join(raw.split()).casefold().replace("ё", "е")
    for name, key in _SYNONYMS.items():
        if name.replace("ё", "е") == normalized:
            return BY_KEY[key]

    # Вхождение как запасной вариант: «штукатурка на кухне» → штукатурка.
    for name, key in _SYNONYMS.items():
        if name.replace("ё", "е") in normalized:
            return BY_KEY[key]
    return None


def normative_days(raw: str | None) -> int | None:
    stage = resolve(raw)
    return stage.normative_days if stage else None


def order_of(raw: str | None) -> int | None:
    stage = resolve(raw)
    return stage.order if stage else None
