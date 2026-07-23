# Short Drama Phase Two Stills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Existing `stills_review` short-drama projects enter a three-column production workspace, generate two paid keyframe candidates per shot, preserve version history, lock selected stills, and advance safely to `voice_review`.

**Architecture:** Keep first-stage project behavior in `short_drama.py` and add a focused `short_drama_production.py` domain for production persistence and invariants. Reuse the existing authenticated image job, point deduction/refund, queue, polling, and idempotency pipeline through a narrow submission adapter in `core.py`; reconcile completed generic jobs into versioned short-drama assets when production state is read. Add an isolated browser module for the production workspace instead of extending the already large first-stage module.

**Tech Stack:** Python 3.12 standard library (`sqlite3`, `unittest`, `http.server`), vanilla JavaScript compatible with Node 22 browser tests, existing Huangque job/points APIs, HTML/CSS.

## Global Constraints

- Support exactly `9:16` and `16:9`; generated image ratio must match the project ratio.
- Keep existing shot durations of exactly 5 or 10 seconds.
- Generate exactly 2 candidate stills per submitted shot in this increment.
- Obtain a server-side live quote and explicit user confirmation before every paid submission.
- Never let batch generation overwrite a locked asset; regeneration appends history.
- Saving, selecting a historical version, locking, and stage confirmation cost zero points.
- Validate authentication, `must_change`, ownership, revision, stage, shot ownership, and referenced asset ownership on every write.
- Require idempotency keys for still generation; retries cannot enqueue or charge twice.
- Do not commit server configuration, secrets, databases, user data, or generated artifacts.
- Keep voice, subtitle, video, assembly, and export controls outside this PR.

---

## File Responsibility Map

- Create `server/content_domains/short_drama_production.py`: production schema, request validation, job linkage, result reconciliation, asset selection/locking, and production-stage confirmation.
- Modify `server/content_domains/short_drama.py`: extend stage constants, initialize the production schema, expose production HTTP routes, and preserve first-stage behavior.
- Modify `server/content_domains/core.py`: translate validated `generate-stills` submissions into the existing image paid-job pipeline and record the resulting job association atomically.
- Create `tests/test_short_drama_production.py`: persistence, ownership, revision, budget, idempotency, reconciliation, and stage tests.
- Create `site/workbench/canvas/canvas-short-drama-production.js`: normalized production state, renderer, event handling, quoting, submission, polling, selection, locking, and confirmation.
- Create `site/workbench/canvas/canvas-short-drama-production.css`: three-column production layout and responsive states.
- Modify `site/workbench/canvas/canvas-short-drama.js`: extended stage labels and delegation to the production module for `stills_review` and later production stages.
- Modify `site/workbench/canvas.html`: load new production assets with cache stamps.
- Create `tests/test_canvas_short_drama_production.js`: deterministic DOM-free renderer/controller tests.
- Modify `.github/workflows/ci.yml`: execute the new production JavaScript test in CI.

---

### Task 1: Extend stages and create production persistence

**Files:**
- Create: `server/content_domains/short_drama_production.py`
- Modify: `server/content_domains/short_drama.py`
- Test: `tests/test_short_drama_production.py`

**Interfaces:**
- Consumes: `db_factory`, existing `short_drama_projects` and `short_drama_shots` tables.
- Produces: `short_drama_production.init_db(db_factory)`, `short_drama_production.ensure_asset_slots(conn, project_id)`, and the stage sequence ending in `completed`.

- [ ] **Step 1: Write failing schema and compatibility tests**

Add a `ShortDramaProductionTests` fixture that creates a temporary SQLite database, initializes the short-drama domain, creates/applies a six-shot project, and advances it to `stills_review`. Add these assertions:

