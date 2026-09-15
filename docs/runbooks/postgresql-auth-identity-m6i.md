# PostgreSQL M6-I：auth-service 身份域（users.db）切换手册

> 草稿状态（2026-09-16，M6 准备批）：本批只交付「PG 建表 + 回填器 + 测试 + 本手册」，
> **不切写、不改生产行为**。store 模块与 `auth_server.py` 的分发改造留到切写阶段，
> 本文的「生产切换步骤」是那时的执行清单。

## 边界

本批把 auth-service 的身份数据库从 SQLite（`/home/ubuntu/auth-service/users.db`）
迁到 PostgreSQL `identity` schema，共 **26 张表**（表名不变，前缀 `identity.`）。
它不迁移其他任何域：点数流水（`points_audit` / `point_transfers` /
`recharge_orders` / `virtual_pay_orders` / `membership_recharge_records` /
`membership_upgrade_records` / `invite_reward_point_records`）归 **ledger 域（0012）**；
`identity.users.points` 只是**余额快照列**，整行照搬、**绝不重算**。
SQLite 文件全程保留，任何时刻都能秒级回退。

- 源：`/home/ubuntu/auth-service/users.db`（7.3MB，2026-09-16 01:11 快照）。
- 目标：`identity` schema（由 M0 foundation 建好），26 张表见下表。
- 迁移文件：`server/db/migrations/versions/20260916_0011_auth_identity.py`（`20260916_0011`）。
- 回填器：`scripts/migrate_auth_identity.py`。
- 测试：`tests/test_auth_identity_migration.py`。

### 表清单与行数（源库实测 2026-09-16）

| # | 源表 = 目标表 `identity.<名>` | 行数 | 主键 | 时间口径 |
|---|---|---|---|---|
| 1 | `users` | 127 | `id`（自增） | `created_at` **TEXT** `YYYY-MM-DD HH:MM:SS`（UTC）；`membership_started_at/expires_at` 秒级 epoch |
| 2 | `tokens` | 527 | `token` | `created_at` **TEXT**（同上）；`expires_at` 秒级 epoch |
| 3 | `cli_device_grants` | 1764 | `id`（自增） | 秒级 epoch |
| 4 | `cli_refresh_tokens` | 1276 | `id`（自增） | 秒级 epoch |
| 5 | `cli_action_requests` | 418 | `(username,action,request_id)` | 秒级 epoch |
| 6 | `membership_audit` | 134 | `id`（自增） | 秒级 epoch |
| 7 | `membership_voice_slot_entitlements` | 95 | `username` | 秒级 epoch |
| 8 | `friendships` | 5 | `id`（自增） | 秒级 epoch |
| 9 | `friend_requests` | 3 | `id`（自增） | 秒级 epoch |
| 10 | `invite_campaigns` | 1 | `id`（自增） | 秒级 epoch |
| 11 | `invite_codes` | 50 | `id`（自增） | 秒级 epoch |
| 12 | `user_invites` | 45 | `id`（自增） | 秒级 epoch |
| 13 | `invite_reward_claims` | 10 | `id`（自增） | 秒级 epoch |
| 14 | `invite_reward_notifications` | **0** | `id`（自增） | 秒级 epoch |
| 15 | `invite_admin_audit` | 23 | `id`（自增） | 秒级 epoch |
| 16 | `canvas_boards` | 13 | `id`（TEXT） | 秒级 epoch |
| 17 | `canvas_members` | 3 | `(board_id,username)` | 秒级 epoch |
| 18 | `canvas_ops` | 41 | `(board_id,version)` | 秒级 epoch |
| 19 | `canvas_presence` | 4 | `(board_id,client_id)` | 秒级 epoch |
| 20 | `business_cards` | 38 | `user_id`（=users.id） | 秒级 epoch |
| 21 | `card_referral_journeys` | 1 | `journey_id`（TEXT） | 秒级 epoch |
| 22 | `network_node_ids` | 7 | `user_id`（=users.id） | 秒级 epoch |
| 23 | `user_notifications` | 191 | `id`（自增） | 秒级 epoch |
| 24 | `announcement_campaigns` | 2 | `id`（自增） | 秒级 epoch |
| 25 | `wechat_subscription_grants` | 26 | `(username,event_type,template_id)` | 秒级 epoch |
| 26 | `wechat_subscription_outbox` | 1060 | `id`（自增） | 秒级 epoch |

