# Invite Reward Claim and Member Network Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add seven-day upgrade-to-claim invite rewards, automatic upward transfer, privacy-safe member relationship browsing, business-card actions, and reward-unlock feedback across the Huangque backend and WeChat mini program.

**Architecture:** The backend remains the only authority for reward eligibility, expiry, transfer, privacy filtering, and traversal authorization. A new `invite_reward_claims` lifecycle table feeds the existing immutable reward-point ledger only after settlement; a focused authenticated invite-network module returns one relationship layer at a time with signed traversal grants. The mini program renders server-provided state, time, and permissions, and never calculates entitlement or exposes hidden partner/initiator reward data.

**Tech Stack:** Python 3 standard library, SQLite transactions, Huangque `auth_server.py`, native WeChat Mini Program JavaScript/WXML/WXSS, Python `unittest`, Node `node:test`.

## Global Constraints

- New reward rules apply only to membership events created after the deployment cutover timestamp; do not backfill or recalculate historical rewards.
- Keep the current membership tiers, membership purchase paths, invite binding rules, reward matrix, independent reward ledger, and existing special reward exclusions unchanged.
- A pending claim expires exactly `7 * 24 * 3600` seconds after the qualifying membership event; store UTC epoch seconds and return `server_time` to clients.
- A lower-tier or nonmember direct inviter receives an upgrade reminder and can auto-settle by reaching at least the invitee target tier before expiry.
- At expiry, skip inactive accounts, nonmembers, expired members, and insufficient tiers; settle to the first eligible ancestor or mark `no_recipient`.
- Refund, revocation, or invalidation before settlement changes the claim to `voided` and prevents transfer.
- Partner and initiator responses must return reward totals and row reward amounts as `0`; never send their real reward amount to web or mini-program clients.
- Nonmembers can only read their own direct children. Active members may traverse one adjacent relationship layer at a time within their connected invite network.
- Other-user relationship responses expose only username, effective membership tier/name, relation direction, card availability, and opaque navigation data.
- Every user row includes a business-card action; missing, unpublished, private, or failed card lookup produces “该用户暂未创建名片”.
- Backend and mini-program repositories use separate isolated worktrees and separate commits. Do not merge, deploy, upload, submit for review, or publish without later explicit authorization.

---

## File Structure

### Backend repository: `D:\codex\huangque-main-site`

- Create `server/invite_network.py`: authenticated one-layer relationship queries, privacy-safe person serialization, and signed traversal grants.
- Create `scripts/process_invite_reward_claims.py`: idempotent expiry processor suitable for scheduled execution.
- Create `tests/test_invite_reward_claims.py`: claim lifecycle, settlement, transfer, refund, and concurrency/idempotency tests.
- Create `tests/test_invite_network_access.py`: membership access boundaries, traversal grants, pagination, privacy, and card-state tests.
- Modify `server/invites.py`: schema migration, claim state machine, settlement functions, display filtering, and notification state.
- Modify `server/auth_server.py`: membership-event hooks, refund hooks, API routes, response compatibility, and payment/admin settlement feedback.
- Modify `server/admin_api.py`: operations UI for pending, settled, transferred, voided, and no-recipient claim records.
- Modify `tests/test_invite_rewards.py`: preserve existing ledger behavior and hidden partner/initiator contract.
- Modify `tests/test_admin_user_insights.py`: verify operations users can inspect real claim lifecycle details.
- Modify `tests/test_business_card_network.py`: verify existing public-card network remains unchanged by authenticated invite-network behavior.
- Modify `docs/membership-launch-runbook.md`: expiry processor command, cutover configuration, health checks, and rollback notes.

### Mini-program repository: `D:\codex\huangque-miniprogram`

