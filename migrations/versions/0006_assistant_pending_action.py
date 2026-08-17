"""Внутренний помощник: действие, ждущее подтверждения

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # В базе, а не в памяти процесса: подтверждение приходит следующим
    # сообщением, а между ними воркер может перезапуститься.
    op.add_column("dialog_states", sa.Column("pending_action", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("dialog_states", "pending_action")
