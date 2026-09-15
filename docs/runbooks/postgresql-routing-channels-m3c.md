# PostgreSQL M3C：渠道配置与执行记录切换手册（channel_management.db → routing schema）

## 边界

本批把「渠道配置 + 版本历史 + 功能映射 + 渠道调用记录」从 SQLite
（`content-api/channel_management.db`，11 张表）迁到 PostgreSQL `routing` schema，
并接入运行时读写。它不迁移其他任何域；SQLite 文件全程保留只读，任何时刻都能秒级回退。

- 源：`/home/ubuntu/content-api/channel_management.db`（生产唯一一份，服务账号独占读写、
  0600，创建者 `channel_manager.db()`）。
- 目标：`routing.channels` / `versions` / `mappings` / `operation_mappings` /
  `operation_mapping_versions` / `runs` / `run_snapshots` / `events` / `schedule` /
  `settings` / `channel_incidents`（迁移 `20260915_0006_routing_channels.py`）。
- 表名与列名与源库逐字相同（源库已是全词命名），没有外键、没有新增列；只重建源库已有的
  5 个二级索引（含 `operation_mapping_history` 的 `revision DESC`）。
- 时间口径与 SQLite 一致：`started`/`updated`/`duration`/`created`/`occurred`/
  `light_due`/`full_due`/`reservation` 一律 `double precision`，秒级 Unix 时间戳（带小数），
  不引入 timestamptz。
- 唯一的 0/1 语义列 `channels.enabled` 映射为 `boolean`，写入与回填一律 `bool()` 归一化。

## 运行时开关

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_CHANNEL_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值直接抛错，取开关即失败 |
| `HQ_DATABASE_URL` | `postgresql://…` | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_CHANNEL_DB_POOL_MAX` | 1–20，默认 4 | `channel_store.py` 自建懒加载连接池上限 |
| `HQ_CHANNEL_DB` | 路径 | SQLite 模式下的库路径（生产 `/home/ubuntu/content-api/channel_management.db`） |

`channel_store.py` 只认 PostgreSQL（**不 import `server.db.postgres`、不 import sqlite3**，
自建 `psycopg_pool.ConnectionPool`，`open=False` + `open(wait=True, timeout=5)`，
`row_factory=dict_row`，线程锁单例，`close_pool()` 供测试）。它绝不吞错：保存、映射发布、
生命周期、调度路径出错即抛；只有源库本来就「读不到证据不算失败」的三处
（`task_recovery_state` / `mark_interrupted_task_unknown` / `search_task_ids`）沿用原语义。

`channel_manager.db()` 在 `postgres` 模式下**直接抛错**（不再返回旧库连接），
`HQ_CHANNEL_STORE=postgres` 时任何仍直连它的模块会立刻失败而不是静默双写。

## 敏感字段与日志纪律（切换前必读）

| 列 | 内容 | 要求 |
|---|---|---|
| `routing.versions.secret` | 渠道 API 密钥密文（AES-GCM，AAD=`hq-channel-v1`，主密钥在服务 env 的 `HQ_PROVIDER_KEYS_MASTER_KEY`，代码侧 `provider_keys._master_key()`） | 迁移按密文原样搬运，密钥从不离开保险箱；**禁止** `SELECT secret` 进日志/终端；回填报告只输出计数与校验和；`version(..., with_secret=True)` 只在管理员查看密钥接口里解密，且现有测试断言 `overview()` 不含明文 |
| `routing.settings.value`（id=1） | 告警通知 endpoint 密文（同一密钥） | 同上；`notification_settings(private=False)` 只返回「已配置（隐藏）」 |
| `routing.versions.config` | 含 `base_url`/`proxy`/供应商等运营敏感信息与测试素材 | 不得整段进日志；`_legacy_capture`/捕获日志只输出渠道编号与版本 |
| `routing.runs.provider_id` | 供应商任务号 | 非密钥，但也不进用户可见文案 |

PostgreSQL 侧额外注意：`channel_store` 不使用任何 `SELECT *` 到日志的路径；备份/`pg_dump`
产物现在包含上述密文，按与 SQLite 备份同等级别保护（0600、不进 Git）。
`iterating` 排查时用 `psql -c "SELECT id,version,enabled FROM routing.channels"`，
不要 `SELECT *`（`versions` 表会带出 `secret`）。

## Staging 验证（生产切换前必须全过）

用 staging 库（例如 `huangque_staging`），**绝不指向生产 `huangque` 库**；回填源用生产
`channel_management.db` 的一致性只读副本（`sqlite3 .backup`，禁止只复制 WAL 模式主文件）。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site
alembic upgrade head                              # 建 routing.* 11 张表与 5 个索引
python scripts/migrate_routing_channels.py --source /path/to/channel_management.db.copy
python scripts/migrate_routing_channels.py \
  --source /path/to/channel_management.db.copy --code-sha <m3c-commit> --apply
```

