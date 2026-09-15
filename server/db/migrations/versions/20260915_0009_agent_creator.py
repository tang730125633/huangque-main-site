"""Move the independent Creator Agent workspace into the agent schema.

Revision ID: 20260915_0009
Revises: 20260915_0008

Source of truth for these six tables is currently the SQLite file
``/var/lib/huangque-creator-agent/creator_agent.db`` (the Creator Agent service,
systemd unit ``huangque-creator-agent.service``, runtime
``/opt/huangque/creator-agent/current``). Five tables are authored by
``server/creator_agent/store.py`` (``creator_account_state`` /
``creator_workspaces`` / ``creator_messages`` / ``creator_batches`` /
``creator_jobs``), the sixth by ``server/creator_agent/model_usage.py``
(``creator_model_calls``). This migration only creates the PostgreSQL tables;
the backfill and the read/write cutover are separate steps (see
``scripts/migrate_agent_creator.py`` and the M3G runbook).

Column parity with the SQLite source is deliberate:

* every ``id`` / ``*_at`` / ``*_expires_at`` / ``*_cost`` column is an integer
  epoch second in the source, so it stays ``BIGINT`` here (never ``timestamptz``,
  which would introduce an implicit time-zone conversion);
* JSON blobs (``*_json``) are unbounded ``TEXT`` in the source (the writers
  serialise with ``ensure_ascii=False, separators=(",",":"), sort_keys=True``),
  so they stay ``TEXT`` here (never ``JSONB``) — the digest of the exact text is
  part of the runtime contract (``plan_hash`` / ``input_hash``);
* the source declares ``DEFAULT ''`` / ``DEFAULT '{}'`` without ``NOT NULL``
  only where noted; everywhere else ``NOT NULL`` mirrors the source;
* ``creator_batches.insert_seq`` has **no** SQLite counterpart column: it is the
  PostgreSQL stand-in for SQLite's implicit ``rowid``, which the source uses as
  the tie-breaker of ``ORDER BY created_at DESC, rowid DESC`` (see
  ``CreatorAgentStore.batches`` / ``latest_batch``). Without it two batches
  created in the same second would come back in arbitrary order. The backfill
  copies the source ``rowid``, the runtime keeps it monotonic per
  (username, project_id).
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0009"
down_revision = "20260915_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "creator_account_state",
        sa.Column("username", sa.Text(), primary_key=True,
                  comment="登录用户名；一个账号一行"),
        sa.Column("active_project_id", sa.Text(), nullable=False,
                  server_default=sa.text("''"),
                  comment="当前选中的创作项目编号；空串表示尚未选择"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳（与 SQLite 源口径一致，不做时区换算）"),
        schema="agent",
    )
    op.create_table(
        "creator_workspaces",
        sa.Column("username", sa.Text(), primary_key=True,
                  comment="登录用户名，与 project_id 组成主键"),
        sa.Column("project_id", sa.Text(), primary_key=True,
                  comment="创作项目编号（12 位十六进制），一个用户可有多份项目"),
        sa.Column("alias", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="项目别名，写入前截断到 120 字符"),
        sa.Column("platforms_json", sa.Text(), nullable=False, server_default=sa.text("'[]'"),
                  comment="已选发布平台 JSON 数组（TEXT，非 JSONB：校验和依赖原始文本）"),
        sa.Column("preferences_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="模板视频偏好 JSON 对象，读回时补 global/platforms 两个默认键"),
        sa.Column("profile_overrides_json", sa.Text(), nullable=False,
                  server_default=sa.text("'{}'"),
                  comment="人工改写的人设覆盖项 JSON 对象，优先级高于模型产出"),
        sa.Column("profile_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="人设四模块终稿 JSON 对象"),
        sa.Column("profile_state_json", sa.Text(), nullable=False,
                  server_default=sa.text("'{}'"),
                  comment="人设问答流程状态 JSON（含 revision），并发保护按该 revision 比对"),
        sa.Column("deliverables_json", sa.Text(), nullable=False,
                  server_default=sa.text("'{}'"),
                  comment="交付物汇总 JSON 对象"),
        sa.Column("flow_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="引导流程进度 JSON 对象，前端据此续跑问答"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳；列表按 (username, updated_at DESC) 取"),
        schema="agent",
    )
    op.create_index(
        "idx_creator_workspaces_user",
        "creator_workspaces",
        ["username", sa.text("updated_at DESC")],
        schema="agent",
    )
    op.create_table(
        "creator_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True,
                  comment="消息自增编号（SQLite AUTOINCREMENT 的等价物）；批次用 source_message_id 引用它"),
        sa.Column("username", sa.Text(), nullable=False, comment="登录用户名"),
        sa.Column("project_id", sa.Text(), nullable=False, comment="创作项目编号"),
        sa.Column("role", sa.Text(), nullable=False,
                  comment="消息角色，只取 user/assistant"),
        sa.Column("content", sa.Text(), nullable=False,
                  comment="消息正文；assistant 回复写入前截断到 8000 字符"),
        sa.Column("source_key", sa.Text(),
                  comment="业务幂等键（如 message-turn:123 / profile-turn:123）；空值不参与去重"),
        sa.Column("request_id", sa.Text(),
                  comment="客户端请求编号，用于重放同一次请求；空值不参与去重"),
        sa.Column("request_hash", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="请求指纹；同 request_id 不同指纹视为冲突（IdempotencyConflict）"),
        sa.Column("public_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="给前端展示的结构化负载 JSON；用户消息回填 turn 后即视为已应答"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳；会话按 id 升序回放"),
        sa.ForeignKeyConstraint(
            ["username", "project_id"],
            ["agent.creator_workspaces.username", "agent.creator_workspaces.project_id"],
            ondelete="CASCADE",
            name="fk_agent_creator_messages_workspace",
        ),
        sa.UniqueConstraint("username", "project_id", "source_key",
                            name="uq_agent_creator_messages_source_key"),
        sa.UniqueConstraint("username", "project_id", "request_id",
                            name="uq_agent_creator_messages_request_id"),
        schema="agent",
    )
    op.create_index(
        "idx_creator_messages_project",
        "creator_messages",
        ["username", "project_id", "id"],
        schema="agent",
    )
    op.create_table(
        "creator_batches",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="批次编号，形如 creator_batch_<uuid4 hex>"),
        sa.Column("username", sa.Text(), nullable=False, comment="登录用户名"),
        sa.Column("project_id", sa.Text(), nullable=False, comment="创作项目编号"),
        sa.Column("insert_seq", sa.BigInteger(), nullable=False,
                  comment="插入序号（SQLite rowid 的等价物）：回填时照抄源 rowid，"
                          "运行时取同用户同项目当前最大值 +1；只用于 created_at 相同时的稳定排序"),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="触发本批次的用户消息编号；大于 0 时同一消息只能建一个批次（部分唯一索引）"),
        sa.Column("last_mutation_message_id", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="最后一次改动本批次的用户消息编号，用于重放幂等"),
        sa.Column("topic", sa.Text(), nullable=False, comment="批次主题（用户口述的目标）"),
        sa.Column("goal", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="批次补充目标说明，可为空串"),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="批次状态：draft/ready/quoting/quoted/submitting/submitted/running/"
                          "done/partial/failed；终态由子任务状态推导"),
        sa.Column("plan_json", sa.Text(), nullable=False,
                  comment="各平台生产计划 JSON 数组；plan_hash 为它的规范化摘要"),
        sa.Column("quote_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="冻结的报价 JSON（含最早过期时间），确认下单前必须与当前 revision 一致"),
        sa.Column("confirmation_id", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="确认单编号；非空表示该批次已下单（公开视图不返回该列）"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1"),
                  comment="批次版本号：每次计划改动 +1，报价/确认都按它做并发保护"),
        sa.Column("plan_hash", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="计划内容摘要（规范化 JSON 的 SHA-256），与 plan_json 不一致即拒绝报价"),
        sa.Column("quoted_revision", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="报价对应的批次 revision；与 revision 不等即报价失效"),
        sa.Column("quote_expires_at", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="报价最早过期时刻，秒级 Unix 时间戳"),
        sa.Column("claim_id", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="报价认领编号：非空表示有进程正在报价，超时（120 秒）后可被抢占"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳"),
        sa.ForeignKeyConstraint(
            ["username", "project_id"],
            ["agent.creator_workspaces.username", "agent.creator_workspaces.project_id"],
            ondelete="CASCADE",
            name="fk_agent_creator_batches_workspace",
        ),
        schema="agent",
    )
    op.create_index(
        "idx_creator_batches_project",
        "creator_batches",
        ["username", "project_id", sa.text("created_at DESC")],
        schema="agent",
    )
    op.create_index(
        "idx_creator_batches_source_message",
        "creator_batches",
        ["username", "project_id", "source_message_id"],
        unique=True,
        schema="agent",
        postgresql_where=sa.text("source_message_id > 0"),
    )
    op.create_index(
        "idx_creator_batches_mutation_message",
        "creator_batches",
        ["username", "project_id", "last_mutation_message_id"],
        schema="agent",
    )
    op.create_table(
        "creator_jobs",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="子任务编号，形如 creator_job_<uuid4 hex>"),
        sa.Column("batch_id", sa.Text(), nullable=False, comment="所属批次编号"),
        sa.Column("username", sa.Text(), nullable=False, comment="登录用户名"),
        sa.Column("project_id", sa.Text(), nullable=False, comment="创作项目编号"),
        sa.Column("platform", sa.Text(), nullable=False,
                  comment="目标平台键（与代码 ALLOWED_PLATFORMS 一致）"),
        sa.Column("version", sa.BigInteger(), nullable=False,
                  comment="同用户同项目同平台的第几次生产；跳过草稿/已报价/提交失败的历史任务"),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="子任务状态：draft/ready/quoted/submit_claimed/submitted/queued/running/"
                          "verifying/processing/done/error/failed/refunded/submission_unknown 等"),
        sa.Column("input_json", sa.Text(), nullable=False,
                  comment="提交给生产服务的入参 JSON；input_hash 为它的规范化摘要"),
        sa.Column("input_hash", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="入参摘要（规范化 JSON 的 SHA-256）；报价与确认都按它校验入参未变"),
        sa.Column("quote_token", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="报价令牌，下单时必须原样带上"),
        sa.Column("quote_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="报价明细 JSON（价格、有效期等原始返回）"),
        sa.Column("quote_cost", sa.BigInteger(), nullable=False, server_default=sa.text("0"),
                  comment="本次生产报价点数；小于等于 0 视为无有效报价"),
        sa.Column("quote_expires_at", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="报价过期时刻，秒级 Unix 时间戳"),
        sa.Column("idempotency_key", sa.Text(), nullable=False,
                  comment="提交生产服务时使用的幂等键，创建子任务时即固定"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1"),
                  comment="子任务版本号：每次状态迁移 +1，所有抢占/回写都按 revision 做乐观锁"),
        sa.Column("submit_input_json", sa.Text(), nullable=False,
                  server_default=sa.text("'{}'"),
                  comment="确认下单时冻结的入参 JSON 快照；提交不确定时用它重放"),
        sa.Column("submit_input_hash", sa.Text(), nullable=False,
                  server_default=sa.text("''"),
                  comment="冻结入参摘要，用于确认后重放时校验入参未变"),
        sa.Column("submit_quote_token", sa.Text(), nullable=False,
                  server_default=sa.text("''"),
                  comment="确认下单时冻结的报价令牌"),
        sa.Column("submit_quote_cost", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="确认下单时冻结的报价点数"),
        sa.Column("submit_quote_expires_at", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="确认下单时冻结的报价过期时刻，秒级 Unix 时间戳"),
        sa.Column("submit_idempotency_key", sa.Text(), nullable=False,
                  server_default=sa.text("''"),
                  comment="确认下单时的幂等键：形如 <confirmation_id>:<platform>"),
        sa.Column("confirmation_id", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="确认单编号；与批次上的确认单同源"),
        sa.Column("job_id", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="生产服务返回的任务编号；空串表示尚未拿到回执"),
        sa.Column("result_json", sa.Text(), nullable=False, server_default=sa.text("'{}'"),
                  comment="生产结果 JSON（成片地址、封面等），失败时保持空对象"),
        sa.Column("error", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="失败原因，写入前截断到 500 字符"),
        sa.Column("refund_status", sa.Text(), nullable=False, server_default=sa.text("''"),
                  comment="退款结论（如 refunded），空串表示无需退款"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="创建时间，秒级 Unix 时间戳"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False,
                  comment="最后修改时间，秒级 Unix 时间戳；抢占超时（120 秒）据此判定"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["agent.creator_batches.id"],
            ondelete="CASCADE", name="fk_agent_creator_jobs_batch",
        ),
        sa.UniqueConstraint("batch_id", "platform",
                            name="uq_agent_creator_jobs_batch_platform"),
        schema="agent",
    )
    op.create_index(
        "idx_creator_jobs_project",
        "creator_jobs",
        ["username", "project_id", sa.text("created_at DESC")],
        schema="agent",
    )
    op.create_table(
        "creator_model_calls",
        sa.Column("id", sa.Text(), primary_key=True,
                  comment="模型调用编号，形如 creator_model_<uuid4 hex>"),
        sa.Column("username", sa.Text(), nullable=False, comment="登录用户名"),
        sa.Column("ip_hash", sa.Text(), nullable=False,
                  comment="来源 IP 的 SHA-256（不存明文 IP），用于按网络维度限流"),
        sa.Column("kind", sa.Text(), nullable=False,
                  comment="调用类型（人设问答/计划生成等），写入前截断到 80 字符"),
        sa.Column("day", sa.Text(), nullable=False,
                  comment="UTC 日期 YYYY-MM-DD，日额度按它聚合"),
        sa.Column("estimated_tokens", sa.BigInteger(), nullable=False,
                  comment="调用前预估的输入+输出 token 上限，计入日 token 预算"),
        sa.Column("estimated_cost_micro_usd", sa.BigInteger(), nullable=False,
                  comment="调用前预估费用，单位百万分之一美元，计入日金额预算"),
        sa.Column("price_version", sa.Text(), nullable=False,
                  server_default=sa.text("'legacy-unversioned'"),
                  comment="计价版本标识；历史行由守卫在首次启动时按当前价格重算并改写此列"),
        sa.Column("input_price_micro_usd_per_million", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="计价时的输入单价（百万 token 的百万分之一美元）；小于等于 0 视为待重算"),
        sa.Column("output_price_micro_usd_per_million", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0"),
                  comment="计价时的输出单价（百万 token 的百万分之一美元）；小于等于 0 视为待重算"),
        sa.Column("state", sa.Text(), nullable=False,
                  comment="调用状态：active=占用中（租约 210 秒），completed=成功，failed=失败，expired=租约超时"),
        sa.Column("created_at", sa.BigInteger(), nullable=False,
                  comment="调用发起时间，秒级 Unix 时间戳；限流窗口据此计算"),
        sa.Column("lease_until", sa.BigInteger(), nullable=False,
                  comment="活跃租约到期时刻，秒级 Unix 时间戳；到期由下一次 acquire 改判 expired"),
        sa.Column("finished_at", sa.BigInteger(), nullable=False, server_default=sa.text("0"),
                  comment="调用结束时间，秒级 Unix 时间戳；0 表示仍在进行"),
        schema="agent",
    )
    op.create_index(
        "idx_creator_model_calls_user_time",
        "creator_model_calls",
        ["username", "created_at"],
        schema="agent",
    )
    op.create_index(
        "idx_creator_model_calls_ip_time",
        "creator_model_calls",
        ["ip_hash", "created_at"],
        schema="agent",
    )
    op.create_index(
        "idx_creator_model_calls_day",
        "creator_model_calls",
        ["day", "state"],
        schema="agent",
    )
    op.create_index(
        "idx_creator_model_calls_price_version",
        "creator_model_calls",
        ["price_version"],
        schema="agent",
    )

    op.execute(
        sa.text(
            "COMMENT ON TABLE agent.creator_account_state IS "
            "'创作 Agent 账号状态：记录每个用户当前选中的项目。写者只有 creator-agent 服务。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE agent.creator_workspaces IS "
            "'创作 Agent 项目工作区：人设、偏好、交付物、引导流程状态都挂在这里；"
            "主键 (username, project_id)，消息与批次通过复合外键级联删除。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE agent.creator_messages IS "
            "'创作 Agent 会话消息：用户与助手各一行，按 id 升序回放；"
            "(username,project_id,source_key) 与 (username,project_id,request_id) "
            "是两条幂等去重键（空值不参与去重）。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE agent.creator_batches IS "
            "'创作 Agent 生产批次：一个批次 = 一次多平台生产计划，携带 revision / 报价 / "
            "确认单与认领状态；报价与确认都必须与 revision、plan_hash 完全一致。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE agent.creator_jobs IS "
            "'创作 Agent 批次下的单平台子任务：draft→quoted→submit_claimed→submitted→"
            "done/failed 的状态机载体，所有状态迁移都按 revision 做乐观锁。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE agent.creator_model_calls IS "
            "'创作 Agent 免费模型调用的资源账本：限流窗口、并发租约与日额度全部由它聚合；"
            "属于运维账本，保留 8 天，非业务数据。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
