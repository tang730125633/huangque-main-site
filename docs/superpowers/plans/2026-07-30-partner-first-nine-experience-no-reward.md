# Partner First Nine Experience Members No-Reward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each partner's first nine direct invitees who become experience members consume immutable qualification slots without producing reward points, while the tenth and later continue to award 240 points.

**Architecture:** Add a dedicated SQLite slot ledger owned by `server/invites.py`. Allocate slots transactionally when membership upgrades are recorded, use the next slot for previews, and idempotently backfill historical first-experience upgrades during schema initialization without changing existing reward records.

**Tech Stack:** Python 3, SQLite, `unittest`, existing Huangque auth and invite domain modules.

## Global Constraints

- Rank by experience-membership upgrade time, then upgrade record ID.
- Slots 1 through 9 produce zero reward; slot 10 and later use the existing 240-point partner-to-experience rule.
- Existing rewards are not added, removed, restored, or voided by backfill.
- Voiding rewards, membership changes, unbinding, and rebinding do not release or move an existing slot.
- Experience-member and initiator reward rules remain unchanged.
- Reward points remain separate from consumable `users.points`.
- Do not merge or deploy this PR.

---

### Task 1: Qualification Slot Schema and Backfill

**Files:**
- Modify: `server/invites.py`
- Test: `tests/test_invite_rewards.py`

**Interfaces:**
- Produces: `backfill_partner_experience_reward_slots(conn) -> int`
- Produces: `ensure_partner_experience_reward_slot(conn, relation, upgrade_record_id, created_at) -> dict`

- [ ] **Step 1: Write failing schema and backfill tests**

Add tests that initialize historical partner direct invitees with effective `to_level='experience'`
upgrade records, run schema initialization twice, and assert stable ordinals, uniqueness, and no
changes to `invite_reward_point_records`.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `python -m unittest tests.test_invite_rewards -v`

Expected: failure because `partner_experience_reward_slots` and its helper functions do not exist.

- [ ] **Step 3: Implement the slot table and idempotent backfill**

Create the table and unique indexes in `init_schema`. Backfill the earliest effective experience
upgrade per current direct relation, grouped by current active partner and ordered by
`membership_upgrade_records.created_at, membership_upgrade_records.id`. If a current experience
member has no historical experience upgrade record, use membership start time and then relation
binding time as the fallback while leaving `upgrade_record_id` null.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest tests.test_invite_rewards -v`

Expected: schema and backfill tests pass.

### Task 2: Reward Preview and Upgrade Enforcement

**Files:**
- Modify: `server/invites.py`
- Test: `tests/test_invite_rewards.py`

**Interfaces:**
- Produces: `partner_experience_reward_preview(conn, inviter_user_id) -> dict`
- Extends: `reward_upgrade_preview(...)` with `partner_experience_ordinal` and `reward_suppressed_reason`.
- Extends: `record_membership_upgrade(...)` to allocate a slot before partner-to-experience rewards.

- [ ] **Step 1: Write failing first-nine and tenth-member tests**

Create ten direct invitees, upgrade each to experience membership, and assert:

```python
self.assertEqual(first_nine_total, 0)
self.assertEqual(tenth_reward["reward_points"], 240)
self.assertEqual(ordinals, list(range(1, 11)))
```

Also assert preview returns zero for ordinal 1 and 240 for ordinal 10.

- [ ] **Step 2: Run focused tests and verify policy failures**

Run: `python -m unittest tests.test_invite_rewards -v`

Expected: current implementation awards 240 points to the first invitee.

- [ ] **Step 3: Implement transactional slot allocation and preview**

For an active partner whose direct invitee upgrades to `experience`, allocate or reuse an immutable
slot in the same transaction. Return no reward for ordinals 1 through 9. For ordinal 10 and later,
continue through the existing reward matrix logic.

- [ ] **Step 4: Verify focused tests pass**

Run: `python -m unittest tests.test_invite_rewards -v`

Expected: all invite reward tests pass.

### Task 3: Regression and Idempotency Coverage

**Files:**
- Modify: `tests/test_invite_rewards.py`

**Interfaces:**
- Consumes: slot allocation and preview helpers from Tasks 1 and 2.
- Produces: regression coverage for unchanged reward paths.

- [ ] **Step 1: Add regression tests**

Cover duplicate source-order retries, reward voiding without slot release, direct partner upgrade,
experience-member inviter rewards, initiator rewards, and historical reward preservation.

- [ ] **Step 2: Run invite-domain tests**

Run:

```powershell
python -m unittest tests.test_invite_rewards tests.test_invite_registration tests.test_invite_admin -v
```

Expected: all tests pass.

- [ ] **Step 3: Run repository validation**

Run:

```powershell
python scripts/ci_validate.py
python -m compileall -q server tests
```

Expected: both commands exit successfully.

### Task 4: Review and Publish PR

**Files:**
- Review all modified files.

**Interfaces:**
- Produces: one GitHub PR from `codex/partner-first-nine-no-reward-20260730`.

- [ ] **Step 1: Inspect diff and repository status**

Run:

```powershell
git diff --check
git status --short
git diff --stat origin/main...HEAD
```

- [ ] **Step 2: Commit implementation**

Commit the design, plan, tests, schema migration, backfill, preview, and enforcement changes with a
single scoped feature commit.

- [ ] **Step 3: Push and create PR**

Push the branch and open a PR describing policy behavior, migration semantics, verification commands,
and the explicit restriction that it is not merged or deployed.