合计 **5864 行**。另有 8 张源库表不在本域：7 张 ledger 表 +
`sqlite_sequence`（SQLite 内部表，不迁移；其值只用于对齐 PG 序列，见下）。

类型映射：`TEXT→TEXT`、`INTEGER→BIGINT`、`REAL→DOUBLE PRECISION`、
`BLOB→BYTEA`（本域实测**没有** REAL/BLOB 列，类型审计零异常）。
`INTEGER` 但语义是 0/1 的列转真布尔（`users.must_change`、
`users.card_initial_password`、`invite_campaigns.code_required`、
`business_cards.{phone_public,email_public,address_public,wechat_qr_public,
discoverable_in_network}`、`announcement_campaigns.wechat_push_requested`）；
写入方必须 `bool()` 归一化，psycopg3 不接受 0/1 进 `boolean`。
时间口径**逐一照搬**：只有 `users.created_at` / `tokens.created_at` 是 TEXT，
其余全是秒级 epoch（BIGINT），不引入 `timestamptz`。

源库没有任何 `FOREIGN KEY`，PG 侧也不加：逻辑引用（`user_id`、`inviter_user_id`…）
保持现状，避免历史孤儿行让切写失败。

### 自增编号必须对齐（重要）

源库 15 张表用 `INTEGER PRIMARY KEY AUTOINCREMENT`，含义是**编号永不复用**：

```
users                        127 行，max(id)=505，sqlite_sequence.seq=505
invite_reward_notifications    0 行，sqlite_sequence.seq=3
```

`users` 已经删过 378 个账号，新序列若从 1 开始，注册新号会**复用已删除账号的 id**，
而 `user_invites.invitee_user_id`、`business_cards.user_id`、`canvas_*.username`…
全部按 id/username 逻辑引用，复用即数据串台。因此回填器在导入后把每张自增表的
PostgreSQL 序列推到 `max(源最大 id, 源 sqlite_sequence.seq) + 1`
（`identity.users` → 506，`identity.invite_reward_notifications` → 4）。
回填输出里的 `sequences` 字段是这一步的留档。

## 写者 / 读者清单（源码 + 生产实例实测，2026-09-16）

`users.db` 是**多进程共用**的库，切写必须一次覆盖全部写者，否则出现双权威。

### 写者

| 进程 / 文件 | 角色 | 写哪些表 |
|---|---|---|
| **`huangque-auth.service`**（`/usr/bin/python3 /home/ubuntu/auth-service/auth_server.py`，WorkingDirectory=`/home/ubuntu/auth-service`） | 唯一主写者，常驻 | 几乎全部：`users`（注册/改密/封禁/点数）、`tokens`、`cli_device_grants`（`hq_cli_api` 函数 + `auth_server.py:6086`）、`cli_refresh_tokens`、`membership_audit`、`membership_voice_slot_entitlements`、`friendships`、`friend_requests`、`invite_*`（经 `invites.py`）、`canvas_*`、`business_cards`（经 `business_cards.py`）、`card_referral_journeys`、`network_node_ids`、`user_notifications`、`announcement_campaigns`、`wechat_subscription_grants`、`wechat_subscription_outbox` |
| **`huangque-invite-reward-claims.service` + `.timer`**（每分钟一次，`/usr/bin/python3 /home/ubuntu/auth-service/process_invite_reward_claims.py --database /home/ubuntu/auth-service/users.db --limit 100`） | **第二个写者进程** | `invite_reward_claims`（过期清算）、`invite_reward_notifications`（经 `invites.expire_pending_claims`） |
| **`huangque-leadgen-api`**（`/usr/bin/python3 /home/ubuntu/content-api/leadgen_api.py`） | 读者 + **退点兜底写者** | `users.points`：仅在 auth 点数接口失败时直连 `AUTH_DB`（`leadgen_api.py:32` 默认 `/home/ubuntu/auth-service/users.db`；`:294-334` 直写并记日志「回退直写 users.db；本次不进 points_audit」） |

