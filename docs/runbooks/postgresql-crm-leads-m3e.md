# PostgreSQL M3E：获客线索 CRM 切换手册

## 边界

本批把「采集线索的人工跟进状态（CRM）」从 SQLite（`content-api/leads_crm.db`，
单表 `lead_crm`，主键 `(username, lead_id)`）迁到 PostgreSQL `crm.leads`，并接入运行时
读写。它不迁移其他任何域；SQLite 文件全程保留只读，任何时刻都能秒级回退。

- 源：`/home/ubuntu/content-api/leads_crm.db`（约 0.01MB，只有 `lead_crm` 一张表；
  行数以 dry-run 输出为准，切换前填入本节）。文件按首次读写懒创建——**若线上从未有
  人保存过跟进状态，该文件可能不存在**，此时回填器无行可导（视为已迁移完毕，空表即
  正确目标态）。
- 目标：`crm.leads`（`crm` schema 由 M0 foundation 迁移建好，本批只加这一张表）。
- 时间口径与源一致：`updated_at` 为秒级 Unix 时间戳（BIGINT），不做时区换算。
- 业务语义：同一 `lead_id` 在不同账号下互相不可见；写入方只有 content-api 的 CRM
  接口，读方是 content-api（合并回显）与 leadgen-api（采集列表合并回显）。

## 写者 / 读者清单（源码实测，2026-09-16）

直连 `leads_crm.db` 的模块只有一个：`server/content_domains/leads.py`
（`sqlite3.connect` 出现在 `crm_db()`，全仓库唯一；`scripts/leads_filter.py` 是离线
jsonl 过滤器，`huangque-web/content-api/content_api.py` 是遗留副本，两者都不碰该库）。

| 角色 | 位置 | 说明 |
|---|---|---|
| 写者（唯一） | `server/content_domains/core.py:3859`（POST `/api/gen/leads/crm` → `upsert_crm`）、`core.py:5258`（DELETE 同路径 → `delete_crm`） | 由 content-api（8096）承载；nginx `^~ /api/gen/` 兜底指向 8096 |
| 读者 | `server/content_domains/core.py:4973`（GET `/api/gen/leads/crm` → `list_crm`） | content-api（8096） |
| 读者 | `server/leadgen_api.py:20` import、`:574` `leads_domain._merge_saved_crm(...)` | leadgen-api（8100）；`/api/gen/leads` 精确路由到 8100，采集列表合并跟进状态 |
| 间接调用方 | `server/hq_cli_api.py:3875-3902`（leads-crm / leads-crm-upsert / leads-delete） | hq CLI 经 HTTP 代理到 CONTENT_BASE，不直连数据库 |
| 测试 | `tests/test_content_domains.py:298`、`tests/test_leadgen_job_cas.py:194`、`tests/test_collect_cos_and_refund.py:571` | 通过 monkeypatch `LEADS_CRM_DB` 指向临时库 |

**两个进程共读同一份代码目录**：`ship` 把 `server/content_domains/*` 同步到
`/home/ubuntu/content-api/content_domains/`，`huangque-content` 与
`huangque-leadgen-api` 的 `WorkingDirectory` 都是 `/home/ubuntu/content-api`。
因此切换必须两个单元一起重启，否则出现「content 写 PG、leadgen 读 SQLite」的
双权威，用户在采集列表里看不到刚保存的跟进状态。

`drift_sentinel.AUTH_SHARED_RUNTIME`（auth-service 共享运行时）**不含** `leads.py` /
`leads_store.py`；auth-service 不读本域，无需第二份部署。

## 运行时开关

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_LEADS_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值直接抛错，进程启动即失败 |
| `HQ_DATABASE_URL` | postgresql://… | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_LEADS_DB_POOL_MAX` | 1–20，默认 4 | 本域连接池上限 |

**默认 sqlite，行为与迁移前逐字节一致**：`leads.py` 的公开 API 签名、SQLite 语句、
`BEGIN IMMEDIATE`、`INSERT OR REPLACE`、异常文案（`线索ID无效` / `意向标签无效` /
`跟进状态无效` / `请选择要删除的线索` / `所选线索不存在或不属于当前账号`）全部未改，
只在读写入口加了一层分发（`list_crm` / `delete_crm` / `upsert_crm`）。

`leads_store.py` 自建懒加载 `psycopg_pool` 池，绝不吞错：连接或执行失败抛异常，由
core.py 既有的 `except Exception → 400/500` 语义转给前端。**不复用仓库里的
`server/db/postgres.py`**：该文件没有任何运行时部署映射，content-api 里不存在
`server.db` 包（M3A 的 `flags_store.py` 同理）；连接串仍统一取 `HQ_DATABASE_URL`。
本域没有缓存——每次读写直接打库，不存在 TTL 与失效问题。

## Staging 验证（生产切换前必须全过）

用 staging 库（例如 `huangque_staging`），**绝不指向生产 `huangque` 库**；回填源用生产
`leads_crm.db` 的只读快照副本。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site          # 或部署机上的 checkout
alembic upgrade head                        # 建 crm.leads（revision 20260915_0008）
python scripts/migrate_crm_leads.py --source /path/to/leads_crm.db.copy
python scripts/migrate_crm_leads.py \
  --source /path/to/leads_crm.db.copy --code-sha <m3e-commit> --apply
