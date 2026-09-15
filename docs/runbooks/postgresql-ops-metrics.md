# PostgreSQL metrics 域切换手册：AI 会话事件 + 运行轨迹（agent-metrics）

本域特殊：**写者代码不在主站 git 仓库里**。生产写者是服务器上的 cron 脚本
`/home/ubuntu/agent-metrics/{collect.py,collect_traj.py}`（读者 `board.py`、`export_json.py`），
所以本批交付的是「主站侧迁移 + 回填器 + 服务器脚本补丁草稿」，补丁不直接改服务器。

## 边界

- 源：`/home/ubuntu/agent-metrics/metrics.db`（单文件，两张表，1.85 MB）
  - `events`：929 行 —— journald 里的会话事件流水（收到/派发/完成/报错）
  - `runs`：768 行 —— 每个 run 一行，含 token 用量与收尾状态（成本账本的基础）
- 目标：`ops.metrics_events`、`ops.metrics_runs`（迁移 `20260915_0010`，
  `down_revision=20260915_0009`）
- 数据口径与源保持一致：
  - `ts` **是文本**，不是秒级 epoch。事件为 `2026-09-15T23:00:08.108+08:00`（本地偏移），
    运行轨迹为 `2026-09-15T15:00:06.273Z`（UTC）。看板按字符串切片展示（`ts[:16]`），
    所以 PG 侧仍是 `varchar`，**不做 timestamptz 转换**（否则展示与比较口径都会变）。
  - `''`（空串）是有意义的值，表示「该事件没有这个字段」；只有 `events.replies`
    在非 complete 行上是真正的 NULL。回填原样搬运，不做 `''`↔NULL 归一。
  - 除主键外源没有 `NOT NULL`，PG 侧也不加；整数 token 列源是 `INT`，PG 用
    `BIGINT` 防溢出。
- 行只增不改：两张表的主键都是**内容指纹**（`md5(实例|日志原文)` /
  `md5(轨迹路径|runId)`），重复采集按主键忽略。没有 `updated_at`，所以回填器的
  「目标比源新」护栏改成等价的**内容护栏**（同主键内容不同即整批中止）。

## 写者 / 读者清单（源码实测，2026-09-16 调研）

| 脚本 | 角色 | 碰到的对象 | 说明 |
|---|---|---|---|
| `collect.py` | **写者** | `events` | 每 10 分钟跑一次，`INSERT OR IGNORE` 逐行插；单次一提交。无批量 upsert、无更新、无删除 |
| `collect_traj.py` | **写者** | `runs` | 每 10 分钟跑一次，扫 `~/.openclaw*/agents/*/sessions/*.trajectory.jsonl`，`INSERT OR IGNORE` 逐行插；单次一提交 |
| `export_json.py` | 读者 | `events` + `runs` | 生成 `web/dashboard_data.json`（看板数据源） |
| `board.py` | 读者 | `events` | 输出 Markdown 看板；**不在 cron 里**，手工运行 |
| `resolve_names.py` | 无关 | 只写 `name_map.json` / `bot_groups.json` | 不碰 metrics.db |
| `balance_alert.py` | 无关 | 只读 journald，写 `.balance_alert_state.json` | 不碰 metrics.db |
| `send_selfcheck.py` | 无关 | 只调飞书 API | 不碰 metrics.db |
| `/home/ubuntu/hq-drift/drift_sentinel.py` | 无关 | 只读 `~/agent-metrics/bot_groups.json` | 不碰 metrics.db |
| `/home/ubuntu/hq-monitor/balance_sentinel.py` | 无关 | 只读 `~/agent-metrics/bot_groups.json` | 不碰 metrics.db |

并发写结论：**`events` 只有 `collect.py` 写，`runs` 只有 `collect_traj.py` 写**，两者是
cron 里的两条串行命令，不并发；其他脚本全是只读。所以本域没有多写者竞争，
切换不受「双写」影响。

crontab 相关条目（原样）：