仓库内对应的代码位置（供改造时定位）：

- `server/auth_server.py`：`DB = .../users.db`（:50）；`users` 写点 :908/:957/:1088/:1107、
  点数 :2298/:2348/:2562/:2567；`tokens` :4563/:2738/:4554/:4746/:4793/:4933/:7362/:7390；
  `membership_audit` :3290；`membership_voice_slot_entitlements` :3314/:4385；好友 :1289/:1293/:1319/:1402/:1428/:1432/:1434；
  `canvas_*`、`user_notifications`、`announcement_campaigns`、`wechat_subscription_*` 全在本文件。
- `server/hq_cli_api.py`（**库**，由 auth_server 注入连接工厂）：`cli_device_grants` :2265/:2407/:1906/:2300/:2318/:2361/:2366/:2374/:2487/:2494/:2515；`cli_refresh_tokens` :1911/:2500。
- `server/invites.py`（**库**，调用方传连接）：`invite_campaigns/invite_codes/user_invites/invite_reward_claims/invite_reward_notifications/invite_admin_audit`；另会 `UPDATE users`（:1555/:1564 封禁/解封）与 `DELETE FROM tokens`（:1556）、`UPDATE cli_device_grants`（:1558）。
- `server/business_cards.py`、`server/invite_network.py`（**库**，调用方传连接）：`business_cards`、`card_referral_journeys`、`network_node_ids`。
- `scripts/backfill_launch_experience_members.py`（离线一次性工具，2026-07 已跑完）：写 `users/membership_audit/membership_voice_slot_entitlements`。

### 读者

| 进程 / 文件 | 读什么 |
|---|---|
| `huangque-auth.service` | 全部表（登录校验、点数、会员、后台只读接口） |
| `huangque-leadgen-api`（`leadgen_api.py:238`） | `users.points`（直连 `AUTH_DB` 读余额） |
| `huangque-admin`（`admin_api.py`） | 只读 `users.db`/`jobs.db` 做后台洞察——**经 auth 的接口**，不直连 |
| `huangque-imggen-api` | 已改造为只调 auth 原子接口，**不再直连 users.db**（`imggen_api.py:410` 注释留痕） |
| `scripts/db_backup.py`（cron 03:30） | 备份清单里有 `auth-service/users.db` |
| `scripts/check_membership_launch_readiness.py` | 离线只读快照（`--db` 传路径） |
| `huangque-web/content-api/content_api.py` | **遗留副本**（含 `AUTH_DB` 与 `UPDATE users`），生产已不部署；现行代码是 `server/content_api.py` |

### 服务与部署事实

- `huangque-auth.service`：`ExecStart=/usr/bin/python3 auth_server.py`，
  `EnvironmentFile=-/home/ubuntu/auth-service/auth.env`，
  drop-in 里还有 `FEATURE_FLAGS_DB`、硬化项与 `points.conf`。
  `/usr/bin/python3` 是 **Python 3.10.12**，已装 **psycopg 3.3.5 + psycopg_pool** ——
  身份域 store 可以沿用同一解释器，**不需要换 Python**（M3A 的 auth 侧 store 也是这么跑的）。
- `huangque-invite-reward-claims.timer`：`OnUnitActiveSec=1min`，服务是
  `Type=oneshot`、`ProtectSystem=strict`、`ReadWritePaths=/home/ubuntu/auth-service`。
  **切写时它必须一起改**：要么停掉（清算本来就是幂等兜底，可停到切写后补跑），
  要么让它同样走 PG。