回填器契约（与 M3A 一致）：

- 不带 `--apply` 永远只读，逐表输出行数与源校验和（规范化 JSON 的 SHA-256）。
- `--apply` 必须带 `--code-sha`；单事务写入 11 张表并记审计
  `ops.data_migration_runs`（`domain='routing'`）/ `ops.data_migration_items`。
- 逐行读回与源比对：布尔按真假、数值按数值（0/1 与 REAL 口径差异不算不一致）、
  其余按文本；任何不一致整批回滚。
- 幂等：同数据重跑逐表报 `unchanged`，不重写。
- 冲突护栏：目标时间列（`updated`/`created`/`occurred`/`light_due`）比源新 → 整批中止；
  `channels`/`run_snapshots`/`settings` 没有时间列，目标内容与源不一致即中止。
- 源库缺列直接报错（源库版本与本迁移不匹配）。

验收：apply 通过 → 再跑一次 dry-run 全 `unchanged` → `psql` 抽查
`routing.channels` / `routing.versions`（不含 secret）与 SQLite 逐字段一致 →
确认 11 张表行数相等 → `alembic downgrade` 不可用（有意禁止破坏性降级，靠备份恢复）。

## 切换前硬前置（本域不能单独切换）

`channel_manager.py` / `channel_lifecycle.py` 已分发到 `channel_store`，但**同一份库里
还有两个模块直连 `channel_manager.db()`，它们不在本批文件边界内**，PG 模式下会
fail-closed 抛错：

1. `server/content_domains/channel_runtime.py`：执行器与调度器写 `runs`、`channel_incidents`、
   `runtime.dispatch` 限流流水、回写 `schedule`（`execute()` / `_mark_terminated()` /
   `_notify()` / `monitor_cycle()`）。**不迁移它，渠道测试与托管任务执行会直接失败。**
2. `server/content_domains/channel_parameters.py`：参数草稿（settings id=3）、工作台布局
   （settings id=4）、发布新版本（`change`）、`quote()`、`historical()`。

切换前必须把这两个模块也接到 `channel_store`（或把本域的 store 接口扩到它们需要的
行级操作），并在 staging 跑通「后台渠道测试 + 参数发布 + 工作台布局 + 报价 + 托管任务」。

其余前置（与 M3A 同一节奏，不可压缩）：

- 影子核对：切换代码上线但仍是 `sqlite` 权威时，连续 **48 小时**每日两次对比 SQLite 与
  PG 回填结果零差异；期间后台任何渠道操作记录时间戳供核对。
- 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物，且手工恢复演练过 staging。
- 另行固定：生产 commit、维护窗口、回滚负责人、老板切换授权。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码：`channel_manager.py`、`channel_lifecycle.py`、`channel_store.py` **同一版本一起**
   部署到 `/home/ubuntu/content-api/content_domains/`（`channel_manager` 在模块导入期
   `from . import channel_store`，漏一个文件 content 起来就 import 失败）。
   三个服务都要重启：`huangque-content`、`huangque-admin`、`huangque-imggen-api`
   （分别经 core/jobs_store/video、admin_api、imggen_api 使用渠道库）。
   注：`channel_manager.py`/`channel_store.py` **不在** `drift_sentinel` 的
   `AUTH_SHARED_RUNTIME` 里（auth-service 不部署渠道模块），本批不动 drift_sentinel。
