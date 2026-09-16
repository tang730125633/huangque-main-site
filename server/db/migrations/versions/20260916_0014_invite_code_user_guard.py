"""邀请码升级为注册准入证：删用户/禁用户自动失效其邀请码（报障台审查结论落实）。

Revision ID: 20260916_0014
Revises: 20260916_0013

Why: 2026-09-16 晚邀请码成为「注册准入证」。校验侧（``invites.validate_code``）
本来就 ``JOIN users`` 且要求 ``account_status='active'`` —— 孤儿码（inviter 已
物理删除）在注册入口永远验证不过；但 ``invite_codes`` 台账里的孤儿行仍停在
``status='active'``，后台看到的是「还有效的码」，且没有任何路径在删用户/禁用户
时同步这张表。

本迁移两件事：
1. 把现有孤儿 active 码（inviter 不存在）标记 ``disabled`` —— 与 09-16 晚的
   手工处置一致（幂等，可重跑）。
2. 建 PG 触发器：``identity.users`` 行被 DELETE、或 ``account_status`` 被改离
   ``'active'`` 时，自动把该用户的 active 邀请码置 ``disabled``。数据库层兜底，
   覆盖所有删除路径（应用层/脚本/手工 SQL），不依赖任何一条代码路径记得做。

降级不提供（破坏性 schema 回滚不支持，恢复走备份）。
"""

from alembic import op
import sqlalchemy as sa

revision = "20260916_0014"
down_revision = "20260916_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 现有孤儿码先处置：inviter 已被物理删除、但状态仍是 active 的码，全部 disabled。
    #    校验入口本来就会拒绝它们（JOIN users），这里是台账一致性。
    result = bind.execute(sa.text(
        "UPDATE identity.invite_codes SET status='disabled' "
        "WHERE status='active' AND inviter_user_id NOT IN (SELECT id FROM identity.users)"
    ))
    print("invite-code-guard: orphan active codes disabled = %d" % (result.rowcount or 0))

    # 2. 触发器：删用户 / 账号状态离开 active → 其 active 邀请码自动失效。
    bind.execute(sa.text(
        """
        CREATE OR REPLACE FUNCTION identity.invite_codes_invalidate_on_user_change()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR NEW.account_status IS DISTINCT FROM 'active' THEN
            UPDATE identity.invite_codes SET status='disabled'
              WHERE inviter_user_id = OLD.id AND status='active';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    ))
    bind.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_invite_codes_invalidate_on_user_change "
        "ON identity.users"
    ))
    bind.execute(sa.text(
        """
        CREATE TRIGGER trg_invite_codes_invalidate_on_user_change
          AFTER DELETE OR UPDATE OF account_status ON identity.users
          FOR EACH ROW
          EXECUTE FUNCTION identity.invite_codes_invalidate_on_user_change()
        """
    ))


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