- Create `miniprogram/utils/invite-rewards.js`: row normalization, countdown calculation, navigation URL construction, and reward-feedback copy.
- Create `tests/invite_reward_network.test.js`: utility, page contract, privacy, pagination, card action, and feedback tests.
- Modify `miniprogram/app.js`: once-per-day pending reminder and unread settlement feedback orchestration.
- Modify `miniprogram/pages/invite/invite.js`: load direct children, paginate, show filtered reward state, open relationship nodes, and open cards.
- Modify `miniprogram/pages/invite/invite.wxml`: replace reward records with “我的下线” rows and card buttons.
- Modify `miniprogram/pages/invite/invite.wxss`: stable two-action row layout, countdown, status, and pagination styling.
- Modify `miniprogram/pages/network/network.js`: one-node relation view, parent/child traversal, pagination, membership denial, and card action.
- Modify `miniprogram/pages/network/network.wxml`: replace orbit/tree expansion with direct parent and direct-child sections.
- Modify `miniprogram/pages/network/network.wxss`: relationship-page layout with fixed card and navigation controls.
- Modify `miniprogram/pages/recharge/recharge.js`: consume backend-confirmed invite reward settlement result after membership payment.
- Modify `miniprogram/pages/recharge/recharge.wxml`: reward-unlock result modal or overlay state.
- Modify `miniprogram/pages/recharge/recharge.wxss`: restrained success feedback animation and responsive modal styling.
- Modify `tests/invite_flow.test.js`: update invite-center contract from reward records to direct downlines.
- Modify `tests/business_card_network.test.js`: update relationship-page behavior while preserving card-page behavior.

---

### Task 1: Backend Claim Schema and Pure Lifecycle Helpers

**Files:**
- Modify: `server/invites.py:73-149`
- Create: `tests/test_invite_reward_claims.py`

**Interfaces:**
- Produces: `reward_claim_for_upgrade(conn, upgrade_record_id) -> dict | None`
- Produces: `minimum_reward_points(target_level: str) -> int`
- Produces: `create_reward_claim(conn, relation, upgrade_record, now: int) -> dict`
- Produces statuses: `pending_upgrade`, `credited`, `transferred`, `voided`, `no_recipient`
- Depends on existing `MEMBERSHIP_LEVEL_ORDER`, `INVITE_REWARD_TOTALS`, `user_invites`, and `membership_upgrade_records`.

- [ ] **Step 1: Write failing schema and claim-creation tests**

```python
def test_init_schema_adds_claim_table(self):
    with self.db() as conn:
        self.invites.init_schema(conn, now=1_800_000_000)
        columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(invite_reward_claims)"
            ).fetchall()
        }
    self.assertTrue({
        "upgrade_record_id", "direct_inviter_user_id", "invitee_user_id",
        "target_level", "status", "expires_at", "recipient_user_id",
        "recipient_level_snapshot", "reward_points", "transfer_depth",
        "settled_at", "voided_at", "reason",
    }.issubset(columns))

def test_ineligible_direct_inviter_gets_seven_day_claim(self):
    claim = self.create_claim(inviter_tier="experience", invitee_tier="partner", now=NOW)
    self.assertEqual(claim["status"], "pending_upgrade")
    self.assertEqual(claim["target_level"], "partner")
    self.assertEqual(claim["expires_at"], NOW + 7 * 24 * 3600)
    self.assertEqual(claim["reward_points"], 1500)
```

- [ ] **Step 2: Run the new tests and verify failure**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims -v
```

Expected: FAIL because `invite_reward_claims` and claim helpers do not exist.

- [ ] **Step 3: Add the additive schema migration**

Implement `CREATE TABLE IF NOT EXISTS invite_reward_claims` with a unique `upgrade_record_id`, indexes on `(direct_inviter_user_id,status,expires_at)` and `(status,expires_at)`, and no historical inserts. Add only nullable/defaulted compatibility columns to `invite_reward_point_records` when linking a settled ledger row to a claim.

```python
conn.execute("""CREATE TABLE IF NOT EXISTS invite_reward_claims(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upgrade_record_id INTEGER NOT NULL UNIQUE,
    source_order_id TEXT,
    invite_relation_id INTEGER NOT NULL,
    direct_inviter_user_id INTEGER NOT NULL,
    invitee_user_id INTEGER NOT NULL,
    target_level TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'upgrade',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    recipient_user_id INTEGER,
    recipient_level_snapshot TEXT,
    reward_points INTEGER NOT NULL DEFAULT 0,
    transfer_depth INTEGER NOT NULL DEFAULT 0,
    settled_at INTEGER,
    voided_at INTEGER,
    reason TEXT,
    updated_at INTEGER NOT NULL
)""")
```

- [ ] **Step 4: Implement pure eligibility and minimum-amount helpers**

```python
def membership_level_at_least(actual, required):
    return MEMBERSHIP_LEVEL_ORDER.get(str(actual or ""), 0) >= MEMBERSHIP_LEVEL_ORDER.get(str(required or ""), 0)