2. 建最小权限运行角色（或沿用既有运行角色）：
   `GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA routing TO huangque_channel;`
   不给 DELETE / DDL / 其他 schema 权限（`unmap` 会 DELETE `routing.mappings`，如需该操作
   再单独授 `routing.mappings` 的 DELETE，并在评审里写清）。
3. 注入环境变量（`/home/ubuntu/content-api/content.env` 与
   `/etc/systemd/system/huangque-admin.service`、`huangque-imggen-api.service` 的
   `Environment=`）：`HQ_CHANNEL_STORE=postgres`、`HQ_DATABASE_URL=postgresql://…`。
   密码只落服务器受保护 env，不进 git、不进聊天。
4. 三个服务同版本同开关一起重启；启动即检查日志无 `HQ_CHANNEL_STORE` / import 报错。
5. **权威校验门禁**：sudo 跑 `scripts/check_store_authority.py --expect postgres`
   必须全绿（任一 ERROR 立即回滚），启动日志应是 INFO
   `authority announced: mode=postgres`（见 `postgresql-cutover-authority-guard.md`）。
6. 后台验证：渠道列表（`overview`）→ 改一次渠道配置（写 `channels`/`versions`/`schedule`/
   `events`）→ 发布一次功能映射 → 跑一次连接测试（`runs`）→ `psql` 查对应行；
   再用 `python scripts/migrate_routing_channels.py --source <快照>` 确认 dry-run 只有
   变化的那几行。任何一步失败立即回滚开关。
7. 观察 48 小时：接单捕获、验收（`acceptance_guard`）、任务恢复态、后台渠道页、
   参数/报价、托管任务执行无异常；`ops.data_migration_runs` 无新失败。
8. SQLite 冰冻监控：切写后 1 小时每 10 分钟查 `channel_management.db` mtime/行数
   必须「冻住」（见 `postgresql-cutover-authority-guard.md`），然后才准归档。
9. SQLite 归档：`channel_management.db` 改名保留（不删除），确认无进程再打开
   （`lsof /home/ubuntu/content-api/channel_management.db`）。

## 回滚

- 秒级：三个服务的 `HQ_CHANNEL_STORE` 改回 `sqlite` 并重启 —— SQLite 全程只读保留，
  立即回到旧权威。
- 完整回退：回退到上一个 commit（三个文件一起回退）。
- 注意：`postgres` 模式期间的渠道修改只在 PG；回滚后后台操作回到 SQLite，两边可能短暂
  分叉 —— 回滚后立刻把 SQLite 重新置为权威，并重新回填 PG 以恢复影子核对基线
  （回填器的冲突护栏会挡住目标更新的行，届时按 runbook 人工核对后再导入）。

## 已知差异（切换前后必须知道）

1. **锁语义**：SQLite 用 `BEGIN IMMEDIATE`（整库单写者）；PG 侧改为「行锁 + 事务级咨询锁」：
   渠道行存在时 `SELECT … FOR UPDATE`（渠道保存、生命周期、映射校验、验收守卫），
   行可能不存在时 `pg_advisory_xact_lock(0x48514331, hashtext(名称))`（新建渠道、映射发布、
   队列与预算检查、settings 单行 JSON 读改写）。`acceptance_guard` 用 FOR UPDATE 行锁
   持有到任务提交完成——与 SQLite 的「持锁到提交」同为线性化点。
2. **`rowid` 次序**：源 SQL 里的 `ORDER BY started DESC,rowid DESC` 在 PG 改为
   `ORDER BY started DESC,updated DESC,id`（`_LATEST_KIND` / `_LATEST_FULL` / `_LATEST_CHECK`）。
   `started` 是微秒级时间戳，需要靠 tiebreak 才分得出的情况实际不会出现；`id` 唯一保证
   结果确定。
3. **`SUM(state='failed')`** → `SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END)`：
   零行时同样返回 NULL，后台展示口径不变。
4. **错误类型**：恢复态三处的「读不到证据」在 PG 下按 `psycopg.Error`（含池超时）判定，
   与源库的 `(OSError, sqlite3.Error)` 对应，其余异常照旧抛出。
5. **版号归一化**：SQLite 的 `WHERE version=2.5` 只是查不到行；PG 侧同样返回
   「渠道版本不存在」（不抛类型错误）。

