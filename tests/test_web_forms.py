"""Каждая форма на странице должна вести на существующий маршрут.

Ошибка дешёвая в исправлении и дорогая в обнаружении: страница
открывается, кнопка нажимается, а в ответ 405 или 404. Тестами
отдельных обработчиков это не ловится — они-то есть, просто форма
отправляется не туда.

Проверка идёт по исходникам шаблонов, потому что именно там опечатка
и живёт.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from repairbot.web import routes as web_routes

TEMPLATES = Path("src/repairbot/web/templates")

#: Адреса в шаблонах содержат вставки Jinja: `/objects/{{ o.id }}/aliases`.
#: Приводим их к виду маршрута FastAPI, где на месте вставки — параметр.
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}")


def _routes(method: str) -> set[str]:
    """Пути, объявленные роутером веб-интерфейса.

    Берём у самого роутера, а не у приложения: FastAPI хранит
    подключённые роутеры завёрнутыми, и обойти их снаружи нельзя —
    попытка сделать это дала пустой список и «сломала» проверку, а не
    нашла ошибку.
    """
    found: set[str] = set()
    for route in web_routes.router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set()) or set()
        if path and method in methods:
            # Имя параметра значения не имеет — важна форма пути.
            found.add(re.sub(r"\{[^}]+\}", "{}", path))
    return found


def _form_actions() -> list[tuple[str, str]]:
    """Формы, отправляемые методом POST.

    Фильтры на странице событий отправляются методом GET и ведут на
    обычную страницу — им POST-маршрут не нужен и не должен требоваться.
    """
    actions = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<form\b[^>]*>", text):
            tag = match.group(0)
            method = re.search(r'method="([^"]+)"', tag)
            if not method or method.group(1).lower() != "post":
                continue
            action = re.search(r'action="([^"]+)"', tag)
            if action:
                actions.append((path.name, action.group(1)))
    return actions


def _normalize(action: str) -> str:
    return _PLACEHOLDER.sub("{}", action).strip()


def test_templates_contain_forms():
    """Если форм не осталось, проверка ниже стала бессмысленной."""
    assert _form_actions()


@pytest.mark.parametrize(("template", "action"), _form_actions())
def test_form_action_has_a_route(template, action):
    assert _normalize(action) in _routes("POST"), (
        f"{template}: форма отправляется на {action}, а такого маршрута нет"
    )
