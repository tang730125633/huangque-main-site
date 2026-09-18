"""Versioned provider (env-backed line) config for the ops schema.

Revision ID: 20260918_0016
Revises: 20260916_0015

Two tables backing ``content_domains/provider_config.py``:

``ops.provider_config_versions``
    Immutable URL+credential versions for env-backed provider lines. URL and
    credential are published as **one** version; ``ciphertext``/``nonce`` are
    AES-GCM (``HQ_PROVIDER_KEYS_MASTER_KEY``) and must never be logged.
    ``source`` distinguishes a backend-published version from a version that
    just pins the environment-variable baseline (``env``) so a task created
    before any backend publish can still be recovered after env changes.

``ops.provider_config_runtime``
    Per-instance acknowledgement of the version a running service actually
    loaded. "配置已生效" is derived from this table, never from a successful
    save/publish alone.

Time convention: second-resolution Unix epoch (BIGINT), same as the rest of
``ops``.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260918_0016"
down_revision = "20260916_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_config_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("target_id", sa.Text(), nullable=False,
                  comment="功能线路标识，如 image.seedream"),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False,
                  comment="同一线路内单调递增的不可变版本号"),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True,
                  comment="AES-GCM 密文；任何日志/响应都不得输出"),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("key_present", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("key_last4", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="draft / published / superseded / revoked"),
        sa.Column("evidence", sa.Text(), nullable=True,
                  comment="验证证据 JSON，绑定本版本"),
        sa.Column("evidence_at", sa.BigInteger(), nullable=True),
        sa.Column("op_id", sa.Text(), nullable=True, comment="幂等操作 ID"),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.Text(), nullable=False, server_default="backend",
                  comment="backend / env"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("target_id", "seq", name="uq_provider_config_versions"),
        schema="ops",
    )
    op.create_index(
        "ix_provider_config_versions_active", "provider_config_versions",
        ["target_id", "status", "seq"], schema="ops",
    )
    op.create_index(
        "uq_provider_config_versions_op", "provider_config_versions",
        ["target_id", "op_id"], unique=True, schema="ops",
        postgresql_where=sa.text("op_id IS NOT NULL"),
    )
    op.create_table(
        "provider_config_runtime",
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True,
                  comment="该实例实际加载的版本；NULL 表示仍在使用环境变量"),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("loaded_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("target_id", "instance_id", name="pk_provider_config_runtime"),
        schema="ops",
    )


def downgrade() -> None:
    op.drop_table("provider_config_runtime", schema="ops")
    op.drop_index("uq_provider_config_versions_op", table_name="provider_config_versions", schema="ops")
    op.drop_index("ix_provider_config_versions_active", table_name="provider_config_versions", schema="ops")
    op.drop_table("provider_config_versions", schema="ops")
