"""Смена своего пароля.

Учётку заводит администратор командой, и пароль при этом знает он.
Пока сменить его нельзя, второй человек в системе работает под паролем,
известным ещё кому-то, — поэтому смена и появилась.
"""

from __future__ import annotations

from repairbot.db.models import User
from repairbot.web.security import (
    MIN_PASSWORD_LENGTH,
    change_password,
    hash_password,
    verify_password,
)

OLD = "старый-пароль-длинный"
NEW = "новый-пароль-длинный"


def _user() -> User:
    return User(
        login="owner",
        display_name="Владелец",
        role="admin",
        password_hash=hash_password(OLD),
    )


def test_password_is_replaced():
    user = _user()

    assert change_password(user, OLD, NEW, NEW) is None
    assert verify_password(user.password_hash, NEW)


def test_old_password_stops_working():
    user = _user()

    change_password(user, OLD, NEW, NEW)

    assert not verify_password(user.password_hash, OLD)


def test_current_password_is_required():
    """Иначе подошедший к незакрытой панели запер бы владельца снаружи."""
    user = _user()

    error = change_password(user, "не тот пароль", NEW, NEW)

    assert error == "Текущий пароль неверен"
    assert verify_password(user.password_hash, OLD)


def test_typo_in_the_repeat_is_refused():
    """Опечатка иначе оставила бы человека с паролем, которого он не знает."""
    user = _user()

    error = change_password(user, OLD, NEW, NEW + "1")

    assert error == "Новые пароли не совпадают"
    assert verify_password(user.password_hash, OLD)


def test_short_password_is_refused():
    user = _user()
    short = "a" * (MIN_PASSWORD_LENGTH - 1)

    error = change_password(user, OLD, short, short)

    assert str(MIN_PASSWORD_LENGTH) in error
    assert verify_password(user.password_hash, OLD)


def test_the_same_password_is_refused():
    """Смена, ничего не меняющая, выглядела бы выполненной."""
    user = _user()

    assert change_password(user, OLD, OLD, OLD) == "Новый пароль совпадает с текущим"


def test_refusal_leaves_the_hash_untouched():
    """Отказ не должен оставлять пользователя без рабочего пароля."""
    user = _user()
    before = user.password_hash

    change_password(user, OLD, "коротко", "коротко")

    assert user.password_hash == before


def test_page_renders():
    """Ошибку в шаблоне иначе видит первым тот, кто пришёл менять пароль."""
    from types import SimpleNamespace

    from repairbot.web.routes import templates

    html = templates.env.get_template("password.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/password")),
        user=_user(),
        error=None,
        done=False,
        min_length=MIN_PASSWORD_LENGTH,
    )

    assert 'action="/password"' in html
    assert 'name="current"' in html and 'name="new"' in html and 'name="repeat"' in html
    assert str(MIN_PASSWORD_LENGTH) in html
