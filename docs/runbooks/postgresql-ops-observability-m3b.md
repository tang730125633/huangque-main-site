# PostgreSQL M3B：运行证据 + 通知发件箱切换手册

## 边界

本批把「任务运行证据（task_trace）+ 健康通知发件箱（alert_outbox）」从 SQLite
（`content-api/runtime_observability.db`）迁到 PostgreSQL `ops` schema，并接入运行时
读写。它不迁移其他任何域；SQLite 文件全程保留（只读、不删除），任何时刻都能秒级回退。

- 源：`/home/ubuntu/content-api/runtime_observability.db`（env `HQ_OBSERVABILITY_DB`），
  两张表 `task_trace`（主键 `job_id`+`stage`）、`alert_outbox`（主键 `event_id`）。
- 目标：`ops.traces`（来自 `task_trace`，表名按任务书改）与 `ops.alert_outbox`。
- 时间口径与 SQLite 一致：秒级 Unix 时间戳带小数（`REAL` → `double precision`），
  不引入 timestamptz 隐式换算；`metadata` / `payload` / `error` 与源一样是无限长
  TEXT（不用 VARCHAR）。
- 列可空性与源一致：源库里除主键外没有 `NOT NULL`（SQLite 的 `DEFAULT` 不等于
  `NOT NULL`），PG 表也只给 `state` / `attempts` / `next_try` / `error` 设默认值。
- 溯源表名映射：`task_trace` → `ops.traces`；其它列名逐字照搬。

### 源库列清单（用于 staging 核对）

| 表 | 列（类型） |
|---|---|
| `task_trace` | `job_id` TEXT、`stage` TEXT、`state` TEXT、`started` REAL、`updated` REAL、`duration` REAL、`metadata` TEXT；主键 (job_id, stage) |
| `alert_outbox` | `event_id` TEXT（主键）、`payload` TEXT、`state` TEXT DEFAULT 'pending'、`attempts` INTEGER DEFAULT 0、`next_try` REAL DEFAULT 0、`updated` REAL、`error` TEXT DEFAULT '' |

## 运行时开关

一个环境变量控制读写路径，**默认 sqlite，行为与迁移前逐字节一致**：

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_OBS_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值直接抛错，绝不静默写到另一个库 |
| `HQ_DATABASE_URL` | postgresql://… | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_OBS_DB_POOL_MAX` | 1–20，默认 4 | 本模块自建连接池上限 |
| `HQ_OBSERVABILITY_DB` | 绝对路径 | 仅 `sqlite` 模式使用的证据库路径（行为不变） |

`observability_store.py` 自建懒加载 `psycopg_pool.ConnectionPool`（与 M3A 的
`flags_store.py` 同一做法：`open=False` + `open(wait=True, timeout=5)`、`dict_row`、
线程锁单例、`close_pool()` 供测试），导入本模块不建连，也**不依赖
`server.db.postgres`** —— 生产 content-api 进程的 sys.path 里没有 `server` 包
（`server/db` 无任何部署映射），store 模块里 import 它会在运行时直接炸。

它绝不吞错：连接或执行失败都抛异常，由 `runtime_observability` 沿用既有降级语义 ——

- 写证据失败（`record` / `call`）：静默丢弃，**绝不影响已付费任务结论**；
- 读证据失败（`traces` / `search_task_ids`）：返回空 / 空集合；
- 读通知计数失败（`alert_status`）：返回 `{'enabled':…, 'error':'通知记录不可读'}`；
- 入队与投递（`enqueue` / `dispatch`）：**不兜底**，失败必须暴露（告警不能悄悄丢）。

## 写者 / 读者服务清单（从源码确认）