## 写者 / 读者清单（源码确认）

唯一 `sqlite3.connect(channel_management.db)` 点是 `channel_manager.db()`；下列模块都经它。

**写者**

- `server/content_domains/channel_manager.py`：渠道版本（`save`/`rollback`）、旧线路映射
  （`save_mapping`）、功能映射（`save_operation_mapping`）、运行记录与影子快照
  （`reserve`/`finish`/`record_shadow`）、通知设置（`save_notifications`）、审计 `events`。
- `server/content_domains/channel_lifecycle.py`：渠道启停/回收站（`mutate`）、内置供应商启停
  （`mutate_legacy`，settings id=2）、删除旧线路映射（`unmap`）。
- `server/content_domains/channel_runtime.py`（**边界外，待迁移**）：`runs` 状态推进、
  `channel_incidents`、`runtime.dispatch` 限流流水、`schedule` 回写、`_mark_terminated`。
- `server/content_domains/channel_parameters.py`（**边界外，待迁移**）：参数草稿（settings id=3）、
  工作台布局（settings id=4）、参数发布写 `versions`/`channels`。

**读者**

- `server/admin_api.py`：无直连读取；全部经 `channel_manager` / `channel_lifecycle`
  公开 API（`overview()` 约 2942、`version(with_secret=True)` 约 2977、`search_task_ids` 约 3390、
  `task_evidence` 约 3439、后台动作表 9200–9206：save/mapping/operation-mapping/
  operation-mapping-rollback/rollback/test/notifications/lifecycle/legacy-lifecycle/unmap）。
- `server/imggen_api.py`：约 459 `task_recovery_state`、约 735 `capture("image")`。
- `server/content_domains/core.py`：约 1926 / 2054 / 4545 恢复态与 `capture`。
- `server/content_domains/jobs_store.py`：约 325–456 `capture` / `acceptance_guard` /
  `record_shadow`。
- `server/content_domains/video.py`：约 1367 `capture("xiaole_video")`。
- `server/content_domains/channel_parameters.py`（**边界外**）：`_published_items`、`quote`、
  `historical`、`layout_state`。
- `server/content_domains/runtime_observability.py`：约 96 `notification_settings(True)`
  （经 `channel_manager`，不直连）。
- `server/content_domains/startup_recovery.py`：约 53 `mark_interrupted_task_unknown`。

## 收编待办（由主 Agent 处理，本批不改）

- `.github/workflows/ci.yml`：在 PostgreSQL 段加
  `python -m unittest discover -s tests -p 'test_channel_store.py' -v`
  （与 M3A 的 `test_flags_store.py` 同一行组）。
- `scripts/drift_sentinel.py`：本域**不需要**改（渠道模块不部署到 auth-service）。
- 部署顺序：`channel_manager.py`、`channel_lifecycle.py`、`channel_store.py` 必须同版本同时发布。

## 停止条件（任一触发立即停并向老板报告）

- 影子核对任何一行不一致；表行数/主键集合不一致。
- `db()` 抛错出现在日志里（说明还有模块在直连旧库）：先回滚开关，再排查。
- 切换期间 SQLite 出现新的 `updated`/`created`（说明仍有进程在写旧库）；SQLite
  冰冻监控 1 小时内 mtime 前进或行数增长（见 `postgresql-cutover-authority-guard.md`）。
- 权威校验器 `--expect postgres` 非全绿（进程与配置不一致 = 双权威风险）。
- 回填或运行时报错无法解释；锁等待影响用户请求。
- 备份不可恢复；双权威并存（三个服务的 `HQ_CHANNEL_STORE` 不一致）。

## channel_runtime / channel_parameters 补迁说明（M3C 收尾）

本节**取代**上文「切换前硬前置」第 1、2 条，以及「写者 / 读者清单」里那两个模块的
`边界外，待迁移` 标注：两个模块已接到 `channel_store`，PG 模式下不再直连
`channel_manager.db()`，本域单独切换的最后一个前置缺口已消除。

