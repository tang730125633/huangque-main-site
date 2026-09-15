"""Move the versioned channel routing tables into the routing schema.

Revision ID: 20260915_0006
Revises: 20260915_0005

Source of truth for these eleven tables is currently the SQLite file
``content-api/channel_management.db``, created by
``server/content_domains/channel_manager.py`` (``db()``). This migration only
creates the PostgreSQL tables; the backfill and the read/write cutover are
separate steps (see ``scripts/migrate_routing_channels.py`` and the M3C
runbook).

Column parity with the SQLite source is deliberate:

* the source declares no ``NOT NULL`` beyond the primary keys, so neither do
  these tables (``DEFAULT`` does not imply ``NOT NULL`` in SQLite);
* ``started`` / ``updated`` / ``duration`` / ``created`` / ``occurred`` /
  ``light_due`` / ``full_due`` / ``reservation`` are ``REAL`` in the source,
  i.e. seconds-level Unix timestamps such as ``1758000000.123456`` — mapped to
  ``double precision`` so no timestamptz implicit conversion shows up;
* ``channels.enabled`` is the only 0/1 column with boolean meaning in the
  source and becomes ``boolean`` (writers and the backfill normalise with
  ``bool()``);
* every TEXT column stays TEXT (never VARCHAR) because the source is unbounded;
* the source has no foreign keys at all, so none are invented here — only the
  primary keys and the five secondary indexes that already exist in SQLite are
  recreated;
* table and column names are already full words, so they are kept verbatim.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0006"
down_revision = "20260915_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="渠道编号：管理员可见的稳定标识（1～64 位字母数字下划线连字符），"
                          "工单、日志与运行记录都用它指代渠道"),
        sa.Column("version", sa.BigInteger(),
                  comment="当前生效版本号，从 1 起单调递增；与 routing.versions.version 配对"),
        sa.Column("enabled", sa.Boolean(),
                  comment="是否放行接单/测试：true=启用；源 SQLite 为 INTEGER 0/1，"
                          "读写两侧一律按真假归一化"),
        schema="routing",
    )
    op.create_table(
        "versions",
        sa.Column("channel", sa.Text(),
                  comment="所属渠道编号，指向 routing.channels.id"),
        sa.Column("version", sa.BigInteger(),
                  comment="版本号；与 channel 组成主键，历史版本只增不删"),
        sa.Column("config", sa.Text(),
                  comment="渠道配置 JSON：name/adapter/model/base_url/supplier/connection_type/"
                          "proxy/timeout/concurrency/queue_limit/rpm/poll_seconds/daily_hour/"
                          "daily_limit/test_cost/daily_budget/monitor/daily_test/fixture/"
                          "parameters/_lifecycle。含上游地址与代理商等运营敏感信息，不得整段进日志"),
        sa.Column("secret", sa.Text(),
                  comment="渠道 API 密钥密文（AES-GCM，AAD=hq-channel-v1，主密钥来自 "
                          "provider_keys._master_key）。绝不出现在日志、接口响应、回填报告或本迁移的 "
                          "审计 details 中；迁移/只读排查一律不得 SELECT 本列"),
        sa.Column("actor", sa.Text(),
                  comment="该版本的提交人（管理员用户名或自动化标识）"),
        sa.Column("created", sa.Double(),
                  comment="该版本创建时间，秒级 Unix 时间戳（带小数），与 SQLite 源 REAL 口径一致"),
        sa.PrimaryKeyConstraint("channel", "version"),
        schema="routing",
    )
    op.create_table(
        "mappings",
        sa.Column("selector", sa.Text(), primary_key=True,
                  comment="旧线路选择器，形如 kind:front（如 image:front-model）；前台按它找渠道"),
        sa.Column("config", sa.Text(),
                  comment="映射 JSON：kind/front/label/channel/backup/enabled"),
        sa.Column("actor", sa.Text(),
                  comment="最后修改人（管理员用户名或自动化标识）"),
        sa.Column("updated", sa.Double(),
                  comment="最后修改时间，秒级 Unix 时间戳（带小数），与 SQLite 源 REAL 口径一致"),
        schema="routing",
    )
    op.create_table(
        "operation_mappings",
        sa.Column("operation_id", sa.Text(), primary_key=True,
                  comment="功能编号（function_registry 的 operation_id），如 image.gpt.nb2"),
        sa.Column("revision", sa.BigInteger(),
                  comment="当前映射版本号，从 1 起单调递增；乐观锁按它比对"),
        sa.Column("state", sa.Text(),
                  comment="映射状态：legacy=走旧线路，shadow=影子观察，managed=托管接单，paused=管理员暂停"),
        sa.Column("config", sa.Text(),
                  comment="映射 JSON：kind/label/channel/backup（托管接单只认这份身份，不含密钥）"),
        sa.Column("actor", sa.Text(),
                  comment="发布人（管理员用户名）"),
        sa.Column("updated", sa.Double(),
                  comment="发布时间，秒级 Unix 时间戳（带小数），与 SQLite 源 REAL 口径一致"),
        schema="routing",
    )
    op.create_table(
        "operation_mapping_versions",
        sa.Column("operation_id", sa.Text(),
                  comment="功能编号，指向 routing.operation_mappings.operation_id"),
        sa.Column("revision", sa.BigInteger(),
                  comment="历史版本号；与 operation_id 组成主键，只增不删（回滚与审计依据）"),
        sa.Column("state", sa.Text(),
                  comment="该次的映射状态：legacy / shadow / managed / paused"),
        sa.Column("config", sa.Text(),
                  comment="该次的映射 JSON（不可变快照）"),
        sa.Column("actor", sa.Text(),
                  comment="该次的发布人（管理员用户名）"),
        sa.Column("created", sa.Double(),
                  comment="该版本的发布时间，秒级 Unix 时间戳（带小数），"
                          "读取时按 created 升序展示历史"),
        sa.PrimaryKeyConstraint("operation_id", "revision"),
        schema="routing",
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="调用记录编号（32 位随机十六进制）；影子观察用它承载 observation_id"),
        sa.Column("channel", sa.Text(),
                  comment="渠道编号，指向 routing.channels.id；影子/历史行可能指向已删除渠道"),
        sa.Column("version", sa.BigInteger(),
                  comment="该次调用使用的渠道版本号；与 channel 一起冻结当时配置"),
        sa.Column("kind", sa.Text(),
                  comment="调用类型：connection=连接探测，auth=鉴权探测，full=完整生成测试，"
                          "task=真实用户任务，shadow=影子观察（未调用供应商）"),
        sa.Column("state", sa.Text(),
                  comment="结果：queued/running/passed/failed/unknown/blocked/terminated/captured；"
                          "unknown 表示提交结果不可判定，禁止当失败重试依据"),
        sa.Column("started", sa.Double(),
                  comment="开始时间，秒级 Unix 时间戳（带小数），与 SQLite 源 REAL 口径一致"),
        sa.Column("updated", sa.Double(),
                  comment="最后更新时间，秒级 Unix 时间戳（带小数）；健康判定按它算 24 小时有效期"),
        sa.Column("duration", sa.Double(),
                  comment="耗时秒数（updated-started），未结束时为空"),
        sa.Column("detail", sa.Text(),
                  comment="进展/结论说明，写入前截断到 300 字；密钥在写前即被替换为 [隐藏]"),
        sa.Column("job_id", sa.Text(),
                  comment="关联任务编号（真实任务与影子观察必填；渠道自检为空）"),
        sa.Column("provider_id", sa.Text(),
                  comment="供应商返回的任务编号，用于对账；不是密钥，但也不进用户可见文案"),
        sa.Column("reservation", sa.Double(), server_default=sa.text("0"),
                  comment="预约占用的测试预算（full 测试写 test_cost，其余为 0）；"
                          "源 SQLite 为 REAL DEFAULT 0 且可空，此处保持可空"),
        schema="routing",
    )
    op.create_table(
        "run_snapshots",
        sa.Column("run_id", sa.Text(), primary_key=True,
                  comment="调用记录编号，指向 routing.runs.id（一条运行最多一份快照）"),
        sa.Column("operation_id", sa.Text(),
                  comment="冻结的功能编号；验收时用于证明「用的就是当时那版映射」"),
        sa.Column("mapping_revision", sa.BigInteger(),
                  comment="冻结的功能映射版本号，与 acceptance 比对，不一致即拒绝受理"),
        sa.Column("invocation_source", sa.Text(),
                  comment="调用来源：web/agent/admin_e2e/internal"),
        sa.Column("snapshot", sa.Text(),
                  comment="脱敏后的执行快照 JSON（渠道身份与映射身份），不含密钥、"
                          "不含上游响应、不含用户数据"),
        schema="routing",
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="事件编号（32 位随机十六进制）"),
        sa.Column("action", sa.Text(),
                  comment="动作名：channel.save/channel.enable/channel.disable/channel.delete/"
                          "channel.restore/mapping.save/mapping.delete/operation-mapping.publish/"
                          "parameters.*/legacy.*/layout.save/notification.save/runtime.dispatch"),
        sa.Column("target", sa.Text(),
                  comment="动作对象：渠道编号、映射选择器或功能编号；runtime.dispatch 时是渠道编号（限流按它统计）"),
        sa.Column("actor", sa.Text(),
                  comment="操作人（管理员用户名、runtime、scheduler）"),
        sa.Column("created", sa.Double(),
                  comment="发生时间，秒级 Unix 时间戳（带小数）；运行器按 (action,target) 统计最近 60 秒"),
        schema="routing",
    )
    op.create_table(
        "schedule",
        sa.Column("channel", sa.Text(), primary_key=True,
                  comment="渠道编号，指向 routing.channels.id；停用或在回收站时删除本行"),
        sa.Column("light_due", sa.Double(),
                  comment="下次轻量自检（连接探测）到点时间，秒级 Unix 时间戳（带小数）"),
        sa.Column("full_due", sa.Double(),
                  comment="下次完整生成测试到点时间，秒级 Unix 时间戳（带小数）；"
                          "按 Asia/Shanghai 的 daily_hour 计算"),
        schema="routing",
    )
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True,
                  comment="设置槽位，代码里是常量：1=渠道告警通知，2=内置供应商启停，"
                          "3=参数草稿，4=工作台布局"),
        sa.Column("value", sa.Text(),
                  comment="设置值 JSON 文本。id=1 的 endpoint 字段是密文（与 versions.secret 同一密钥），"
                          "同样不得进日志；id=2/3/4 为运营数据"),
        schema="routing",
    )
    op.create_table(
        "channel_incidents",
        sa.Column("channel", sa.Text(),
                  comment="渠道编号 + kind 组成主键：同一渠道同一检查类型只保留最近一次状态"),
        sa.Column("kind", sa.Text(),
                  comment="检查类型：connection/auth/full/task"),
        sa.Column("state", sa.Text(),
                  comment="最近一次状态：passed/failed/unknown/blocked 等，与 routing.runs.state 同口径"),
        sa.Column("action", sa.Text(),
                  comment="已通知的事件名（channel.failed/channel.unknown/channel.recovered），"
                          "空串表示本次无需通知；用于避免重复告警"),
        sa.Column("occurred", sa.Double(),
                  comment="该状态的发生时间，秒级 Unix 时间戳（带小数）"),
        sa.PrimaryKeyConstraint("channel", "kind"),
        schema="routing",
    )

    # 与 SQLite 源逐一对应的二级索引（名称保持不变，便于两侧对账）。
    op.create_index("channel_runs_recent", "runs", ["channel", "kind", "started"], schema="routing")
    op.create_index("channel_runs_job", "runs", ["job_id", "kind"], schema="routing")
    op.create_index("channel_runs_state", "runs", ["state", "started"], schema="routing")
    op.create_index("channel_events_rate", "events", ["action", "target", "created"], schema="routing")
    # 源索引 operation_mapping_history 带 DESC，SQLAlchemy 的 op.create_index 不便表达，
    # 这里按源 DDL 原样创建（PG 索引名只需在同一 schema 内唯一）。
    op.execute(sa.text(
        "CREATE INDEX operation_mapping_history "
        "ON routing.operation_mapping_versions (operation_id, revision DESC)"
    ))

    op.execute(sa.text(
        "COMMENT ON TABLE routing.channels IS "
        "'渠道主表：每个渠道一行，指向 routing.versions 的当前生效版本。"
        "后台渠道列表、映射校验与接单捕获都会读；admin 与 content 两个服务写。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.versions IS "
        "'渠道版本历史（不可变）：每次保存、启停、删除或改参数都新增一版并保留密钥密文，"
        "回滚按旧版本重放；读取方只有 admin 与 content 的渠道模块。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.mappings IS "
        "'旧线路映射：前台标识（kind:front）→ 主/备渠道，供尚未发布到新控制面的功能使用；"
        "前台按它选渠道，后台按它做兼容校验，admin 写、content 读。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.operation_mappings IS "
        "'功能映射当前版本：按 operation_id 指向主/备渠道与状态（legacy/shadow/managed/paused）。"
        "接单捕获与报价都按它决定走哪条线路，admin 写、content 读。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.operation_mapping_versions IS "
        "'功能映射历史（不可变）：每次发布新增一版，用于回滚到历史映射与审计。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.runs IS "
        "'渠道调用记录：连接/鉴权/完整测试/真实任务/影子观察共用一张表。"
        "渠道健康、日预算与次数、并发与限流判定都以它为准；执行器与调度器写，后台与任务详情读。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.run_snapshots IS "
        "'任务执行快照：托管渠道任务落库时冻结的映射/渠道身份，"
        "验收时用来证明任务用的就是当时那版渠道与映射（不一致即拒绝受理）。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.events IS "
        "'渠道操作审计与调度限流流水：保存渠道、发布映射、参数发布、调度派发都写一行；"
        "限流窗口按 (action,target,created) 统计，后台只读最近 30 条。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.schedule IS "
        "'渠道自检排期：下次轻量与完整测试的到点时间；调度器按它触发并回写，"
        "渠道启用时重建、停用时删除。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.settings IS "
        "'渠道域单行设置：id=1 告警通知（endpoint 为密文）、id=2 内置供应商启停、"
        "id=3 参数草稿、id=4 工作台布局。'"
    ))
    op.execute(sa.text(
        "COMMENT ON TABLE routing.channel_incidents IS "
        "'渠道故障/恢复态：每个 (渠道,检查类型) 一行，记录最近一次状态与已通知事件，"
        "避免重复告警；状态变好时通知 recovered。'"
    ))


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
