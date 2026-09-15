# PostgreSQL M3G：创作 Agent（creator_agent）切换手册

## 边界

本批把独立创作 Agent（AI 创作助手）的**六张表**从 SQLite 迁到 PostgreSQL `agent`
schema，并接入运行时读写。它不迁移其他任何域；SQLite 文件全程保留只读，任何时刻都能
秒级回退。

- 源：`/var/lib/huangque-creator-agent/creator_agent.db`
  —— 来自 `deploy/systemd/huangque-creator-agent.service` 的
  `CREATOR_AGENT_DB`，`server/creator_agent/service.py` 的默认值同路径。
  **注意**：生产数据**不在** `/opt/huangque/creator-agent/current/`，那个目录只有代码
  与独立 venv（`current/creator_agent/`、`current/.venv/`）。
- 目标：`agent.creator_account_state`、`agent.creator_workspaces`、
  `agent.creator_messages`、`agent.creator_batches`、`agent.creator_jobs`、
  `agent.creator_model_calls`（表名保留源库的 `creator_` 前缀）。
- 时间口径与源一致：所有 `*_at` / `*_expires_at` 是秒级 Unix 时间戳（BIGINT）；
  `*_json` 保持 TEXT（`plan_hash` / `input_hash` 依赖原始文本的规范化摘要，不能换成
  JSONB）。

### 写者 / 读者清单（全仓 `sqlite3.connect` 与该库路径 grep 结果）

| 角色 | 进程 | 位置 | 说明 |
|---|---|---|---|
| **写者（唯一）** | creator-agent 服务 | `/opt/huangque/creator-agent/current/creator_agent_api.py`，127.0.0.1:8114，systemd `huangque-creator-agent.service` | 经 `server/creator_agent/store.py` 的 `CreatorAgentStore` 写前五张表 |
| **写者（同一进程）** | 同上 | `server/creator_agent/model_usage.py` 的 `ModelUsageGuard` | 复用它拿到的 `store.db` 连接工厂写 `creator_model_calls`（免费模型调用账本） |
| 读者 | 同上（HTTP API） | `GET /bootstrap`、`/messages`、`/batches/...` | 其他服务不直连该库 |
| 不涉及 | `server/hq_cli_api.py`、`server/auth_server.py`、`tools/hq-cli` | —— | 全部走 HTTP 桥（`CREATOR_AGENT_BASE`），不打开这个库 |

全仓只有一处 `sqlite3.connect` 指向该库：`server/creator_agent/store.py` 的 `db()`。
`hq_cli_api.py` / `creator_agent_api.py` 里没有任何本库路径，因此本批**没有改动它们**。

### 与 hq-ip-agent 的关系：互不相干

`agent` schema 只是命名空间，被两个独立系统共用：

- `agent.sessions` / `agent.session_aux`（M2）= **hq-ip-agent**（IP 人设定位 Agent）的
  会话 JSON 快照；
- `agent.creator_*`（M3G）= **主站 creator-agent** 的创作工作区。

两边服务不同、表不同、无外键、无数据流；同一个 PG 实例里唯一共用的是
`ops.data_migration_*` 审计表。切 creator-agent 不会碰到 IP Agent 的任何数据。

## 六张表 schema 摘要

| 表 | 主键 | 关键列 | 说明 |
|---|---|---|---|
| `creator_account_state` | `username` | `active_project_id`, `updated_at` | 每个账号当前选中的项目 |
| `creator_workspaces` | (`username`,`project_id`) | 各 `*_json`（人设/偏好/交付物/流程）, `created_at`, `updated_at` | 项目工作区；消息与批次靠复合外键级联删除 |
| `creator_messages` | `id`（BIGSERIAL） | `role`, `content`, `source_key`, `request_id`, `request_hash`, `public_json` | 会话消息；两条唯一键 (username,project_id,source_key) 与 (…,request_id) 做幂等去重 |
| `creator_batches` | `id`（`creator_batch_<uuid>`） | `plan_json`, `plan_hash`, `revision`, `quoted_revision`, `quote_expires_at`, `claim_id`, `confirmation_id`, `status`, **`insert_seq`** | 一次多平台生产计划；报价/确认都要与 revision、plan_hash 完全一致 |
| `creator_jobs` | `id`（`creator_job_<uuid>`） | `platform`, `version`, `status`, `input_json`/`input_hash`, 报价与提交冻结列, `revision` | 批次下的单平台子任务状态机 |
| `creator_model_calls` | `id`（`creator_model_<uuid>`） | `username`, `ip_hash`, `day`, `state`, `lease_until`, `estimated_*` | 免费模型调用的限流/额度账本（保留 8 天，属运维账本） |

两点与源库不同、但都是有意的：

1. **`creator_batches.insert_seq`**：源库用 SQLite 隐式 `rowid` 作为
   `ORDER BY created_at DESC, rowid DESC` 的兜底排序（同一秒内建两个批次时决定
   `latest_batch` 取谁，产品上就是「重新生成视频」取上一批）。PostgreSQL 没有 rowid，
   所以加了这一列：回填照抄源 rowid，运行时取同用户同项目 `MAX(insert_seq)+1`
   （建批次时先锁工作区行，天然串行）。
