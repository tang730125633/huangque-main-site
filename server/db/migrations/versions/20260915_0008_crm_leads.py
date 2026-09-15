"""Move the lead follow-up CRM rows into the crm schema.

Revision ID: 20260915_0008
Revises: 20260915_0007

Source of truth for this table is currently the SQLite file
``content-api/leads_crm.db`` (single table ``lead_crm``). This migration only
creates the PostgreSQL table; the backfill and the read/write cutover are
separate steps (see ``scripts/migrate_crm_leads.py`` and the M3E runbook).
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0008"
down_revision = "20260915_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("username", sa.String(128), primary_key=True,
                  comment="线索归属账号：content-api 登录用户名；同一 lead_id 在不同账号下互不可见"),
        sa.Column("lead_id", sa.String(64), primary_key=True,
                  comment="线索指纹：sha1(平台:用户:去空白评论) 前 16 位十六进制；同一评论重复采集恒得同一值"),
        sa.Column("intent", sa.String(32), nullable=False, server_default=sa.text("'高意向'"),
                  comment="意向标签，只允许 高意向/咨询/价格敏感/围观（代码常量 CRM_INTENTS）"),
        sa.Column("follow_status", sa.String(32), nullable=False, server_default=sa.text("'待跟进'"),
                  comment="跟进状态，只允许 待跟进/跟进中/已加微/已成交/无效（代码常量 CRM_STATUSES）"),
        sa.Column("follow_note", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="人工跟进备注，写入前截断到 300 字"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致，不做时区换算）"),
        schema="crm",
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE crm.leads IS "
            "'获客线索跟进台账：采集列表按 username+lead_id 合并回显人工跟进状态；"
            "写入方只有 content-api 的 CRM 接口，读方为 content-api 与 leadgen-api。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