```
*/10 * * * * cd /home/ubuntu/agent-metrics && python3 collect.py "40 min ago" >/dev/null 2>&1; python3 collect_traj.py >/dev/null 2>&1; python3 resolve_names.py >/dev/null 2>&1; python3 export_json.py >/dev/null 2>&1; python3 balance_alert.py >/dev/null 2>&1
```

**注意**：这一行把输出全部丢进 `/dev/null`，脚本报错在 cron 里完全不可见。所以切换前
必须手工带输出跑一遍（见「服务器部署步骤」第 4 步）。

## 运行时开关

两个环境变量决定脚本走哪条路，**默认 sqlite，行为与迁移前逐字节一致**：

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_METRICS_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值直接抛错 |
| `HQ_DATABASE_URL` | `postgresql://…` | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_METRICS_DB_POOL_MIN/MAX` | 默认 1 / 4 | 懒加载连接池下限/上限 |
| `HQ_METRICS_DB_POOL_TIMEOUT` | 默认 5 | 取连接超时秒数 |

密码只落在服务器受保护 env 文件（root-only，例如现有的
`/etc/huangque/postgresql/migrator.env`），**绝不进 git、不进聊天、不写进任何脚本**。

## 服务器脚本补丁与部署（本批只交付草稿，不代改服务器）

补丁在 `server_side_patches/agent-metrics/`，与原文件同名：

| 补丁文件 | 改动 |
|---|---|
| `metrics_store.py` | **新增**。PG-only 分发模块：开关、`table()` 表名映射、`?`→`%s` 与 `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING` 两条机械改写、懒加载 psycopg 池、兼容 `r['列']`/`r[0]` 的结果行 |
| `collect.py` | 连接点分支 + `events` 表名走 `metrics_store.table('events')`；SQLite 分支原地保留（含建表语句） |
| `collect_traj.py` | 连接点分支 + `runs` 表名映射；其余逐字未动 |
| `board.py` | 连接点分支 + 表名映射；3 条 SQL 改成两模式等价写法（见下） |
| `export_json.py` | 连接点分支 + 表名映射；`sum(布尔)` 改 CASE；`like 'oc_%'` 改参数；`by_client` 一条查询双写法 |

部署步骤（在服务器上，按顺序）：

1. **备份原脚本**（改之前）：
   ```bash
   cd /home/ubuntu/agent-metrics
   mkdir -p bak-m3-metrics-$(date +%Y%m%d-%H%M%S)
   cp -p collect.py collect_traj.py board.py export_json.py bak-m3-metrics-*/
   ```
2. **放置补丁**：把 `server_side_patches/agent-metrics/*.py`（5 个文件，含新增的
   `metrics_store.py`）拷到 `/home/ubuntu/agent-metrics/`。`metrics_store.py` 必须与
   四个脚本同目录（cron 是 `cd /home/ubuntu/agent-metrics && python3 collect.py`，
   脚本目录自动进 `sys.path`）。
3. **语法自检**：`python3 -m py_compile metrics_store.py collect.py collect_traj.py board.py export_json.py`
4. **不带开关手工跑一遍（看输出，不重定向）**：
   ```bash
   cd /home/ubuntu/agent-metrics
   python3 collect.py "40 min ago"; python3 collect_traj.py; python3 export_json.py; python3 board.py
   ```
   期望：与迁移前同样的「本次新增 N 条 / 库内总计 929 条」输出，`events`/`runs` 行数不变。
5. **注入 env**（两种方式，二选一，建议 A）：
   - **A（推荐）**：新建 root-only env 文件 `/etc/huangque/postgresql/metrics.env`，
     内容是 `export HQ_METRICS_STORE=postgres` 与 `export HQ_DATABASE_URL=postgresql://…`，
     然后把 cron 那一行改成用 `. ` 载入后串行跑（**整行在同一个 shell 里，5 个脚本
     拿到同一份 env，不会出现读写分家**）：
     ```
     */10 * * * * bash -c 'cd /home/ubuntu/agent-metrics && set -a && . /etc/huangque/postgresql/metrics.env && set +a && python3 collect.py "40 min ago" >/dev/null 2>&1; python3 collect_traj.py >/dev/null 2>&1; python3 resolve_names.py >/dev/null 2>&1; python3 export_json.py >/dev/null 2>&1; python3 balance_alert.py >/dev/null 2>&1'
     ```
   - **B（复用 M3A 现成模式）**：cron 行里先 `export`，URL 用 M3A 影子核对同样的
     `sudo grep -oP "(?<==).*" /etc/huangque/postgresql/migrator.env` 取。
     ⚠️ 取值为空时脚本会 fail-loud（不写任何行），但那一次运行会整条退回 SQLite 口径，
     存在「一轮 PG、下一轮 SQLite」的抖动风险 —— 建议 A。
6. **验证 cron 正在跑**（切换后 10~20 分钟）：
   - PG 侧行数在涨、SQLite 侧 `events`/`runs` 行数不再涨；
   - `ls -l /home/ubuntu/agent-metrics/web/dashboard_data.json` mtime 每 10 分钟更新；
   - `crontab -l` 里那一行没有语法错（`bash -c` 引号嵌套要检查）。
7. **最小权限角色**（切换前建好）：
   `GRANT SELECT, INSERT, UPDATE ON ops.metrics_events, ops.metrics_runs TO huangque_metrics;`
   不给 DELETE/DDL/其他 schema。回滚不需要回收权限。

## SQL 口径约定（改脚本时必读）

`metrics_store` 只做两条机械改写（`?`→`%s`、`INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`），
**不猜查询意图**。所以脚本里的 SQL 必须自己满足：

- 布尔聚合写 `sum(case when <条件> then 1 else 0 end)`，不要写 SQLite 的 `sum(列='x')`
  （PG 没有 `sum(boolean)`）。
- `GROUP BY` 不写输出别名、不留裸列（SQLite 允许，PG 报错）。
- 需要 LIKE 通配时把 `'oc_%'` 当参数传，别写进 SQL 文本（避开占位符转义）。
- 结果行同时支持 `r['列']` 与 `r[0]`。

## Staging 验证（生产切换前必须全过）

```bash
cp -p /home/ubuntu/agent-metrics/metrics.db /tmp/metrics.db.snapshot   # 或先 python3 sqlite3 backup()
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site
alembic upgrade head                                     # 建 ops.metrics_events / ops.metrics_runs
python3 scripts/migrate_ops_metrics.py --source /tmp/metrics.db.snapshot                 # dry-run
python3 scripts/migrate_ops_metrics.py --source /tmp/metrics.db.snapshot \
        --code-sha <metrics-域 commit> --apply