```python
class ShortDramaProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        short_drama.init_db(self.db)

    def test_init_creates_versioned_production_tables(self):
        with closing(self.db()) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({
            "short_drama_assets",
            "short_drama_asset_versions",
            "short_drama_production_jobs",
        }.issubset(names))

    def test_stage_sequence_keeps_existing_stills_projects_eligible(self):
        self.assertEqual(short_drama.NEXT_STAGE["storyboard_review"], "stills_review")
        self.assertEqual(short_drama.NEXT_STAGE["stills_review"], "voice_review")
        self.assertEqual(short_drama.STAGES[-4:], (
            "voice_review", "video_review", "assembly_review", "completed",
        ))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_short_drama_production -v
```

Expected: failures because production tables and post-stills stages do not exist.

- [ ] **Step 3: Implement the schema and stage extension**

Define in `short_drama_production.py`:

```python
ASSET_TYPES = {"still"}
JOB_KINDS = {"still"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_drama_assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (type IN ('still')),
  current_version INTEGER,
  locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(project_id, shot_id, type)
);
CREATE TABLE IF NOT EXISTS short_drama_asset_versions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES short_drama_assets(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  job_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  prompt TEXT NOT NULL,
  ratio TEXT NOT NULL CHECK (ratio IN ('9:16','16:9')),
  cost INTEGER NOT NULL DEFAULT 0 CHECK (cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('done','failed')),
  created_at INTEGER NOT NULL,
  UNIQUE(asset_id, version),
  UNIQUE(asset_id, job_id, url)
);
CREATE TABLE IF NOT EXISTS short_drama_production_jobs (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES short_drama_projects(id) ON DELETE CASCADE,
  shot_id TEXT NOT NULL REFERENCES short_drama_shots(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('still')),
  job_id INTEGER NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL,
  quoted_cost INTEGER NOT NULL CHECK (quoted_cost >= 0),
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(username, kind, idempotency_key)
);
"""

def init_db(db_factory):
    conn = db_factory()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
```

Extend `short_drama.py` constants to:

```python
STAGES = (
    "draft", "characters_review", "script_review", "storyboard_review",
    "stills_review", "voice_review", "video_review", "assembly_review", "completed",
)
NEXT_STAGE = {
    "characters_review": "script_review",
    "script_review": "storyboard_review",
    "storyboard_review": "stills_review",
    "stills_review": "voice_review",
    "voice_review": "video_review",
    "video_review": "assembly_review",
    "assembly_review": "completed",
}
```

Call `short_drama_production.init_db(db_factory)` after the existing schema commit.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the same command. Expected: both new tests pass and existing stage tests remain green.

- [ ] **Step 5: Commit the persistence increment**

```powershell
git add server/content_domains/short_drama.py server/content_domains/short_drama_production.py tests/test_short_drama_production.py
git commit -m "feat: add short drama production persistence"
```

---

### Task 2: Read normalized production state with ownership and recovery

**Files:**
- Modify: `server/content_domains/short_drama_production.py`
- Modify: `server/content_domains/short_drama.py`
- Test: `tests/test_short_drama_production.py`

**Interfaces:**
- Consumes: `jobs(id, username, kind, cost, status, payload, result)` and Task 1 tables.
- Produces: `get_production(db_factory, username, project_id) -> dict` and authenticated `GET /api/gen/short-drama/production?project_id=<project_id>`.

- [ ] **Step 1: Add failing production-read tests**

Add tests asserting that an owner receives six ordered shot entries with empty still slots, a non-owner gets `LookupError`, and a linked completed image job is reconciled once even across repeated reads:

```python
def test_production_state_bootstraps_slots_for_existing_stills_project(self):
    project = self._stills_project("alice")
    state = production.get_production(self.db, "alice", project["id"])
    self.assertEqual(state["stage"], "stills_review")
    self.assertEqual(len(state["shots"]), 6)
    self.assertTrue(all(item["still"]["versions"] == [] for item in state["shots"]))

def test_production_state_does_not_disclose_another_users_project(self):
    project = self._stills_project("alice")
    with self.assertRaises(LookupError):
        production.get_production(self.db, "mallory", project["id"])
```

- [ ] **Step 2: Run tests and confirm RED**

Expected: `get_production` and the production route are absent.

- [ ] **Step 3: Implement state bootstrapping and reconciliation**

