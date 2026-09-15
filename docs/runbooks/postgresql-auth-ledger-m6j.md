# PostgreSQL M6J：账务域（ledger）切换手册（草稿）

> 状态：**准备阶段**（只写了迁移文件、回填器、测试与本手册；未切写、未改生产行为、未碰生产库）。
> 本域红线：**账务零容忍** —— 点数、退款、金额、会员到期任何修正都必须由老板批准后人工执行；
> 迁移代码只做「照搬 + 核对」，**绝不自动改任何一方数据**。

## 边界

本批把 auth-service 的 7 张账务表从 SQLite（`/home/ubuntu/auth-service/users.db`）迁到
PostgreSQL `ledger` schema，并预留运行时读写开关。它不迁移其他任何域；SQLite 文件全程保留，
任何时刻都能秒级回退。

- 源：`/home/ubuntu/auth-service/users.db`（7.0 MB，auth-service 独占写入；
  同库还有身份域的表，由 M6 身份批负责，本批不碰）。
- 目标：`ledger` schema 的 7 张表（同名）：
  `points_audit`、`point_transfers`、`invite_reward_point_records`、`recharge_orders`、
  `membership_recharge_records`、`membership_upgrade_records`、`virtual_pay_orders`。
  SQLite 内部表 `sqlite_sequence` 不是业务数据，跳过（PG 侧由 identity 序列接管）。
- 迁移文件：`server/db/migrations/versions/20260916_0012_auth_ledger.py`
  （`revision="20260916_0012"`，`down_revision="20260916_0011"` 身份域，单链接龙）。
- **时间口径**：7 张表全部是**秒级 Unix 时间戳（BIGINT）**，没有一条 TIMESTAMP 文本。
  抽样与源码双确认：`points_audit.created_at = 1789478747`、`point_transfers.created_at = 1786114671`、
  `virtual_pay_orders.created_at = 1785908903`；写入方一律 `int(time.time())`。
  PG 侧不引入 `timestamptz`，避免隐式换算在影子核对里移动一行。
  （同库 `users.created_at` 是 TEXT `datetime('now')`，属身份域，本批不涉及。）
- **布尔口径**：本域 **7 张表没有任何 0/1 布尔列**。`virtual_pay_orders.env` 是支付环境
  枚举（`0` = 正式、`1` = 沙箱），`server/wechat_virtual_pay.py` 用 `int(env)` / `env == 0`
  选密钥（PROD / SANDBOX），因此 PG 侧保留 `BIGINT`；把它当布尔会让退款查单打到错误环境。

## 源库 schema 摘要（2026-09-16 实测，只读快照）

| 表 | 行数 | 主键 | 关键约束/索引 | 时间列 |
|---|---|---|---|---|
| `points_audit` | 8933 | `id` AUTOINCREMENT | 唯一 `transaction_key`；`(username, id DESC)` | `created_at` 秒级 epoch |
| `point_transfers` | 4 | `id` AUTOINCREMENT | 唯一 `transfer_id`；唯一 `(sender_user_id, request_id)`；CHECK `amount>0`、双方不同；两侧 `(…, created_at DESC, id DESC)` | `created_at` |
| `invite_reward_point_records` | 28 | `id` AUTOINCREMENT | 唯一 `upgrade_record_id`；部分唯一 `claim_id`；部分唯一 `(invite_relation_id, invitee_level) WHERE event_type='upgrade' AND status IN ('recorded','pending_review')`；`(inviter_user_id, id DESC)` | `created_at`、`voided_at` |
| `recharge_orders` | 70 | `order_id` TEXT | — | `created_at`、`reviewed_at` |
| `membership_recharge_records` | 20 | `id` AUTOINCREMENT | 唯一 `request_id`；`(username, id DESC)` | `created_at` |
| `membership_upgrade_records` | 106 | `id` AUTOINCREMENT | 部分唯一 `(source, source_order_id) WHERE source_order_id NOT NULL AND <>''`；`(user_id, id DESC)` | `created_at`、`voided_at` |
| `virtual_pay_orders` | 41 | `order_id` TEXT | `(username, created_at DESC)` | `created_at`、`paid_at`、`credited_at`、`delivered_at` |

`users` 余额快照（同一快照，只用于核对，不迁移）：**127 个账号，`SUM(points) = 1,407,366`**。
7 张表合计 **9202 行**；dry-run 源校验和（规范化 JSON 的 SHA-256）见回填器输出。

