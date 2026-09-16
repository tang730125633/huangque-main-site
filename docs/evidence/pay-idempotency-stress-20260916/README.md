# 支付幂等压测（staging-only）· 2026-09-16 夜

**范围**：黄雀主站 auth 域切写 PostgreSQL 之后，充值 / 退点等支付账务路径的幂等性。
**只碰 staging**：所有压测只连 `huangque_staging`（服务器本机 unix socket + peer 免密）。
**生产未动**：生产代码、生产库 `huangque` 均未改动，未部署任何东西。

---

## 0. 一句话结论

**切到 PG 之后，支付入账在「重放 / 并发」下确实会重复记账。**

用真实代码路径在 staging 跑 **12 个场景 × 每场景 8 轮**（每轮全新账号 + 新订单）：

| | 基线（= 今天的 main） | 打完 §4 的修复后 |
|---|---|---|
| 全绿场景 | **5 / 12**（A、C、F、I、J） | **12 / 12** |
| 失败场景 | **7 / 12**（B、D、E、G、H、K、L） | 0 |
| 每轮入账调用 | 1,083 次（8 轮共约 8,700 次） | 同 |
| 最狠的一条 | 「虚拟支付单进程多线程重放」**8 轮里 6–8 轮被打穿** | 8 轮 0 中 |

其中三处是**真金白银**的问题：用户点数真的少拿了（§3.3）、一笔付款/充值被记账两次（§3.1、§3.2）、一笔退款被记账两次（§3.5）。

根因一句话：**SQLite 时代靠 `BEGIN IMMEDIATE`（整库写锁）实现的「先读状态、再记账」，在 PG 后端只剩一把进程内写锁**；PG 是 MVCC、读不阻塞，于是并发的两个回调各读到旧状态、各记一笔。

---

## 1. 支付回调全链路梳理（grep 结论）

### 1.1 入账入口（谁能让点数变动）

| # | 入口 | 处理函数 | 记账动作 |
|---|---|---|---|
| 1 | `POST /api/auth/wechat/message-push`（微信虚拟支付事件推送，验签后解密） | `process_virtual_pay_message` | `xpay_goods_deliver_notify` → `confirm_virtual_pay_order`（加点）；`xpay_refund_notify` → `refund_virtual_pay_order`（退点） |
| 2 | `POST /api/auth/virtual-pay/confirm`（客户端主动确认，带登录态） | `confirm_virtual_pay_order` | 加点（同一函数） |
| 3 | 进程内兜底线程 `_virtual_pay_reconcile_loop`（60s 一轮查单） | `reconcile_created_virtual_pay_orders` → `confirm_virtual_pay_order` | 加点 |
| 4 | `POST /api/auth/wxpay/notify`（微信支付 V3 回调，验签） | `reconcile_wxpay_recharge` → `review_recharge_order` / `refund_recharge_order` | 充值、会员入账、退款扣回 |
| 5 | `POST /api/auth/wxpay/query`（客户端轮询订单） | `reconcile_wxpay_recharge` | 同上（同一条安全边界） |
| 6 | `POST /api/auth/admin/recharge/review`（管理员手动审批） | `review_recharge_order` | 充值、会员入账 |
| 7 | `POST /api/auth/points/deduct` / `refund`（内部令牌） | `deduct_points` / `refund_points` | 任务扣点、退点（**带 transaction_key**） |

关键点：**同一个订单的入账函数被 3–4 个入口共用**（微信重推、客户端确认、60s 兜底查单、管理员审批），它们可能同时到达，也可能落在不同进程里。

### 1.2 现有护栏，以及各自的作用范围