def minimum_reward_points(target_level):
    return int(INVITE_REWARD_TOTALS.get(target_level, {}).get(target_level, 0))
```

Create the pending claim only for post-cutover events and set `expires_at` from the server event timestamp.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims -v
```

Expected: schema and pending-claim tests PASS.

- [ ] **Step 6: Commit the schema and pure lifecycle foundation**

```powershell
git add server/invites.py tests/test_invite_reward_claims.py
git commit -m "feat(invites): add pending reward claim lifecycle"
```

---

### Task 2: Direct Settlement, Upgrade Unlock, Transfer, and Void Logic

**Files:**
- Modify: `server/invites.py:268-400`
- Modify: `tests/test_invite_reward_claims.py`
- Modify: `tests/test_invite_rewards.py`

**Interfaces:**
- Produces: `settle_claim(conn, claim_id: int, recipient_user_id: int, now: int, transferred: bool, depth: int = 0) -> dict`
- Produces: `settle_pending_for_user(conn, user_id: int, now: int) -> dict`
- Produces: `expire_pending_claims(conn, now: int, limit: int = 100) -> dict`
- Produces: `void_claims_for_upgrade(conn, upgrade_record_id: int, reason: str, now: int) -> int`
- Returns settlement summaries shaped as `{count, total_points, claim_ids, status}` before client privacy filtering.

- [ ] **Step 1: Add failing direct-settlement and idempotency tests**

```python
def test_eligible_partner_receives_partner_reward_immediately(self):
    result = self.membership_event(inviter_tier="partner", invitee_tier="partner")
    self.assertEqual(result["claim"]["status"], "credited")
    self.assertEqual(result["claim"]["reward_points"], 1500)
    self.assertEqual(self.ledger_points(result["claim"]["id"]), 1500)

def test_duplicate_upgrade_event_does_not_duplicate_claim_or_ledger(self):
    first = self.membership_event(source_order_id="order-1")
    second = self.membership_event(source_order_id="order-1")
    self.assertEqual(first["claim"]["id"], second["claim"]["id"])
    self.assertEqual(self.count_claims(), 1)
    self.assertLessEqual(self.count_ledger_rows(), 1)
```

- [ ] **Step 2: Add failing upgrade-unlock tests**

Cover nonmember to experience, experience to partner, partner to initiator, multiple pending claims unlocked in one upgrade, and a user upgrading above the minimum target tier.

```python
def test_actual_settlement_tier_recalculates_points(self):
    claim = self.pending_claim(inviter_tier="", invitee_tier="experience")
    summary = self.upgrade_inviter(claim, new_tier="partner")
    self.assertEqual(summary["count"], 1)
    self.assertEqual(summary["total_points"], 240)
```

- [ ] **Step 3: Add failing expiry-transfer tests**

Build a chain where the direct inviter is nonmember, the next ancestor is expired, the next is an insufficient experience member, and the next is an active initiator. Assert settlement to the initiator with correct depth and matrix amount. Add a second test asserting `no_recipient` when no ancestor qualifies. Add a malformed-cycle test that terminates without settlement.

- [ ] **Step 4: Add failing refund/void tests**

```python
def test_refund_voids_pending_claim_and_blocks_transfer(self):
    claim = self.pending_claim()
    changed = self.invites.void_claims_for_upgrade(
        self.conn, claim["upgrade_record_id"], "membership_refund", NOW + 60
    )
    self.assertEqual(changed, 1)
    self.expire(NOW + 8 * 24 * 3600)
    self.assertEqual(self.claim(claim["id"])["status"], "voided")
    self.assertEqual(self.count_ledger_rows(), 0)
```

- [ ] **Step 5: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims tests.test_invite_rewards -v
```

Expected: FAIL on missing settlement functions and old immediate-no-reward behavior.

- [ ] **Step 6: Implement transactional settlement**

Inside one transaction, re-read the claim with `status='pending_upgrade'`, re-check recipient account and active membership, calculate points from `INVITE_REWARD_TOTALS[recipient_tier][target_level]`, insert one ledger row linked to `claim_id`, and conditionally update the claim to `credited` or `transferred`. If the conditional update changes zero rows, return the existing settled result without inserting another ledger row.

- [ ] **Step 7: Implement upgrade unlock and ancestor transfer**

`settle_pending_for_user` queries unexpired pending claims for the upgraded user. `expire_pending_claims` uses valid `user_invites` one parent at a time, a `seen` set, and a maximum depth of 100. Re-check eligibility inside the settlement transaction before accepting a candidate.

- [ ] **Step 8: Implement void logic**

Update only `pending_upgrade` rows. Do not debit already settled ledger rows. Return the count of newly voided rows for audit and retry safety.

- [ ] **Step 9: Run focused and existing reward tests**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims tests.test_invite_rewards tests.test_invite_registration -v
```