## 写者 / 读者清单（源码实测，2026-09-16）

`users.db` 的**业务写者只有一个进程**：auth-service（systemd `huangque-auth`，
`ExecStart=/usr/bin/python3 /home/ubuntu/auth-service/auth_server.py`，
`WorkingDirectory=/home/ubuntu/auth-service`）。7 张账务表全部由它（含被它 import 的
`invites.py`）读写；没有任何其他服务打开这些表。

| 角色 | 位置 | 说明 |
|---|---|---|
| 写者（唯一进程） | `server/auth_server.py:348-600` `init_db()` | 建表 / 加列 / 建索引（含本域 7 表） |
| 写者 | `auth_server.py:2255 _write_audit` ← 调用点 `2309`（任务预扣）、`2357`（任务退点）、`2581/2586`（点数赠送出/入）、`4211`（虚拟支付入账）、`4398`（会员首购退款扣点）、`4450`（微信支付退款扣点）、`4499` | `points_audit` 的全部写入；扣点/退点与余额同事务，余额不足整体回滚不留孤儿流水 |
| 写者 | `auth_server.py:1115`（名片邀请注册奖励）、`3573 adjust_points_admin`、`3796`（充值审批入账） | `points_audit` 的直写路径（**无 transaction_key**） |
| 写者 | `auth_server.py:2572`（点数赠送事务） | `point_transfers` + 两条镜像 `points_audit`（`points-transfer:<id>:out|in`） |
| 写者 | `auth_server.py:3495 / 3540` | `membership_recharge_records`（管理员开通、支付订单入账） |
| 写者 | `auth_server.py:3711`（下单）、`3814/3843/3856/4453`（审批、补流水号、拒绝、退款） | `recharge_orders` |
| 写者 | `auth_server.py:4099`（下单）、`3986/4001/4126/4134/4167/4181/4222/4485/4504`（状态、发货、退款） | `virtual_pay_orders` |
| 写者 | `server/invites.py:440 / 1539-1565`、`auth_server.py:4379`（退款作废） | `membership_upgrade_records` |
| 写者 | `server/invites.py:495 / 591 / 521 / 993 / 1017 / 1544-1552` | `invite_reward_point_records`（发奖、绑定 claim、作废） |
| 写者（离线脚本） | `scripts/backfill_launch_experience_members.py:251` | 体验官上线一次性补 `membership_upgrade_records`（读 `recharge_orders`） |
| 读者 | `auth_server.py:3282`（用户详情 `ledger`）、`3609 list_points_audit`、`2611 list_point_transfers`、`3726 list_recharge_orders`、`3204/3208`（充值单 + 虚拟支付单）、`3455/4355`（会员记录）、`4369`（作废查询） | 工作台 / 后台 HTTP 接口 |
| 读者 | `server/invites.py:1192-1194`（后台营收统计读 `recharge_orders`+`virtual_pay_orders`）、`1379`（join `points_audit` 做名片归因） | 后台报表 |
| 读者 | `server/invite_network.py:97/181`、`server/business_cards.py:562` | 邀请网络与名片页展示奖励 |
| 读者（只读核对） | `scripts/check_membership_launch_readiness.py`（`--db` 指向 users.db 快照） | 上线前体检 |
| **不直连** | `server/content_domains/points.py` | 全部走 HTTP 调 auth-service `/api/auth/points*`（扣、退、查、按 transaction_key 查） |

**必须一起处理的旁路写者（本批不迁移，但会绕开 `points_audit` 改余额）**：

- `server/leadgen_api.py:286 _add_points_direct` —— auth 接口失败时**直写 `users.points`**，
  「无事务保护、不进 points_audit」（源码注释原话）。当前 4 张流水表里看不到这类变动，
  核对时表现为「未入流水的余额变动」。
- `huangque-web/content-api/content_api.py:743` —— 旧主站副本，同样直写 `users.points`。
- `auth_server.py:1079-1107` —— 新用户注册赠送 `NEW_USER_TRIAL_POINTS`（env
  `HQ_AUTH_TRIAL_POINTS`，生产为 16）**直接写 `users.points`**，只有「名片邀请注册」才写流水。

> 因此余额核对里出现的 +16 差额绝大多数是注册赠送，不是丢钱；两类差额都要人过一遍（见下）。

