"""一次性邀请码：批量发放、一码一人（2026-09-17 老板发首批 100 人的需求）。

Revision ID: 20260916_0015
Revises: 20260916_0014

Why: 2026-09-17 老板要发 100 个邀请码给 100 个人注册。现有机制
（``idx_invite_codes_active_user`` 部分唯一索引）是「一个邀请人一个永久码、
码可反复使用」。要支持批量码，需要：

1. ``invite_codes`` 加 ``single_use``（1=一次性，绑定即作废）与 ``batch_label``
   （批次名，台账用）；顺手补上 SQLite schema 里已有、PG 里缺失的 ``short_slug``。
2. 唯一索引条件改为 ``status='active' AND single_use=0``：
   永久码仍然一人一个 active，一次性码不限数量。

应用层配合：``invites.admin_bulk_create_codes`` 批量生成；
``invites.bind_registration`` 对 single_use 码原子占用（UPDATE ... WHERE
status='active'），占用失败回 409「该邀请码已被使用」。

降级不提供（破坏性 schema 回滚不支持，恢复走备份）。
"""

from alembic import op
import sqlalchemy as sa

revision = "20260916_0015"
down_revision = "20260916_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(sa.text(
        "ALTER TABLE identity.invite_codes "
        "ADD COLUMN IF NOT EXISTS single_use INTEGER NOT NULL DEFAULT 0"
    ))
    bind.execute(sa.text(
        "ALTER TABLE identity.invite_codes "
        "ADD COLUMN IF NOT EXISTS batch_label TEXT"
    ))
    bind.execute(sa.text(
        "ALTER TABLE identity.invite_codes "
        "ADD COLUMN IF NOT EXISTS short_slug TEXT"
    ))
    # 永久码保持「一人一 active」；一次性码放开数量限制。
    bind.execute(sa.text("DROP INDEX IF EXISTS identity.idx_invite_codes_active_user"))
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX idx_invite_codes_active_user "
        "ON identity.invite_codes (campaign_id, inviter_user_id) "
        "WHERE status = 'active' AND single_use = 0"
    ))


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
