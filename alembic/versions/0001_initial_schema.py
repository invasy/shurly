"""initial schema

Revision ID: 0001
Revises:
Create Date: 2024-09-30

Creates the ``urls``, ``analytics``, and ``api_keys`` tables plus the
indexes and constraints described in the project design.

Idempotency ("same (url, user) -> same short code") is enforced in
application code (see :class:`app.repositories.url_repository`), so no
functional / ``COALESCE`` index is required -- only a regular
``(original_url, user_id)`` index to keep that lookup fast.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "urls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("short_code", sa.String(length=12), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("custom_alias", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("click_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("char_length(short_code) > 0", name="ck_urls_short_code_nonempty"),
        sa.PrimaryKeyConstraint("id", name="pk_urls"),
    )
    op.create_index(op.f("urls_short_code_key"), "urls", ["short_code"], unique=True)
    op.create_index(op.f("ix_urls_original_url"), "urls", ["original_url"], unique=False)
    op.create_index(op.f("ix_urls_expires_at"), "urls", ["expires_at"], unique=False)
    op.create_index(op.f("ix_urls_user_id"), "urls", ["user_id"], unique=False)
    op.create_index(op.f("ix_urls_url_user"), "urls", ["original_url", "user_id"], unique=False)

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.String(length=160), nullable=False),
        sa.Column("rate_limit", sa.BigInteger(), server_default=sa.text("100"), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )

    op.create_table(
        "analytics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("url_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "clicked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.ForeignKeyConstraint(
            ["url_id"], ["urls.id"], name="fk_analytics_url_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analytics"),
    )
    op.create_index(op.f("ix_analytics_url_id"), "analytics", ["url_id"], unique=False)
    op.create_index(op.f("ix_analytics_clicked_at"), "analytics", ["clicked_at"], unique=False)
    op.create_index(op.f("ix_analytics_referrer"), "analytics", ["referrer"], unique=False)
    op.create_index(op.f("ix_analytics_country"), "analytics", ["country"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_analytics_country"), table_name="analytics")
    op.drop_index(op.f("ix_analytics_referrer"), table_name="analytics")
    op.drop_index(op.f("ix_analytics_clicked_at"), table_name="analytics")
    op.drop_index(op.f("ix_analytics_url_id"), table_name="analytics")
    op.drop_table("analytics")
    op.drop_table("api_keys")
    op.drop_index(op.f("ix_urls_url_user"), table_name="urls")
    op.drop_index(op.f("ix_urls_user_id"), table_name="urls")
    op.drop_index(op.f("ix_urls_expires_at"), table_name="urls")
    op.drop_index(op.f("ix_urls_original_url"), table_name="urls")
    op.drop_index(op.f("urls_short_code_key"), table_name="urls")
    op.drop_table("urls")