Implement these exact public functions:

```python
def ensure_asset_slots(conn, project_id):
    now = int(time.time())
    shots = conn.execute(
        "SELECT id FROM short_drama_shots WHERE project_id=? ORDER BY sort_order, id",
        (project_id,),
    ).fetchall()
    for (shot_id,) in shots:
        conn.execute(
            "INSERT OR IGNORE INTO short_drama_assets "
            "(id, project_id, shot_id, type, created_at, updated_at) VALUES (?, ?, ?, 'still', ?, ?)",
            (str(uuid.uuid4()), project_id, shot_id, now, now),
        )

def reconcile_jobs(conn, username, project_id):
    rows = conn.execute(
        "SELECT p.id, p.shot_id, p.job_id, p.status, j.status, j.cost, j.payload, j.result "
        "FROM short_drama_production_jobs p JOIN jobs j ON j.id=p.job_id "
        "WHERE p.username=? AND p.project_id=?",
        (username, project_id),
    ).fetchall()
    now = int(time.time())
    for link_id, shot_id, job_id, link_status, job_status, cost, payload_json, result_json in rows:
        status = job_status if job_status in {"pending", "running", "done", "failed"} else "failed"
        conn.execute(
            "UPDATE short_drama_production_jobs SET status=?, updated_at=? WHERE id=?",
            (status, now, link_id),
        )
        if status != "done":
            continue
        payload = json.loads(payload_json or "{}")
        result = json.loads(result_json or "{}")
        project_ratio = conn.execute(
            "SELECT ratio FROM short_drama_projects WHERE id=?", (project_id,)
        ).fetchone()[0]
        if result.get("ratio") != project_ratio or payload.get("ratio") != project_ratio:
            raise ValueError("关键帧任务比例与项目不一致")
        urls = result.get("urls") or ([result.get("url")] if result.get("url") else [])
        if len(urls) != 2 or any(not isinstance(url, str) or not url for url in urls):
            raise ValueError("关键帧任务必须返回 2 张候选图")
        asset_id = conn.execute(
            "SELECT id FROM short_drama_assets WHERE project_id=? AND shot_id=? AND type='still'",
            (project_id, shot_id),
        ).fetchone()[0]
        next_version = int(conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM short_drama_asset_versions WHERE asset_id=?",
            (asset_id,),
        ).fetchone()[0])
        for offset, url in enumerate(urls):
            conn.execute(
                "INSERT OR IGNORE INTO short_drama_asset_versions "
                "(id, asset_id, version, job_id, url, prompt, ratio, cost, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
                (str(uuid.uuid4()), asset_id, next_version + offset, job_id, url,
                 payload.get("prompt") or "", project_ratio, int(cost or 0), now),
            )
        conn.execute(
            "UPDATE short_drama_assets SET current_version=COALESCE(current_version, ?), updated_at=? "
            "WHERE id=?",
            (next_version, now, asset_id),
        )

def get_production(db_factory, username, project_id):
    conn = db_factory()
    conn.row_factory = sqlite3.Row
    try:
        project = conn.execute(
            "SELECT * FROM short_drama_projects WHERE id=? AND username=? AND deleted=0",
            (project_id, username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project["stage"] not in PRODUCTION_STAGES:
            raise ValueError("短剧项目尚未进入素材制作")
        ensure_asset_slots(conn, project_id)
        reconcile_jobs(conn, username, project_id)
        conn.commit()
        return build_production_snapshot(conn, dict(project), username)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

Implement `PRODUCTION_STAGES` as `{ "stills_review", "voice_review", "video_review",
"assembly_review", "completed" }`. Implement `build_production_snapshot(conn, project, username)` with the
exact response shape below using ordered shot, asset, version, and active production-job queries; calculate
`reserved_points` as the sum of `quoted_cost` for `pending` and `running` links.

The returned shape must be:

```python
{
    "project_id": project_id,
    "revision": 5,
    "stage": "stills_review",
    "ratio": "9:16",
    "point_budget": 1400,
    "spent_points": 120,
    "reserved_points": 40,
    "shots": [{
        "id": "shot-row-id",
        "shot_key": "shot-0",
        "sort_order": 0,
        "duration": 5,
        "image_prompt": "潮湿门厅画面",
        "still": {
            "asset_id": "asset-id",
            "current_version": None,
            "locked": False,
            "versions": [],
            "job": None,
        },
    }],
}
```

Add the GET route to `_HTTP_ROUTES` and dispatch it after standard authentication and `must_change` checks.

- [ ] **Step 4: Run focused and first-stage regression tests**

```powershell
python -m unittest tests.test_short_drama_production tests.test_short_drama_projects -v
```

Expected: all production read tests and all existing first-stage project tests pass.

- [ ] **Step 5: Commit production state reading**

```powershell
git add server/content_domains/short_drama.py server/content_domains/short_drama_production.py tests/test_short_drama_production.py
git commit -m "feat: expose short drama production state"
```

---

### Task 3: Quote and submit idempotent paid still jobs

**Files:**
- Modify: `server/content_domains/short_drama_production.py`
- Modify: `server/content_domains/short_drama.py`
- Modify: `server/content_domains/core.py`
- Test: `tests/test_short_drama_production.py`

**Interfaces:**
- Consumes: existing `image.validate_image_payload`, `points.cost_of`, `jobs_store.create_paid_job`, core idempotency functions, and image queue.
- Produces: `prepare_still_quote`, `prepare_still_submission`, `record_submitted_job`, `POST /asset-quote`, and `POST /generate-stills`.

- [ ] **Step 1: Add failing validation, budget, idempotency, and ownership tests**

Add tests for exactly two candidates, ratio derived from the project, rejection outside `stills_review`, rejection of a foreign shot, rejection of a locked shot in batch mode, budget failure before deduction, and replay of the same `Idempotency-Key`.

```python
def test_still_submission_is_bound_to_owned_project_shot_and_ratio(self):
    project = self._stills_project("alice")
    shot = project["shots"][0]
    prepared = production.prepare_still_submission(
        self.db, "alice", {
            "project_id": project["id"], "revision": project["revision"],
            "shot_id": shot["id"], "prompt": "雨夜门厅，角色一致",
            "mode": "single", "count": 2,
        },
    )
    self.assertEqual(prepared["image_payload"]["ratio"], project["ratio"])
    self.assertEqual(prepared["image_payload"]["count"], 2)
