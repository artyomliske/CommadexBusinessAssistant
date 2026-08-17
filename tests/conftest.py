from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from repairbot.config import Settings
from repairbot.db.models import Base

os.environ.setdefault("MAX_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("MAX_ACCESS_TOKEN", "test-token")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="dev",
        max_webhook_secret="test-secret",  # type: ignore[arg-type]
        max_access_token="test-token",  # type: ignore[arg-type]
        postgres_db=os.getenv("TEST_POSTGRES_DB", "repairbot_test"),
    )


@pytest.fixture
async def db_engine(settings: Settings):
    """Движок тестовой БД.

    Тесты, требующие Postgres, пропускаются, если база недоступна:
    локально её может не быть, в docker compose она есть.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Postgres недоступен: {exc.__class__.__name__}")

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()