Expected: PASS with existing reward totals and special restrictions unchanged.

- [ ] **Step 10: Commit the settlement engine**

```powershell
git add server/invites.py tests/test_invite_reward_claims.py tests/test_invite_rewards.py
git commit -m "feat(invites): settle and transfer upgrade rewards"
```

---

### Task 3: Membership Hooks, Refund Hooks, Expiry Processor, and Feedback Batches

**Files:**
- Modify: `server/auth_server.py:2705-2895`
- Modify: `server/auth_server.py:3690-3750`
- Modify: `server/invites.py`
- Create: `scripts/process_invite_reward_claims.py`
- Modify: `tests/test_invite_reward_claims.py`
- Modify: `docs/membership-launch-runbook.md`

**Interfaces:**
- Consumes: Task 2 lifecycle functions.
- Produces: membership success payload field `invite_reward_result`.
- Produces: unread feedback query `next_reward_notice(conn, user_id, now) -> dict | None`.
- Produces: `ack_reward_notice(conn, user_id, notice_id, now) -> bool`.
- Produces CLI exit code `0` with JSON `{processed, transferred, no_recipient, failed}`.

- [ ] **Step 1: Add failing hook tests**

Patch tests around `_activate_experience_membership`, `recharge_membership_admin`, and refund handling. Assert that online experience purchase and offline admin upgrade both call the same claim settlement path, and that refund voids pending claims.

- [ ] **Step 2: Add failing feedback-batch tests**

```python
def test_multiple_unlocks_create_one_unread_feedback_batch(self):
    result = self.upgrade_with_three_pending_claims()
    notice = self.invites.next_reward_notice(self.conn, result["user_id"], NOW)
    self.assertEqual(notice["claim_count"], 3)
    self.assertEqual(notice["total_points"], result["total_points"])
    self.invites.ack_reward_notice(self.conn, result["user_id"], notice["id"], NOW)
    self.assertIsNone(self.invites.next_reward_notice(self.conn, result["user_id"], NOW))
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims -v
```

Expected: FAIL because hooks and feedback batches are absent.

- [ ] **Step 4: Wire every membership activation path to the same domain call**

After the membership row and `membership_upgrade_records` row are created in the existing transaction, call claim creation and `settle_pending_for_user`. Return a combined summary without changing the membership success decision if notification formatting fails.

- [ ] **Step 5: Wire refund/revocation paths**

Before committing a membership refund or void event, call `void_claims_for_upgrade` for affected upgrade records. Preserve the existing manual handling policy for already recorded rewards.

- [ ] **Step 6: Add durable reminder and feedback state**

Create `invite_reward_notifications` with `user_id`, `notice_type`, `operation_key`, `payload_json`, `last_shown_day`, `read_at`, `created_at`, and `updated_at`. Use a unique `(user_id,notice_type,operation_key)` key so repeated callbacks reuse the same row.

- A `pending_upgrade` notice remains active while its claim remains pending. Acknowledge updates `last_shown_day` to the current Shanghai date, allowing the next reminder only on a later date.
- A `reward_unlocked` notice is terminal. Acknowledge sets `read_at`, so it is shown only once.
- When a claim becomes credited, transferred, voided, or no-recipient, stop selecting its pending notice even if the notice row remains for audit.

- [ ] **Step 7: Add the expiry processor script**

The script must import the existing auth database initializer, start a transaction per bounded batch, call `expire_pending_claims`, print one JSON summary, and return nonzero only on an unhandled processing failure. It must not alter unrelated membership or invite records.

- [ ] **Step 8: Document scheduler and cutover configuration**

Add exact dry-run/read-only queries, the processor command, expected JSON output, and rollback behavior to `docs/membership-launch-runbook.md`. The cutover timestamp must be explicit configuration captured at deployment, not inferred from old rows.