```

- [ ] **Step 2: Run focused tests and confirm RED**

Expected: submission preparation and routes are absent.

- [ ] **Step 3: Implement strict preparation and quote functions**

Use this immutable request contract:

```python
STILL_REQUEST_FIELDS = {
    "project_id", "revision", "shot_id", "prompt", "mode", "count",
}

def prepare_still_submission(db_factory, username, body):
    if not isinstance(body, dict) or set(body) != STILL_REQUEST_FIELDS:
        raise ValueError("关键帧请求字段不正确")
    if body["mode"] not in {"single", "retry", "batch"} or body["count"] != 2:
        raise ValueError("关键帧每次必须生成 2 张候选图")
    # Read owned project and owned shot, require exact revision and stills_review.
    # For batch mode reject/skip locked slots before any paid work.
    # Return a server-built image payload; never accept provider cost or ratio from the client.
    return {
        "project": project,
        "shot": shot,
        "image_payload": {
            "provider": "seedream",
            "variant": "std",
            "quality": "hd",
            "prompt": normalized_prompt,
            "ratio": project["ratio"],
            "count": 2,
        },
    }

def prepare_still_quote(db_factory, username, body, cost_of):
    prepared = prepare_still_submission(db_factory, username, body)
    cost = int(cost_of("image", prepared["image_payload"]))
    check_production_budget(db_factory, username, prepared["project"]["id"], cost)
    return {"cost": cost, "count": 2, "kind": "still"}
