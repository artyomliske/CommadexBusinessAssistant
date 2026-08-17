"""Сборка приложения FastAPI: приём вебхуков и веб-интерфейс наблюдения."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from starlette.middleware.sessions import SessionMiddleware

from repairbot.api import health, webhooks
from repairbot.channels import registry
from repairbot.channels.max.adapter import MaxAdapter
from repairbot.channels.telegram import TelegramAdapter
from repairbot.config import Settings, get_settings
from repairbot.db.session import dispose_engine, init_engine
from repairbot.memory.working import WorkingMemory
from repairbot.observability import get_logger, setup_observability
from repairbot.outbound.controller import KillSwitch
from repairbot.web import routes as web_routes
from repairbot.web.security import LoginRequired, login_redirect

log = get_logger(__name__)

STATIC_DIR = Path(web_routes.__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    init_engine(settings)
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.working_memory = WorkingMemory(app.state.redis, settings)
    # Аварийная остановка живёт в Redis: она должна срабатывать сразу на
    # всех процессах, а не только в том, где нажали кнопку.
    app.state.kill_switch = KillSwitch(app.state.redis)

    adapter = MaxAdapter(settings)
    registry.register(adapter)
    app.state.max_adapter = adapter

    # Telegram подключается тем же шлюзом: выше него разницы между
    # каналами нет (этап 6 ТЗ). Без токена канал просто не регистрируется.
    telegram = None
    if settings.telegram_enabled:
        telegram = TelegramAdapter(settings)
        registry.register(telegram)
    app.state.telegram_adapter = telegram

    try:
        app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception as exc:
        log.error("arq.pool_unavailable", error=str(exc))
        app.state.arq = None

    # Подписку на вебхук регистрируем только при явном разрешении: в dev
    # публичного HTTPS-адреса нет, а лишний вызов API платформы не нужен.
    register_webhook = os.getenv("REGISTER_WEBHOOK_ON_START") == "1"
    if register_webhook and settings.max_access_token.get_secret_value():
        try:
            await adapter.ensure_subscription()
        except Exception as exc:
            log.error("max.subscription_failed", error=str(exc))
    if register_webhook and telegram is not None:
        try:
            await telegram.ensure_subscription()
        except Exception as exc:
            log.error("telegram.subscription_failed", error=str(exc))

    log.info("app.started", env=settings.env, channels=[c.value for c in registry.registered()])
    try:
        yield
    finally:
        await adapter.aclose()
        if telegram is not None:
            await telegram.aclose()
        if app.state.arq is not None:
            await app.state.arq.aclose()
        await app.state.redis.aclose()
        await dispose_engine()
        registry.clear()
        log.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_observability(settings)

    app = FastAPI(
        title="Мультиагентная система: ремонт квартир",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        openapi_url="/openapi.json" if settings.env != "prod" else None,
    )
    app.state.settings = settings
    app.state.arq = None

    # Обработчики берут настройки через Depends(get_settings), а тот читает
    # закэшированный глобальный экземпляр. Без подмены переданные сюда
    # настройки действовали бы только на запуск, но не на проверку секрета
    # вебхука — и тест с «правильным» секретом молча проверял бы не то.
    app.dependency_overrides[get_settings] = lambda: settings

    _install_sessions(app, settings)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(web_routes.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        """Не вошёл — ведём на форму входа, а не отдаём 401 без объяснений."""
        return login_redirect(exc.next_url)

    return app


def _install_sessions(app: FastAPI, settings: Settings) -> None:
    """Подписанная cookie сессии.

    В prod ключ обязателен: со случайным ключом каждый перезапуск разлогинивал
    бы всех, а хуже — на нескольких воркерах сессии не совпадали бы вовсе.
    В dev допускаем случайный, чтобы не заводить .env ради одной страницы.
    """
    secret = settings.web_session_secret.get_secret_value()
    if not secret:
        if settings.env == "prod":
            raise RuntimeError(
                "WEB_SESSION_SECRET обязателен в prod: без него сессии не выживают "
                "перезапуск и не совпадают между воркерами"
            )
        secret = secrets.token_urlsafe(32)
        log.warning("web.session_secret_generated", hint="задайте WEB_SESSION_SECRET")

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="repairbot_session",
        max_age=settings.web_session_max_age_seconds,
        same_site="lax",
        # По http cookie с флагом Secure не доедет — в dev выключаем.
        https_only=settings.web_session_https_only and settings.env != "dev",
    )


app = create_app()