- [ ] **Step 9: Run focused tests and script smoke test**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims tests.test_invite_rewards -v
python scripts/process_invite_reward_claims.py --limit 1 --database :memory:
```

Expected: tests PASS; script prints a zero-count JSON summary without modifying historical records.

- [ ] **Step 10: Commit integration hooks and processor**

```powershell
git add server/auth_server.py server/invites.py scripts/process_invite_reward_claims.py tests/test_invite_reward_claims.py docs/membership-launch-runbook.md
git commit -m "feat(invites): integrate claim settlement with membership events"
```

---

### Task 4: Privacy-Safe Relationship and Reward APIs

**Files:**
- Create: `server/invite_network.py`
- Modify: `server/auth_server.py:5615-5740`
- Modify: `server/auth_server.py:5870-5895`
- Modify: `server/admin_api.py:2141-2660`
- Create: `tests/test_invite_network_access.py`
- Modify: `tests/test_business_card_network.py`
- Modify: `tests/test_invite_rewards.py`
- Modify: `tests/test_admin_user_insights.py`

**Interfaces:**
- Produces: `network_page(conn, viewer_id: int, target_grant: str | None, cursor: int, limit: int, now: int) -> dict`
- Produces: `issue_node_grant(viewer_id: int, target_user_id: int, secret: str, now: int) -> str`
- Produces: `verify_node_grant(token: str, viewer_id: int, secret: str, now: int) -> int | None`
- Produces GET `/api/auth/invite/downlines?cursor=<id>&limit=20`.
- Produces GET `/api/auth/invite/network?grant=<signed>&cursor=<id>&limit=20`.
- Produces GET `/api/auth/invite/notices/next` and POST `/api/auth/invite/notices/<id>/read`.
- Produces GET `/api/auth/admin/invite/reward-claims` with real, paginated lifecycle details for operations users.
- Keeps existing `/api/auth/invite/reward-points` response keys compatible.

- [ ] **Step 1: Write failing access-control tests**

```python
def test_nonmember_can_read_own_children_but_cannot_open_child_node(self):
    own = self.get_downlines(self.nonmember)
    self.assertEqual(own.status, 200)
    denied = self.get_network(self.nonmember, own.json["items"][0]["node_grant"])
    self.assertEqual(denied.status, 403)

def test_member_can_move_from_child_to_parent_and_parent_children(self):
    child = self.open_child(self.member)
    parent = self.open_grant(self.member, child["parent"]["node_grant"])
    self.assertEqual(parent["node"]["username"], self.member.username)
    self.assertTrue(any(item["username"] == child["node"]["username"] for item in parent["items"]))