```

- [ ] **Step 4: Route submission through the existing paid-job pipeline**

In `core.py`, before the generic `/api/gen/<kind>` branch, recognize
`/api/gen/short-drama/generate-stills`, authenticate, require an `Idempotency-Key`, call
`prepare_still_submission`, and pass its server-built `image_payload` through the same content-security,
upstream-guard, cost, idempotency, point-deduction, job insertion, and queue checks used by `/api/gen/image`.

After `create_paid_job` succeeds and before queueing, call:

```python
production.record_submitted_job(
    jdb, username=user["username"], project_id=prepared["project"]["id"],
    shot_id=prepared["shot"]["id"], job_id=jid,
    idempotency_key=idem_key, quoted_cost=cost,
)
```

If association recording or queueing fails, use the existing pending-job rejection/refund path and abort
the idempotency record. Return the existing response fields plus `project_id` and `shot_id`.

- [ ] **Step 5: Run backend tests**

```powershell
python -m unittest tests.test_short_drama_production tests.test_short_drama_projects tests.test_job_refund_cas tests.test_imggen_job_cas -v
```

Expected: paid still tests, first-stage tests, image CAS, and refund regression tests all pass.

- [ ] **Step 6: Commit paid still submission**

```powershell
git add server/content_domains/core.py server/content_domains/short_drama.py server/content_domains/short_drama_production.py tests/test_short_drama_production.py
git commit -m "feat: submit quoted short drama still jobs"
```

---

### Task 4: Reconcile versions, select and lock assets, and confirm the stage

**Files:**
- Modify: `server/content_domains/short_drama_production.py`
- Modify: `server/content_domains/short_drama.py`
- Test: `tests/test_short_drama_production.py`

**Interfaces:**
- Consumes: Task 2 reconciliation and Task 3 job links.
- Produces: `select_asset`, `confirm_stage`, `POST /select-asset`, and `POST /confirm-production-stage`.

- [ ] **Step 1: Add failing version-safety and confirmation tests**

Cover these transitions:

```python
def test_selecting_a_version_preserves_history_and_can_lock(self):
    project, asset, versions = self._completed_still_versions("alice")
    updated = production.select_asset(self.db, "alice", {
        "project_id": project["id"], "revision": project["revision"],
        "asset_id": asset["id"], "version": versions[1]["version"], "lock": True,
    })
    selected = updated["shots"][0]["still"]
    self.assertEqual(selected["current_version"], versions[1]["version"])
    self.assertTrue(selected["locked"])
    self.assertEqual(len(selected["versions"]), 2)

def test_confirm_requires_every_current_shot_to_have_a_locked_still(self):
    project = self._stills_project("alice")
    with self.assertRaises(ValueError):
        production.confirm_stage(self.db, "alice", {
            "project_id": project["id"], "revision": project["revision"],
            "stage": "stills_review",
        })
```

Also test non-owner asset IDs, failed versions, stale revisions, duplicate confirmation, and regeneration after locking.

- [ ] **Step 2: Run tests and confirm RED**

Expected: selection and production confirmation functions are absent.

- [ ] **Step 3: Implement CAS selection and locking**

Use an exact request contract:

```python
def select_asset(db_factory, username, body):
    if set(body or {}) != {"project_id", "revision", "asset_id", "version", "lock"}:
        raise ValueError("资产选择请求字段不正确")
    if type(body["revision"]) is not int or type(body["version"]) is not int:
        raise ValueError("资产版本无效")
    if type(body["lock"]) is not bool:
        raise ValueError("锁定状态无效")
    conn = db_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT p.revision, p.stage FROM short_drama_assets a "
            "JOIN short_drama_projects p ON p.id=a.project_id "
            "JOIN short_drama_asset_versions v ON v.asset_id=a.id AND v.version=? "
            "WHERE a.id=? AND a.project_id=? AND p.username=? AND p.deleted=0 "
            "AND v.status='done'",
            (body["version"], body["asset_id"], body["project_id"], username),
        ).fetchone()
        if not row:
            raise LookupError("关键帧版本不存在")
        if row[0] != body["revision"]:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if row[1] != "stills_review":
            raise ValueError("当前阶段不能选择关键帧")
        conn.execute(
            "UPDATE short_drama_assets SET current_version=?, locked=?, updated_at=? WHERE id=?",
            (body["version"], int(body["lock"]), int(time.time()), body["asset_id"]),
        )
        cur = conn.execute(
            "UPDATE short_drama_projects SET revision=revision+1, updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage='stills_review' AND deleted=0",
            (int(time.time()), body["project_id"], username, body["revision"]),
        )
        if cur.rowcount != 1:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_production(db_factory, username, body["project_id"])
