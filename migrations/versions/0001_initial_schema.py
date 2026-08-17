"""Начальная схема: объекты, люди, чаты, сообщения, вложения, журнал событий

Revision ID: 0001
Revises:
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Понадобится на этапе 2 для семантического поиска (раздел 4 ТЗ).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "objects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("started_on", sa.DateTime(timezone=True)),
        sa.Column("planned_finish_on", sa.DateTime(timezone=True)),
        sa.Column("spreadsheet_id", sa.String(128)),
        sa.Column("drive_folder_id", sa.String(128)),
        sa.Column("crm_deal_id", sa.String(128)),
        sa.Column(
            "state", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("phone", sa.String(32)),
        sa.Column("email", sa.String(255)),
        sa.Column("pseudonym", sa.String(64), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "channel_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_user_id", sa.String(64), nullable=False),
        sa.Column("username", sa.String(128)),
        sa.Column("display_name", sa.Text()),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("channel", "channel_user_id", name="uq_channel_identity"),
    )

    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_chat_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="group"),
        sa.Column("title", sa.Text()),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id", ondelete="SET NULL")),
        sa.Column("bot_is_member", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("can_read_all_messages", sa.Boolean()),
        sa.Column("history_backfilled_at", sa.DateTime(timezone=True)),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("channel", "channel_chat_id", name="uq_chat"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_chat_id", sa.String(64), nullable=False),
        sa.Column("channel_message_id", sa.String(128), nullable=False),
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("chats.id", ondelete="SET NULL")),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id", ondelete="SET NULL")),
        sa.Column(
            "author_identity_id",
            sa.Integer(),
            sa.ForeignKey("channel_identities.id", ondelete="SET NULL"),
        ),
        sa.Column("reply_to_message_id", sa.String(128)),
        sa.Column("text", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_outbound", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "channel", "channel_chat_id", "channel_message_id", name="uq_message"
        ),
    )
    op.create_index("ix_messages_chat_sent", "messages", ["chat_id", "sent_at"])

    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("channel_file_id", sa.String(255)),
        sa.Column("source_url", sa.Text()),
        sa.Column("filename", sa.Text()),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("doc_class", sa.String(32)),
        sa.Column("drive_file_id", sa.String(128)),
        sa.Column("stored_at", sa.DateTime(timezone=True)),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_attachments_message_id", "attachments", ["message_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("object_id", sa.Integer(), sa.ForeignKey("objects.id", ondelete="SET NULL")),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_chat_id", sa.String(64)),
        sa.Column("channel_message_id", sa.String(128)),
        sa.Column(
            "source_message_id", sa.BigInteger(), sa.ForeignKey("messages.id", ondelete="SET NULL")
        ),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence", sa.Numeric(3, 2)),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("needs_human", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dedup_key", sa.String(160), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("channel", "channel_chat_id", "dedup_key", name="uq_event_dedup"),
    )
    op.create_index("ix_events_object_created", "events", ["object_id", "created_at"])
    op.create_index("ix_events_payload_gin", "events", ["payload"], postgresql_using="gin")
    op.create_index("ix_events_pending", "events", ["applied", "needs_human"])

    # Журнал неизменяем: правки и удаления запрещены на уровне БД, а не соглашения.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Таблица events доступна только для добавления записей';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_no_delete
        BEFORE DELETE ON events
        FOR EACH STATEMENT EXECUTE FUNCTION events_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_no_delete ON events")
    op.execute("DROP FUNCTION IF EXISTS events_append_only()")
    op.drop_table("events")
    op.drop_table("attachments")
    op.drop_index("ix_messages_chat_sent", table_name="messages")
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_table("channel_identities")
    op.drop_table("people")
    op.drop_table("objects")