```

- [ ] **Step 2: Write failing privacy and hidden-reward tests**

Assert exact allowed keys for other-user rows. For partner and initiator viewers, assert `total_reward_points == 0`, row `reward_points == 0`, and absence of real reward records. Assert the database still contains the real amount for admin queries.

- [ ] **Step 3: Write failing card-state and pagination tests**

Cover published card, missing card, draft card, disabled discoverability, card query failure fallback, 20-row first page, second cursor page, no duplicates, and a stable empty final page.

- [ ] **Step 4: Write failing operations visibility tests**

Create pending, credited, transferred, voided, and no-recipient claims. Assert the authenticated internal-admin endpoint returns the direct inviter, invitee, final recipient, target tier, real points, transfer depth, timestamps, source order, status, and reason. Assert the public and mini-program endpoints still receive filtered values.

- [ ] **Step 5: Run API tests and verify failure**

Run:

```powershell
python -m unittest tests.test_invite_network_access tests.test_business_card_network tests.test_invite_rewards tests.test_admin_user_insights -v
```

Expected: FAIL because authenticated invite-network APIs and signed grants do not exist.

- [ ] **Step 6: Implement `invite_network.py`**

Serialize only:

```python
{
    "username": username,
    "membership_tier": effective_tier,
    "membership_name": membership_name,
    "relation": "parent" or "child",
    "card_available": bool(public_id),
    "card_public_id": public_id or "",
    "node_grant": signed_grant,
}
```

Use HMAC-SHA256 over viewer ID, target user ID, and expiry. Reject wrong-viewer, malformed, and expired grants. Do not expose numeric user IDs.

- [ ] **Step 7: Implement one-layer routes and membership enforcement**

The own-downline route works for every active account. Opening any other node requires active membership and a valid viewer-bound grant. Return one direct parent and one cursor-paginated direct-child page. Limit defaults to 20 and is capped at 50.

- [ ] **Step 8: Implement reward display filtering and notice routes**

The response serializer, not the front end, forces all partner/initiator amounts to zero. Pending notices include `required_tier`, `expires_at`, and `server_time`; partner/initiator notices omit real `reward_points`.

- [ ] **Step 9: Implement operations claim inspection**

Add a paginated domain query and internal-admin route for all claim states. Extend the existing operations invite area with status, inviter, invitee, recipient, target tier, real points, transfer depth, source order, and timestamps. Escape all rendered values and keep internal/admin authentication identical to existing invite-reward views.

- [ ] **Step 10: Run focused API tests**

Run:

```powershell
python -m unittest tests.test_invite_network_access tests.test_business_card_network tests.test_invite_rewards tests.test_admin_user_insights -v
```

Expected: PASS and existing public-card APIs remain unchanged.

- [ ] **Step 11: Commit API and access-control changes**

```powershell
git add server/invite_network.py server/auth_server.py server/admin_api.py tests/test_invite_network_access.py tests/test_business_card_network.py tests/test_invite_rewards.py tests/test_admin_user_insights.py
git commit -m "feat(invites): expose privacy-safe member network APIs"
```

---

### Task 5: Mini-Program Reward Utilities and Direct-Downline Page

**Files:**
- Create: `miniprogram/utils/invite-rewards.js`
- Create: `tests/invite_reward_network.test.js`
- Modify: `miniprogram/pages/invite/invite.js`
- Modify: `miniprogram/pages/invite/invite.wxml`
- Modify: `miniprogram/pages/invite/invite.wxss`
- Modify: `tests/invite_flow.test.js`

**Interfaces:**
- Produces: `formatDownline(item, serverTime, nowMs) -> viewModel`.
- Produces: `countdown(expiresAt, serverTime, nowMs) -> {expired, text}`.
- Produces: `networkUrl(nodeGrant) -> string`.
- Consumes backend GET `/api/auth/invite/downlines?limit=20&cursor=<id>`.

- [ ] **Step 1: Write failing utility tests**

```javascript
test('countdown uses server time instead of the device clock', () => {
  const result = rewards.countdown(1_800_086_400, 1_800_000_000, 9_999_999_999_999);
  assert.strictEqual(result.text, '剩余 1天 0小时 0分');
});

