"""Платёжный календарь: регулярные платежи компании

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recurring_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(24), nullable=False, server_default="other"),
        sa.Column("amount", sa.Numeric(12, 2)),
        sa.Column("currency", sa.String(3), nullable=False, server_default="RUB"),
        sa.Column("period", sa.String(16), nullable=False, server_default="monthly"),
        # Якорный день хранится отдельно от даты: платёж 31-го числа в
        # феврале приходится на 28-е, и без якоря он остался бы 28-м навсегда.
        sa.Column("day_of_month", sa.Integer()),
        sa.Column("next_due_on", sa.Date(), nullable=False),
        sa.Column("notify_days_before", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text()),
        sa.Column("last_paid_on", sa.Date()),
        # Ключи идемпотентности напоминаний: без них ежедневная задача
        # присылала бы одно и то же каждый день до срока.
        sa.Column("notified_for", sa.Date()),
        sa.Column("overdue_notified_for", sa.Date()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_payments_due", "recurring_payments", ["active", "next_due_on"])

    op.create_check_constraint(
        "ck_payments_day_of_month",
        "recurring_payments",
        "day_of_month IS NULL OR (day_of_month BETWEEN 1 AND 31)",
    )
    op.create_check_constraint(
        "ck_payments_notify_days",
        "recurring_payments",
        "notify_days_before BETWEEN 0 AND 60",
    )


def downgrade() -> None:
    op.drop_table("recurring_payments")