```

- [ ] **Step 4: Implement guarded stage confirmation**

```python
def confirm_stage(db_factory, username, body):
    if set(body or {}) != {"project_id", "revision", "stage"}:
        raise ValueError("生产阶段确认请求字段不正确")
    if body["stage"] != "stills_review":
        raise ValueError("当前批次只能确认关键帧阶段")
    conn = db_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT revision, stage FROM short_drama_projects "
            "WHERE id=? AND username=? AND deleted=0",
            (body["project_id"], username),
        ).fetchone()
        if not project:
            raise LookupError("短剧项目不存在")
        if project[0] != body["revision"]:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        if project[1] != "stills_review":
            raise ValueError("不能跳过短剧生产阶段")
        shot_ids = {row[0] for row in conn.execute(
            "SELECT id FROM short_drama_shots WHERE project_id=?", (body["project_id"],)
        )}
        locked_ids = {row[0] for row in conn.execute(
            "SELECT a.shot_id FROM short_drama_assets a "
            "JOIN short_drama_asset_versions v "
            "ON v.asset_id=a.id AND v.version=a.current_version "
            "WHERE a.project_id=? AND a.type='still' AND a.locked=1 AND v.status='done'",
            (body["project_id"],),
        )}
        if not shot_ids or locked_ids != shot_ids:
            raise ValueError("请先锁定所有镜头的关键帧")
        cur = conn.execute(
            "UPDATE short_drama_projects SET stage='voice_review', revision=revision+1, updated_at=? "
            "WHERE id=? AND username=? AND revision=? AND stage='stills_review' AND deleted=0",
            (int(time.time()), body["project_id"], username, body["revision"]),
        )
        if cur.rowcount != 1:
            raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_production(db_factory, username, body["project_id"])
```

Register both POST routes through the authenticated production dispatcher.

- [ ] **Step 5: Run all short-drama backend tests**

```powershell
python -m unittest tests.test_short_drama_production tests.test_short_drama_projects tests.test_short_drama_planning -v
```

Expected: all production and first-stage short-drama tests pass.

- [ ] **Step 6: Commit asset locking and stage confirmation**

```powershell
git add server/content_domains/short_drama.py server/content_domains/short_drama_production.py tests/test_short_drama_production.py
git commit -m "feat: lock short drama still versions"
```

---

### Task 5: Build the three-column production workspace module

**Files:**
- Create: `site/workbench/canvas/canvas-short-drama-production.js`
- Create: `site/workbench/canvas/canvas-short-drama-production.css`
- Test: `tests/test_canvas_short_drama_production.js`

**Interfaces:**
- Consumes: a JSON client with `json(path, options)`, production state from Task 2, and a confirmation callback.
- Produces: `HQCanvas.shortDramaProduction.normalizeState`, `renderWorkspace`, and `createWorkspace`.

- [ ] **Step 1: Add failing renderer/controller tests**

Use Node's strict assertions and a fake client. Include tests for:

```javascript
const assert = require('node:assert/strict');
const production = require('../site/workbench/canvas/canvas-short-drama-production.js');

assert.match(production.renderWorkspace(sampleState(), {selectedShotId:'shot-1'}),
  /镜头列表[\s\S]*关键帧候选[\s\S]*生成控制台/);
assert.match(production.renderWorkspace(sampleState({ratio:'9:16'}), {}),
  /data-ratio="9:16"/);
assert.doesNotMatch(production.renderWorkspace(sampleState({canEdit:false}), {}),
  /data-action="generate-current"(?![^>]*disabled)/);