2. **索引与源一一对应**，含 `idx_creator_batches_source_message` 的部分唯一索引
   （`WHERE source_message_id > 0`）。

## 运行时开关

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_CREATOR_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值在第一次调用存储层时直接抛错 |
| `HQ_DATABASE_URL` | `postgresql://…` | 仅 `postgres` 模式需要；空值即报错，绝不退回 SQLite |
| `HQ_CREATOR_DB_POOL_MAX` | 1–20，默认 4 | 连接池上限 |

`pg_store.py` **自建**懒加载 `psycopg_pool` 池（`min_size=1`、`open(wait=True, timeout=5)`）；
刻意**不** import `server.db.postgres`：creator-agent 是独立 venv + 独立 systemd 单元，
`sys.path` 里没有 `server/` 包，import 会直接炸。开关是**进程级**的，而 creator-agent 是
这个库的唯一写者，所以「一个进程 + 一个开关」就完成了切换，不存在双权威窗口。

`postgres` 模式下 `CreatorAgentStore.__init__` 不再打开/迁移 SQLite 文件（表结构交给
Alembic），`path` 仍用于 `profile-pdfs` 等本地目录定位。

`creator_model_calls` 的写入方 `ModelUsageGuard` 按 SQLite 方言发语句（`?` 占位符、
`BEGIN IMMEDIATE`、`PRAGMA table_info`、标量 `MAX(a,b)`），而 `model_usage.py` 不在本批
改动边界内，因此 `pg_store.usage_connection()` 显式翻译这一小组语句：`BEGIN IMMEDIATE`
→ `LOCK TABLE agent.creator_model_calls IN EXCLUSIVE MODE`（复现「先拿写锁再查额度」的
串行化）、`PRAGMA table_info` → `information_schema.columns`、`MAX(a,b)` →
`GREATEST(a,b)`、表名限定到 `agent.`；**建表/建索引一律空操作**（schema 归 Alembic），
缺列时的 `ALTER TABLE` 直接报错让人回去跑迁移；未登记的语句一律抛错，绝不静默执行错语义。

## 收编前必须补的两处（本域不动这两个文件）

1. **`deploy/requirements-creator-agent.txt` 加 `psycopg[binary,pool]>=3.2,<4`**。
   当前该文件只有 reportlab/pillow/charset-normalizer：`postgres` 模式启动时会
   `ModuleNotFoundError: psycopg_pool`（`sqlite` 模式不受影响，因为 import 是懒加载的）。
2. **`.github/workflows/ci.yml` PostgreSQL 段加一行**
   `python -m unittest discover -s tests -p 'test_creator_pg_store.py' -v`
   （CI 已在该段先跑 `alembic upgrade head`，agent schema 会自动建好）。

不需要改 `scripts/drift_sentinel.py` 与 `deploy/creator-agent-release.sh`：
`server/creator_agent/*.py` 是**通配复制**到 `/opt/huangque/creator-agent/current/creator_agent/`，
新增的 `pg_store.py` 自动随包（release 脚本顶部的文件清单只是存在性预检，不限制新文件）。

## Staging 验证（生产切换前必须全过）

用 staging 库（例如 `huangque_staging`），**绝不指向生产 `huangque` 库**；回填源用生产
`creator_agent.db` 的只读快照副本。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site
alembic upgrade head                     # 建 agent.creator_* 六张表
alembic current                          # 必须是 20260915_0009

python scripts/migrate_agent_creator.py --source /path/to/creator_agent.db.copy
python scripts/migrate_agent_creator.py \
  --source /path/to/creator_agent.db.copy --code-sha <m3g-commit> --apply
python scripts/migrate_agent_creator.py --source /path/to/creator_agent.db.copy   # 再 dry-run：行数不变
```

回填器契约与 M3A/M3E 一致：不带 `--apply` 永远只读；`--apply` 必须带 `--code-sha`；
单事务；逐行读回比对（含类型），任何不一致整批回滚；相同数据重跑幂等；写
`ops.data_migration_runs` / `items` 审计（`domain='agent'`，按表记 6 行）；源校验和 =
六张表规范化 JSON 的 SHA-256；带 `updated_at` 的四张表有「目标比源新即整批中止」护栏，
`creator_messages` / `creator_model_calls` 只补缺失行、不覆盖目标既有行；导入后把
`creator_messages` 的自增序列推到 `max(id)`（否则切换后新消息会撞号）；源库若有孤儿行
（消息/批次指向不存在的工作区）直接拒绝导入。

验收（本机已用真实 PostgreSQL 全跑过一遍，staging 照做）：

1. dry-run 后目标六表行数全部为 0（对照源库 `count(*)`）。
2. `--apply` 后六表行数、`ops.data_migration_runs`（state=verified、domain=agent）、
   6 条 `data_migration_items`（全 verified）与源一致。
3. 影子核对：逐行逐字段比对源与目标，零差异（脚本内已做，另用 `psql` 抽查两行）。
4. 第二次 `--apply` 幂等：行数不变、不再产生新值。
5. 冲突护栏演练：`UPDATE agent.creator_workspaces SET updated_at = updated_at + 100000;`
   后重跑 `--apply`，必须以「目标 updated_at 比源新，停止整批导入」整批中止（演练后改回）。
6. 序列演练：回填后插入一条新消息，id 必须大于源最大 id。
7. 应用层演练：`HQ_CREATOR_STORE=postgres` 启动服务，`/health` 的
   `database_writable` 与 `model_usage_store` 均为 true；走一轮
   bootstrap → 消息 → 建批次 → 报价 → 确认。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 备份：确认 `huangque-postgres-backup.timer` 近 48 小时有成功产物；把
   `/var/lib/huangque-creator-agent/creator_agent.db` 打一份只读快照（回填源）。
2. 建最小权限运行角色（已实测：只给下列权限即可跑通全部读写，且它**不能**建表/建索引）：

```sql
GRANT USAGE ON SCHEMA agent TO huangque_creator;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  agent.creator_account_state, agent.creator_workspaces, agent.creator_messages,
  agent.creator_batches, agent.creator_jobs, agent.creator_model_calls
  TO huangque_creator;