- `scripts/drift_sentinel.py` 的 `BACKEND_RUNTIME` 已覆盖
  `auth_server.py`/`hq_cli_api.py`/`invites.py`/`invite_network.py`/`business_cards.py`/
  `wechat_subscribe.py`/`process_invite_reward_claims.py`；新增的 store 模块要按
  `AUTH_SHARED_RUNTIME` 或对应映射补进去（漏部署 = auth 起来就 import 失败）。

## 运行时开关（切写阶段引入，本批不落地）

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_IDENTITY_STORE` | `sqlite`（默认）/ `postgres` | 身份域读写权威；非法值直接抛错 |
| `HQ_DATABASE_URL` | `postgresql://…` | 仅 `postgres` 模式需要；空值即报错 |
| `HQ_IDENTITY_DB_POOL_MAX` | 1–20，默认 4 | 身份域自己的连接池上限 |

- 开关名已与 `scripts/check_store_authority.py`（`huangque-auth` 段的
  `HQ_IDENTITY_STORE` 期望值 `sqlite`）和
  `docs/runbooks/postgresql-cutover-authority-guard.md` 对齐。
- 行为契约与 M3A 一致：默认 `sqlite` 时**逐字节不变**（公开 API 签名、SQLite 语句、
  `BEGIN IMMEDIATE`、异常文案、fail-closed 语义全保留），只在读写入口加分发；
  `postgres` 模式下 store 模块自建懒加载 `psycopg_pool`，绝不吞错。
- **按表分组切**是允许且推荐的（同 `admin_config` 的做法），但同一张表同一时刻
  只能有一个权威：
  - A 组（auth 主进程独占）：`users`、`tokens`、`cli_*`、`membership_*`、
    `friendships`、`friend_requests`、`canvas_*`、`user_notifications`、
    `announcement_campaigns`、`wechat_subscription_*`。
  - B 组（auth + 每分钟清算服务双写）：`invite_campaigns`、`invite_codes`、
    `user_invites`、`invite_reward_claims`、`invite_reward_notifications`、
    `invite_admin_audit`、`business_cards`、`card_referral_journeys`、`network_node_ids`
    —— 切这两组时必须**两个进程同版本同开关**。
  - `cli_action_requests`：**当前代码已无读写方**（旧版 `hq_cli_api.py` 的残留，
    最后写入 2026-09-01）。建表 + 回填只为留档，**不接线**；归档期再评估删除。

## Staging 验证（生产切换前必须全过）

本批**不在 staging 跑 alembic**（由主 Agent 收编后统一跑 `0011→0012` 链）。
收编后按下面执行，**绝不指向生产库 `huangque`**：

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site            # 部署机 checkout
alembic upgrade head                          # 建 identity.* 26 张表（rev 20260916_0011）
python3 scripts/migrate_auth_identity.py --source /path/to/users.db.copy          # dry-run
python3 scripts/migrate_auth_identity.py \
  --source /path/to/users.db.copy --code-sha <m6i-commit> --apply                 # 回填