```

Add an async controller test proving quote -> confirm -> submit order and proving cancellation never calls
`generate-stills`.

- [ ] **Step 2: Run the JS test and confirm RED**

```powershell
node tests/test_canvas_short_drama_production.js
```

Expected: module not found.

- [ ] **Step 3: Implement state normalization and pure renderer**

Export this public surface:

```javascript
return {
  normalizeState: normalizeState,
  renderWorkspace: renderWorkspace,
  createWorkspace: createWorkspace
};
```

`normalizeState` must coerce only server-returned display fields, retain all version IDs, and select the first
shot when the requested shot is absent. `renderWorkspace` must render:

- left: ordered shot cards and state filters;
- center: editable prompt, references, candidates, history, selection, and lock controls;
- right: quote, budget, spent/reserved points, progress, errors, retry, batch, and confirmation controls;
- explicit disabled attributes in read-only, busy, stale, or non-`stills_review` states;
- `data-ratio="9:16"` or `data-ratio="16:9"` on preview containers.

- [ ] **Step 4: Implement controller request order and recovery**

The controller must use these exact routes:

```javascript
GET  /api/gen/short-drama/production?project_id=<encoded>
POST /api/gen/short-drama/asset-quote
POST /api/gen/short-drama/generate-stills
POST /api/gen/short-drama/select-asset
POST /api/gen/short-drama/confirm-production-stage
```

Generate an idempotency key once per user action and pass it as `Idempotency-Key`; keep the same key if a
network timeout is retried. After submission, poll production state until the linked shot is no longer pending
or running. On 409, set `stale=true`, disable writes, and show a refresh action.

- [ ] **Step 5: Implement responsive CSS**

Use the existing short-drama design tokens. Desktop layout is `260px minmax(0,1fr) 300px`; below 980px,
stack the inspector below the editor while keeping the shot rail horizontally scrollable. Use `aspect-ratio:
9 / 16` and `16 / 9` selectors to prevent stretching and `object-fit: contain` for candidates.

- [ ] **Step 6: Run the production JS test**

Expected: all renderer, quoting, cancellation, idempotency, read-only, ratio, and stale-state assertions pass.

- [ ] **Step 7: Commit the production workspace**

```powershell
git add site/workbench/canvas/canvas-short-drama-production.js site/workbench/canvas/canvas-short-drama-production.css tests/test_canvas_short_drama_production.js
git commit -m "feat: add short drama stills workspace"
```

---

### Task 6: Integrate production workspace into the canvas

**Files:**
- Modify: `site/workbench/canvas/canvas-short-drama.js`
- Modify: `site/workbench/canvas.html`
- Modify: `tests/test_canvas_short_drama.js`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `window.HQCanvas.shortDramaProduction` from Task 5.
- Produces: current `stills_review` nodes open the production workspace; first-stage APIs remain unchanged.

- [ ] **Step 1: Update failing first-stage integration expectations**

Replace the obsolete assertion that `stills_review` shows “已完成第一阶段” with assertions that it delegates
to production and labels the tab “画面确认”. Preserve an explicit test that projects before `stills_review`
cannot open production controls.

- [ ] **Step 2: Run both canvas tests and confirm RED**

```powershell
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
```

Expected: the existing module still renders the old completion page.

- [ ] **Step 3: Extend labels and delegate production stages**

Update the browser stage constants and labels:

```javascript
var STAGES = ['draft','characters_review','script_review','storyboard_review',
  'stills_review','voice_review','video_review','assembly_review','completed'];