python -m unittest discover -s tests -p 'test_leads_store.py' -v   # PG 段不再 skip
```

回填器契约（与 M3A 一致）：

- 不带 `--apply` 永远只读，输出线索行数与源校验和（规范化 JSON 的 SHA-256，≥7 位的
  `--code-sha` 只对 apply 强制）。
- `--apply` 单事务写入 `crm.leads` 并记审计 `ops.data_migration_runs`
  （`domain='crm'`、`source_kind='sqlite'`）与 `ops.data_migration_items`
  （`source_table='lead_crm'`，chunk_key 形如 `username/lead_id`）。
- 逐行读回与源逐字段比对（intent / follow_status / follow_note / updated_at），任何
  不一致整批回滚。
- 幂等：相同源数据重跑为 no-op（数据不变，只多一条 run 记录）。
- **冲突护栏**：目标行 `updated_at` 比源新（说明 PG 侧已有切换后的新写入）→ 整批中止。
- 空源（0 行）拒绝导入；源文件缺失报错退出。

验收：apply 通过 → 再跑一次 dry-run 显示相同行数与校验和 → `psql` 抽查任一行与 SQLite
逐字段一致 → `alembic downgrade` 不可用（有意禁止破坏性降级，靠备份恢复）。

## 生产切换前仍需（观察窗口不可压缩）

1. staging 验证全过（含 `tests/test_leads_store.py` 的 PG 段）。
2. 影子核对：切换代码上线后（仍 `HQ_LEADS_STORE=sqlite`），连续 **48 小时**每日两次用
   只读快照对比 SQLite 与 PG 回填结果零差异；期间任何跟进状态修改均记录时间戳供核对。
3. 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物，且手工恢复演练过
   一次 staging。
4. 另行固定：生产 commit、维护窗口、回滚负责人、切换授权。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码：`leads.py` + `leads_store.py` 同步到
   `/home/ubuntu/content-api/content_domains/`。注意 `ship` 现有映射把
   `server/content_domains/*` 的 `svc` 只写成 `huangque-content`——**必须手工把
   `huangque-leadgen-api` 一起重启**（建议主 Agent 在 `ship` 里补一条
   `leads.py`/`leads_store.py` → `svc="huangque-content huangque-leadgen-api"` 的映射，
   与 `feature_flags.py` 的多服务写法一致）。
2. 建最小权限运行角色（或沿用既有运行角色，仅授予）：
   `GRANT SELECT, INSERT, UPDATE, DELETE ON crm.leads TO huangque_leads;`
   不给 DDL / 其他 schema 任何权限。
3. 注入环境变量：`/home/ubuntu/content-api/content.env` 加
   `HQ_LEADS_STORE=postgres` 与
   `HQ_DATABASE_URL=postgresql://huangque_leads:…@127.0.0.1:5432/huangque`。
   该文件被 content 与 leadgen-api 两个单元共用；密码只落在服务器受保护 env 文件，
   不进 git、不进聊天。
4. **同时**重启 `huangque-content` 与 `huangque-leadgen-api`。启动即检查日志无
   `HQ_LEADS_STORE` / import 报错；`journalctl -u huangque-leadgen-api -n 50` 确认
   leadgen 加载的是新代码。
5. **权威校验门禁**：sudo 跑 `scripts/check_store_authority.py --expect postgres`
   必须全绿（任一 ERROR 立即回滚），启动日志应是 INFO
   `authority announced: mode=postgres`（见 `postgresql-cutover-authority-guard.md`）。
6. 端到端验证：小程序/工作台对某条线索保存跟进状态 → `psql` 查 `crm.leads` 新值落库、
   `updated_at` 是秒级时间戳 → 重新采集同关键词，列表里跟进状态仍在该线索上（证明
   leadgen-api 也走 PG）→ 删除该线索并确认 PG 行消失。
7. 观察 48 小时：CRM 保存/删除/合并回显无异常；`ops.data_migration_runs` 无新失败。
8. SQLite 冰冻监控：切写后 1 小时每 10 分钟查 `leads_crm.db` mtime/行数必须
   「冻住」（见 `postgresql-cutover-authority-guard.md`），然后才准归档。
9. SQLite 归档：`leads_crm.db` 改名保留（不删除），确认无进程再打开（`lsof`）。

## 回滚

- 秒级：把 `content.env` 的 `HQ_LEADS_STORE` 改回 `sqlite`（或删除该行）并同时重启
  `huangque-content` 与 `huangque-leadgen-api` —— SQLite 全程只读保留，立即回到旧权威。
- 完整回退：部署上一个 commit（`leads.py` 与 `leads_store.py` 一起回退）。
- 注意：`postgres` 模式期间的 CRM 修改只在 PG；回滚后回到 SQLite，两边可能短暂分叉
  —— 回滚后立刻把 SQLite 重新置为权威，并重新回填 PG 以恢复影子核对基线。

## 停止条件（任一触发立即停并报告）

- 影子核对任何一行不一致；行数或主键集合不一致。
- 切换期间 SQLite 出现新的 `updated_at`（说明仍有进程在写旧库，通常是漏重启
  `huangque-leadgen-api`）；SQLite 冰冻监控 1 小时内 mtime 前进或行数增长
  （见 `postgresql-cutover-authority-guard.md`）。
- 权威校验器 `--expect postgres` 非全绿（进程与配置不一致 = 双权威风险）。
- 回填或运行时报错无法解释；锁等待影响用户请求。
- 备份不可恢复；双权威并存（两个单元读到不同的 `HQ_LEADS_STORE`）。