test('network URL carries only the opaque grant', () => {
  assert.strictEqual(
    rewards.networkUrl('signed grant'),
    '/pages/network/network?grant=signed%20grant'
  );
});
```

- [ ] **Step 2: Write failing invite-page contract tests**

Assert page title “我的下线”, page size 20, a separate card button, a separate row click, loading-more behavior, membership-tier text, reward status, and absence of the old “奖励记录” list.

- [ ] **Step 3: Run tests and verify failure**

Run:

```powershell
node --test tests/invite_reward_network.test.js tests/invite_flow.test.js
```

Expected: FAIL because utilities and new page contract do not exist.

- [ ] **Step 4: Implement pure formatting utilities**

Do not calculate eligibility. Map only server fields to Chinese text. Use `server_time + elapsed client milliseconds` for the visual countdown, and refresh from the server when the page returns to foreground.

- [ ] **Step 5: Replace reward-record loading with direct-downline loading**

Keep existing code, dashboard, referrer, and share-card calls. Replace the reward-record request with `/api/auth/invite/downlines?limit=20`. Store `downlines`, `nextCursor`, `serverTime`, and `loadingMore`. Append pages by stable node grant or username without duplicates.

- [ ] **Step 6: Implement row and card interactions**

- Row tap: if `can_open_network`, navigate to `networkUrl(node_grant)`; otherwise show “开通会员后可查看该用户的上下线”.
- Card button tap: stop propagation; if `card_available`, open `/pages/card/card?id=<public_id>`; otherwise show “该用户暂未创建名片”.
- Every row keeps the card button visible.

- [ ] **Step 7: Implement the page layout**

Use a compact list with username and membership tier as the primary scan line. Put reward amount/status/countdown beneath only when the backend supplies visible values. Keep partner/initiator amount text as `0` and never infer hidden values. Add a stable bottom pagination control.

- [ ] **Step 8: Run focused mini-program tests**

Run:

```powershell
node --test tests/invite_reward_network.test.js tests/invite_flow.test.js
```

Expected: PASS.

- [ ] **Step 9: Commit the invite-center page**

```powershell
git add miniprogram/utils/invite-rewards.js miniprogram/pages/invite tests/invite_reward_network.test.js tests/invite_flow.test.js
git commit -m "feat(invites): show paginated direct downlines"
```

---

### Task 6: Mini-Program Layered Relationship Navigation and Card Actions

**Files:**
- Modify: `miniprogram/pages/network/network.js`
- Modify: `miniprogram/pages/network/network.wxml`
- Modify: `miniprogram/pages/network/network.wxss`
- Modify: `tests/invite_reward_network.test.js`
- Modify: `tests/business_card_network.test.js`

**Interfaces:**
- Consumes GET `/api/auth/invite/network?grant=<signed>&limit=20&cursor=<id>`.
- Consumes `networkUrl`, card availability, and node grants from Task 5.
- Produces one-node relation page with `node`, `parent`, `children`, `nextCursor`, and local navigation history.

- [ ] **Step 1: Write failing navigation tests**

Test loading a node, clicking its parent, clicking a child, returning to the previous node, paging children, nonmember denial, expired membership denial, and stale-load suppression.

- [ ] **Step 2: Write failing card-action tests**

Assert row tap navigates within the relationship network while the independent card button opens a published card or shows the required missing-card message.

- [ ] **Step 3: Run tests and verify failure**

Run:

```powershell
node --test tests/invite_reward_network.test.js tests/business_card_network.test.js
```

Expected: FAIL because the existing orbit/tree page only expands descendants and opens cards on node tap.

- [ ] **Step 4: Replace orbit/tree expansion with one-layer relation state**

`onLoad` reads the opaque grant. `loadNode(grant)` fetches the node, one parent, and first child page. Maintain a stack of `{grant, scrollTop}` for in-page back navigation while preserving native page back to the invite center.

- [ ] **Step 5: Implement pagination and stale-request protection**

Use a monotonically increasing request ID. Ignore results from older nodes after the user navigates. Child pagination appends by grant without replacing the parent or current node.

- [ ] **Step 6: Implement privacy-safe layout**

Show current username and tier, one optional direct parent, and paginated direct children. Do not render phone, invite code, balances, account IDs, or reward details. Keep username and tier text within constrained rows on narrow screens.

- [ ] **Step 7: Run focused navigation tests**

Run:

```powershell
node --test tests/invite_reward_network.test.js tests/business_card_network.test.js
```

Expected: PASS.

- [ ] **Step 8: Commit the layered network page**

```powershell
git add miniprogram/pages/network tests/invite_reward_network.test.js tests/business_card_network.test.js
git commit -m "feat(invites): add member relationship navigation"
```

---

### Task 7: Pending Reminders and Reward-Unlock Satisfaction Feedback

**Files:**
- Modify: `miniprogram/app.js`
- Modify: `miniprogram/utils/invite-rewards.js`
- Modify: `miniprogram/pages/recharge/recharge.js`
- Modify: `miniprogram/pages/recharge/recharge.wxml`
- Modify: `miniprogram/pages/recharge/recharge.wxss`
- Modify: `tests/invite_reward_network.test.js`
- Modify: `tests/business_card_network.test.js`

**Interfaces:**
- Consumes GET `/api/auth/invite/notices/next`.
- Consumes POST `/api/auth/invite/notices/<id>/read`.
- Consumes payment confirmation field `invite_reward_result`.
- Produces `rewardFeedback(data, membershipTier) -> {title, content, showAmount, actions}`.

- [ ] **Step 1: Write failing reminder-policy tests**

Test first creation notice, once-per-Shanghai-day reminder, no repeat after read terminal feedback, pending notice with required tier/countdown, and no real amount for partner/initiator.

- [ ] **Step 2: Write failing payment-feedback tests**

```javascript
test('three unlocked rewards produce a combined satisfaction message', () => {
  const feedback = rewards.rewardFeedback({ claim_count: 3, total_points: 600 }, 'experience');
  assert.strictEqual(feedback.title, '升级成功，邀请奖励已解锁');
  assert.match(feedback.content, /3 笔/);
  assert.match(feedback.content, /600 点/);
});

