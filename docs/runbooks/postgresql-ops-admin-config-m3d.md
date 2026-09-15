# PostgreSQL M3D：管理配置（admin_config.db）切换手册

## 边界

本批把「托管密钥池 + 渠道开关 + 管理审计 + 后台验收台账 + 灵感案例库」从 SQLite
（`content-api/admin_config.db`）迁到 PostgreSQL `ops` schema。它不迁移其他任何域；
SQLite 文件全程保留，任何时刻都能按表秒级回退。

- 源：`/home/ubuntu/content-api/admin_config.db`（生产 7 张表：`provider_api_keys` 7 行、
  `admin_audit` 129 行、`admin_e2e_runs` 134 行、`admin_e2e_delivery_checks` 20 行、
  `admin_e2e_fixture_attempts` 1 行、`admin_channel_config` 0 行、`inspiration_cases` 0 行；
  另有 SQLite 内部表 `sqlite_sequence`，不迁移）。
- 目标：`ops` schema，见下表。时间口径与 SQLite 一致：秒级 Unix 时间戳（BIGINT），
  不引入 `timestamptz`，避免影子核对出现隐式换算差异。

| 源表 | 目标表 | 主键 | M3D 是否已切写 |
|---|---|---|---|
| `provider_api_keys` | `ops.admin_provider_api_keys` | `id` | ✅ 已接线（`provider_keys.py`） |
| `admin_channel_config` | `ops.admin_channel_config` | `channel` | ✅ 已接线（`admin_api.py`） |
| `admin_audit` | `ops.admin_audit` | `id`（Identity） | ✅ 已接线（`admin_api.py`） |
| `admin_e2e_runs` | `ops.admin_e2e_runs` | `run_id` | ⛔ 未接线，SQLite 仍是权威 |
| `admin_e2e_fixture_attempts` | `ops.admin_e2e_fixture_attempts` | `fixture_key` | ⛔ 未接线，SQLite 仍是权威 |
| `admin_e2e_delivery_checks` | `ops.admin_e2e_delivery_checks` | `job_id` | ⛔ 未接线，SQLite 仍是权威 |
| `inspiration_cases` | `ops.admin_inspiration_cases` | `id`（Identity） | ⛔ 未接线，SQLite 仍是权威 |

`provider_api_keys` 与 `inspiration_cases` 落 `ops` 时补了 `admin_` 前缀，避免与既有
`ops` 表（`feature_flags`/`pricing_rules`/`data_migration_*`）撞名；其余表名本身已是
`admin_*`，保持不变。

**本域是按表切换的**，不是整文件切换：同一时刻每张表只能有一个权威。未接线的四张表
（`admin_e2e_*` 与 `inspiration_cases`）在 PG 建表 + 回填后仍是 SQLite 权威，切换时
不必也不得把它们当权威；它们留给 M3E（`admin_e2e_*` 的读写散在 `admin_api.py` 的
约 45 个函数里，需要逐条 PG SQL 改写，不属于本次最小改动范围；
`server/inspiration_cases.py` 不在本批文件边界内）。

## 写者 / 读者清单（从源码扫出来的）

写者（按表）：

- `ops.admin_provider_api_keys`
  - **admin**（`server/admin_api.py` → `provider_keys.add_key` / `retire_key` / `set_health`，
    后台密钥保险箱 UI）
  - **content**（`server/content_domains/video*.py`、`server/providers/short_drama_visual/*`
    → `provider_keys.claim_candidate` 使 `use_count+1`；`provider_keys.set_health` 写健康状态；
    进程启动时 `_snapshot_legacy_env_keys` 把环境变量密钥一次性托管）
  - ⚠️ 两个服务都会写，**必须同版本同开关一起切**，否则同一把密钥被两处轮转记账。
- `ops.admin_channel_config`：**admin**（`save_channel`）。
- `ops.admin_audit`：**admin**（`_admin_audit` 统一入口 + 渠道/开关/价格/批次四处内联插入）。
- `ops.admin_e2e_*` / `ops.admin_inspiration_cases`：**admin**（未接线，仍写 SQLite）。

读者：

- **admin**：后台全部页面（渠道清单、运行历史、服务事件、验收台账、密钥池列表）。
- **content**：派单前 `provider_keys.candidates()` / `claim_candidate()`（可能扣点的付费任务）。
- **auth-service 不读 admin_config.db**（auth 侧只共享 `feature_flags`/`pricing`/`cos` 等，
  `content_domains/provider_keys.py` 不部署到 auth），因此本轮**不需要**动 `drift_sentinel`
  的 `AUTH_SHARED_RUNTIME`。
