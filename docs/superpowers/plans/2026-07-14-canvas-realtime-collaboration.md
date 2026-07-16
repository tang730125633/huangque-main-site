# Canvas Realtime Collaboration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Synchronize collaborative canvas node and edge edits between members within roughly one second without browser refreshes.

**Architecture:** Keep `canvas_boards.data_json` as the recovery snapshot and add versioned operation batches. The browser derives operations by diffing snapshots, posts idempotent batches, polls missing batches, and applies remote operations to the open editor.

**Tech Stack:** Python standard library HTTP server, SQLite, browser JavaScript, Node.js regression tests, Python `unittest`.

## Global Constraints

- Do not add third-party runtime dependencies.
- Preserve the existing `/save` endpoint for compatibility.
- Only owners and editors may submit operations; viewers remain read-only.
- Target visible synchronization latency is 0.5-1 second.
- Do not implement remote cursors, chat, CRDT, or WebSocket infrastructure.

---

### Task 1: Operation Store And Merge Rules

**Files:**
- Modify: `server/auth_server.py`
- Test: `tests/test_auth_canvas_collab.py`

**Interfaces:**
- Produces: `apply_canvas_ops(username, board_id, payload)` returning `(result, error)`.
- Produces: `sync_canvas_ops(username, board_id, since_version)` returning `(result, error)`.

- [ ] Add failing tests for two editors changing different nodes, duplicate `op_id`, stale patch after delete, and viewer rejection.
- [ ] Run `python -m unittest tests.test_auth_canvas_collab -v` and confirm the new tests fail because operation functions/routes do not exist.
- [ ] Add `canvas_ops` schema migration and strict operation validation.
- [ ] Apply a batch inside `BEGIN IMMEDIATE`, update `data_json`, increment `version`, and insert the idempotency record atomically.
- [ ] Return ordered batches from `sync_canvas_ops`; return `reset: true` with a full board when `since_version` predates retained history.
- [ ] Run the collaboration test module and confirm all tests pass.

### Task 2: Operation And Sync HTTP Routes

**Files:**
- Modify: `server/auth_server.py`
- Test: `tests/test_auth_canvas_collab.py`

**Interfaces:**
- Consumes: `apply_canvas_ops` and `sync_canvas_ops` from Task 1.
- Produces: `POST /api/auth/canvas/boards/<id>/ops` and `GET /api/auth/canvas/boards/<id>/sync?since=<version>`.

- [ ] Add failing HTTP tests for successful submission, ordered sync, duplicate submission, malformed operations, missing membership, and viewer access.
- [ ] Run the targeted tests and verify route requests return 404 before implementation.
- [ ] Map store errors to 400, 403, 404, and 413 responses using the existing canvas route style.
- [ ] Run the HTTP tests and the existing `/save` version-guard tests.

### Task 3: Snapshot Diff And Remote Apply

**Files:**
- Modify: `site/workbench/canvas.html`
- Create: `tests/test_canvas_realtime_sync.js`

**Interfaces:**
- Produces: `diffCollabSnapshots(base, next)` returning protocol operations.
- Produces: `applyCollabOps(snapshot, ops)` returning a new normalized snapshot.

- [ ] Add Node tests for node create/patch/delete, edge create/patch/delete, different-field merge, delete priority, and immutability.
- [ ] Run `node tests/test_canvas_realtime_sync.js` and confirm missing helpers fail.
- [ ] Implement pure diff/apply helpers next to the existing snapshot helpers and expose them only through the page script test harness.
- [ ] Run the Node test and confirm all operation cases pass.

### Task 4: Browser Synchronization Controller

**Files:**
- Modify: `site/workbench/canvas.html`
- Test: `tests/test_canvas_realtime_sync.js`

**Interfaces:**
- Consumes: operation routes and diff/apply helpers.
- Produces: `startCollabSync`, `stopCollabSync`, `flushCollabOps`, and `pollCollabOps` lifecycle functions.

- [ ] Add failing tests using a fake request adapter for ordered submission, self-operation filtering, remote apply, retry backoff, visibility interval, and full reset.
- [ ] Run the Node test and verify controller cases fail before implementation.
- [ ] Track `collabBaseSnap`, `currentCollabVersion`, `collabClientId`, pending batches, timer state, and retry delay.
- [ ] Replace collaborative full-snapshot autosave with diff batches while leaving local canvas saving unchanged.
- [ ] Poll every 800ms while visible and every 3 seconds while hidden; sync immediately on visibility restoration.
- [ ] On remote operations, apply them to the base and current snapshot, preserving unsent local differences, then render once.
- [ ] On reset, replace the base with the server snapshot and reapply pending local operations.
- [ ] Stop timers when leaving a board or unloading the page.
- [ ] Run all frontend collaboration tests.

### Task 5: Presence And Sync Status

**Files:**
- Modify: `server/auth_server.py`
- Modify: `site/workbench/canvas.html`
- Test: `tests/test_auth_canvas_collab.py`
- Test: `tests/test_canvas_realtime_sync.js`

**Interfaces:**
- Produces: `POST /api/auth/canvas/boards/<id>/presence`.
- Produces: active editor count in sync and presence responses.

- [ ] Add failing tests for heartbeat upsert, 30-second expiry, member authorization, and client cleanup semantics.
- [ ] Add `canvas_presence` schema and heartbeat route.
- [ ] Send heartbeats every 10 seconds and render `X 人在线`.
- [ ] Map controller states to `正在同步`, `已同步`, `离线重连`, `同步失败`, and `只读`.
- [ ] Verify presence and status tests pass.

### Task 6: Regression And Two-Client Verification

**Files:**
- Modify: `tests/test_auth_canvas_collab.py` only if integration coverage requires shared helpers.
- Modify: `tests/test_canvas_realtime_sync.js` only for discovered regressions.

- [ ] Run `python -m unittest tests.test_auth_canvas_collab -v`.
- [ ] Run `node tests/test_canvas_realtime_sync.js`.
- [ ] Run `node tests/test_canvas_board_card_layout.js` and `node tests/test_cloud_shell_sidebar.js`.
- [ ] Run `git diff --check` and the repository CI validation command when Python is available.
- [ ] Open the same board as owner and editor in two browser sessions; verify node creation, movement, text edits, edges, deletion, offline recovery, and viewer read-only behavior.
- [ ] Confirm the page no longer requires refresh and the observed synchronization delay stays near one second.
