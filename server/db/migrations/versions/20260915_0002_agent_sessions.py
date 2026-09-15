"""Add durable IP Agent session snapshots.

Revision ID: 20260915_0002
Revises: 20260915_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260915_0002"
down_revision = "20260915_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("owner_username", sa.String(128)),
        sa.Column("owner_account_id", sa.String(128)),
        sa.Column("legacy_owner_missing", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_checksum", sa.String(64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_agent_sessions_source_sha256",
        ),
        sa.CheckConstraint(
            "snapshot_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_agent_sessions_snapshot_checksum",
        ),
        schema="agent",
    )
    op.create_index(
        "ix_agent_sessions_owner",
        "sessions",
        ["owner_account_id", "updated_at"],
        schema="agent",
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
