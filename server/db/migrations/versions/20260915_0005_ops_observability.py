"""Move runtime observation evidence and the alert outbox into the ops schema.

Revision ID: 20260915_0005
Revises: 20260915_0004

Source of truth for these tables is currently the SQLite file
``content-api/runtime_observability.db`` (``task_trace`` / ``alert_outbox``,
authored by ``server/content_domains/runtime_observability.py``). This migration
only creates the PostgreSQL tables; the backfill and the read/write cutover are
separate steps (see ``scripts/migrate_ops_observability.py`` and the M3B
runbook).

Column parity with the SQLite source is deliberate:

* the source declares no ``NOT NULL`` beyond the primary keys, so neither do
  these tables (``DEFAULT`` does not imply ``NOT NULL`` in SQLite);
* ``started`` / ``updated`` / ``duration`` / ``next_try`` are ``REAL`` in the
  source, i.e. seconds-level Unix timestamps such as ``1758000000.123456`` —
  mapped to ``double precision`` so no timestamptz implicit conversion shows up;
* the trace metadata and outbox payload/error are unbounded TEXT in the source,
  so they stay TEXT here (never VARCHAR);
* the source has no secondary index (dispatch scans a small pending outbox), so
  no extra index is created — revisit if the outbox grows.

One deliberate asymmetry: a SQLite rowid table whose PRIMARY KEY is not an
INTEGER column may still hold NULL keys, so the source tolerates a NULL
``job_id``/``stage``/``event_id``; a PostgreSQL primary key cannot. The runtime
module never writes a null key (``record`` returns early when ``job_id`` is
falsy) and the backfill refuses to import rows with empty keys, so no existing
row can hit this.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260915_0005"
down_revision = "20260915_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traces",
        sa.Column("job_id", sa.String(128), primary_key=True,
                  comment="任务编号；与 jobs 域同一标识。空编号的任务不记证据（源模块直接跳过）"),
        sa.Column("stage", sa.String(64), primary_key=True,
                  comment="阶段名，如 route/provider_submit/provider_accepted/provider_query/"
                          "download/generation/generation_resume/artifact/delivery"),
        sa.Column("state", sa.String(24),
                  comment="阶段结论：running=进行中，recorded=已记录，failed=失败，"
                          "unknown=提交结果不可判定（禁止当成失败重试依据），passed=产物通过校验"),
        sa.Column("started", sa.Double(),
                  comment="阶段开始时间，秒级 Unix 时间戳（带小数），与 SQLite 源 REAL 口径一致"),
        sa.Column("updated", sa.Double(),
                  comment="阶段最后更新时间，秒级 Unix 时间戳（带小数）；同一 (job_id,stage) "
                          "重写只更新本列与 state/duration/metadata，started 保持首次值"),
        sa.Column("duration", sa.Double(),
                  comment="阶段耗时秒数，取单调时钟差值；进行中或未提供时为空"),
        sa.Column("metadata", sa.Text(),
                  comment="脱敏元数据 JSON：只允许 provider/model/host/transport/"
                          "provider_task_id/error_type，值截断 160 字符，绝不含密钥、用户数据或产物内容"),
        schema="ops",
    )
    op.create_table(
        "alert_outbox",
        sa.Column("event_id", sa.String(255), primary_key=True,
                  comment="事件唯一键 action:service:occurred_at；重复入队按主键忽略，保证同一事件只投一次"),
        sa.Column("payload", sa.Text(),
                  comment="外发通知正文 JSON，只有 event/service/occurred_at 三个字段"),
        sa.Column("state", sa.String(24), server_default=sa.text("'pending'"),
                  comment="投递状态：pending=待投递，sent=已送达，failed=重试 5 次仍未送达（需人工看）"),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"),
                  comment="已尝试投递次数，每投一次加一"),
        sa.Column("next_try", sa.Double(), server_default=sa.text("0"),
                  comment="下次可投递时间，秒级 Unix 时间戳；失败后退避 60*2^attempts 秒、上限 1 小时"),
        sa.Column("updated", sa.Double(),
                  comment="最后一次入队或投递尝试时间，秒级 Unix 时间戳；待投递按本列升序处理"),
        sa.Column("error", sa.Text(), server_default=sa.text("''"),
                  comment="最后一次失败原因，只记异常类名，不含响应正文与密钥"),
        schema="ops",
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.traces IS "
            "'任务运行证据：内容服务、管理后台、受影响渠道执行器在关键阶段写入，"
            "后台任务详情与排障页面读取。(job_id,stage) 唯一，重复阶段按覆盖更新。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE ops.alert_outbox IS "
            "'健康事件通知发件箱：渠道异常与监控事件先落库再投递，投递失败的记录留库等待重试，"
            "因此告警不因进程重启而丢失。'"
        )
    )


def downgrade() -> None:
    raise RuntimeError("Destructive PostgreSQL schema downgrade is not supported; restore a backup")