| 角色 | 服务 | 代码位置 | 干什么 |
|---|---|---|---|
| 写者 | content（`huangque-content`） | `content_domains/video.py:7860`、`content_domains/video_minimax_h3.py:347,396,403,406,412,424`、`content_domains/channel_runtime.py:272,276,287,295,315,324,329,338,351,379,381` | 经 `call()` / `record()` 写 `task_trace` 各阶段：route / provider_submit / provider_accepted / provider_query / download / generation / generation_resume / artifact / delivery |
| 写者 | content | `content_domains/channel_runtime.py:515,561,576` | 经 `enqueue()` 写告警：`channel.*` 渠道异常、排期被挡 |
| 写者 | admin（`huangque-admin`） | `admin_api.py:3733`（`run_service_monitor_cycle`） | 经 `enqueue()` 写服务健康事件（`service.*`） |
| 读者 | admin | `admin_api.py:3391`（任务搜索）、`3437` / `7519`（任务详情与完成统计的 `traces()`）、`3800`（`service_health_monitor` 的 `alert_status()`） | 后台任务面板、证据链、通知计数 |
| 读者 | content | `content_domains/channel_manager.py` 的 `notification_settings()`（写本手册时约 760-762 行，`from . import runtime_observability` + `runtime_observability.database()`） | **直接**读 `alert_outbox` 的 `channel.*` 计数（不经开关，见「已知风险」第 1 条） |
| 驱动 | admin | `admin_api.py:3813`（`service_monitor_loop`） | 周期调用 `runtime_observability.dispatch()` 真正外发通知（只有 admin 在发） |

写者只有两个进程（content / admin），读者就是这两个服务的上述页面与循环。

## Staging 验证（生产切换前必须全过）

用 staging 库（例如 `huangque_staging`），**绝不指向生产 `huangque` 库**；回填源用生产
`runtime_observability.db` 的只读快照副本（`cp` 一份即可，源库全程只读打开）。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site          # 或部署机上的 checkout
alembic upgrade head                        # 建 ops.traces / ops.alert_outbox
python scripts/migrate_ops_observability.py --source /path/to/runtime_observability.db.copy
python scripts/migrate_ops_observability.py \
  --source /path/to/runtime_observability.db.copy --code-sha <m3b-commit> --apply