GRANT USAGE, SELECT ON SEQUENCE agent.creator_messages_id_seq TO huangque_creator;
```

3. 确认 `deploy/requirements-creator-agent.txt` 已含 `psycopg[binary,pool]`，再发布代码
   （`server/creator_agent/*.py` 会整目录进 release；因为池是自建的，creator-agent 运行
   目录**不需要**额外的 `db/postgres.py`）。发布后校验
   `/opt/huangque/creator-agent/current/creator_agent/pg_store.py` 已就位。
4. 注入环境变量到 **creator-agent 自己的** env 文件（`huangque-creator-agent.service` 的
   `EnvironmentFile=-/etc/huangque/creator-agent.env`，**不是** content.env）：

```ini
HQ_CREATOR_STORE=postgres
HQ_DATABASE_URL=postgresql://huangque_creator:…@127.0.0.1:5432/huangque
# 可选：HQ_CREATOR_DB_POOL_MAX=4
```

   密码只落在服务器受保护文件（root、600/640），不进 git、不进聊天。

5. 停写窗口内跑回填（此时服务应已切 `HQ_CREATOR_STORE=postgres` 或先停服）：按上面
   Staging 步骤对**生产库**执行 `--apply`；确认 `creator_model_calls` 这类账本表的
   历史行不影响新写入（`state='active'` 的记录迁移后会按租约自然过期）。
6. `systemctl restart huangque-creator-agent`；日志里不得出现 `HQ_CREATOR_STORE` /
   `ModuleNotFoundError` / `permission denied for schema agent`。
7. 验证：`curl -fsS http://127.0.0.1:8114/health`（`database_writable`、
   `model_usage_store` 为 true）；真实账号走一轮 bootstrap → 一段对话 → 建批次 → 报价
   → 确认；`psql` 查 `agent.creator_messages` / `creator_batches` 落到新值且
   `updated_at` 秒级时间戳正确。
8. 观察 48 小时：`ops.data_migration_runs` 无新失败；服务日志无 5xx 尖峰；
   `creator_model_calls` 的限流仍然生效（连点两次模型请求应被「今日模型请求次数已达上限」
   之类的话术挡住，而不是无限制放行）。
9. SQLite 归档：`creator_agent.db` 改名保留（不删除），确认无进程再打开（`lsof`）；
   若要彻底断掉旧路径，再摘 `CREATOR_AGENT_DB`（回滚时需要它）。

## 回滚

- 秒级：把 `/etc/huangque/creator-agent.env` 的 `HQ_CREATOR_STORE` 改回 `sqlite`
  （或删掉该行）并重启服务 —— SQLite 文件全程只读保留，立即回到旧权威。
- **数据注意**：`postgres` 模式期间的新写入（消息、批次、模型账本）只落在 PostgreSQL，
  回滚不会自动回灌 SQLite。所以要①尽量缩短窗口，②回滚前用
  `scripts/migrate_agent_creator.py` 的反向思路手工导出这段时间的新行（或接受丢失并
  告知老板）。回滚后立刻把 SQLite 重新置为唯一权威，并重跑回填以恢复影子核对基线。
- 完整回退：部署上一个 commit（`creator_agent/*.py` 一起回退，`pg_store.py` 可留着不用）。

## 停止条件（任一触发立即停并向老板报告）

- 影子核对任何一行不一致；六表行数或主键集合不一致。
- 切换期间 SQLite 出现新的 `updated_at`（说明仍有进程在写旧库，即双权威）。
- 回填或运行时报错无法解释；`permission denied` / `relation does not exist` 之类
  说明部署或迁移没对齐。
- 模型调用账本失效（限流失效或误封），或 `/health` 的 `database_writable` /
  `model_usage_store` 为 false。
- 锁等待影响用户请求（`LOCK TABLE` 与批次行锁都在同一进程内，若出现跨进程等待说明
  有第二个写者）。
- 备份不可恢复。