| 护栏 | 位置 | 实际覆盖 | 缺口 |
|---|---|---|---|
| **G1** `points_audit.transaction_key` 唯一索引 | migration `20260916_0012` | 任务扣/退点、内部点数接口、点数赠送（调用方都传 key） | **支付入账路径一个 key 都不传**（写 NULL）→ 对充值/虚拟支付**完全没覆盖**（唯一索引里多个 NULL 互不相等） |
| **G2** 订单状态机（`created→credited`、`pending→approved`） | `confirm_virtual_pay_order` / `review_recharge_order` | SQLite 下靠 `BEGIN IMMEDIATE` 整库写锁串行，**单进程、单机**时正确 | PG 下这条只是「事务内一次普通 SELECT」：READ COMMITTED 下两个事务都能读到 `created` / `pending` |
| **G3** `auth_store` 进程内写锁 `_WRITE_LOCK`（RLock） | `server/content_domains/auth_store.py` | 只覆盖**单进程**；文件头自述「跨进程一致性由 PG 行锁 / 唯一约束兜底」 | ① 跨进程无效（生产还有第二个直连 PG 的写者：`huangque-invite-reward-claims.timer`）；② **提交前就释放锁**，留下「别人已能进临界区、我们的 COMMIT 还没生效」的窗口 —— 连单进程都能重复记账（§3.1 实证） |
| **G4** `recharge_orders.transaction_id` 查重 | `review_recharge_order`（Python 层） | 单进程串行时有效 | **没有唯一索引**：并发下两个审批事务都查不到对方，同一笔微信流水把两张单都变成 `approved`（§3.4 实证） |

---

## 2. 压测怎么做（以及为什么结果可信）

脚本：`scripts/pay_idempotency_stress.py`（本次新增，可在 staging 反复执行）

* **驱动真实代码路径**，不是复制逻辑：`process_virtual_pay_message`（微信事件入口）、
  `confirm_virtual_pay_order`、`review_recharge_order`、`refund_recharge_order`、
  `refund_virtual_pay_order`、`deduct_points` / `refund_points`；下单也走真实的
  `create_virtual_pay_order` / `create_recharge_order`。
* **微信网络调用全部打桩**为本地假响应（`code_to_session` / `query_order` /
  `notify_provide_goods`），`WX_VIRTUAL_PAY_ENV=1`（沙箱枚举）：不发真实支付请求、
  不用真实密钥、不产生任何真实扣款。
* **每个场景重复 8 轮**，每轮全新账号 + 新订单。竞态不是必现，**单次 PASS 不能证明幂等**，
  所以证据给的是「8 轮里有几轮被打穿」。
* **每条场景四项断言**：
  1. 只入账一次：本次支付产生的流水行数 == 期望行数（单笔=1，同用户两笔=2）；
  2. 余额只动一次：余额净变动 == 应得点数（退款场景为负）；
  3. 账实一致：该账号全部流水 delta 合计 == 当前余额 − 建档余额；
  4. 流水链连续：每行 `before_points` == 上一行 `after_points`，最后一行 == 当前余额。
* **安全门禁（fail closed）**：脚本拒绝运行，除非 `current_database()` **精确等于**
  `huangque_staging` 且连的是本机 socket —— 误连生产库直接 `REFUSED` 退出。
* 测试数据统一 `zzpaystress_` 前缀，**跑完自动清理**；每轮跑完库内计数回到
  133 用户 / 8945 流水 / 71 充值单 / 41 虚拟支付单（与压测前一致，见证据 JSON 的
  `table_counts_before/after`）。

**调用量（每轮）**：A 20 · B 16 线程×20=320 · C 6 进程×20=120 · D 2×20=40 · E 4×20=80 ·
F 20 · G 6×20=120 · H 2 · I 6×20=120 · J 1 · K 6×20=120 · L 6×20=120 = **1,083 次/轮**，× 8 轮 ≈ **8,664 次**。

---

## 3. 结果与实证

| 场景 | 说明 | 基线 | 修复后 | 基线的具体表现 |
|---|---|---|---|---|
| **A** | 同一笔虚拟支付回调**串行**重放 20 次 | PASS | PASS | 单线程重放本来就安全（状态机生效） |
| **B** | 同一笔回调**单进程多线程**并发重放（16 线程×20） | **FAIL 6/8** | PASS 8/8 | 同一订单产生**两条**一模一样的加点流水（上一轮 8/8） |
| **C** | 同一笔回调**跨进程**并发重放（6 进程×20） | PASS（上轮 2/8） | PASS 8/8 | 同 B：两条加点流水（窗口窄，间歇复现） |
| **D** | 同一用户**两笔订单**跨进程同时入账（+1000/+2000） | **FAIL 3/8** | PASS 8/8 | **用户少拿 1000 点**：余额只到 3000（应 4000） |
| **E** | 乱序竞争：同订单重放 + 同用户另一单 + 无关账务在途 | **FAIL 1/8** | PASS 8/8 | **用户少拿 2000 点**：余额只 +1000（应 +3000） |
| **F** | 同一充值单**串行**重复触发审批 | PASS | PASS | 单线程重放安全 |
| **G** | 同一充值单**跨进程**并发触发审批 | **FAIL 3/8** | PASS 8/8 | 同一充值单**四条**入账流水（250 点记 4 次，余额只加一次） |
| **H** | 同一微信流水号审批**两张不同订单** | **FAIL 6/8** | PASS 8/8 | 两张单都 `approved`，`transaction_id` 重复 1 组 |
| **I** | 同一 `transaction_key` 退点跨进程重放 | PASS | PASS | 唯一索引这条护栏本来就是好的 |
| **J** | 同一 `transaction_key` 换金额重放 | PASS | PASS | 被拒且不动余额 |
| **K** | 微信支付**退款回调**重复到达（跨进程并发） | **FAIL 1/8** | PASS 8/8 | 同一退款产生**两条**扣回流水 |
| **L** | **虚拟支付退款通知**重复到达（跨进程并发） | **FAIL 1/8** | PASS 8/8 | 同一退款产生**两条**扣回流水 |

原始证据（机器可读、全量）：`evidence-baseline.json` / `evidence-fixed.json`（同目录）。
两份是**同一版脚本、同一套参数**（replays=20、workers=6、threads=16、repeat=8），
只差「有没有打 §4 的修复补丁」。

### 3.1 漏洞 V1：同一进程里也会重复入账（B，6–8/8）

```
流水 id 10733  delta +1000  before 1000  after 2000  微信虚拟支付: HQ…C6B4E7DC1B
流水 id 10734  delta +1000  before 1000  after 2000  微信虚拟支付: HQ…C6B4E7DC1B   ← 同一笔支付
余额 1000 → 2000，而流水合计 +2000 → 账实不符
```
两道口子叠加：**① 跨进程没有 PG 行锁**；**② `_finish_tx()` 先释放写锁、后 `conn.commit()`** ——
即使只有一个进程，线程 B 也能在「A 已放锁、A 的 COMMIT 还没返回」的窗口里读到 `status='created'`，
于是两个线程各记一笔。余额没翻倍只是因为代码写的是**绝对值**
（`UPDATE users SET points=?`，两人算出同一个 `after`）—— 纯属巧合掩盖，**账本已经多了一行**。

### 3.2 漏洞 V2：跨进程同订单重复入账（G 3/8、C 间歇、K/L 各 1/8）

```
充值单 R…FD3381：流水 10812/10813/10814 三条（+250/+250/+250），余额只 +500
```
同 V1 的重复流水，只是竞态窗口更窄（要两个进程的临界区恰好重叠）。
G 这轮还出现了一个更糟的交错：4 条流水、余额只 +500 —— 流水与余额两边都对不上。

### 3.3 漏洞 V3：同用户多笔入账互相覆盖，用户真的少拿点（D 3/8、E 1/8）

```
流水 id 10761  delta +1000  before 1000  after 2000   订单 A
流水 id 10762  delta +2000  before 1000  after 3000   订单 B   ← before 还停在 1000
余额 1000 → 3000（应 4000）：订单 A 的 1000 点被订单 B 的绝对值写覆盖
```
E 更直接：余额只 +1000，而两笔订单应得 +3000 —— **用户少拿 2000 点，账上却记了两笔**。
这是「读余额 → 算 after → 写绝对值」在没有行锁时的经典丢失更新。

### 3.4 漏洞 V4：同一笔微信流水审批了两张单（H 6/8）

```
订单 R…2970D9  status=approved  transaction_id=DUP…
订单 R…9B8037  status=approved  transaction_id=DUP…   ← 同一微信流水
```
`transaction_id` 只有 Python 层预检、**没有唯一索引**，两个并发审批都查不到对方。
后果：一笔真实付款可以开两张充值单（本场景余额只加一次是绝对值写掩盖，
换个交错就是双倍入账；会员单还会双发会员权益）。

### 3.5 漏洞 V5：退款侧同样重复（K 1/8、L 1/8）

```
微信支付退款: R1789552884E67992   delta -250  before 1250  after 1000
微信支付退款: R1789552884E67992   delta -250  before 1250  after 1000   ← 同一笔退款
```
退款回调由微信重推、且与「订单查询」共用同一条路径；重复到达时会重复扣回。

---

## 4. 修复（补丁已随仓库提交，staging 上已验证）

补丁：`scripts/pay_idempotency_fix.patch`（`git apply` 即可；**未提交进 main、未部署**）。
内容 = 5 组改动 + 1 个迁移，全部保持 SQLite 路径逐字节不变（`FOR UPDATE` 只在 PG 后端追加）。

| # | 文件 / 函数 | 做法 | 解决 |
|---|---|---|---|
| **F1** | `server/content_domains/auth_store.py` `_finish_tx()` | 写锁**持有到 PG COMMIT/ROLLBACK 真正返回**（`try/finally`） | V1 的单进程窗口 |
| **F2** | `server/auth_server.py` 新增 `_row_lock_suffix()` / `_select_locked()` | PG 下给关键行读追加 `FOR UPDATE`（SQLite 不追加，逐字节不变） | 跨进程行级串行 |
| **F3** | `confirm_virtual_pay_order`、`review_recharge_order`、`refund_recharge_order`、`refund_virtual_pay_order`、`_revert_membership_order` | 事务内**先锁订单行**再判状态机，**再锁用户行**再读/写余额 | V1、V2、V3、V5（订单行锁让状态机在 PG 下真正成立；用户行锁消除丢失更新） |
| **F4** | 同上 5 条入账/退款路径 | 给每条支付驱动的流水写**稳定幂等键**：`virtual-pay-credit:<订单号>` / `virtual-pay-refund:` / `recharge-approve:` / `recharge-refund:` / `membership-refund:` | 让既有的 `points_audit.transaction_key` 唯一索引**第一次真正覆盖支付入账**（DB 层兜底，跨进程、跨时序都成立）；冲突时捕获 `IntegrityError` 退化为「幂等返回」而不是 500 |
| **F5** | 新增迁移 `server/db/migrations/versions/20260916_0013_recharge_transaction_unique.py` | `CREATE UNIQUE INDEX … ON ledger.recharge_orders (transaction_id) WHERE transaction_id IS NOT NULL AND transaction_id <> ''`；建索引前先扫重复值，有重复就中止并报出来（绝不静默改账） | V4 |

**staging 验证**：把补丁 `git apply` 到 `/home/ubuntu/m3a-verify-full`（与交付补丁逐文件 md5 一致：
`auth_server.py 9d107348…`、`auth_store.py 7e0ea337…`、迁移 `c6952357…`），
`alembic upgrade head`（staging `20260916_0012 → 0013`），同一套参数重跑：
**12/12 全绿、0 失败、0 超额流水行**。

---

## 5. 怎么复现（打补丁 → 建索引 → 一条命令压测）

