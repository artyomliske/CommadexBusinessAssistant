"""Доступ к веб-интерфейсу и безопасность входа.

Интерфейс показывает переписку и персональные данные, поэтому «страница
открывается без входа» — это дефект, а не удобство.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from repairbot.app import create_app
from repairbot.config import Settings
from repairbot.web.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    safe_next,
    verify_password,
)

PROTECTED_PAGES = [
    "/", "/pending", "/feed", "/chats", "/health", "/objects/1", "/password",
    "/objects", "/unassigned", "/files", "/knowledge", "/cash",
]


def _client(env: str = "dev") -> TestClient:
    settings = Settings(
        env=env,  # type: ignore[arg-type]
        max_webhook_secret="test-secret",  # type: ignore[arg-type]
        web_session_secret="unit-test-session-secret",  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings), follow_redirects=False)


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_pages_require_login(path):
    with _client() as client:
        response = client.get(path)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_page_is_open():
    with _client() as client:
        response = client.get("/login")

    assert response.status_code == 200
    assert "Вход" in response.text or "Войти" in response.text


def test_healthz_stays_open():
    """Проверка живости не должна требовать входа."""
    with _client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200


def test_static_is_served():
    with _client() as client:
        response = client.get("/static/app.css")

    assert response.status_code == 200
    assert "--accent" in response.text


def test_next_url_preserved_on_redirect():
    with _client() as client:
        response = client.get("/pending")

    assert "next=/pending" in response.headers["location"]


def test_webhook_still_works_alongside_web():
    """Веб-интерфейс не должен ломать приём событий."""
    with _client() as client:
        response = client.post("/webhooks/max/nope", json={})

    assert response.status_code == 404


# --- пароли ---


def test_password_roundtrip():
    hashed = hash_password("правильный-пароль-1")

    assert verify_password(hashed, "правильный-пароль-1")
    assert not verify_password(hashed, "неправильный-пароль")


def test_hash_is_salted():
    """Два одинаковых пароля не должны давать одинаковый хеш."""
    assert hash_password("одинаковый-пароль") != hash_password("одинаковый-пароль")


def test_short_password_rejected():
    with pytest.raises(ValueError):
        hash_password("a" * (MIN_PASSWORD_LENGTH - 1))


@pytest.mark.parametrize(
    "corrupt", ["не-хеш-вообще", "garbage", "", "$argon2id$обрезанный", None]
)
def test_corrupt_hash_does_not_raise(corrupt):
    """Функция на пути входа обязана быть тотальной: исключение здесь —
    это 500 вместо «неверный пароль»."""
    assert verify_password(corrupt, "любой-пароль") is False


# --- открытое перенаправление ---


@pytest.mark.parametrize(
    "candidate",
    ["https://evil.example", "//evil.example", "http://evil.example/x", None, "", "javascript:x"],
)
def test_external_next_is_rejected(candidate):
    assert safe_next(candidate) == "/"


@pytest.mark.parametrize("candidate", ["/pending", "/feed?object_id=3", "/objects/7"])
def test_internal_next_is_kept(candidate):
    assert safe_next(candidate) == candidate


# --- сессии ---


def test_prod_requires_session_secret():
    """Со случайным ключом сессии не выживали бы перезапуск и не совпадали
    бы между воркерами — в prod это отказ при старте, а не предупреждение.

    Ключ задаётся пустым явно: иначе тест зависел бы от того, лежит ли
    рядом .env разработчика, и молча перестал бы проверять то, ради чего
    написан.
    """
    settings = Settings(env="prod", web_session_secret="")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="WEB_SESSION_SECRET"):
        create_app(settings)


def test_dev_generates_session_secret():
    """В dev пустой ключ допустим — генерируется случайный."""
    settings = Settings(env="dev", web_session_secret="")  # type: ignore[arg-type]

    assert create_app(settings) is not None