- `scripts/db_backup.py` 把 `admin_config.db` 列入备份清单（运维工具，不是业务读者）。

## 运行时开关

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_ADMIN_CONFIG_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值直接抛错 |
| `HQ_DATABASE_URL` | postgresql://… | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_ADMIN_CONFIG_DB_POOL_MAX` | 1–20，默认 4 | `admin_config_store` 自己的连接池上限 |

默认 `sqlite` 时行为与迁移前逐字节一致：公开 API 签名、缓存语义、fail-closed、报错
行为全部不变，只在读写入口加了分发。`admin_config_store.py` 是 PG-only 的新模块
（自建懒加载 `psycopg_pool`，不 import `server.db.postgres`），绝不吞错：连接或执行
失败一律抛异常，由上层沿用既有「fail-closed / 安全缓存 / 只读降级」语义。

## Staging 验证（生产切换前必须全过）

用 staging 库（如 `huangque_staging`），**绝不指向生产 `huangque` 库**；回填源用生产
`admin_config.db` 的只读快照副本。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site
alembic upgrade head                       # 建 7 张 ops.admin_* 表（rev 20260915_0007）
python scripts/migrate_ops_admin_config.py --source /path/to/admin_config.db.copy
python scripts/migrate_ops_admin_config.py \
  --source /path/to/admin_config.db.copy --code-sha <m3d-commit> --apply
HQ_DATABASE_URL=… python -m unittest discover -s tests -p 'test_admin_config_store.py'
```

回填器契约（与 M3A 一致）：

- 不带 `--apply` 永远只读，输出 7 张表的行数与源校验和；**加密列只输出长度，绝不回显**。
- `--apply` 必须带 `--code-sha`；单事务写全部 7 张表，并记审计
  `ops.data_migration_runs` / `ops.data_migration_items`（`domain='ops'`）。
- 逐行读回与源比对（布尔按真假、加密列按字节、其余按字符串），任何不一致整批回滚。
- 幂等：同数据重跑等价 no-op（再次逐行 upsert 并读回比对，数据不变）。
- **冲突护栏**：目标行 `updated_at` 比源新 → 说明切换期间 PG 侧已有新写入，整批中止。
- 自增主键（`admin_audit.id` / `admin_inspiration_cases.id`）显式带源编号回填，收尾把
  PostgreSQL 序列推到源最大编号之后（否则新插入会撞主键）。
- 源校验和 = 7 张表规范化 JSON 的 SHA-256（`sort_keys` + 紧凑分隔符 + `default=str`）。

验收：apply 通过 → 再跑一次 apply 无差异 → `psql` 抽查任一行与 SQLite 逐字段一致 →
`alembic downgrade` 不可用（有意禁止破坏性降级，靠备份恢复）。

## 生产切换前仍需（观察窗口不可压缩）

1. staging 验证全过（含 `test_admin_config_store.py` 的 PG 部分）。
2. 影子核对：切换代码上线后（仍 `HQ_ADMIN_CONFIG_STORE=sqlite`）连续 **48 小时**对比
   SQLite 与 PG 回填结果零差异；期间后台的密钥/渠道操作记录时间戳供核对。
3. 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物，且手工恢复演练过一次
   staging；`admin_config.db` 本身仍在 `db_backup.py` 清单里。
4. 另行固定：生产 commit、维护窗口、回滚负责人、老板切换授权。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码（一次性、同版本）：`server/content_domains/admin_config_store.py`、
   `server/content_domains/provider_keys.py`、`server/admin_api.py`。
   - `content-api/content_domains/` 侧必须**同时**有 `admin_config_store.py`：`provider_keys.py`
     新增了 `from . import admin_config_store`，漏部署 = 起服务就 import 失败。
   - 该目录由 `drift_sentinel` 的 `content_domains` 目录映射自动覆盖新增文件；主 Agent
     收编时确认映射对新增文件生效（`AUTH_SHARED_RUNTIME` 不需要新增条目）。
2. 建最小权限运行角色（或沿用既有角色，仅授予）：
   `GRANT SELECT, INSERT, UPDATE ON ops.admin_provider_api_keys, ops.admin_channel_config,
   ops.admin_audit TO huangque_admincfg;` 不给 DELETE / DDL / 其他 schema 权限。
   （未接线的四张表暂不授权。）
3. 注入环境变量：`/home/ubuntu/content-api/content.env` 与 admin 服务的 systemd 单元加
   `HQ_ADMIN_CONFIG_STORE=postgres`、`HQ_DATABASE_URL=postgresql://huangque_admincfg:…@127.0.0.1:5432/huangque`。
   密码只落在服务器受保护 env 文件，不进 git、不进聊天。