python3 scripts/migrate_auth_identity.py --source /path/to/users.db.copy          # 再 dry-run：行数/校验和不变
HQ_DATABASE_URL=… python3 -m unittest discover -s tests -p 'test_auth_identity_migration.py' -v
```

回填源用生产 `users.db` 的**只读快照副本**（`cp` 一次即可；不要指向活库，
更不要为了「一致性」去停 auth 服务）。

回填器契约（与 M3A/M3D 完全一致）：

- 不带 `--apply` 永远只读，输出 26 张表行数与源校验和（规范化 JSON 的 SHA-256，
  `sort_keys` + 紧凑分隔符 + `default=str`）；`--code-sha` 只对 `--apply` 强制，且
  长度 <7 位直接拒绝。
- `--apply` **单事务**写入 `identity.*`，并记审计
  `ops.data_migration_runs`（`domain='identity'`、`source_kind='sqlite'`）与
  `ops.data_migration_items`（`chunk_key` 形如 `username/action/request_id`）。
- 逐行读回与源逐列比对（布尔按真假、其余按字符串），任何不一致整批回滚。
- 幂等：同数据重跑只多一条 run 记录，行不重复、值不变。
- **冲突护栏**：目标行 `updated_at` 比源新 → 说明 PG 侧已有切写后的新写入，整批中止。
  只有 9 张带 `updated_at` 的表参与护栏（`cli_action_requests`、`invite_campaigns`、
  `invite_reward_claims`、`invite_reward_notifications`、`canvas_boards`、
  `business_cards`、`user_invites`、`wechat_subscription_grants`、
  `wechat_subscription_outbox`）；其余表源没有该列。
- **源库加列即停**：读源时先用 `PRAGMA table_info` 核对列集合，与回填清单不一致
  （有人 `ALTER TABLE` 加了列）立刻报错，绝不静默漏搬字段。
- 空源（0 行）拒绝导入；源文件缺失报错退出。
- 敏感列（口令哈希/盐、各类令牌与设备码哈希、openid、手机号/邮箱）只参与比对与
  校验和，**报错信息只打印长度**（`<redacted N chars>`），不落明文。

验收：apply 通过 → 再跑 dry-run 行数与校验和一致 → `psql` 抽查
`identity.users`（含 `points` 与 `created_at` 格式）、`business_cards`（布尔列是真布尔）、
`wechat_subscription_outbox`（UNIQUE 三列键）→ 确认 `identity.users_id_seq`
`last_value=506`、`is_called=false` → `alembic downgrade` 不可用（有意禁止，靠备份恢复）。

## 生产切换前仍需（观察窗口不可压缩）

1. staging 验证全过（含 `test_auth_identity_migration.py` 的 PG 段）。
2. 影子核对：切写代码上线后（仍 `HQ_IDENTITY_STORE=sqlite`）连续 **48 小时**
   用只读快照比对 SQLite 与 PG 回填结果零差异；期间任何注册/登录/邀请动作记时间戳备查。
3. 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物且演练过恢复；
   `users.db` 仍在 `db_backup.py` 清单里，切换前手工留一份只读快照。
4. 另行固定：生产 commit、维护窗口、回滚负责人、老板切换授权。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码（同版本、一次性）：
   - `server/content_domains/identity_store.py`（PG-only 新模块，切写阶段才创建）
     与各入口的分发改造（`auth_server.py`、`invites.py`、`business_cards.py`、
     `hq_cli_api.py`）；
   - `server/process_invite_reward_claims.py` 分支（脚本在服务器上，注意它 import
     的是 `invites`，改造后必须同版本）。
   部署后按 `drift_sentinel` 映射确认线上文件 == git（含新增的 store 模块）。
2. 建最小权限运行角色：
   ```sql
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA identity TO huangque_identity;
   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA identity TO huangque_identity;
   ```
   不给 DDL / 其他 schema 权限。
3. 注入环境变量：`/home/ubuntu/auth-service/auth.env` 加
   `HQ_IDENTITY_STORE=postgres` 与
   `HQ_DATABASE_URL=postgresql://huangque_identity:…@127.0.0.1:5432/huangque`。
   密码只落在服务器受保护 env 文件，不进 git、不进聊天。
4. 重启并核对：
   - `sudo systemctl restart huangque-auth`；
   - `sudo systemctl stop huangque-invite-reward-claims.timer`（或让它同版本同开关，
     二选一；**不能一边停一边留旧版脚本写 SQLite**）；
   - `huangque-leadgen-api` 的退点兜底：切写期间必须让它走 auth 的 HTTP 点数接口，
     **不得再直连 `AUTH_DB` 写 SQLite**（`AUTH_DB` 指向只读快照或改成走 PG）。
   - 日志检查：`journalctl -u huangque-auth -n 50` 无 import/开关报错，
     且看到 store 的权威声明日志 `mode=postgres`（缺了就是没配到，见
     `postgresql-cutover-authority-guard.md` 第一层）。
