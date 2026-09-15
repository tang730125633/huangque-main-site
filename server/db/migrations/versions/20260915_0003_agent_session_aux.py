"""Add IP Agent profile/report snapshots.

Revision ID: 20260915_0003
Revises: 20260915_0002
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260915_0003"
down_revision = "20260915_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_aux",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_checksum", sa.String(64), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_agent_session_aux_source_sha256",
        ),
        sa.CheckConstraint(
            "snapshot_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_agent_session_aux_snapshot_checksum",
        ),
        schema="agent",
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