分发写法与 `channel_manager` / `channel_lifecycle` 一致：每个读写入口
`if channel_store.enabled(): … else: 原 SQLite 路径`，SQLite 分支**逐字节保留**
（只有层级缩进与新增的 `from . import channel_store` 有差异）。两个模块仍**不 import
sqlite3**，`scripts/sqlite_inventory.py --check` 保持通过。

### 读写点清单（源码确认）

`server/content_domains/channel_runtime.py`（执行器 + 调度器）

| 函数 | 原 SQLite 读写 | PG 侧 |
|---|---|---|
| `_mark_terminated` | `UPDATE runs SET state='terminated' …` | `channel_store.mark_terminated` |
| `execute`（入口读） | `SELECT * FROM runs WHERE id=?` | `channel_store.run_record` |
| `execute`（准入闸门） | `BEGIN IMMEDIATE` + 陈旧 `running` 清理 + 并发/限流统计 + `UPDATE runs … running` + `runtime.dispatch` 流水 | `channel_store.try_start_run` |
| `execute`（失败分支） | `SELECT detail FROM runs WHERE id=?` | `channel_store.execution_phase` |
| `_notify` | `channel_incidents` 读 + `INSERT OR REPLACE` | `channel_store.note_incident` |
| `start_test` | `events` 审计（`test.*`） | `channel_store.record_audit` |
| `monitor_cycle`（补投） | `SELECT … FROM channel_incidents WHERE action!=''` | `channel_store.pending_incidents` |
| `monitor_cycle`（排期） | `BEGIN IMMEDIATE` + `schedule ⨝ channels` + 回写 `light_due`/`full_due` | `channel_store.poll_schedule` |
| `monitor_cycle`（补跑） | `SELECT id FROM runs WHERE state='queued' AND kind!='task' ORDER BY started LIMIT 8` | `channel_store.queued_test_run_ids` |
| `monitor_cycle`（阻断审计） | `events` 审计（`scheduler.blocked.*`） | `channel_store.record_audit` |

`server/content_domains/channel_parameters.py`（参数发布 + 工作台布局）

| 函数 | 原 SQLite 读写 | PG 侧 |
|---|---|---|
| `layout_state` | `SELECT value FROM settings WHERE id=4` | `channel_store.layout_setting` |
| `_published_items` | `mappings` + `channels` + `versions` 三连读 | `channel_store.published_channel_configs` |
| `layout_save` | `BEGIN IMMEDIATE` + `INSERT OR REPLACE settings(4)` + 审计 | `channel_store.save_layout` |
| `admin_state` | `settings id=3` + `versions` 历史 | `channel_store.draft_setting` + `channel_versions` |
| `change` | `BEGIN IMMEDIATE` + 渠道版本加入/推进 + `settings id=3` + 审计 | `channel_store.change_parameters` |
| `quote` | `SELECT config FROM mappings WHERE selector=?` | `channel_store.mapping_by_selector` |

`drafts(connection)` 仍是 SQLite 专用小工具（只被 SQLite 分支调用）；PG 分支用
`channel_store.draft_setting()`。`channel_runtime` 里 `store.reserve` / `store.finish` /
`store.version` / `store._next_daily` 走的是 `channel_manager` 已有的公开入口，本次未改。

### 复用的 channel_store 既有函数

`version` / `save` / `reserve` / `finish` / `record_shadow` / `acceptance_guard` /
`task_evidence` / 恢复态三兄弟，以及内部件 `_audit`、`_lock`、`_fetch_version`、
`_pool_instance`、`_mgr`、`_revision`。**未改动任何既有函数的签名或语义。**

### 本次新增的 channel_store 函数（只增不改）

- 执行器：`run_record`、`try_start_run`、`execution_phase`、`mark_terminated`、
  `note_incident`、`pending_incidents`、`poll_schedule`、`queued_test_run_ids`、`record_audit`。
- 参数与布局：`layout_setting`、`draft_setting`、`save_layout`、`mapping_by_selector`、
  `published_channel_configs`、`channel_versions`、`change_parameters`
  （内部件 `_setting_json`、`_save_setting`）。

