"""Защита публичного эндпоинта вебхука.

Секрет проверяется до обращения к БД, поэтому эти тесты не требуют
запущенных Postgres и Redis.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from repairbot.app import create_app
from repairbot.config import Settings
from tests.fixtures import max_updates as fx

SECRET = "test-secret"


def _client() -> TestClient:
    settings = Settings(
        env="dev",
        max_webhook_secret=SECRET,  # type: ignore[arg-type]
        max_access_token="test-token",  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings))


def test_wrong_secret_returns_404_and_does_not_touch_db():
    with _client() as client:
        response = client.post("/webhooks/max/nope", json=fx.message_created())

    # 404, а не 403: наличие эндпоинта не подтверждаем.
    assert response.status_code == 404


def test_empty_secret_path_is_not_routed():
    with _client() as client:
        response = client.post("/webhooks/max/", json=fx.message_created())

    assert response.status_code in (404, 405, 307)


def test_healthz_available_without_dependencies():
    with _client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_webhook_path_contains_secret():
    settings = Settings(
        max_webhook_secret=SECRET,  # type: ignore[arg-type]
        public_base_url="https://bot.example.ru",
    )

    assert settings.max_webhook_path == f"/webhooks/max/{SECRET}"
    assert settings.public_base_url + settings.max_webhook_path == (
        f"https://bot.example.ru/webhooks/max/{SECRET}"
    )


# --- Telegram: секрет приходит заголовком, а не в пути ---

TELEGRAM_SECRET = "tg-secret"


def _telegram_client(*, token: str = "tg-token") -> TestClient:
    from tests.fixtures import telegram_updates as tg  # noqa: F401

    settings = Settings(
        env="dev",
        max_webhook_secret=SECRET,  # type: ignore[arg-type]
        max_access_token="test-token",  # type: ignore[arg-type]
        telegram_bot_token=token,  # type: ignore[arg-type]
        telegram_webhook_secret=TELEGRAM_SECRET,  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings))


def test_telegram_without_the_header_returns_404():
    from tests.fixtures import telegram_updates as tg

    with _telegram_client() as client:
        response = client.post("/webhooks/telegram", json=tg.message())

    assert response.status_code == 404


def test_telegram_with_a_wrong_header_returns_404():
    from tests.fixtures import telegram_updates as tg

    with _telegram_client() as client:
        response = client.post(
            "/webhooks/telegram",
            json=tg.message(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )

    assert response.status_code == 404


def test_telegram_path_carries_no_secret():
    """Адрес попадает в журналы обратного прокси, заголовок — нет.

    Поэтому у Telegram путь постоянный, в отличие от MAX, где секрет
    приходится класть в URL: заголовка там платформа не присылает.
    """
    settings = Settings(
        telegram_webhook_secret=TELEGRAM_SECRET,  # type: ignore[arg-type]
        max_webhook_secret=SECRET,  # type: ignore[arg-type]
    )

    assert settings.telegram_webhook_path == "/webhooks/telegram"
    assert TELEGRAM_SECRET not in settings.telegram_webhook_path
    # У MAX иначе — и это видно рядом.
    assert SECRET in settings.max_webhook_path


def test_telegram_without_a_token_answers_200_and_does_nothing():
    """Канал не подключён.

    Отвечаем 200, иначе платформа копила бы повторы месяцами.
    """
    from tests.fixtures import telegram_updates as tg

    with _telegram_client(token="") as client:
        response = client.post(
            "/webhooks/telegram",
            json=tg.message(),
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
        )

    assert response.status_code == 200
    assert response.json()["error"] == "channel_disabled"


def test_non_ascii_secret_in_the_path_is_refused_not_crashed():
    """Секрет из пути URL может содержать что угодно.

    `secrets.compare_digest` со строками требует только ASCII и бросает
    UnicodeEncodeError на всём остальном — неаутентифицированный клиент
    получал бы 500 вместо 404 и заодно узнавал, что эндпоинт существует.
    """
    with _client() as client:
        response = client.post("/webhooks/max/секрет", json=fx.message_created())

    assert response.status_code == 404