test('partner feedback does not reveal points', () => {
  const feedback = rewards.rewardFeedback({ claim_count: 2, total_points: 4000 }, 'partner');
  assert.strictEqual(feedback.content, '邀请权益已自动发放');
  assert.doesNotMatch(feedback.content, /4000/);
});
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```powershell
node --test tests/invite_reward_network.test.js tests/business_card_network.test.js
```

Expected: FAIL because reminder and feedback orchestration do not exist.

- [ ] **Step 4: Add app-level notice polling**

On authenticated app foreground, call the next-notice endpoint once. Guard concurrent calls and open modals. For pending notices, show the required tier and countdown; route experience purchase to recharge and tell partner/initiator candidates to contact an administrator. Acknowledge only according to the backend notice contract.

- [ ] **Step 5: Add immediate payment-settlement feedback**

After membership payment confirmation, inspect only the server-returned `invite_reward_result`. If settled, open the success feedback with “查看我的下线” and “知道了”. If processing, show “奖励结算中” and leave the durable notice unread for the next status check.

- [ ] **Step 6: Add restrained success presentation**

Use a modal overlay with a short opacity/scale transition, a clear success mark, compact title, body, and two actions. Do not use a full-screen marketing page or decorative card nesting. Disable action overlap and preserve safe-area spacing.

- [ ] **Step 7: Run focused feedback tests**

Run:

```powershell
node --test tests/invite_reward_network.test.js tests/business_card_network.test.js
```

Expected: PASS.

- [ ] **Step 8: Commit reminders and feedback**

```powershell
git add miniprogram/app.js miniprogram/utils/invite-rewards.js miniprogram/pages/recharge tests/invite_reward_network.test.js tests/business_card_network.test.js
git commit -m "feat(invites): add reward unlock feedback"
```

---

### Task 8: Full Verification and Review-Ready Branches

**Files:**
- Verify all files changed in Tasks 1-7.
- Modify only tests or documentation when verification exposes a scoped defect.

**Interfaces:**
- Produces two review-ready branch heads, one per repository.
- Does not push, merge, deploy, upload, submit, or publish.

- [ ] **Step 1: Run the complete backend invite and membership suite**

Run:

```powershell
python -m unittest tests.test_invite_reward_claims tests.test_invite_network_access tests.test_invite_rewards tests.test_invite_registration tests.test_business_card_network tests.test_admin_user_insights -v
```

Expected: PASS.

- [ ] **Step 2: Run the complete backend test suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: PASS. If an unrelated Windows-only dependency prevents collection, record the exact module and error, rerun the affected suite in the repository's supported Linux CI environment, and still require all invite, membership, card, payment, and admin tests to pass before review.

- [ ] **Step 3: Run the complete mini-program test suite**

Run:

```powershell
node --test tests/*.test.js
```

Expected: PASS.

- [ ] **Step 4: Compile in WeChat DevTools**

Open the isolated mini-program worktree, compile, and verify there are no WXML, WXSS, JavaScript, page-registration, or domain errors. Use test accounts for nonmember, experience, partner, and initiator views.

- [ ] **Step 5: Manually verify the permission matrix**

- Nonmember: own direct children visible; other-node row tap denied; card button works or shows missing-card message.
- Experience: upward/downward traversal works; real visible reward data and countdown are correct.
- Partner and initiator: traversal works; every client reward total and row amount is `0`; no network response contains real amounts.
- Expired member: traversal becomes denied without requiring relaunch.

- [ ] **Step 6: Manually verify lifecycle feedback with controlled data**

Create post-cutover controlled claims for direct settlement, upgrade unlock, multiple unlocks, expiry transfer, no recipient, and refund void. Compare `invite_reward_claims`, `invite_reward_point_records`, API responses, and admin output after each case.

- [ ] **Step 7: Inspect diffs and repository status**

Run in each worktree:

```powershell
git diff origin/main...HEAD --check
git status --short --branch
git log --oneline --decorate origin/main..HEAD
```

Expected: no whitespace errors, no secrets, no generated local settings, no unrelated user changes, and only intentional commits.

- [ ] **Step 8: Request code review**

Use `superpowers:requesting-code-review` against both branch heads. Resolve blocking findings with new focused tests and commits. Report branch names, commit SHAs, test commands, and remaining operational steps. Wait for explicit authorization before push/PR, merge, deployment, mini-program upload, review submission, or publication.
