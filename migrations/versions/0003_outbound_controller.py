"""Контролёр исходящих действий: журнал аудита и состояние диалогов

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id", ondelete="SET NULL")),
        sa.Column("source_event_id", sa.BigInteger()),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_chat_id", sa.String(64), nullable=False),
        sa.Column("reply_to_message_id", sa.String(128)),
        sa.Column("intent", sa.String(32), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("channel_message_id", sa.String(128)),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision", sa.String(16)),
        sa.Column("edited_text", sa.Text()),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_outbound_idempotency"),
    )
    op.create_index("ix_outbound_pending", "outbound_messages", ["verdict", "created_at"])
    op.create_index(
        "ix_outbound_recipient",
        "outbound_messages",
        ["channel", "channel_chat_id", "created_at"],
    )

    op.create_table(
        "dialog_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_chat_id", sa.String(64), nullable=False),
        sa.Column("autoreply_paused_until", sa.DateTime(timezone=True)),
        sa.Column("paused_reason", sa.String(64)),
        sa.Column("automation_disclosed_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
        sa.Column("outbound_count_hour", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hour_window_started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("channel", "channel_chat_id", name="uq_dialog_state"),
    )

    # Журнал аудита неизменяем в той же мере, что и журнал событий:
    # удалять записи о заблокированных действиях нельзя.
    op.execute(
        """
        CREATE TRIGGER outbound_no_delete
        BEFORE DELETE ON outbound_messages
        FOR EACH STATEMENT EXECUTE FUNCTION events_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS outbound_no_delete ON outbound_messages")
    op.drop_table("dialog_states")
    op.drop_index("ix_outbound_recipient", table_name="outbound_messages")
    op.drop_index("ix_outbound_pending", table_name="outbound_messages")
    op.drop_table("outbound_messages")
