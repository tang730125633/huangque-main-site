"""Move the admin configuration database into the ops schema.

Revision ID: 20260915_0007
Revises: 20260915_0006

Source of truth for these tables is currently the SQLite file
``content-api/admin_config.db`` (7 tables). Every source table lands in the
``ops`` schema; the two source tables that do not already start with ``admin_``
(``provider_api_keys`` / ``inspiration_cases``) are renamed with an ``admin_``
prefix so they cannot collide with existing ``ops`` tables
(``ops.feature_flags``, ``ops.pricing_rules``, ``ops.data_migration_*``).

This migration only creates the PostgreSQL tables; the backfill and the
read/write cutover are separate steps (see
``scripts/migrate_ops_admin_config.py`` and the M3D runbook).

Time convention: every timestamp column stays a second-resolution Unix epoch
(BIGINT), exactly like the SQLite source. No ``timestamptz`` is introduced, so
no implicit conversion can shift a row during the shadow comparison.

Boolean convention: SQLite ``INTEGER`` 0/1 columns become real ``boolean``
columns; every writer must normalise with ``bool()`` (psycopg3 refuses 0/1
for a boolean column).
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0007"
down_revision = "20260915_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. provider_api_keys -> ops.admin_provider_api_keys
    #    加密托管的视频渠道 API 密钥池。ciphertext/nonce 是 AES-GCM 密文与随机数，
    #    使用 HQ_PROVIDER_KEYS_MASTER_KEY 解开；任何日志都不得打印这两列。
    # ------------------------------------------------------------------
    op.create_table(
        "admin_provider_api_keys",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="密钥编号，任务是绑定它而不是绑定明文；一经创建不再改变"),
        sa.Column("provider", sa.Text(), nullable=False,
                  comment="视频渠道标识：xai/deepseek/sora/seedance/omni/minimax"),
        sa.Column("label", sa.Text(), nullable=False,
                  comment="管理员可读的备注名，最长 60 字"),
        sa.Column("last4", sa.Text(), nullable=False,
                  comment="密钥末四位，仅用于后台辨认，不可用于解密"),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False,
                  comment="AES-GCM 密文（BYTEA）；迁移与日志禁止回显该列"),
        sa.Column("nonce", sa.LargeBinary(), nullable=False,
                  comment="AES-GCM 随机数（BYTEA）；与 ciphertext 成对使用"),
        sa.Column("base_url", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="创建时冻结的上游地址；env 改动不得挪动已付款任务的上游"),
        sa.Column("priority", sa.Integer(), nullable=False,
                  comment="同渠道内的人工优先顺序，数字小的先用"),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'active'"),
                  comment="active=可派单；retired=已下架但绑定它的老任务仍能跑完"),
        sa.Column("health_status", sa.Text(), nullable=False, server_default=sa.text("'unknown'"),
                  comment="unknown/healthy/unhealthy；unhealthy 不再被自动派单"),
        sa.Column("last_checked_at", sa.BigInteger(),
                  comment="最近一次健康探测时间，秒级 Unix 时间戳"),
        sa.Column("last_latency_ms", sa.Integer(),
                  comment="最近一次健康探测耗时（毫秒）"),
        sa.Column("last_error", sa.Text(),
                  comment="最近一次失败原因，最长 180 字；不含密钥明文"),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default=sa.text("0"),
                  comment="累计派单次数；轮转按最少使用挑密钥"),
        sa.Column("last_used_at", sa.BigInteger(),
                  comment="最近一次派单时间，秒级 Unix 时间戳"),
        sa.Column("created_by", sa.Text(), nullable=False,
                  comment="创建人；system-env-migration 表示由环境变量一次性托管"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )
    op.create_index(
        "idx_admin_provider_api_keys_active",
        "admin_provider_api_keys",
        ["provider", "state", "health_status", "priority", "id"],
        schema="ops",
    )
    op.create_index(
        "idx_admin_provider_api_keys_rotation",
        "admin_provider_api_keys",
        ["provider", "state", "health_status", "use_count", "priority", "id"],
        schema="ops",
    )

    # ------------------------------------------------------------------
    # 2. admin_channel_config -> ops.admin_channel_config
    # ------------------------------------------------------------------
    op.create_table(
        "admin_channel_config",
        sa.Column("channel", sa.Text(), primary_key=True,
                  comment="接单渠道键，取值与代码 CHANNELS 一致"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"),
                  comment="是否接受新任务；关闭不影响已在跑的任务"),
        sa.Column("config", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="渠道配置 JSON 文本；禁止出现密钥字段"),
        sa.Column("updated_by", sa.Text(),
                  comment="最后修改人（管理员用户名或自动化标识）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )

    # ------------------------------------------------------------------
    # 3. admin_audit -> ops.admin_audit
    #    管理操作审计台账。源表是 INTEGER PRIMARY KEY AUTOINCREMENT，这里用
    #    BIGSERIAL 保持「id 单调递增」，读取方按 (created_at DESC, id DESC) 排序，
    #    同一秒内的多条记录顺序仍然稳定。
    # ------------------------------------------------------------------
    op.create_table(
        "admin_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键；回填时显式带源编号（保留历史排序），之后由序列继续"),
        sa.Column("actor", sa.Text(), nullable=False,
                  comment="操作人（管理员用户名、system:xxx 自动化标识）"),
        sa.Column("action", sa.Text(), nullable=False,
                  comment="动作键，形如 channel.save / feature.toggle / provider_key.add"),
        sa.Column("target", sa.Text(), nullable=False,
                  comment="操作对象编号（渠道键、开关键、密钥编号、批次号）"),
        sa.Column("detail", sa.Text(), nullable=False,
                  comment="动作详情 JSON 文本；禁止记录密钥明文"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="发生时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )

    # ------------------------------------------------------------------
    # 4. admin_e2e_runs -> ops.admin_e2e_runs
    #    后台验收批次台账（每行一次可能扣点的全流程测试）。
    # ------------------------------------------------------------------
    op.create_table(
        "admin_e2e_runs",
        sa.Column("run_id", sa.Text(), primary_key=True,
                  comment="验收运行编号，由后台生成"),
        sa.Column("batch_id", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="所属批次号；空串表示单条运行"),
        sa.Column("operation_id", sa.Text(), nullable=False,
                  comment="被验收的能力键，形如 video.cinematic.open"),
        sa.Column("username", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="验收使用的专用测试账号"),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="planned/queued/submitting/running/completed/failed/unknown"),
        sa.Column("job_id", sa.BigInteger(),
                  comment="提交成功后绑定的任务编号；用于幂等与结果回收"),
        sa.Column("acceptance_id", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="验收产物编号，用于回查是否过期"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="验收证据 JSON 文本"),
        sa.Column("cost", sa.Integer(), nullable=False, server_default=sa.text("0"),
                  comment="本次验收消耗的点数"),
        sa.Column("points_before", sa.Integer(),
                  comment="提交前账号余额"),
        sa.Column("points_after", sa.Integer(),
                  comment="结束后账号余额"),
        sa.Column("transaction_key", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="扣点幂等键"),
        sa.Column("error", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="失败或停止原因"),
        sa.Column("created_by", sa.Text(), nullable=False,
                  comment="发起人（管理员用户名）"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )
    op.create_index(
        "idx_admin_e2e_operation", "admin_e2e_runs",
        ["operation_id", "created_at"], schema="ops",
    )
    op.create_index(
        "idx_admin_e2e_batch", "admin_e2e_runs", ["batch_id", "created_at"], schema="ops",
    )

    # ------------------------------------------------------------------
    # 5. admin_e2e_fixture_attempts -> ops.admin_e2e_fixture_attempts
    # ------------------------------------------------------------------
    op.create_table(
        "admin_e2e_fixture_attempts",
        sa.Column("fixture_key", sa.Text(), primary_key=True,
                  comment="验收夹具键，形如 audio.personal"),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="夹具准备状态；同一夹具一天只允许成功一次，避免重复扣点"),
        sa.Column("error", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="失败原因，最长 240 字"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后尝试时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )

    # ------------------------------------------------------------------
    # 6. admin_e2e_delivery_checks -> ops.admin_e2e_delivery_checks
    # ------------------------------------------------------------------
    op.create_table(
        "admin_e2e_delivery_checks",
        sa.Column("job_id", sa.BigInteger(), primary_key=True,
                  comment="被验收的任务编号"),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="checking/passed/failed；checking 表示已有进程在验收"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="验收结论说明（下载结果、解码结果）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        schema="ops",
    )

    # ------------------------------------------------------------------
    # 7. inspiration_cases -> ops.admin_inspiration_cases
    # ------------------------------------------------------------------
    op.create_table(
        "admin_inspiration_cases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True,
                  comment="自增主键（源表为 INTEGER PRIMARY KEY AUTOINCREMENT，回填保留原编号）"),
        sa.Column("title", sa.Text(), nullable=False, comment="案例标题"),
        sa.Column("category", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="案例分类"),
        sa.Column("tags", sa.Text(), nullable=False, server_default=sa.text("'[]'"),
                  comment="标签 JSON 数组文本"),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="复刻用的提示词"),
        sa.Column("media_type", sa.Text(), nullable=False, server_default=sa.text("'image'"),
                  comment="image 或 video"),
        sa.Column("media_url", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="主素材地址"),
        sa.Column("cover_url", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="封面地址"),
        sa.Column("target", sa.Text(), nullable=False, server_default=sa.text("'nb2'"),
                  comment="目标模型键"),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'"),
                  comment="draft/published/archived"),
        sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.text("false"),
                  comment="是否精选（前台优先展示）"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("100"),
                  comment="人工排序，数字小的在前"),
        sa.Column("source_platform", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="素材来源平台"),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="素材原始链接"),
        sa.Column("rights_status", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="版权状态"),
        sa.Column("rights_note", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="版权备注"),
        sa.Column("created_by", sa.Text(), nullable=False, comment="创建人"),
        sa.Column("updated_by", sa.Text(), nullable=False, comment="最后修改人"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致）"),
        sa.Column("published_at", sa.BigInteger(),
                  comment="发布时间，秒级 Unix 时间戳；未发布为空"),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default=sa.text("0"),
                  comment="曝光次数"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default=sa.text("0"),
                  comment="点击次数"),
        schema="ops",
    )
    # 前台列表固定按 (精选优先, 人工排序, 发布时间倒序) 取数，源库也是降序索引。
    op.execute(
        sa.text(
            "CREATE INDEX idx_admin_inspiration_cases_public "
            "ON ops.admin_inspiration_cases"
            "(status, featured DESC, sort_order, published_at DESC)"
        )
    )

    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_provider_api_keys IS "
            "'托管视频渠道 API 密钥池：content 派单前取用、admin 增删查。"
            "ciphertext/nonce 为加密列，任何日志、报表、核对输出都不得回显。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_channel_config IS "
            "'渠道接单开关与参数：admin 写入，content 接单前读取。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_audit IS "
            "'管理操作审计台账：后台任何敏感动作（渠道、开关、价格、密钥）都追加一行，只增不改。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_e2e_runs IS "
            "'后台全流程验收台账（内部质检用，可能扣点）：切写未完成前权威仍是 SQLite。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_e2e_fixture_attempts IS "
            "'验收夹具尝试记录：同一夹具按天限制，防止重复准备造成重复扣点。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_e2e_delivery_checks IS "
            "'验收成片可下载/可解码检查缓存：按任务编号一行。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.admin_inspiration_cases IS "
            "'灵感案例库（前台展示 + 后台维护）：业务模块仍在 SQLite，切写另批。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
