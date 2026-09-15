"""Move feature flags and pricing rules into the ops schema.

Revision ID: 20260915_0004
Revises: 20260915_0003

Source of truth for these tables is currently the SQLite file
``content-api/feature_flags.db``. This migration only creates the PostgreSQL
tables; the backfill and the read/write cutover are separate steps
(see ``scripts/migrate_ops_flags_pricing.py`` and the M3A runbook).
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0004"
down_revision = "20260915_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("feature", sa.String(64), primary_key=True,
                  comment="功能开关键，取值与代码 CATALOG 一致；未知键一律按目录默认处理"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"),
                  comment="是否放行该功能；开关写库后 5 秒内对所有读方生效"),
        sa.Column("updated_by", sa.String(128),
                  comment="最后修改人（管理员用户名或自动化标识）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )
    op.create_table(
        "pricing_rules",
        sa.Column("rule", sa.String(128), primary_key=True,
                  comment="价格规则键，形如 video.cinematic.open；未知键不报价"),
        sa.Column("points", sa.Integer(), nullable=False,
                  comment="单次生成扣除的点数，正整数"),
        sa.Column("updated_by", sa.String(128),
                  comment="最后修改人（管理员用户名或自动化标识）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.feature_flags IS "
            "'平台功能开关：content/imggen/leadgen 接单前读取，admin 写入。"
            "行缺失时按代码 CATALOG 的默认值（多数为放行）。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.pricing_rules IS "
            "'能力单价表：与 CATALOG 配合报价，行缺失表示该能力当前不提供。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