```

回填器契约（与 M3A 回填器同口径）：

- 不带 `--apply` 永远只读（`mode=ro` 打开源库），输出 traces/alerts 行数与源校验和。
- `--apply` 必须带 `--code-sha`；单事务写入 `ops.traces` / `ops.alert_outbox` 并记审计
  `ops.data_migration_runs` / `ops.data_migration_items`（`domain='ops'`、
  `run_id` 前缀 `ops-observability-`）。
- 逐行读回与源比对：`started/updated/duration/next_try` 按数值比，其余按字符串比，
  任何不一致整批回滚。
- 幂等：相同数据重跑为 no-op。
- **冲突护栏**：目标行 `updated` 比源新 → 说明该行已有切换后的新写入，整批中止；
  源或目标 `updated` 为空无法判定新旧时同样中止。
- 源校验和 = 两表规范化 JSON 的 SHA-256（与 M3A 同一算法），严禁只比行数。
- 源库缺表 / 空主键 / 行键超过 128 字符（审计 chunk_key 上限）都会明确报错拒绝导入。

验收：apply 通过 → 再跑一次 dry-run 行数与校验和一致 → `psql` 抽查任一行与 SQLite
逐字段一致（含浮点时间戳小数位）→ `alembic downgrade` 不可用（有意禁止破坏性降级）。

## 生产切换前仍需（观察窗口不可压缩）

1. staging 验证全过。
2. 影子核对：切换代码上线后（仍 `HQ_OBS_STORE=sqlite`），连续 48 小时对比 SQLite 与 PG
   回填结果零差异。维护窗口前的参考基线（只读 dry-run，生产库当前为空）：
   `{"traces": 0, "alerts": 0, "source_checksum": "ab78faf8878054f8fcaf2528867ed3561f3f40a3623ef7ce79224f9615a7dad7"}`
   —— 切换前请以线上当时的最新 dry-run 输出为准，不要照抄此处数值。
3. 依赖确认：目标运行环境装有 `psycopg`（psycopg 3）与 `psycopg_pool`。
4. 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物。
5. 另行固定：生产 commit、维护窗口、回滚负责人、老板切换授权。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码：`runtime_observability.py`、`observability_store.py`、`channel_manager.py`
   同时部署到 `/home/ubuntu/content-api/content_domains/`；`admin_api.py` 部署到
   `/home/ubuntu/content-api/`。**两个服务必须是同一版本**（content 与 admin 都 import
   本模块）；`server/db/*` 与本域无关，不需要部署。
2. 建最小权限运行角色（或沿用既有运行角色，仅授予）：
   `GRANT SELECT, INSERT, UPDATE ON ops.traces, ops.alert_outbox TO huangque_obs;`
   不给 DELETE / DDL / 其他 schema 任何权限。
3. 注入环境变量（两处都加，值必须一致）：
   - content：`/home/ubuntu/content-api/content.env` 加 `HQ_OBS_STORE=postgres`、
     `HQ_DATABASE_URL=postgresql://huangque_obs:…@127.0.0.1:5432/huangque`。
   - admin：`/etc/systemd/system/huangque-admin.service` 的 `Environment=`（或 admin 的
     env 文件）加同样两行。密码只落在服务器受保护 env 文件，不进 git、不进聊天。
4. 同时重启 `huangque-content` 与 `huangque-admin`。启动即检查日志无 `HQ_OBS_STORE` /
   import 报错；启动后可先自检一次：
   `HQ_OBS_STORE=postgres python3 -c "from content_domains import observability_store as s; print(s.mode())"`。
5. 端到端验证：跑一次短视频生成任务 → 后台任务详情能看到证据链且 `psql` 里
   `ops.traces` 落行；再让 admin 的服务监控产生一条事件 → `ops.alert_outbox` 落行、
   下一次 `dispatch()` 后状态变 `sent`（或按退避变 `pending`）。
6. 观察 48 小时：接单/生成/下载证据链不断、后台证据页无异常、告警条数不再增长为
   `failed`、`ops.data_migration_runs` 无新失败。SQLite 侧 `task_trace` / `alert_outbox`
   不应再出现新的 `updated`（说明没有旁路还在写旧库）。
7. SQLite 归档：`runtime_observability.db` 改名保留（不删除），`HQ_OBSERVABILITY_DB`
   保留指向（停写后仅供回滚），确认 `lsof` 无进程长期持有。

## 回滚

- 秒级：把两处 `HQ_OBS_STORE` 改回 `sqlite` 并重启 content/admin —— SQLite 文件全程只读
  保留，立即回到旧权威（证据页会只剩切换前的历史证据，这是预期现象）。
- 完整回退：部署上一个 commit（同一组文件一起回退）。
- 注意：`postgres` 模式期间写入的证据只在 PG；回滚后新证据回到 SQLite，两边会分叉。
  回滚后应立即把 SQLite 重新置为权威，并重新回填 PG 以恢复影子核对基线。
- `ops.traces` / `ops.alert_outbox` 是临时证据与发件箱：回滚后不必删除行，但不能把 PG
  行再回灌 SQLite（会与旧库的去重/重试状态打架）；要恢复一致，以 SQLite 为准重建 PG。

## 停止条件（任一触发立即停并向老板报告）

- 影子核对任何一行不一致；行数/主键集合不一致。
- 切换期间 SQLite 出现新的 `updated`（说明仍有进程在写旧库）。
- 回填或运行时报错无法解释；PG 锁等待影响用户请求；证据链在后台断档。
- 告警大面积 `failed`（说明通知投递链路坏了，不只是记录问题）。
- 备份不可恢复；双权威并存（同一时刻两处 `HQ_OBS_STORE` 不一致）。

## 已知风险与未覆盖项（收编时必须处理）

1. **`channel_manager.notification_settings()` 直连旧库**（写本手册时约
   `channel_manager.py:760-762`）：即使 `HQ_OBS_STORE=postgres`，它仍会
   `runtime_observability.database()` 打开（必要时甚至创建）SQLite 文件读 `channel.*`
   计数。后果：① 通知设置页的 `delivery` 计数显示的是旧库数据；② PG 模式下仍会碰 SQLite
   文件（`lsof` 会看到句柄，文件必须保留而不能删）。本域文件边界内无法修改该调用点，
   需主 Agent 在收编时改为走 `observability_store`（或加开关分支）—— 本域测试里对该读取
   打了桩，不是为了掩盖它，而是让断言「PG 模式不碰旧库」只针对本域代码。
2. **`admin_api.py:3729-3733` 的入队兜底只认 `sqlite3.Error`**：PG 模式下 `enqueue()`
   抛的是 psycopg 异常，会冒泡到 `service_monitor_loop` 的 `except Exception: pass`
   （`admin_api.py:3813-3818`）被静默丢弃，日志线索比 SQLite 模式更少。建议收编时把
   该处改为广域捕获 + 打日志（两模式行为一致化）。
3. **store 模块不得 import `server.db.postgres`**（本轮契约纠正）：生产 content-api 的
   sys.path 没有 `server` 包，运行时 import 会直接炸。本模块自建连接池规避了这一点；
   收编评审时请守住这条，别为了「复用池」把 `server.db.postgres` 引进来。
4. **`dispatch()` 的 PG 分支与 SQLite 循环是两份代码**（有意为之：契约要求 SQLite 路径
   逐字节不动）。任何一侧改了投递语义（退避、失败阈值、`channel.*` 路由），另一侧必须
   同步改，否则切回/切换后行为不同。收编评审时请把这两段对着看一遍。
5. 未加二级索引：与源库一致（`alert_outbox` 是小表，`dispatch` 扫描量极小）。若发件箱
   历史行数上万，再加 `(state, next_try)` 索引。
6. 回填与运行不写同一张表的两套行不会互相覆盖，但**回填期间若已切写 PG**，PG 会出现
   新行（`updated` 比源新），冲突护栏会让整批中止 —— 这是有意保护，不要绕过。
7. **主键可空性差异（唯一一处 schema 不同）**：SQLite rowid 表的非整数主键允许 NULL，
   所以源库理论上能存 `job_id/stage/event_id` 为空的行；PG 主键不允许。运行时不写空键
   （`record()` 对空 `job_id` 直接返回），回填器遇到空键会明确报错拒绝导入。若 staging
   回填真的报「空主键」，说明源里有脏数据，先人工确认再处理。
8. CI 需要在 PG 段加一行 `test_observability_store.py`（`.github/workflows/ci.yml` 由主
   Agent 改）。本域 PG 测试用 `m3b-<uuid>` 前缀的任务号与事件号，跑完自行清理，可在共享
   staging 库上重复执行。

## 本机验证记录（无 PostgreSQL 环境，PG 部分在 CI/staging 跑）

```bash
python3 -m py_compile server/db/migrations/versions/20260915_0005_ops_observability.py \
  server/content_domains/observability_store.py server/content_domains/runtime_observability.py \
  scripts/migrate_ops_observability.py tests/test_observability_store.py   # 通过
python3 scripts/sqlite_inventory.py --check                               # 本域新文件零 sqlite 依赖
python3 -m unittest discover -s tests -p 'test_observability_store.py'    # Ran 18, OK (skipped=3)
python3 -m unittest discover -s tests -p 'test_runtime_observability.py'  # Ran 10, OK
python3 -m unittest discover -s tests -p 'test_channel_manager.py'        # 本域无关联（该域另在改）
# 迁移 DDL 离线校验（mock 引擎 + PG dialect）：CREATE TABLE / 列注释 / 表注释均正常，
#   ops.traces 主键 (job_id,stage)，时间列 double precision，metadata/payload/error 为 TEXT
# 平铺布局自检：sqlite 模式不导入 psycopg；postgres 模式缺 HQ_DATABASE_URL 时报
#   "HQ_DATABASE_URL is not configured"
# 回填器 dry-run（只读）：真实源 {"traces":0,"alerts":0,…}；合成源 {"traces":3,"alerts":1,…}
# --apply 无 HQ_DATABASE_URL 时报 "HQ_DATABASE_URL is not configured"；空源报 "源没有任何行，拒绝导入"
```

18 条用例中 15 条在 sqlite 模式下验证「行为不变 / 降级语义」与「PG 分支接线」
（`PostgresBranchWiringTest` 用替身 store 在本机无 PG 时也跑），3 条是 PG 真链路
（`HQ_DATABASE_URL` 缺失时 skip，CI/staging 跑）。

CI 需要在 PG 段加一行（`.github/workflows/ci.yml` 由主 Agent 改，本域不动）：

```yaml
python -m unittest discover -s tests -p 'test_observability_store.py' -v
```
