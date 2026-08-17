"""Узнавание объекта из текста: названия объектов и происхождение привязки

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "object_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "object_id",
            sa.Integer(),
            sa.ForeignKey("objects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.Text(), nullable=False),
        # Приведённый вид уникален по всей таблице: одна и та же запись
        # не может указывать на два объекта, иначе узнавание адреса
        # превратилось бы в гадание.
        sa.Column("normalized", sa.Text(), nullable=False),
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
        sa.UniqueConstraint("normalized", name="uq_object_alias_normalized"),
    )
    op.create_index("ix_object_aliases_object_id", "object_aliases", ["object_id"])

    op.add_column("messages", sa.Column("object_source", sa.String(16)))


def downgrade() -> None:
    op.drop_column("messages", "object_source")
    op.drop_index("ix_object_aliases_object_id", table_name="object_aliases")
    op.drop_table("object_aliases")