`drift_sentinel.py` 的 `AUTH_SHARED_RUNTIME` 目前不含本域的 store 文件；切写阶段新增
auth-service 侧 store 代码后，**主 Agent 必须在 `scripts/drift_sentinel.py` 里补映射**，
否则 auth-service 运行目录缺文件会 import 失败（M3A 已踩过）。

## 运行时开关（切写阶段落地，本批只写设想）

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_LEDGER_STORE` | `sqlite`（默认）/ `postgres` | 本域 7 张表的读写权威；非法值直接抛错，进程启动即失败 |
| `HQ_DATABASE_URL` | `postgresql://…` | 仅 `postgres` 模式需要；空值即报错（沿用 `server/db/postgres.py` 懒加载池） |
| `HQ_LEDGER_DB_POOL_MAX` | 1–20，默认 4 | 本域连接池上限 |

约定与其余域一致：**默认 sqlite，行为与迁移前逐字节一致**；只在读写入口加分发，不改公开 API、
不加缓存、不改报错文案。`points_audit` 的写入必须继续与余额变更同事务（免费内测期间
`apply_balance=False` 的记账口径不变）。

## Staging 验证（生产切换前必须全过）

用 staging 库（例如 `huangque_staging`），**绝不指向生产 `huangque`**；回填源用生产
`users.db` 的只读快照副本（`scp` 一份，不要在原库上跑任何东西）。

```bash
export HQ_DATABASE_URL='postgresql://migrator:…@127.0.0.1:5432/huangque_staging'
cd /home/ubuntu/huangque-main-site
alembic upgrade head          # 建 ledger 7 表（revision 20260916_0012）
# 1) 只读 dry-run：7 表行数 + 源校验和
python scripts/migrate_auth_ledger.py --source /path/users.db.copy
# 2) 余额核对（只读 SQLite，不需要 PG）
python scripts/migrate_auth_ledger.py --source /path/users.db.copy --verify-balances --limit 0
# 3) 回填（单事务；差异需老板批准后带上指纹）
python scripts/migrate_auth_ledger.py --source /path/users.db.copy \
  --code-sha <m6j-commit> --ack-balance-report <上一步的 balance_report_checksum> --apply
# 4) 幂等性：重跑同一条命令应为 no-op（只多一条 run 记录）
# 5) PG 段测试（不再 skip）
python -m unittest discover -s tests -p 'test_auth_ledger_migration.py' -v
# 6) 抽查：psql 里逐字段比几行（点数/金额/时间戳/状态）
```

回填器契约（与 M3A/M3E 一致）：

- 不带 `--apply` 永远只读；`--apply` 必须带 `--code-sha`（≥7 位）。
- 单事务写 7 张表 + 写审计 `ops.data_migration_runs`（`domain='ledger'`、`source_kind='sqlite'`）
  与 `ops.data_migration_items`（`source_table` 为源表名，`chunk_key` 为源主键）。
- 逐行读回逐字段比对，任何不一致整批回滚；幂等（相同数据重跑 no-op）。
- **冲突护栏**：7 张表都没有 `updated_at`，改用本表真正会变的「最新活动时间」列
  （`points_audit/point_transfers/membership_recharge_records` 用 `created_at`；
  `invite_reward_point_records`/`membership_upgrade_records` 用 `max(created_at, voided_at)`；
  `recharge_orders` 用 `max(created_at, reviewed_at)`；
  `virtual_pay_orders` 用 `max(created_at, paid_at, credited_at, delivered_at)`）。
  目标行比源新 → 说明 PG 侧已有切换后的新写入 → **整批中止**。
- 自增主键显式带源编号回填，结束前把 5 张表的 identity 序列推到源最大编号之后。
- **硬报警一律拒绝 `--apply`**：余额差异无法归因、最后一条流水的 `after_points` 与
  `users.points` 不符、流水行自身不自洽（`before != after` 且 `after-before != delta`）、
  流水存在重复主键。

## 余额核对（本域最关键的验收项）

命令：

```bash
python scripts/migrate_auth_ledger.py --source /path/users.db.copy --verify-balances \
  --balance-scope consume --report /tmp/ledger-balance.json
```

**口径（源码依据）**：

1. `points_audit` 是唯一权威流水：每条 `delta` 就是当次余额变动，`before_points`/`after_points`
   是当时真实余额快照，`transaction_key` 是幂等键。
2. `point_transfers` 的每笔都会同时写两条 `points_audit`
   （`points-transfer:<transfer_id>:out|in`）；核对时按 `transaction_key` 去重，
   **只补算没有镜像的那部分**（本批 4 笔全部有镜像，补算数 = 0）。