var STAGE_LABELS = {
  settings:'项目设置', characters_review:'角色确认', script_review:'剧本确认',
  storyboard_review:'分镜确认', stills_review:'画面确认', voice_review:'配音字幕',
  video_review:'视频确认', assembly_review:'成片确认', completed:'已交付'
};
```

When the loaded project is in a production stage, instantiate Task 5's workspace with the existing authenticated
canvas API client, project ID, collaboration edit permission, and summary `onChange` callback. Destroy it on
close, node deletion, board switch, and role downgrade exactly as the current first-stage workspace is destroyed.

- [ ] **Step 4: Load assets and add CI coverage**

In `canvas.html`, load production CSS after the existing short-drama CSS and production JS before
`canvas-short-drama.js`. Run `python scripts/stamp_assets.py` to update deterministic cache stamps rather than
editing query values manually.

Add to `.github/workflows/ci.yml`:

```yaml
      - name: 测试短剧第二阶段工作区
        run: node tests/test_canvas_short_drama_production.js
```

- [ ] **Step 5: Run canvas regression tests and stamp checks**

```powershell
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
python scripts/stamp_assets.py --check
```

Expected: every command exits zero.

- [ ] **Step 6: Commit canvas integration**

```powershell
git add site/workbench/canvas.html site/workbench/canvas/canvas-short-drama.js tests/test_canvas_short_drama.js .github/workflows/ci.yml
git commit -m "feat: open phase two stills from canvas"
```

---

### Task 7: Full verification and one authorized real-generation smoke test

**Files:**
- Modify only if verification reveals an in-scope defect; add a regression test before each fix.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: evidence that automated gates pass and one test-server still generation has correct charging and persistence.

- [ ] **Step 1: Run all automated Python tests**

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass; no skipped production security, budget, or idempotency tests.

- [ ] **Step 2: Run JavaScript and static gates**

```powershell
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

Expected: every command exits zero with no sensitive/config/generated files reported.

- [ ] **Step 3: Check syntax and working-tree scope**

```powershell
python -m py_compile server/content_domains/short_drama.py server/content_domains/short_drama_production.py server/content_domains/core.py
node --check site/workbench/canvas/canvas-short-drama.js
node --check site/workbench/canvas/canvas-short-drama-production.js
git diff --check main...HEAD
git status --short
```

Expected: syntax and whitespace checks pass; status contains no database, environment, user data, or generated image.

- [ ] **Step 4: Run local UI smoke tests without paid generation**

At `http://127.0.0.1:8097/workbench/canvas.html`, verify an existing `stills_review` project opens the production
workspace, six shots render in order, read-only behavior follows collaboration role, and both ratios render without
stretching or subtitle/video controls.

- [ ] **Step 5: Run one authorized real paid smoke test**

Using the test account and test server only:

1. Record account points and project spent/reserved values.
2. Choose one unlocked test shot and request a quote.
3. Confirm one two-candidate generation.
4. Verify one job association, one charge, two candidate versions, and correct ratio.
5. Refresh the page and restart only the local proxy if needed; verify state recovery.
6. Select and lock one version; verify no additional point charge.
7. Do not batch-generate remaining shots during this smoke test unless separately authorized.

- [ ] **Step 6: Record defects and fix only in-scope regressions**

For every observed failure, record the exact route/action, response/status, expected result, actual result, and
evidence. Add a failing automated test, implement the minimal fix, and rerun the focused plus full gate.

- [ ] **Step 7: Prepare handoff without deploying or opening a PR automatically**

Report branch, commits, changed files, tests, real point delta, whether services were restarted, and residual risks.
Do not push, deploy, or open the PR until the user requests that external action.

---

## Plan Self-Review

- Spec coverage: Tasks 1-4 cover stage, schema, ownership, quote, budget, idempotency, recovery, versions, locking,
  and confirmation. Tasks 5-6 cover the three-column UI, collaboration read-only behavior, ratios, cache stamps,
  and CI. Task 7 covers full regression and the authorized real paid smoke test.
- Scope: voice, subtitles, video, assembly, export, server configuration, and unrelated canvas refactoring are
  explicitly excluded.
- Interface consistency: `get_production`, `prepare_still_quote`, `prepare_still_submission`,
  `record_submitted_job`, `select_asset`, and `confirm_stage` are defined once and consumed by later tasks with the
  same names and request fields.
- Placeholder scan: implementation steps name concrete routes, fields, SQL transitions, test commands, expected
  outcomes, and failure behavior; the plan contains no deferred implementation markers.
