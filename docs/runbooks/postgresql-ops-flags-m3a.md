# PostgreSQL M3A：功能开关 + 价格规则切换手册

## 边界

本批把「平台功能开关 + 能力单价」从 SQLite（`content-api/feature_flags.db`）迁到
PostgreSQL `ops` schema，并接入运行时读写。它不迁移其他任何域；SQLite 文件全程保留
只读，任何时刻都能秒级回退。

- 源：`/home/ubuntu/content-api/feature_flags.db`，两张表 `feature_flags`（17 个开关）、
  `pricing_rules`（2 条单价）。生产只有这一份，auth/content 都经 `FEATURE_FLAGS_DB`
  env 指向它。
- 目标：`ops.feature_flags`、`ops.pricing_rules`。时间口径与 SQLite 一致：秒级 Unix
  时间戳（`updated_at` BIGINT）。
- 读方：content / imggen / leadgen 接单前读；admin 写入；auth-service 持只读共享副本。
- 写者唯一：admin（`set_enabled` / `set_price`）。切换只换权威，不改变任何 API 语义。

## 运行时开关

两个环境变量控制读写路径，**默认 sqlite，行为与迁移前逐字节一致**：

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_FLAGS_STORE` | `sqlite`（默认）/ `postgres` | 读写权威；非法值直接抛错，进程启动失败 |
| `HQ_DATABASE_URL` | postgresql://… | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_FLAGS_DB_POOL_MAX` | 1–20，默认 4 | 连接池上限 |

`flags_store.py` 自建懒加载 `psycopg_pool` 池，绝不吞错：连接或执行失败抛异常，上层沿用
既有「5 秒安全缓存 + 目录默认 / fail-closed」语义。写操作成功后本进程缓存立即失效
（`invalidate_cache()`）；其余进程最多 5 秒后看到新值。

## Staging 验证（生产切换前必须全过）

用 staging 库（例如 `huangque_staging`），**绝不指向生产 `huangque` 库**；回填源用
生产 `feature_flags.db` 的只读快照副本。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site          # 或部署机上的 checkout
alembic upgrade head                          # 建 ops.feature_flags / ops.pricing_rules
python scripts/migrate_ops_flags_pricing.py --source /path/to/feature_flags.db.copy
python scripts/migrate_ops_flags_pricing.py \
  --source /path/to/feature_flags.db.copy --code-sha <m3a-commit> --apply
```

回填器契约（与 Codex 惯例一致）：

- 不带 `--apply` 永远只读，输出 flags/prices 行数与源校验和。
- `--apply` 必须带 `--code-sha`；单事务写入 `ops.feature_flags` / `ops.pricing_rules` 并
  记审计 `ops.data_migration_runs` / `items`。
- 逐行读回与源比对（含类型），任何不一致整批回滚。
- 幂等：相同数据重跑为 no-op（skipped）。
- **冲突护栏**：目标行 `updated_at` 比源新，说明切换期间 SQLite 上还有写入，整批中止。
- 源校验和 = 两表规范化 JSON 的 SHA-256，严禁只比行数。

验收：apply 通过 → 再跑一次 dry-run 显示全 skipped → 用 `psql` 抽查任一行与 SQLite
逐字段一致 → `alembic downgrade` 不可用（有意禁止破坏性降级，靠备份恢复）。

## 生产切换前仍需（观察窗口不可压缩）

1. staging 验证全过。
2. 影子核对：切换代码上线后（仍 `HQ_FLAGS_STORE=sqlite`），连续 **48 小时**每日两次
   对比 SQLite 与 PG 回填结果零差异；期间 admin 任何开关操作均记录时间戳供核对。
3. 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物，且手工恢复演练过
   一次 staging。
4. 另行固定：生产 commit、维护窗口、回滚负责人、老板切换授权。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码：`feature_flags.py`、`pricing.py`、`flags_store.py` 三个文件同时部署到
   `content-api/content_domains/` 与 `auth-service/content_domains/` 两份（
   `drift_sentinel` 的 `AUTH_SHARED_RUNTIME` 已含 `flags_store.py`；两份任何一份漏部署
   都会 import 失败）。
2. 建最小权限运行角色（或沿用既有运行角色，仅授予）：
   `GRANT SELECT, INSERT, UPDATE ON ops.feature_flags, ops.pricing_rules TO huangque_flags;`
   不给 DELETE / DDL / 其他 schema 任何权限。
3. 注入环境变量（两处）：
   - content：`/home/ubuntu/content-api/content.env` 加 `HQ_FLAGS_STORE=postgres`、
     `HQ_DATABASE_URL=postgresql://huangque_flags:…@127.0.0.1:5432/huangque`。
   - auth：`/etc/systemd/system/huangque-auth.service` 的 `Environment=` 加同样两行。
   - 密码只落在服务器受保护 env 文件，不进 git、不进聊天。
4. 同时重启 `huangque-content` 与 `huangque-auth`（其余读方服务同版本同开关一起切）。
   启动即检查日志无 `HQ_FLAGS_STORE` / import 报错。
5. **权威校验门禁**：sudo 跑 `scripts/check_store_authority.py --expect postgres`
   必须全绿（任一 ERROR 立即回滚），并确认启动日志里是 INFO
   `authority announced: mode=postgres` 而非 WARNING「not set, falling back」
   （见 `postgresql-cutover-authority-guard.md`）。
6. 后台开关验证：admin 改一个开关 → 5 秒内 content 侧生效 → `psql` 查 `ops.feature_flags`
   新值落库且 `updated_at` 秒级时间戳正确；再改回。
7. 观察 48 小时：接单、报价、管理员操作无异常；`ops.data_migration_runs` 无新失败。
8. SQLite 冰冻监控：切写后 1 小时每 10 分钟查 `feature_flags.db` mtime/行数必须
   「冻住」（见 `postgresql-cutover-authority-guard.md`），然后才准归档。
9. SQLite 归档：`feature_flags.db` 改名保留（不删除），`FEATURE_FLAGS_DB` 指向失效后
   确认无进程再打开（`lsof`）。

## 回滚

- 秒级：把两处 `HQ_FLAGS_STORE` 改回 `sqlite` 并重启 content/auth —— SQLite 全程只读
  保留，立即回到旧权威。
- 完整回退：部署上一个 commit（三个文件一起回退）。
- 注意：`postgres` 模式期间的开关修改只在 PG；回滚后 admin 操作回到 SQLite，两边可能
  短暂分叉 —— 回滚后立刻把 SQLite 重新置为权威，并重新回填 PG 以恢复影子核对基线。

## 停止条件（任一触发立即停并向老板报告）

- 影子核对任何一行不一致；行数/主键集合不一致。
- 切换期间 SQLite 出现新的 `updated_at`（说明仍有进程在写旧库）；SQLite 冰冻监控
  1 小时内 mtime 前进或行数增长（见 `postgresql-cutover-authority-guard.md`）。
- 权威校验器 `--expect postgres` 非全绿（进程与配置不一致 = 双权威风险）。
- 回填或运行时报错无法解释；锁等待影响用户请求。
- 备份不可恢复；双权威并存（同一时刻两处 `HQ_FLAGS_STORE` 不一致）。
