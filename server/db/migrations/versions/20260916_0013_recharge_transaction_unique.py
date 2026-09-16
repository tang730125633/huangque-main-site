"""Make the WeChat transaction id unique on ledger.recharge_orders.

Revision ID: 20260916_0013
Revises: 20260916_0012

Why: ``recharge_orders.transaction_id`` 是微信侧支付流水号，一笔真实付款只应
对应一张充值单。M6 切到 PostgreSQL 后 ``BEGIN IMMEDIATE`` 只剩进程内写锁
（见 ``server/content_domains/auth_store.py`` 文件头），``review_recharge_order``
里那句「同一流水号不得对应两张单」的 Python 预检在并发下失效 —— 两个并发的
审批事务都查不到对方的 transaction_id，于是同一笔微信流水把两张单都变成
``approved``（实证：``scripts/pay_idempotency_stress.py`` 场景 H）。

唯一索引是这条不变量的数据库层兜底；应用侧在 ``review_recharge_order`` 捕获
``IntegrityError`` 后退化为 ``transaction_in_use``，订单保持 ``pending`` 交人工核对。

部分索引（``WHERE transaction_id IS NOT NULL AND transaction_id <> ''``）保留
历史人工单的空值语义：NULL/空串可以有任意多行。建索引前先扫描重复值，绝不静默
修复账务数据 —— 有重复就中止，交人工核对。
"""

from alembic import op
import sqlalchemy as sa

revision = "20260916_0013"
down_revision = "20260916_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(
        "SELECT transaction_id, count(*) AS n FROM ledger.recharge_orders "
        "WHERE transaction_id IS NOT NULL AND transaction_id <> '' "
        "GROUP BY transaction_id HAVING count(*) > 1 ORDER BY n DESC, transaction_id LIMIT 20"
    )).fetchall()
    if duplicates:
        raise RuntimeError(
            "ledger.recharge_orders 已存在重复微信流水号，禁止自动建唯一索引，"
            "请人工核对后再执行: %s" % "; ".join(
                "%s x%s" % (row[0], row[1]) for row in duplicates))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_recharge_orders_transaction_id "
        "ON ledger.recharge_orders (transaction_id) "
        "WHERE transaction_id IS NOT NULL AND transaction_id <> ''"
    ))


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