语义映射沿用本 runbook「已知差异」的同一套口径：`BEGIN IMMEDIATE` → 事务级咨询锁
（派发闸门与调度排期用全局锁 `routing.runtime.dispatch` / `routing.schedule`，
渠道事件用 `routing.incident:<渠道>:<类型>`，参数发布用 `routing.parameters:<渠道>` +
`SELECT … FOR UPDATE OF ch`），`INSERT OR REPLACE` → `ON CONFLICT … DO UPDATE SET 全列`，
限流/预算计数与 `SUM(CASE WHEN …)` 口径不变，0/1 一律 `bool()`。

### 与 SQLite 路径的已知差异（本批新增，切换前后必须知道）

1. **缺行时的报错类型**：`execute` 入口读（`run_record`）、失败分支的阶段读
   （`execution_phase`）在 PG 下缺行返回 `None`，随后取列抛
   `TypeError: 'NoneType' object is not subscriptable`；SQLite 抛
   `TypeError: 'NoneType' object is not iterable`。两者都是 TypeError 且都发生在
   「运行记录不存在」这一不可能路径（`reserve` 刚写入）。
2. **闸门遇到不存在的 run**：`try_start_run` 按「已不在 queued」返回（调用方结束执行），
   SQLite 在同一位置抛 TypeError。方向更安全（不会继续提交付费请求）。
3. **确定性次序**：`queued_test_run_ids` 用 `ORDER BY started,id`（SQLite 是
   `ORDER BY started`，同一时间戳次序未定义）；`pending_incidents` 用
   `ORDER BY channel,kind`（SQLite 无排序）；`published_channel_configs` 用
   `ORDER BY selector`（SQLite 无排序）。内容与条数完全一致，只把未定义的次序定死。
4. **调度取数的一致性**：`poll_schedule` 在同一个事务里读渠道版本，
   SQLite 的 `monitor_cycle` 是用独立连接读 `store.version`。只影响「并发改渠道配置」
   那一瞬的可见性，不改变派发语义。

### 验证

```bash
python3 -m py_compile server/content_domains/channel_runtime.py \
  server/content_domains/channel_parameters.py server/content_domains/channel_store.py
python3 scripts/sqlite_inventory.py --check
python3 -m unittest discover -s tests -p 'test_channel_runtime_store.py'   # 新增用例
python3 -m unittest discover -s tests -p 'test_channel_manager.py'
python3 -m unittest discover -s tests -p 'test_channel_lifecycle.py'
python3 -m unittest discover -s tests -p 'test_channel_parameters.py'
python3 -m unittest discover -s tests -p 'test_managed_channel_recovery.py'
python3 -m unittest discover -s tests -p 'test_managed_channel_termination.py'
python3 -m unittest discover -s tests -p 'test_task_termination.py'
```

`tests/test_channel_runtime_store.py` 与 `tests/test_channel_store.py` 同构：
SQLite 部分证明分发不改变行为与旧库字节，PG 部分（`HQ_DATABASE_URL` 存在时才跑）
覆盖执行闸门、终止态、事件去重、调度排期、参数起草/发布/回滚、报价与「切换后不碰旧库」。
CI 的 PG 段已含 `python -m unittest discover -s tests -p 'test_channel_runtime_store.py' -v`
（主 Agent 收编项，本批未改 `.github/workflows/ci.yml`）。

staging 追加验收（在原有「后台渠道测试 + 参数发布 + 工作台布局 + 报价 + 托管任务」之上）：

1. 后台跑一次连接测试与一次完整生成测试：`routing.runs` 出现 `queued→running→passed`，
   `routing.events` 出现 `runtime.dispatch`、`routing.channel_incidents` 出现对应行。
2. 把 `schedule.light_due`/`full_due` 手工置 0 后等一个调度周期：两项各派发一次并回写新时间。
3. 参数页：存草稿（`routing.settings` id=3）→ 发布（`routing.versions` 新版本 +
   `channels.version` 推进 + id=3 的 `base_version` 跟着走）→ 前台目录与报价一起变。
4. 工作台布局保存（id=4）后刷新前台目录顺序。
5. 终止一个托管任务：`runs.state='terminated'`，不产生 `channel.failed` 告警。
6. 全程 `lsof /home/ubuntu/content-api/channel_management.db` 无新写入；
   `db()` 抛错不得出现在日志里。
