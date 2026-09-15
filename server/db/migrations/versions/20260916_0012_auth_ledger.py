"""Move the auth-service accounting tables into the ledger schema.

Revision ID: 20260916_0012
Revises: 20260916_0011

Source of truth for these seven tables is currently the SQLite file
``/home/ubuntu/auth-service/users.db`` (served by the ``huangque-auth`` systemd
unit through ``/usr/bin/python3``). Every table keeps its source name and lands
in the ``ledger`` schema: ``points_audit``, ``point_transfers``,
``invite_reward_point_records``, ``recharge_orders``,
``membership_recharge_records``, ``membership_upgrade_records``,
``virtual_pay_orders``. The SQLite internal table ``sqlite_sequence`` is not
business data and is deliberately skipped (PostgreSQL sequences take its place).

This migration only creates the PostgreSQL tables; the backfill and the
read/write cutover are separate steps (see ``scripts/migrate_auth_ledger.py``
and the M6J runbook). Nothing here touches production data or behaviour.

Time convention: every ``created_at`` / ``paid_at`` / ``credited_at`` /
``delivered_at`` / ``reviewed_at`` / ``voided_at`` / ``*_expires_at`` column is a
second-resolution Unix epoch stored as BIGINT, exactly like the SQLite source
(sampled 2026-09-16: ``points_audit.created_at = 1789478747``). No
``timestamptz`` is introduced, so no implicit conversion can shift a row during
the shadow comparison. ``users.created_at`` (TEXT ``datetime('now')``) belongs to
the identity domain and is not part of this batch.

Boolean convention: the M3 rule is "SQLite INTEGER used as a 0/1 flag becomes
``boolean``". **This batch has no such column.** ``virtual_pay_orders.env`` is a
payment-environment enum (0 = production, 1 = sandbox) consumed as
``int(env)`` / ``env == 0`` by ``server/wechat_virtual_pay.py``, so it stays
BIGINT; turning it into a boolean would silently change the WeChat Pay endpoint
it selects. Every writer of this schema must keep sending ``0``/``1`` integers.

Money/points: the ledger is append-only and zero-tolerance. This migration
copies shapes only — no value is ever recomputed, rounded or repaired, neither
here nor in the backfill script.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260916_0012"
down_revision = "20260916_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. points_audit -> ledger.points_audit
    #    点数总账（唯一权威流水）：管理员加减点、充值审批、任务扣点/退点、
    #    点数赠送、名片邀请注册奖励都往这张表追加一行。
    #    不变量：before_points/after_points 是写这一行前后 users.points 的真实值；
    #    delta <> 0 与 after - before == delta 同时成立（免费内测期间的
    #    apply_balance=False 记账是唯一例外：delta 记了但余额没动）。
    #    transaction_key 是幂等键（同一 key 只允许一行），NULL 表示无幂等键的
    #    人工操作 —— SQLite 与 PostgreSQL 的唯一索引都把多个 NULL 视为互不相同。
    # ------------------------------------------------------------------
    op.create_table(
        "points_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键；回填时显式带源编号以保留历史排序，之后由序列继续"),
        sa.Column("who_admin", sa.Text(), nullable=False,
                  comment="操作者：'system' 表示任务扣点/退点等非人工操作，'用户赠送' 表示点数赠送，"
                          "其余为管理员用户名（充值审批为 'wxpay'）"),
        sa.Column("username", sa.Text(), nullable=False,
                  comment="被记账账号（用户名，不是编号；与 users.username 对应）"),
        sa.Column("delta", sa.BigInteger(), nullable=False,
                  comment="本次变动点数：正为加、负为减；0 不应出现（写库方会短路）"),
        sa.Column("before_points", sa.BigInteger(), nullable=False,
                  comment="写这一行之前的余额；免费内测期间扣点不落余额时与 after_points 相等"),
        sa.Column("after_points", sa.BigInteger(), nullable=False,
                  comment="写这一行之后的余额；该用户最后一条流水的 after_points 必须等于 users.points"),
        sa.Column("reason", sa.Text(),
                  comment="变动原因，最长 120 字（job:<能力>、job#<编号>、充值审批: …）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="发生时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("transaction_key", sa.Text(),
                  comment="幂等键，形如 job-charge:<用户名>:<路径>:<请求号> / points-transfer:<编号>:in|out；NULL 表示无幂等键"),
        schema="ledger",
    )
    op.execute(
        sa.text("CREATE INDEX idx_points_audit_user ON ledger.points_audit (username, id DESC)")
    )
    op.execute(
        sa.text("CREATE UNIQUE INDEX idx_points_audit_transaction_key "
                "ON ledger.points_audit (transaction_key)")
    )

    # ------------------------------------------------------------------
    # 2. point_transfers -> ledger.point_transfers
    #    用户之间的点数赠送（仅合伙人/发起人会员可发起）。sender_before/after 与
    #    recipient_before/after 是同一事务里的两侧快照，必须逐字保留：
    #    这本账不接受任何重新计算。每笔赠送同时写两行 points_audit
    #    （transaction_key = points-transfer:<transfer_id>:out|in），核对时必须去重。
    # ------------------------------------------------------------------
    op.create_table(
        "point_transfers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键；回填时显式带源编号"),
        sa.Column("transfer_id", sa.Text(), nullable=False,
                  comment="赠送流水号，形如 PT<24 位十六进制>；全局唯一"),
        sa.Column("request_id", sa.Text(), nullable=False,
                  comment="调用方幂等号；与 sender_user_id 组成唯一键，重放不重复赠送"),
        sa.Column("sender_user_id", sa.BigInteger(), nullable=False,
                  comment="赠送方用户编号（users.id）"),
        sa.Column("recipient_user_id", sa.BigInteger(), nullable=False,
                  comment="接收方用户编号（users.id）；必须与赠送方不同"),
        sa.Column("amount", sa.BigInteger(), nullable=False,
                  comment="赠送点数，必须大于 0（CHECK 约束）"),
        sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="赠送附言，用户可见"),
        sa.Column("sender_before", sa.BigInteger(), nullable=False,
                  comment="赠送方转出前余额快照"),
        sa.Column("sender_after", sa.BigInteger(), nullable=False,
                  comment="赠送方转出后余额快照，必须等于 sender_before - amount"),
        sa.Column("recipient_before", sa.BigInteger(), nullable=False,
                  comment="接收方转入前余额快照"),
        sa.Column("recipient_after", sa.BigInteger(), nullable=False,
                  comment="接收方转入后余额快照，必须等于 recipient_before + amount"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="赠送时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.CheckConstraint("amount > 0", name="ck_point_transfers_amount_positive"),
        sa.CheckConstraint("sender_user_id <> recipient_user_id",
                           name="ck_point_transfers_distinct_party"),
        sa.UniqueConstraint("transfer_id", name="uq_point_transfers_transfer_id"),
        sa.UniqueConstraint("sender_user_id", "request_id",
                            name="uq_point_transfers_sender_request"),
        schema="ledger",
    )
    op.execute(sa.text(
        "CREATE INDEX idx_point_transfers_sender_created "
        "ON ledger.point_transfers (sender_user_id, created_at DESC, id DESC)"
    ))
    op.execute(sa.text(
        "CREATE INDEX idx_point_transfers_recipient_created "
        "ON ledger.point_transfers (recipient_user_id, created_at DESC, id DESC)"
    ))

    # ------------------------------------------------------------------
    # 3. invite_reward_point_records -> ledger.invite_reward_point_records
    #    邀请奖励点数台账。**独立账本**：按源码约定它绝不并入 users.points
    #    （server/invites.py「返回独立邀请奖励积分汇总；绝不读取或修改 users.points」，
    #    docs/membership-launch-runbook.md「邀请奖励进入独立奖励点数台账，不改变可消费点数」）。
    #    因此余额核对时它必须单独列报，不得与 points_audit 相加后再比对余额。
    # ------------------------------------------------------------------
    op.create_table(
        "invite_reward_point_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键；回填时显式带源编号"),
        sa.Column("invite_relation_id", sa.BigInteger(), nullable=False,
                  comment="邀请关系编号（user_invites.id），指向下层用户"),
        sa.Column("upgrade_record_id", sa.BigInteger(), nullable=False,
                  comment="触发的会员升级记录编号（membership_upgrade_records.id）；一条升级只记一次"),
        sa.Column("inviter_user_id", sa.BigInteger(), nullable=False,
                  comment="获得奖励的邀请人用户编号（可能是上溯转让后的接收人）"),
        sa.Column("invitee_user_id", sa.BigInteger(), nullable=False,
                  comment="被邀请人用户编号"),
        sa.Column("inviter_level_snapshot", sa.Text(), nullable=False,
                  comment="发奖时邀请人会员等级快照（experience/initiator/partner）"),
        sa.Column("invitee_level", sa.Text(), nullable=False,
                  comment="被邀请人达成并被奖励的等级"),
        sa.Column("reward_points", sa.BigInteger(), nullable=False,
                  comment="本次奖励点数（独立奖励账本，不进入可消费点数）"),
        sa.Column("reward_total_after", sa.BigInteger(), nullable=False,
                  comment="写这一行后该邀请关系的奖励累计值，用于审计"),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'recorded'"),
                  comment="recorded=已记录生效；pending_review=风控待复核；voided=已作废（此时 voided_at/voided_by 有值）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="记录时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("voided_at", sa.BigInteger(),
                  comment="作废时间，秒级 Unix 时间戳；未作废为空"),
        sa.Column("void_reason", sa.Text(), comment="作废原因，人工填写"),
        sa.Column("voided_by", sa.Text(), comment="作废操作人（管理员用户名）"),
        sa.Column("event_type", sa.Text(), nullable=False, server_default=sa.text("'upgrade'"),
                  comment="upgrade=首次升级奖励；renewal=续费奖励"),
        sa.Column("claim_id", sa.BigInteger(),
                  comment="对应的待领取条目编号（invite_reward_claims.id）；NULL 表示直接发放"),
        schema="ledger",
    )
    op.execute(sa.text(
        "CREATE INDEX idx_invite_rewards_inviter "
        "ON ledger.invite_reward_point_records (inviter_user_id, id DESC)"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX idx_invite_rewards_claim "
        "ON ledger.invite_reward_point_records (claim_id) WHERE claim_id IS NOT NULL"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX idx_invite_rewards_upgrade_relation_level "
        "ON ledger.invite_reward_point_records (invite_relation_id, invitee_level) "
        "WHERE event_type = 'upgrade' AND status IN ('recorded','pending_review')"
    ))

    # ------------------------------------------------------------------
    # 4. recharge_orders -> ledger.recharge_orders
    #    人工/微信扫码充值单。金额是元（DOUBLE PRECISION，源为 REAL），
    #    points 是批准后要加到余额的点数；approve 会同时写 points_audit 并（会员单）
    #    写 membership_recharge_records。状态机 pending/approved/rejected/refunded/refund_review。
    # ------------------------------------------------------------------
    op.create_table(
        "recharge_orders",
        sa.Column("order_id", sa.Text(), primary_key=True,
                  comment="充值单号，形如 R<秒级时间戳><随机码>；业务幂等键"),
        sa.Column("username", sa.Text(), nullable=False,
                  comment="充值账号（用户名）"),
        sa.Column("amount", sa.Double(), nullable=False,
                  comment="实付金额（元）；微信支付退款按此金额核对分单位总额"),
        sa.Column("points", sa.BigInteger(), nullable=False,
                  comment="批准后入账点数；会员单（order_type=membership_*）为 0"),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'"),
                  comment="pending=待审；approved=已入账；rejected=已拒绝；refunded=已退款；refund_review=退款需人工核对"),
        sa.Column("note", sa.Text(), comment="用户备注（如 '微信扫码充值'）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="下单时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("reviewed_by", sa.Text(), comment="审批人（管理员用户名；退款为 'system'）"),
        sa.Column("reviewed_at", sa.BigInteger(),
                  comment="审批/退款时间，秒级 Unix 时间戳；未处理为空"),
        sa.Column("review_note", sa.Text(), comment="审批意见或失败原因（如退款不一致需人工核对）"),
        sa.Column("transaction_id", sa.Text(),
                  comment="微信支付流水号；同一流水号不得对应两张单"),
        sa.Column("pay_channel", sa.Text(),
                  comment="支付渠道：wxpay_native / wxpay_jsapi / manual"),
        sa.Column("order_type", sa.Text(), nullable=False, server_default=sa.text("'points'"),
                  comment="points=点数充值；membership_experience/membership_renewal=会员开通/续费"),
        sa.Column("list_amount", sa.Double(),
                  comment="原价（元），用于展示折扣；NULL 表示源未记录"),
        sa.Column("pricing_tier", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="下单时的会员等级快照（折扣依据），空串表示按原价"),
        sa.Column("discount_bps", sa.BigInteger(), nullable=False, server_default=sa.text("10000"),
                  comment="折扣基点：10000 = 不打折；5500 = 5.5 折（与价格快照一起冻结）"),
        schema="ledger",
    )

    # ------------------------------------------------------------------
    # 5. membership_recharge_records -> ledger.membership_recharge_records
    #    会员（体验官）开通/续费台账：谁在什么时候把某个账号的到期时间推到了哪一刻。
    #    不涉及点数余额，但与 recharge_orders / membership_upgrade_records 同属账务域，
    #    必须同批迁移，否则核对时缺少会员到期这一侧的权威。
    # ------------------------------------------------------------------
    op.create_table(
        "membership_recharge_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键；回填时显式带源编号"),
        sa.Column("request_id", sa.Text(), nullable=False,
                  comment="幂等号：管理员操作为 UUID，订单入账为 'membership-order:<订单号>'"),
        sa.Column("username", sa.Text(), nullable=False, comment="被开通的账号"),
        sa.Column("tier", sa.Text(), nullable=False, comment="会员等级（当前固定 experience）"),
        sa.Column("before_expires_at", sa.BigInteger(),
                  comment="变更前到期时间，秒级 Unix 时间戳；NULL/0 表示此前无会员"),
        sa.Column("after_expires_at", sa.BigInteger(), nullable=False,
                  comment="变更后到期时间，秒级 Unix 时间戳"),
        sa.Column("operator", sa.Text(), nullable=False,
                  comment="操作人：管理员用户名，或 'system'（支付订单自动入账）"),
        sa.Column("reason", sa.Text(), comment="开通原因（如 '管理员充值一年会员'）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="操作时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.UniqueConstraint("request_id", name="uq_membership_recharge_request"),
        schema="ledger",
    )
    op.execute(sa.text(
        "CREATE INDEX idx_membership_recharge_user "
        "ON ledger.membership_recharge_records (username, id DESC)"
    ))

    # ------------------------------------------------------------------
    # 6. membership_upgrade_records -> ledger.membership_upgrade_records
    #    会员等级变更流水（升级/续费/退会员作废）：邀请奖励由它触发，
    #    refund 会把对应行标记 voided。source + source_order_id 唯一（非空时），
    #    保证同一订单不会重复发放等级与邀请奖励。
    # ------------------------------------------------------------------
    op.create_table(
        "membership_upgrade_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键；回填时显式带源编号"),
        sa.Column("user_id", sa.BigInteger(), nullable=False,
                  comment="会员所属用户编号（users.id）"),
        sa.Column("from_level", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="变更前等级，空串表示此前不是会员"),
        sa.Column("to_level", sa.Text(), nullable=False,
                  comment="变更后等级（experience/initiator/partner）；退会员时应同 source 作废"),
        sa.Column("source", sa.Text(), nullable=False,
                  comment="来源：online=微信支付；offline_admin=后台手工；membership_audit:<id> 等"),
        sa.Column("source_order_id", sa.Text(),
                  comment="来源订单编号，与 source 组成唯一键；NULL/空串表示无对应订单（如后台手工）"),
        sa.Column("operator", sa.Text(), comment="操作人：管理员用户名或 'system'"),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'effective'"),
                  comment="effective=生效；voided=已作废（退款/纠错）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="变更时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("voided_at", sa.BigInteger(),
                  comment="作废时间，秒级 Unix 时间戳；未作废为空"),
        sa.Column("void_reason", sa.Text(), comment="作废原因（如 membership_refund）"),
        sa.Column("event_type", sa.Text(), nullable=False, server_default=sa.text("'upgrade'"),
                  comment="upgrade=升级/开通；renewal=续费"),
        schema="ledger",
    )
    op.execute(sa.text(
        "CREATE INDEX idx_membership_upgrades_user "
        "ON ledger.membership_upgrade_records (user_id, id DESC)"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX idx_membership_upgrades_source "
        "ON ledger.membership_upgrade_records (source, source_order_id) "
        "WHERE source_order_id IS NOT NULL AND source_order_id <> ''"
    ))

    # ------------------------------------------------------------------
    # 7. virtual_pay_orders -> ledger.virtual_pay_orders
    #    微信虚拟支付（小程序内购）订单。amount_fen 是分；env 是支付环境枚举
    #    （0 = 正式，1 = 沙箱），保留 BIGINT；raw_order_json 存回调原文供申诉核对。
    #    状态机 created/paid/credited/failed/refund_review/refunded。
    #    本表不含任何密钥；raw_order_json 可能含用户标识，报表输出需脱敏。
    # ------------------------------------------------------------------
    op.create_table(
        "virtual_pay_orders",
        sa.Column("order_id", sa.Text(), primary_key=True,
                  comment="商户订单号，形如 HQ<yyMMddHHmmss><随机码>"),
        sa.Column("username", sa.Text(), nullable=False, comment="下单账号（用户名）"),
        sa.Column("openid", sa.Text(), nullable=False,
                  comment="微信小程序 openid；下单与查单都用它，属个人标识，输出需脱敏"),
        sa.Column("package_id", sa.Text(), nullable=False,
                  comment="道具包键，形如 points_1000"),
        sa.Column("product_id", sa.Text(), nullable=False,
                  comment="微信侧商品编号"),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False,
                  comment="实付金额（分）"),
        sa.Column("points", sa.BigInteger(), nullable=False,
                  comment="到账点数；会员单为 0"),
        sa.Column("env", sa.BigInteger(), nullable=False,
                  comment="支付环境枚举：0=正式（PROD 密钥）、1=沙箱；代码按 int(env)/env==0 取密钥，"
                          "不是布尔标志，故保留 BIGINT"),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'created'"),
                  comment="created=已下单未支付；paid/credited=已支付并入账；failed=失败；"
                          "refund_review=退款需人工核对；refunded=已退款"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="下单时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("paid_at", sa.BigInteger(),
                  comment="微信支付时间，秒级 Unix 时间戳（来自回调 paid_time）"),
        sa.Column("credited_at", sa.BigInteger(),
                  comment="点数入账时间，秒级 Unix 时间戳；此时才写 points_audit"),
        sa.Column("delivered_at", sa.BigInteger(),
                  comment="向微信确认发货时间，秒级 Unix 时间戳（不发货会触发退款）"),
        sa.Column("wx_order_id", sa.Text(), comment="微信侧订单号"),
        sa.Column("wxpay_order_id", sa.Text(), comment="微信支付单号（对账用）"),
        sa.Column("raw_order_json", sa.Text(),
                  comment="查单/回调原文 JSON，仅用于申诉核对；不得进入报表"),
        sa.Column("last_error", sa.Text(), comment="最近一次失败原因"),
        sa.Column("list_amount_fen", sa.BigInteger(),
                  comment="原价（分），用于展示折扣；NULL 表示源未记录"),
        sa.Column("pricing_tier", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="下单时的会员等级快照，空串表示按原价"),
        sa.Column("discount_bps", sa.BigInteger(), nullable=False, server_default=sa.text("10000"),
                  comment="折扣基点：10000 = 不打折（与 price 快照一起冻结，退款按实付金额）"),
        sa.Column("order_type", sa.Text(), nullable=False, server_default=sa.text("'points'"),
                  comment="points=点数；membership_experience/membership_renewal=会员开通/续费"),
        schema="ledger",
    )
    op.execute(sa.text(
        "CREATE INDEX idx_virtual_pay_orders_user "
        "ON ledger.virtual_pay_orders (username, created_at DESC)"
    ))

    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.points_audit IS "
            "'点数总账（唯一权威流水）：人工加减点、充值审批、任务扣退点、点数赠送、"
            "名片邀请注册奖励都追加一行；before/after 是当时真实余额快照。"
            "账务零容忍：任何修正都需人工批准，迁移代码只许照搬与核对。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.point_transfers IS "
            "'用户间点数赠送台账（仅会员可发起）：两侧余额快照逐字保留，"
            "每笔同时产生 points_audit 的 out/in 两行，余额核对时按 transaction_key 去重。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.invite_reward_point_records IS "
            "'邀请奖励点数台账：独立账本，按业务约定绝不并入可消费点数（users.points），"
            "余额核对时单独列报，不与 points_audit 相加。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.recharge_orders IS "
            "'充值与会员订单：审批通过才入账（写 points_audit / 会员到期），"
            "退款不一致时置 refund_review 交人工核对，绝不自动改数。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.membership_recharge_records IS "
            "'会员开通/续费台账：管理员手工与支付订单自动入账共用，"
            "request_id 幂等，决定 membership_expires_at 的权威历史。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.membership_upgrade_records IS "
            "'会员等级变更流水：邀请奖励由它触发，退款作废也记在这里，"
            "（source, source_order_id）唯一保证同一订单不重复发放权益。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ledger.virtual_pay_orders IS "
            "'微信虚拟支付订单：credited 时才给用户加点并写 points_audit；"
            "raw_order_json/openid 含个人标识，报表与核对输出必须脱敏。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