```

回填器契约：

- 不带 `--apply` 永远只读：只报「源行数 + 源校验和（全表规范化 JSON 的 SHA-256）」。
- `--apply` 必须带 `--code-sha`；单事务写入两张表并记审计
  `ops.data_migration_runs` / `items`（`domain='ops'`）。
- 逐行读回与源逐字段比对（含类型），任何不一致整批回滚。
- 幂等：内容一致的行不写，重跑输出 `written=0`。
- **内容护栏**：同主键在目标里已存在但内容不同 → 整批中止，绝不覆盖。
- 源为空（0 行）直接拒绝导入。

验收：apply 通过 → 再 dry-run 一次 → `psql` 抽查任一行逐字段与 SQLite 一致 →
`alembic downgrade` 不可用（有意禁止破坏性降级，靠备份恢复）。

## 影子核对（48 小时观察期，仍以 SQLite 为权威）

推荐做法：cron 里每 12 小时对**新鲜快照**再跑一次回填 `--apply`（幂等），
期望输出 `written=0, unchanged=1697`；退出码非零（护栏触发）即告警。
另外在每个观察周期里，用 PG 数据跑一遍 `export_json.py` 的等价查询，
与 `web/dashboard_data.json` 逐字段对比 —— 唯一允许的差异见「已知差异」。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. staging 全过 + 影子核对 48h 零差异 + 备份确认（`huangque-postgres-backup.timer` 有成功产物）。
2. 服务器上执行「部署步骤」1~4 已经做过（补丁就位、SQLite 模式跑通）。
3. 停写窗口：先把 crontab 那一行注释掉（或改为手工触发），确认没有在途的
   `collect.py` / `collect_traj.py`（`pgrep -f collect.py`）。
4. 用停写后的 SQLite 快照做**最后一次**回填 `--apply`（应 `written=0`）。
5. 注入 env（A 或 B），放开 crontab，观察一轮：PG 行数涨、SQLite 行数不涨。
6. 观察 48 小时：看板数字与切换前一致、`ops.data_migration_runs` 无新失败、
   服务器磁盘上 `metrics.db` mtime 不再变化。
7. SQLite 归档：`metrics.db` 改名保留（不删除），确认无进程再打开（`lsof`）。

## 回滚（秒级）

- 把 `HQ_METRICS_STORE` 改回 `sqlite`（或原样恢复 crontab 那一行）——SQLite 文件全程
  留在原地、只增不改，立即回到旧权威。
- 脚本文件回退：从 `bak-m3-metrics-*` 覆盖回去（补丁的 SQLite 分支与迁移前一致，
  正常不需要回退脚本）。
- 注意：`postgres` 模式期间新采集的**增量行只在 PG**。回滚后要把这段时间的 PG 增量
  回灌进 SQLite 才能保住时间序（回灌脚本不在本批范围，切换窗口建议选在低峰，
  且窗口内先停写、后切换，把增量窗口压到一轮以内）。

## 停止条件（任一触发立即停并上报）

- 回填护栏触发（同主键内容不同）；行数或校验和对不上。
- cron 一轮里出现「PG 没涨、SQLite 涨了」（说明 env 注入失效，退回旧权威）。
- 脚本报错无法解释；`dashboard_data.json` 出现数字跳变或字段缺失。
- 服务器上任何进程仍在写 `metrics.db`（切换后 `mtime` 还在变）。
- 备份不可恢复；两处 env 不一致导致双权威并存。

## 已知差异与风险（切换前必须看）

1. **`by_client` 的「客户标签」列在 PG 模式下取值确定性不同**（唯一有意差异）。
   SQLite 原文 `group by capability, agent, client` 里 `client` 既是输出别名又是输入列，
   实测 SQLite 取**输入列**（与显式 `events.client` 逐行相同），但同一 SELECT 里还留了
   裸列 `grp`，PG 报错。PG 侧改为显式按输入列分组 + 显示列用 `max(...)`。
   分组键、dispatch/reply 计数、排序、`limit` 完全一致（实测逐行一致），
   只有这一列的标签可能取到组内不同成员（SQLite 是未定义顺序的任意一行）。
   要彻底消除，得先修 SQLite 原文的歧义写法（本域不做，避免改变迁移前行为）。
2. **`ts` 是文本**：本域不引入 timestamptz，因此没有时区换算差异，但也意味着
   PG 侧不能直接用时间函数比较（看板本来就是字符串切片）。
3. **语句级自动提交**：PG 模式下每条写语句自成事务，`con.commit()` 是空操作。
   最终落库数据与 SQLite 模式一致，只是更早可见；没有跨表事务需求。
4. **cron 静默**：那一行把输出丢进 `/dev/null`，所以任何 SQL 方言没改干净、env 注入失败
   都只能靠「行数不涨」发现。切换后头几天要人工盯行数。
5. **本地无 PG**：本批的 PG 路径未实跑（本机没有 PostgreSQL），已实跑的是
   SQLite 模式逐字节等价 + 回填器 dry-run + `metrics_store` 自检；
   PG 侧必须在服务器 staging 上按上面的命令实跑一遍再切换。
6. `runs` 主键 `run_key = md5(轨迹文件绝对路径|runId)`：**把轨迹文件搬家会导致同一个
   run 生成新主键**。切换前后不要移动 `~/.openclaw*/agents/*/sessions/` 目录。