3. `invite_reward_point_records` 是**独立奖励账本，不并入可消费点数**
   （`server/invites.py`：「返回独立邀请奖励积分汇总；绝不读取或修改 users.points」；
   `docs/membership-launch-runbook.md`：「邀请奖励进入独立奖励点数台账，不改变可消费点数」）。
   因此默认口径 `consume` 不把它计入余额，只单独列报；`--balance-scope strict` 才按任务书
   的「三流水合计」口径计算（那一口径必然出现差异，仅供交叉核对）。
4. 逐笔重算的余额恒等式：
   `users.points = 期初余额 + Σ 流水行之间的未入流水变动 + Σ(delta 真正落余额的部分)`。
   回填器把每一项的差额来源逐条列出：`opening_balance`（第一条流水之前的余额）、
   `between_ledger_rows`（两行之间的未入流水变动，例如 `leadgen_api` 兜底直写、注册赠送 16 点）、
   `non_balance_delta`（免费内测期间 `apply_balance=False`：`delta` 记了但余额没动）。

### 本次准备阶段实测（2026-09-16，生产 users.db 只读快照）

- 127 个账号 + 4 个已被删除账号的历史流水（`123456789`、`new2026-7-26`、
  `qa_mp_001_…`、`test-mystery-shopper`，仍在 `points_audit` 里，**照搬不删**，仅点名）。
- 余额与流水**完全一致**：12 人（`diff = 0`）；**每个有流水的账号，最后一条流水的
  `after_points` 都等于 `users.points`**（`ledger_end_mismatch = 0`）。
- 其余 115 人有差额，且**全部可归因，没有一笔无法解释**（`unexplained = 0`）：
  - 99 人差额正好 **+16**（注册赠送 `HQ_AUTH_TRIAL_POINTS=16` 直接写 `users.points`，无流水）；
  - 少数老账号是**流水启用前的期初余额**（`opening_balance`：fang 11585、tang1 1762、
    qilin 8732、fang1 1718、yuelei 7083、yuanzhi 467 …）；
  - 1439 条「免费内测记账不落余额」的行（`delta ≠ 0` 且 `before == after`，合计 −7460 点）；
  - 3 个账号（dapeng/dapeng1/zepeng，共 5776 点）**一条流水都没有** → 只能点名，无法从流水复原。
- 报告指纹（`consume` 口径）：`balance_report_checksum = 6522d61963b096b660a07903bbaa5e43c8f355469ff772836e667f47808a94c7`。

### 差异处置（唯一合法流程）

1. 差异清单**只报告**：`--verify-balances --report` 输出逐用户明细（用户、SQLite 余额、
   重算余额、差额、差额来源、标签）。
2. **绝不自动修改任何一方**：回填器不改 `users.points`，不改流水，不做「补齐」写入。
3. 处置必须**老板批准**后人工执行：批准前不得 `--apply`；`--apply` 需要把老板已看过的
   那份报告的 `balance_report_checksum` 原样传入（`--ack-balance-report`），
   指纹不匹配即拒绝写入 —— 防止「批的是一份、写的是另一份」。
4. 任何**硬报警**（见上「回填器契约」）都不属于「可批准」范畴：停下、报告、查清再谈。
5. 已删除账号的历史流水一律照搬（账务只增不删），不作废、不迁移到别的账号。

## 生产切换前仍需（观察窗口不可压缩）

1. staging 验证全过（含 `tests/test_auth_ledger_migration.py` 的 PG 段与回填源段）。
2. 身份域（0011）与本域的代码分发同时到位：auth-service 侧 store 文件必须先跑通
   `drift_sentinel` 映射；`server/db/postgres.py` 与 `HQ_DATABASE_URL` 在 auth 机器上可用。
3. 影子核对：连续 **48 小时**每日两次用只读快照对比 SQLite 与 PG 回填结果零差异；
   期间任何充值/退款/赠送/会员变更都记录时间戳与订单号供核对。
4. 备份确认：`huangque-postgres-backup.timer` 近 48 小时有成功产物；`scripts/db_backup.py`
   的 `users.db` 备份也在跑（两边的恢复演练都做过一次）。
5. 另行固定：生产 commit、维护窗口、回滚负责人、**老板的切换授权与余额差异清单批准**。

## 生产切换步骤（低峰维护窗口，可秒级回滚）

