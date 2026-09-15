"""Move agent metrics (conversation events + trajectory runs) into the ops schema.

Revision ID: 20260915_0010
Revises: 20260915_0009

Source of truth for these two tables is the SQLite file
``/home/ubuntu/agent-metrics/metrics.db`` (tables ``events`` and ``runs``).
**Its writers live on the server, not in this repository**: the cron scripts
``collect.py`` / ``collect_traj.py`` under ``/home/ubuntu/agent-metrics/`` insert
into that file, and ``board.py`` / ``export_json.py`` read it. The patched
drafts of those four scripts plus the ``metrics_store.py`` dispatch module are
delivered under ``server_side_patches/agent-metrics/``.

This migration only creates the PostgreSQL tables; the backfill and the
read/write cutover are separate steps (see ``scripts/migrate_ops_metrics.py``
and ``docs/runbooks/postgresql-ops-metrics.md``).

Column parity with the SQLite source is deliberate:

* the source declares no ``NOT NULL`` beyond the primary keys, so neither do
  these tables. The empty string is a meaningful value in this data
  (``''`` = 该事件没有这个字段), it is never a NULL substitute; only
  ``events.replies`` is genuinely NULL for non-``complete`` rows;
* ``ts`` is TEXT in the source and stays text here: events carry
  ``2026-09-15T23:00:08.108+08:00`` (local offset) while runs carry
  ``2026-09-15T15:00:06.273Z`` (UTC), and the dashboards slice the string as
  ``ts[:16]``. Converting to ``timestamptz`` would change both the displayed
  value and the comparison semantics, so the 口径 is kept as-is;
* ``detail`` (error excerpt, truncated to 140 chars by the collector) and
  ``session_key`` (composite ``agent:…:cron:…:run:…`` key) are unbounded text
  in the source and stay TEXT here;
* integer token/count columns are 32-bit in the source (``INT``); ``BigInteger``
  is used here so a long-running install cannot overflow mid-migration;
* the source has no secondary index, and every read is an aggregate over the
  whole table (929 events / 768 runs at cutover time). No index is created;
  revisit if these tables ever pass ~100k rows.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0010"
down_revision = "20260915_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics_events",
        sa.Column("hash", sa.String(64), primary_key=True,
                  comment="事件指纹 md5(实例|日志原文)，也是去重键：同一行日志重复采集时按主键忽略"),
        sa.Column("ts", sa.String(64),
                  comment="事件时间，ISO-8601 文本（含毫秒与 +08:00 偏移，如 2026-09-15T23:00:08.108+08:00）；"
                          "极少数行为空串。与 SQLite 源同为文本口径，看板按前 16 字符截断展示"),
        sa.Column("instance", sa.String(64),
                  comment="来源 systemd 用户单元：openclaw-gateway / openclaw-second / openclaw-visual"),
        sa.Column("capability", sa.String(32),
                  comment="能力组中文名：获客 / 文案 / 图片"),
        sa.Column("agent", sa.String(64),
                  comment="飞书子 agent 名（日志里 feishu[xxx]）；无法归属时为空串"),
        sa.Column("client", sa.String(128),
                  comment="客户标识：群聊里是发送者 id，私聊是对方 id；无法识别时为空串"),
        sa.Column("grp", sa.String(128),
                  comment="群标识 oc_…；私聊或无法识别时为空串"),
        sa.Column("session", sa.String(256),
                  comment="会话键 agent:<名>:group:<群id> 或 agent:<名>:direct:<客户id>；无法识别时为空串"),
        sa.Column("kind", sa.String(32),
                  comment="事件类型：received=收到消息，dispatch=派发子 agent，complete=派发完成，error=报错"),
        sa.Column("replies", sa.Integer(),
                  comment="回复条数，只有 kind=complete 有值；received/dispatch/error 为 NULL（源即如此）"),
        sa.Column("detail", sa.Text(),
                  comment="报错摘要：命中余额不足/异常等关键词的日志行尾 140 字符；非报错行为空串"),
        schema="ops",
    )
    op.create_table(
        "metrics_runs",
        sa.Column("run_key", sa.String(64), primary_key=True,
                  comment="运行指纹 md5(轨迹文件路径|runId)，也是去重键：同一 run 重复采集时按主键忽略"),
        sa.Column("ts", sa.String(64),
                  comment="会话开始时间，ISO-8601 文本（UTC，以 Z 结尾，如 2026-09-15T15:00:06.273Z）；"
                          "与 SQLite 源同为文本口径"),
        sa.Column("instance", sa.String(64),
                  comment="来源实例目录：.openclaw / .openclaw-second / .openclaw-visual"),
        sa.Column("capability", sa.String(32),
                  comment="能力组中文名：获客 / 文案 / 图片"),
        sa.Column("agent", sa.String(64),
                  comment="子 agent 名；取自 sessionKey，取不到时回落到轨迹里的 agentId"),
        sa.Column("chat_id", sa.String(128),
                  comment="客户标识（sessionKey 里的 oc_/ou_ id）；定时任务等无客户时为空串"),
        sa.Column("session_key", sa.Text(),
                  comment="复合会话键，形如 agent:main:cron:<uuid>:run:<uuid>；用于回溯是哪次运行"),
        sa.Column("provider", sa.String(64),
                  comment="模型供应方，如 deepseek / openai"),
        sa.Column("model", sa.String(64),
                  comment="模型名，如 deepseek-v4-pro；成本账本按本列乘 model_prices.json 单价"),
        sa.Column("tok_in", sa.BigInteger(),
                  comment="输入 token 合计；轨迹缺失时为 0"),
        sa.Column("tok_out", sa.BigInteger(),
                  comment="输出 token 合计；轨迹缺失时为 0"),
        sa.Column("tok_total", sa.BigInteger(),
                  comment="总 token 合计；轨迹缺失时为 0"),
        sa.Column("cache_read", sa.BigInteger(),
                  comment="命中缓存的输入 token 合计；轨迹缺失时为 0"),
        sa.Column("status", sa.String(32),
                  comment="运行收尾状态：success=正常结束，error=报错结束"),
        sa.Column("replied", sa.Integer(),
                  comment="是否产生过回复，0/1；不是布尔列，保持与源的 0/1 口径一致"),
        sa.Column("model_calls", sa.Integer(),
                  comment="本次运行模型调用次数"),
        sa.Column("trigger_", sa.String(32),
                  comment="触发来源：user=用户消息，cron=定时任务，空串=轨迹未记录（列名与源一致，带下划线）"),
        sa.Column("msg_provider", sa.String(32),
                  comment="消息通道：feishu / webchat / 空串；统计回复率时按 feishu 过滤"),
        schema="ops",
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.metrics_events IS "
            "'AI 会话事件流水：服务器 cron（每 10 分钟）解析 OpenClaw 三实例 journald 日志写入，"
            "看板按能力组/子 agent/客户统计接收与回复数、报错 Top。"
            "主键 hash 为内容指纹，重复采集按主键忽略，行只增不改。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.metrics_runs IS "
            "'AI 运行轨迹与成本账本：每个 run 一行（session.started→session.ended），"
            "记 token 用量与收尾状态，配合单价表算各能力组/客户成本。"
            "主键 run_key 为内容指纹，重复采集按主键忽略，行只增不改。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
