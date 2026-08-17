"""Проверки живости и готовности."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.session import db_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    request: Request,
    session: Annotated[AsyncSession, Depends(db_session)],
) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"fail: {exc.__class__.__name__}"

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"fail: {exc.__class__.__name__}"

    healthy = all(value == "ok" for value in checks.values())
    checks["status"] = "ok" if healthy else "degraded"
    return checks
