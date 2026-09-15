"""Create Huangque schemas and migration-audit tables.

Revision ID: 20260915_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260915_0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = (
    "identity", "ledger", "jobs", "media", "workflow",
    "agent", "render", "routing", "crm", "ops",
)


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

    op.create_table(
        "data_migration_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("domain", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(128)),
        sa.Column("code_sha", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("source_count", sa.BigInteger()),
        sa.Column("target_count", sa.BigInteger()),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_summary", sa.Text()),
        sa.CheckConstraint(
            "state IN ('planned','running','verified','failed','rolled_back')",
            name="ck_data_migration_runs_state",
        ),
        schema="ops",
    )
    op.create_table(
        "data_migration_items",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("source_table", sa.String(128), nullable=False),
        sa.Column("chunk_key", sa.String(128), nullable=False),
        sa.Column("source_count", sa.BigInteger(), nullable=False),
        sa.Column("target_count", sa.BigInteger()),
        sa.Column("source_checksum", sa.String(128)),
        sa.Column("target_checksum", sa.String(128)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ops.data_migration_runs.run_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "source_table", "chunk_key"),
        sa.CheckConstraint(
            "state IN ('planned','running','verified','failed')",
            name="ck_data_migration_items_state",
        ),
        schema="ops",
    )
    op.create_index(
        "ix_data_migration_runs_domain_started",
        "data_migration_runs",
        ["domain", "started_at"],
        schema="ops",
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