1. 发布代码到 `/home/ubuntu/auth-service/`（auth_server.py 及被它 import 的模块），
   并在 `scripts/drift_sentinel.py` 里补 `AUTH_SHARED_RUNTIME` 映射（主 Agent 收编）。
   **运行环境注意**：`huangque-auth.service` 用的是系统解释器 `/usr/bin/python3`
   （不是 venv）。实测该解释器已有 `psycopg 3.3.5` 与 `psycopg_pool 3.3.1`，
   `server/db/postgres.py` 的懒加载池可用；**部署前仍要再确认一次**
   （`/usr/bin/python3 -c 'import psycopg, psycopg_pool'`），若缺失则先装依赖再改开关，
   否则 auth-service 起来就 import 失败、点数接口全挂。
2. 建最小权限运行角色（或沿用既有角色，仅授予）：
   `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ledger TO huangque_ledger;`
   另需 `SELECT` ‑ 由身份域按需授予 `identity.users`（若切写阶段余额查询改读 PG）。
   **不授予** DDL / 其他 schema 权限。
3. 注入环境变量：`/home/ubuntu/auth-service/auth.env`（或 systemd `Environment=`）加
   `HQ_LEDGER_STORE=postgres` 与 `HQ_DATABASE_URL=postgresql://huangque_ledger:…@127.0.0.1:5432/huangque`。
   密码只落在服务器受保护 env 文件，不进 git、不进聊天。
4. **停写窗口**：短时间内停止受理触点（充值、赠送、会员开通、扣退点），
   等 `points_audit` 最后一条 `after_points` 与 `users.points` 再次核对一致；
   再跑一次 `--verify-balances` 确认没有新的未归因差异。
5. 回填在停写窗口内执行一次 `--apply`（带当次批准指纹），随后重启 `huangque-auth`，
   日志确认无 import / `HQ_LEDGER_STORE` 报错。
6. 端到端验证（每一类都做一次并留证据）：
   - 充值：后台批一张待审充值单 → PG `ledger.points_audit` 多一条、`recharge_orders.status=approved`；
   - 任务扣退点：跑一个真实任务 → `points_audit` 有 `job-charge` 与 `job-refund` 两行，
     余额与流水末条 `after_points` 一致；
   - 点数赠送：会员发起一笔小额赠送 → `point_transfers` 1 行 + `points_audit` 2 行（out/in）；
   - 会员开通/退款：各走一次，确认 `membership_recharge_records` 与
     `membership_upgrade_records` 状态、`invite_reward_point_records` 作废联动正确。
7. 观察 48 小时：点数接口无 5xx、无「余额与流水不符」新告警、
   `ops.data_migration_runs` 无失败；每日两次余额核对保持 `unexplained = 0`。
8. 归档：`users.db` 改名保留（不删除），确认无进程再打开（`lsof`）后才谈归档。

## 回滚

- 秒级：把 `auth.env`（或 systemd `Environment=`）的 `HQ_LEDGER_STORE` 改回 `sqlite`
  （或删除该行）并重启 `huangque-auth` —— SQLite 全程只读保留，立即回到旧权威。
- 完整回退：部署上一个 commit（auth-service 侧所有改动一起回退）。
- 注意：`postgres` 模式期间的账务写入只在 PG；回滚后这些记录**不会自动回到 SQLite**。
  回滚后立刻把 SQLite 重新置为权威、重新回填 PG 以恢复影子核对基线，
  **并把 PG 侧窗口期内的写入导出成一份人工对账单**交老板决定如何补记（账务零容忍：
  不允许代码自动回灌任何点数或金额）。

## 停止条件（任一触发立即停并向老板报告）

- 余额核对出现**无法归因**的差异，或出现硬报警（末条流水与余额不符、流水行不自洽、重复主键）。
- 影子核对任何一行不一致；7 表行数或主键集合不一致。
- 切换窗口内 SQLite 仍有新写入（说明还有进程在写旧库）；
  或 `leadgen_api` / 旧 content-api 的直写 `users.points` 兜底路径被触发（会绕开流水）。
- 充值/退款/会员订单出现「已扣款未入账」或状态与 PG 不一致；
  微信支付回调、虚拟支付发货出现异常。
- auth-service 启动/运行报错无法解释；点数接口错误率上升；锁等待影响用户请求。
- 备份不可恢复；双权威并存（同一时刻两个进程读到不同的 `HQ_LEDGER_STORE`）。
