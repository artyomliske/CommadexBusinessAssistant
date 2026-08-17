"""Вход в веб-интерфейс.

Интерфейс показывает рабочую переписку и персональные данные, поэтому
доступ без входа не предусмотрен ни на одной странице, кроме самой формы
входа и проверок живости.

Пароли хранятся хешами argon2id. Сессия — подписанная cookie: своего
хранилища сессий для пятнадцати человек не нужно, а подпись не даёт
подделать идентификатор пользователя на клиенте.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import User
from repairbot.db.session import db_session
from repairbot.observability import get_logger

log = get_logger(__name__)

_hasher = PasswordHasher()

SESSION_USER_KEY = "user_id"
MIN_PASSWORD_LENGTH = 10


class LoginRequired(Exception):
    """Не вошёл. Обрабатывается перенаправлением на форму входа."""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль короче {MIN_PASSWORD_LENGTH} символов")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Проверить пароль. На любой негодный хеш возвращает False, не бросая.

    Функция обязана быть тотальной: она стоит на пути входа, и исключение
    здесь — это 500 вместо «неверный пароль».

    Неверный пароль и испорченный хеш различаются намеренно. Первое —
    обычное дело, второе означает повреждение данных в `users`, и об этом
    должно быть видно в логах.
    """
    try:
        _hasher.verify(password_hash, password)
        return True
    except VerificationError:
        # Сюда же VerifyMismatchError — обычный неверный пароль.
        return False
    except (InvalidHashError, UnicodeEncodeError, TypeError, AttributeError):
        log.error("web.corrupt_password_hash", hint="запись в users повреждена")
        return False


async def authenticate(session: AsyncSession, login: str, password: str) -> User | None:
    """Проверить логин и пароль.

    При отсутствии пользователя всё равно считаем хеш: иначе время ответа
    выдавало бы, существует ли такой логин.
    """
    user = (
        await session.execute(select(User).where(User.login == login.strip().casefold()))
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        _hasher.hash(secrets.token_urlsafe(16))
        log.warning("web.login_failed", login=login[:32], reason="нет пользователя")
        return None

    if not verify_password(user.password_hash, password):
        log.warning("web.login_failed", login=user.login, reason="неверный пароль")
        return None

    await session.execute(
        sql_update(User).where(User.id == user.id).values(last_login_at=datetime.now(tz=UTC))
    )
    log.info("web.login_ok", login=user.login, role=user.role)
    return user


def change_password(user: User, current: str, new: str, repeat: str) -> str | None:
    """Сменить пароль. Возвращает причину отказа или None при успехе.

    Логика отдельно от обработчика запроса: здесь решается, менять или
    нет, и это единственное место, которое стоит проверять тестами.

    Текущий пароль спрашивается не для проформы. Без него любой, кто
    подошёл к незакрытой панели, сменил бы пароль владельцу и запер бы
    его снаружи.
    """
    if not verify_password(user.password_hash, current):
        return "Текущий пароль неверен"
    if new != repeat:
        return "Новые пароли не совпадают"
    if new == current:
        return "Новый пароль совпадает с текущим"
    try:
        user.password_hash = hash_password(new)
    except ValueError as exc:
        return str(exc)
    return None


async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(db_session)],
) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise LoginRequired(_next_url(request))

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        # Пользователя отключили, пока сессия была жива.
        request.session.clear()
        raise LoginRequired(_next_url(request))
    return user


async def reviewer(user: Annotated[User, Depends(current_user)]) -> User:
    """Право подтверждать факты — у admin и manager."""
    if not user.can_review:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав: нужна роль manager или admin",
        )
    return user


def login_redirect(next_url: str) -> RedirectResponse:
    target = "/login"
    if next_url and next_url != "/":
        target = f"/login?next={next_url}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _next_url(request: Request) -> str:
    path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


def safe_next(candidate: str | None) -> str:
    """Отфильтровать открытое перенаправление.

    Принимаем только относительные пути внутри интерфейса: иначе ссылка
    вида `/login?next=https://...` увела бы вошедшего на чужой сайт.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate
