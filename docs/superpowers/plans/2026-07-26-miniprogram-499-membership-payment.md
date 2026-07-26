# 小程序 499 元体验官支付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让小程序通过微信虚拟支付完成 499 元体验官开通，并可靠发放一年体验官、1000 点和一个免费音色槽位。

**Architecture:** 为体验官开通创建独立虚拟商品和订单类型，复用现有会员激活事务逻辑。小程序把唯一的 499 开通入口从普通 JSAPI 支付切换到虚拟支付，点数充值链路保持不变。

**Tech Stack:** Python 3、SQLite、微信小程序 JavaScript、`unittest`

## Global Constraints

- 商品 ID 固定为 `hq_member_exp_1y`，标价固定为 49900 分。
- 权益固定为一年体验官、1000 点、1 个免费音色槽位。
- 有效会员不得重复购买。
- 不修改普通点数商品及其会员折扣。
- 只提交 PR，不合并、不部署。

---

### Task 1: 后端虚拟商品与订单权限

**Files:**
- Modify: `server/wechat_virtual_pay.py`
- Modify: `server/auth_server.py`
- Test: `tests/test_membership_system.py`

**Interfaces:**
- Produces: `hq_member_exp_1y` 商品；`virtual_pay_orders.order_type`；非会员体验官订单创建能力。

- [x] **Step 1: Write the failing tests**

新增测试断言商品为 49900 分、非会员可创建体验官订单、有效会员被拒绝、普通点数订单仍要求会员。

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_membership_system -v`

- [x] **Step 3: Implement minimal product and order-type support**

增加独立商品元数据、兼容迁移和按订单类型分流的权限校验。

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_membership_system -v`

### Task 2: 体验官权益原子到账

**Files:**
- Modify: `server/auth_server.py`
- Test: `tests/test_membership_system.py`

**Interfaces:**
- Consumes: `virtual_pay_orders.order_type=membership_experience`。
- Produces: 幂等的体验官虚拟订单履约。

- [x] **Step 1: Write failing confirmation and idempotency tests**

测试支付确认后点数、会员期限和槽位全部到账，重复确认不重复发放，事务失败全部回滚。

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_membership_system -v`

- [x] **Step 3: Implement transaction-based fulfillment**

在现有确认事务中按订单类型调用 `_activate_experience_membership`，并保留普通点数履约路径。

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_membership_system -v`

### Task 3: 小程序切换 499 支付入口

**Files:**
- Modify: `miniprogram/pages/recharge/recharge.js`
- Test: `tests/recharge.test.js`

**Interfaces:**
- Consumes: `/api/auth/virtual-pay/order` 和商品 ID `hq_member_exp_1y`。
- Produces: 通过 `wx.requestVirtualPayment` 完成 499 支付并确认到账的页面流程。

- [x] **Step 1: Write a failing route-selection test**

测试体验官套餐选择虚拟支付接口、传递独立商品 ID，且不再调用 `wx.requestPayment`。

- [x] **Step 2: Run test to verify it fails**

Run: `node --test tests/recharge.test.js`

- [x] **Step 3: Implement virtual payment flow**

复用点数充值的创建订单、支付和确认逻辑，并保留体验官专属成功提示。

- [x] **Step 4: Run test to verify it passes**

Run: `node --test tests/recharge.test.js`

### Task 4: 回归验证与提交 PR

**Files:**
- Verify all changed files in both repositories.

**Interfaces:**
- Consumes: Tasks 1-3 completed changes.
- Produces: 两个可独立审查、需要配套合并的 PR。

- [x] **Step 1: Run backend focused and relevant regression tests**

Run: `python -m unittest tests.test_membership_system -v`

- [x] **Step 2: Run mini-program test suite**

Run: `node --test tests/*.test.js`

- [x] **Step 3: Inspect scoped diffs and secrets**

Run: `git diff --check` and inspect `git diff --stat` in both repositories.

- [x] **Step 4: Commit, push, and open draft PRs**

主站和小程序分别提交到各自分支，PR 描述注明需要配套合并且未部署。