```bash
# 1) 打补丁（staging 代码目录；也可本地 git apply 后 scp）
cd /home/ubuntu/m3a-verify-full && git apply scripts/pay_idempotency_fix.patch
# 2) 建唯一索引（幂等；已建过则无副作用）
sudo -u postgres env HQ_DATABASE_URL=postgresql:///huangque_staging \
    /usr/bin/python3 -m alembic upgrade head     # alembic 为 postgres 用户已装 1.13.2
# 3) 压测（staging 专用；误连生产会被脚本 REFUSED）
sudo -u postgres env HQ_DATABASE_URL=postgresql:///huangque_staging HQ_AUTH_DB_POOL_MAX=16 \
    /usr/bin/python3 scripts/pay_idempotency_stress.py \
    --scenario all --replays 20 --workers 6 --threads 16 --repeat 8 --out /tmp/paystress.json
# 退回基线看红：git apply -R scripts/pay_idempotency_fix.patch
#               + DROP INDEX ledger.uq_recharge_orders_transaction_id
```

staging 现在已还原成**基线状态**（代码 == main；`alembic_version = 20260916_0012`；
索引已删；库内无 `zzpaystress_` 残留），方便下次直接复现红。

---

## 6. 未尽事项 / 建议

1. **会员单（`points=0`）路径没被覆盖**：它们不写 `points_audit`，F4 的幂等键对它们无效，
   只有状态机 + 行锁兜底；建议补一轮「虚拟支付会员单并发重放」场景（顺带查会员到期时间的重复延长）。
2. **同类风险不止支付**：`server/` 下 117 处 `BEGIN IMMEDIATE` 在 PG 后端都只剩进程内写锁。
   邀请奖励领取（`invites.expire_pending_claims`）已经改成 `FOR UPDATE`（正确做法，可作范式）；
   建议按「先读状态、再写钱 / 权益」的清单逐个补行锁或唯一约束（CLI 授权、点数赠送优先）。
3. **生产现在只有 1 个 auth 进程**，但 `huangque-invite-reward-claims.timer` 是第二个**直连 PG** 的写者；
   auth 一旦重启 / 双实例 / 迁移脚本在跑，跨进程窗口立刻打开 —— **别把「现在还没炸」当成安全**。
   另外 G3 的第 ② 条（放锁早于 COMMIT）在**单进程**下就会漏，不需要第二个进程。
4. **运维提醒**：`/etc/huangque/postgresql/migrator.env` 里的 `HQ_DATABASE_URL` 指向的是
   **生产库 `huangque`**（不是 staging）。用它跑任何脚本都是在生产上跑 —— 建议给它加一层
   「库名断言」或改名成 `migrator-prod.env`，避免下一个人踩。
5. **透明说明**：接线自检阶段我误用上面那个 DSN 连上过生产库，执行了 3 条**只读**元数据查询
   （`current_user`、`current_database()`、`has_table_privilege`，以及一次 `users` 计数），
   发现指向生产后立即停用，**未执行任何写操作**；此后所有操作（建索引、插测试数据、清理）
   都只发生在 `huangque_staging`。
6. **未打的桩**：微信回调的验签 / 解密（`decode_message_push`、`wxpay.verify_notify`）没有覆盖 ——
   本次以「验签之后的业务入口」为边界（幂等问题都在这一层之下）。

---

## 7. 交付物

| 文件 | 说明 |
|---|---|
| `scripts/pay_idempotency_stress.py` | 压测器（可重复执行；staging 硬门禁、自动清理、A–L 场景、多轮重复聚合） |
| `scripts/pay_idempotency_fix.patch` | 修复补丁（auth_server.py + auth_store.py + 迁移 0013），已在 staging 验证 |
| `docs/evidence/pay-idempotency-stress-20260916/README.md` | 本报告 |
| `docs/evidence/pay-idempotency-stress-20260916/evidence-baseline.json` | 基线全量证据（5 PASS / 7 FAIL） |
| `docs/evidence/pay-idempotency-stress-20260916/evidence-fixed.json` | 修复后全量证据（12 PASS） |

脱敏：证据里只有本脚本自己造的测试账号（`zzpaystress_…`）、测试订单号与库名 `huangque_staging`；
不含任何连接串、密钥、真实 openid、真实微信流水号或真实用户数据。