5. 跑权威校验器（门禁，作为「生产切换步骤」的必过项）：
   ```bash
   sudo /usr/bin/python3 /home/ubuntu/m3a-verify-full/scripts/check_store_authority.py
   sudo /usr/bin/python3 /home/ubuntu/m3a-verify-full/scripts/check_store_authority.py --expect postgres
   ```
   全绿才许进观察期；不过即按下面「回滚」处置。
6. 功能验证（逐项，都是用户能看见的现象）：
   - 注册一个新账号 → `psql` 查 `identity.users` 有行、`created_at` 是
     `YYYY-MM-DD HH:MM:SS` 格式、`points=16`；同时确认
     `identity.users_id_seq` 已经越过 505（新号 id 不落回历史区间）。
   - 登录/登出 → `identity.tokens` 增删；`hq` CLI 重新登录 → `identity.cli_device_grants`
     走 pending→approved→issued。
   - 会员/邀请：报名邀请码绑定 → `identity.user_invites` 有行；
     每分钟清算服务跑完不报错（`journalctl -u huangque-invite-reward-claims`）。
   - 名片：发布名片 → `identity.business_cards` 布尔列是真布尔（`psql` 会显示 `t/f`）。
   - 微信订阅：触发一次推送 → `identity.wechat_subscription_outbox`
     新增行且 `status=sent`、`sent_at` 有值。
7. 观察 48 小时：登录/点数/邀请/名片/公告/订阅推送无异常；
   `ops.data_migration_runs` 无新失败；`users.db` **mtime 不动**（冰冻监控）。
8. SQLite 归档：确认无进程再打开后改名保留（**不删除**）——
   `sudo lsof /home/ubuntu/auth-service/users.db` 必须为空。

## 回滚（秒级）

- **秒级**：把 `auth.env` 的 `HQ_IDENTITY_STORE` 改回 `sqlite`（或删除该行），
  `sudo systemctl restart huangque-auth`，并按第 4 步恢复
  `huangque-invite-reward-claims.timer` 与 leadgen 的直连兜底。
  SQLite 全程只读保留，立刻回到旧权威。
- 完整回退：部署上一个 commit（store 模块与四个入口文件一起回退）。
- 注意：`postgres` 模式期间的写入只在 PG（新注册、新邀请、新令牌、新名片…）。
  回滚后这些数据不在 SQLite 里 —— 回滚后立刻把 SQLite 置为权威，并重跑回填器
  把 PG 拉回对齐基线；必要时人工比对 `identity.users.id`/`username` 集合后再决定
  合并方向（**点数余额尤其要人工确认**，不得自动合并）。

## 敏感数据纪律

`users.db` 里有口令哈希/盐、登录令牌、CLI 设备码与刷新令牌哈希、微信 openid、
手机号/邮箱：

- 回填器对敏感列只做搬运与比对，报错只打印长度；校验和是 SHA-256，不落明文。
- 迁移日志、`psql` 抽查、工单、聊天里都不得贴这些列的原文；
  `psql` 抽查请只选 `id/username/points/status/...` 这类非敏感列。
- 生产快照副本用完即删；不要留在共享目录。

## 停止条件（任一触发立即停并向老板报告）

- 影子核对任何一行不一致；任一张表行数或主键集合不一致。
- 切写后 `users.db` mtime 前进或行数增长（= 仍有进程写旧库，常见漏网：
  `huangque-invite-reward-claims.timer`、leadgen 退点兜底、手动脚本）。
- 权威校验器报 ERROR / 退出码非 0；auth 日志出现
  「not set, falling back to default」(= 开关没生效)。
- 新注册账号 id 落回历史区间（序列没对齐），或出现重复 id/username 冲突。
- 登录失败率、点数读写异常升高；`ops.data_migration_runs` 出现 failed。
- 备份不可恢复；双权威并存（auth 配了 postgres 而某个写者仍用 sqlite）。