4. 同时重启 admin 与 content（密钥池两个写者必须同版本同开关），启动即检查日志无
   `HQ_ADMIN_CONFIG_STORE` / import 报错。
5. **权威校验门禁**：sudo 跑 `scripts/check_store_authority.py --expect postgres`
   必须全绿（任一 ERROR 立即回滚），启动日志应是 INFO
   `authority announced: mode=postgres`（见 `postgresql-cutover-authority-guard.md`）。
6. 功能验证（逐项）：
   - 后台密钥池：加一把测试密钥 → `psql` 查 `ops.admin_provider_api_keys` 有行、
     `ciphertext` 是密文、`base_url` 已冻结；再下架该密钥。
   - 派单：`provider_keys.claim_candidate()` 走 PG 时 `use_count` 累加，任务能正常取到密钥。
   - 渠道开关：改一个渠道 → 5 秒内生效 → `ops.admin_channel_config` 有行且同一事务里
     `ops.admin_audit` 有 `channel.save` 行。
   - 审计读回：后台运行历史页能看到新审计行。
7. 观察 48 小时：付费任务取密钥、轮转、后台操作无异常；`ops.data_migration_runs` 无新失败。
8. SQLite 冰冻监控：切写后 1 小时每 10 分钟查 `admin_config.db` mtime/行数必须
   「冻住」（见 `postgresql-cutover-authority-guard.md`），然后才准归档。
9. SQLite 归档：`admin_config.db` 改名保留（**不删除**，未接线的四张表仍在用），
   确认没有进程打开已归档文件（`lsof`）。

## 回滚

- 秒级：`HQ_ADMIN_CONFIG_STORE` 改回 `sqlite` 并重启 admin + content —— SQLite 全程保留，
  立即回到旧权威。
- 完整回退：部署上一个 commit（三个文件一起回退）。
- 注意：`postgres` 模式期间的密钥/渠道/审计变更只在 PG；回滚后这些表回到 SQLite，
  两边可能短暂分叉 —— 回滚后立刻把 SQLite 置为权威，并重跑回填器把 PG 拉回对齐基线。
  密钥池尤其要留意：回滚期间若两处都产生过新密钥，需人工比对 `id` 集合后再决定合并方向。

## 敏感数据（迁移与日志纪律）

`ops.admin_provider_api_keys` 存的是**真实渠道密钥的加密副本**：

- 敏感列：`ciphertext`（AES-GCM 密文）、`nonce`（随机数）；配合
  `HQ_PROVIDER_KEYS_MASTER_KEY`（env，只存服务器）才能解开；`last4` 只有末四位，
  不构成凭证但也不应进入对外报表。
- 迁移注意事项：回填器只把这两列当字节搬运与比对，报错信息里**只打印长度**
  （`<binary N bytes>`）；校验和是 SHA-256 哈希，不落明文。备份/`psql` 抽查时不要把
  这两列贴进聊天、工单或日志。
- 日志脱敏：`provider_keys` 的日志与异常里从不带明文；PG 侧的 `last_error` 只存
  「渠道返回的错误摘要」（最长 180 字），上游错误体若可能回显密钥必须在写入前截断
  （现有实现沿用 SQLite 行为）。`ops.admin_audit.detail` 禁止记录密钥明文
  （后台已有的 `SECRET_RE` 校验不变）。
- 权限：运行角色只给 `SELECT/INSERT/UPDATE`，不给 `DELETE`（审计与密钥池只增不删）。
- 环境变量在 `postgres` 模式下**不再**被反复读成权威：新付费任务优先取 PG 里的托管密钥；
  但 `_snapshot_legacy_env_keys` 仍会在启动时把 env 里的密钥托管一次，切换时不要同时
  改 env 密钥，否则会出现两把「新」密钥。

## 停止条件（任一触发立即停并向老板报告）

- 影子核对任何一行不一致；行数/主键集合不一致。
- 切换期间 SQLite 出现新的 `updated_at`（说明仍有进程在写旧库）；SQLite 冰冻监控
  1 小时内 mtime 前进或行数增长（见 `postgresql-cutover-authority-guard.md`）。
- 权威校验器 `--expect postgres` 非全绿（进程与配置不一致 = 双权威风险）。
- 密钥池出现「同明文重复添加」「同一把密钥被两个服务重复记账」或任务取不到密钥。
- 回填或运行时报错无法解释；锁等待影响付费任务接单。
- 备份不可恢复；admin 与 content 两处 `HQ_ADMIN_CONFIG_STORE` 不一致（双权威）。
