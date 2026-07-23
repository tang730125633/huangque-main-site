# PR #19 Review Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every PR #19 review concern, record all nine real browser acceptance results, and convert the existing pull request from Draft to Ready with green CI.

**Architecture:** Extend the existing voice schema initializer with deterministic integrity-trigger migrations, centralize still-to-voice eligibility in one server-generated handoff decision, and make both the mutation and frontend consume that decision. Keep PR 3-A read-only, align the OpenAPI anti-enumeration contract, and validate the finished branch with an isolated browser fixture before updating the existing PR.

**Tech Stack:** Python 3.12 standard library, SQLite, browser JavaScript UMD modules, CSS, Node.js 22 contract tests, Python `unittest`, Git/GitHub CLI.

## Global Constraints

- Work only on `codex/short-drama-phase3-voice-spec`; update PR #19 instead of creating another PR.
- Rebase latest `origin/main` before implementation, then rerun every gate after the rebase.
- PR 3-A remains read-only: no TTS submission, point deduction endpoint, subtitle/timeline mutation, shot locking, voice version generation, or `voice_review -> video_review` progression.
- Preserve Phase 2 still billing, idempotency, reconciliation, recovery, refund, and single-winner confirmation behavior.
- Database triggers enforce internal identity consistency; authentication and canvas roles remain service-layer responsibilities.
- Project-level missing and unauthorized reads both return 404; 403 is limited to account-level restrictions.
- Browser acceptance uses synthetic local data only; never commit databases, tokens, cookies, passwords, screenshots containing secrets, or generated media.
- Use explicit `git add` commands listing the files named by each task; never use `git add -A`.
- Do not merge, deploy, restart services, or modify any server through SSH.

---

## File Map

### Create

- `tests/fixtures/short_drama_voice_acceptance.py` — deterministic synthetic six-shot acceptance fixture builder used only by local test setup.
- `tests/test_short_drama_voice_acceptance.py` — fixture isolation, identities, roles, narrator/silent coverage, and cleanup tests.

### Modify

- `server/content_domains/short_drama_voice.py` — migrate and strengthen snapshot, quote, job, attempt, and version identity triggers.
- `server/content_domains/short_drama_production.py` — canonical handoff decision and transaction-side recheck.
- `site/workbench/canvas/canvas-short-drama-production.js` — normalize/render/consume server handoff blockers.
- `docs/api/openapi.json` — production blocker fields and corrected voice 403/404 descriptions.
- `tests/test_short_drama_voice.py` — trigger migration and reverse-update coverage.
- `tests/test_short_drama_production.py` — canonical blocker, reconciliation, rollback, and concurrency coverage.
- `tests/test_canvas_short_drama_production.js` — frontend server-blocker behavior.
- `tests/test_canvas_short_drama.js` — OpenAPI contract assertions.
- `site/workbench/canvas.html` — only when asset stamps change after frontend edits.

---

### Task 1: Rebase PR #19 onto latest `origin/main` and establish the baseline

**Files:**

- Verify only; resolve rebase conflicts only in files already owned by PR #19.

**Interfaces:**

- Consumes: current branch `codex/short-drama-phase3-voice-spec` and remote `origin/main`.
- Produces: rebased local branch with a clean working tree and passing pre-change baseline.

- [ ] **Step 1: Confirm environment, identity, branch, and clean state**

Run:

```powershell
python --version
node --version
npm.cmd --version
gh auth status
git config user.name
git config user.email
git status --short --branch
git branch --show-current
```

Expected:

- Python reports 3.12.x.
- Node reports 22.x. If Node 22 is unavailable, stop and install/select it before continuing.
- Git identity is `kongli` / `kong74007@gmail.com`.
- Branch is `codex/short-drama-phase3-voice-spec` and the worktree is clean.

- [ ] **Step 2: Fetch and rebase latest main**

Run:

```powershell
git fetch origin --prune
git rebase origin/main
```

If a conflict occurs, edit only the conflicting PR #19 files, then stage exactly the paths Git still marks unresolved and continue:

```powershell
$conflictedPaths = git diff --name-only --diff-filter=U
git add -- $conflictedPaths
git rebase --continue
```

Expected: rebase exits successfully and `git merge-base HEAD origin/main` equals `git rev-parse origin/main`.

- [ ] **Step 3: Run the pre-change baseline gates**

Run:

```powershell
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
python -m compileall -q server scripts worker
node --check site/workbench/cloud-shell.js
python -m unittest tests.test_short_drama_voice tests.test_short_drama_production -v
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
node tests/test_canvas_short_drama_voice.js
git diff --check origin/main...HEAD
```

Expected: every command exits 0. Record any Windows-only environment failure separately and rerun the identical command in an approved environment; do not treat an environment failure as a passing gate.

- [ ] **Step 4: Record the rebased baseline**

Run:

```powershell
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

Expected: the worktree is clean and contains only PR #19 commits/files. No new commit is created for this task.

---

### Task 2: Close voice ledger reverse-update and identity constraints

**Files:**

- Modify: `server/content_domains/short_drama_voice.py:51-318`
- Modify: `tests/test_short_drama_voice.py:220-490`

**Interfaces:**

- Consumes: `short_drama_voice.init_db(db_factory) -> None` and the six existing voice tables.
- Produces: idempotent canonical trigger migration that protects snapshot, quote, job, attempt, and version identities while permitting a valid editor billing actor.

- [ ] **Step 1: Write failing migration and reverse-update tests**

Add tests with these exact public behaviors:

```python
def test_init_replaces_all_legacy_voice_identity_triggers(self):
    self._install_legacy_voice_triggers()
    short_drama_voice.init_db(self.db)
    short_drama_voice.init_db(self.db)
    definitions = self._voice_trigger_definitions()
    self.assertIn("short_drama_voice_versions_line_job_guard", definitions)
    self.assertNotIn("project.username = NEW.username", "\n".join(definitions.values()))

def test_referenced_quote_identity_cannot_be_updated(self):
    self._insert_editor_voice_ledger()
    with closing(self.db()) as conn, self.assertRaises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE short_drama_voice_quotes SET username='alice' WHERE token='quote-editor'"
        )

def test_linked_job_identity_cannot_orphan_old_references(self):
    self._insert_editor_voice_ledger()
    with closing(self.db()) as conn, self.assertRaises(sqlite3.IntegrityError):
        conn.execute("UPDATE short_drama_voice_jobs SET job_id=202 WHERE id='voice-job-editor'")

def test_voice_snapshot_source_identity_is_immutable(self):
    self._insert_editor_voice_ledger()
    with closing(self.db()) as conn, self.assertRaises(sqlite3.IntegrityError):
        conn.execute("UPDATE short_drama_voice_lines SET character_key='other' WHERE id='line-1'")

def test_voice_version_job_must_belong_to_the_same_line(self):
    self._insert_second_voice_line_and_job()
    with closing(self.db()) as conn, self.assertRaises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO short_drama_voice_versions "
            "(id,voice_line_id,version,job_id,speech_text,voice_key,settings_json,input_hash,cost,status,created_at) "
            "VALUES ('bad-version','line-1',1,202,'text','voice','{}','hash',0,'done',1)"
        )
```

Extend `_insert_editor_voice_ledger()` so quote, job, and attempt share the editor actor and valid project/shot/line/job identities. Add inverse cases for actor, project, line, shot, quote token, old job ID, and consumed job ID.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
```

Expected: the new tests fail because reverse quote/job/snapshot updates and version-to-line mismatches are currently accepted.

- [ ] **Step 3: Add deterministic trigger replacement and invariant guards**

In `short_drama_voice.py`, extend the trigger reset block to include every canonical identity trigger:

```python
_VOICE_TRIGGER_NAMES = (
    "short_drama_voice_shots_project_guard",
    "short_drama_voice_shots_project_update_guard",
    "short_drama_voice_lines_project_guard",
    "short_drama_voice_lines_project_update_guard",
    "short_drama_voice_lines_source_text_immutable",
    "short_drama_voice_jobs_project_guard",
    "short_drama_voice_jobs_project_update_guard",
    "short_drama_voice_quotes_project_guard",
    "short_drama_voice_quotes_project_update_guard",
    "short_drama_voice_charge_attempts_project_guard",
    "short_drama_voice_charge_attempts_project_update_guard",
    "short_drama_voice_versions_line_job_guard",
    "short_drama_voice_versions_line_job_update_guard",
)


def _replace_voice_triggers(conn):
    for name in _VOICE_TRIGGER_NAMES:
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    conn.executescript(_TRIGGER_SCHEMA)
```

Call `_replace_voice_triggers(conn)` after table/index creation and before commit.

Use `BEFORE UPDATE OF` guards to reject changes when linked rows exist. The quote reverse guard must enforce:

```sql
SELECT CASE WHEN EXISTS (
  SELECT 1
  FROM short_drama_voice_charge_attempts AS attempt
  WHERE attempt.quote_token = OLD.token
    AND (
      attempt.username <> NEW.username OR
      attempt.project_id <> NEW.project_id OR
      attempt.voice_line_id <> NEW.voice_line_id OR
      (NEW.consumed_job_id IS NOT NULL AND attempt.job_id IS NOT NULL
       AND attempt.job_id <> NEW.consumed_job_id)
    )
) THEN RAISE(ABORT, 'voice quote identity is referenced') END;
```

The version INSERT/UPDATE guard must enforce:

```sql
SELECT CASE WHEN NOT EXISTS (
  SELECT 1
  FROM short_drama_voice_jobs AS job
  WHERE job.job_id = NEW.job_id
    AND job.voice_line_id = NEW.voice_line_id
) THEN RAISE(ABORT, 'voice version job does not belong to line') END;
```

Freeze voice-shot and voice-line source identity with `BEFORE UPDATE OF` triggers that abort when any protected `OLD` value differs from `NEW`.

- [ ] **Step 4: Run focused and compatibility tests**

Run:

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
python -m unittest tests.test_short_drama_voice tests.test_short_drama_projects -v
git diff --check
```

Expected: all tests pass; editor actor inserts remain valid; every mismatch and reverse update is rejected.

- [ ] **Step 5: Commit the ledger migration**

```powershell
git add server/content_domains/short_drama_voice.py tests/test_short_drama_voice.py
git commit -m "fix: close short drama voice ledger identities"
```

---

### Task 3: Produce one canonical server handoff decision

**Files:**

- Modify: `server/content_domains/short_drama_production.py:1048-1167,1235-1302`
- Modify: `tests/test_short_drama_production.py:1550-1785,3180-3230`

**Interfaces:**

- Produces: `build_phase_two_handoff(conn, project_id, ratio) -> dict`.
- Returned shape: `{"blocked": bool, "blockers": list[dict]}`.
- Adds production read fields: `handoff_blocked: bool`, `handoff_blockers: list[dict]`.
- `confirm_stage` consumes the same builder inside its transaction after reconciliation.

- [ ] **Step 1: Write failing canonical-blocker tests**

Add:

```python
def test_snapshot_reports_old_running_job_hidden_by_new_done_job(self):
    self._insert_running_job(job_id=101, shot_id=self.shot_ids[0])
    self._insert_done_job(job_id=102, shot_id=self.shot_ids[0])
    snapshot = short_drama_production.get_production(self.db, "alice", self.project_id)
    self.assertTrue(snapshot["handoff_blocked"])
    self.assertEqual("active_job", snapshot["handoff_blockers"][0]["code"])

def test_snapshot_reports_refund_and_charge_attempt_blockers(self):
    self._insert_refund_pending_link()
    self._insert_charge_attempt(state="charged")
    snapshot = short_drama_production.get_production(self.db, "alice", self.project_id)
    self.assertEqual(
        ["refund_pending", "charge_attempt_pending"],
        [item["code"] for item in snapshot["handoff_blockers"]],
    )

def test_confirm_uses_the_same_handoff_decision_as_snapshot(self):
    snapshot = short_drama_production.get_production(self.db, "alice", self.project_id)
    self.assertTrue(snapshot["handoff_blocked"])
    with self.assertRaisesRegex(ValueError, snapshot["handoff_blockers"][0]["message"]):
        short_drama_production.confirm_stage(
            self.db, "alice",
            {"project_id": self.project_id, "revision": snapshot["revision"], "stage": "stills_review"},
        )
```

Retain and extend the existing tests for late terminal success, durable refund intent, snapshot rollback, prepared-before-handoff acceptance, and concurrent confirmation.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_short_drama_production.ShortDramaProductionTests -v
```

Expected: the new snapshot assertions fail because the response does not yet contain `handoff_blocked` or all historical blockers.

- [ ] **Step 3: Replace the single-message helper with a structured decision**

Implement:

```python
_HANDOFF_ORDER = {
    "missing_locked_still": 0,
    "active_job": 1,
    "refund_pending": 2,
    "charge_attempt_pending": 3,
    "ledger_inconsistent": 4,
}


def _blocker(code, message, shot_id=None):
    item = {"code": code, "message": message}
    if shot_id:
        item["shot_id"] = shot_id
    return item


def build_phase_two_handoff(conn, project_id, ratio):
    blockers = []
    # Query every current shot/locked version, every production job, and every
    # unresolved charge attempt. Do not collapse jobs to the latest job_id.
    # Append one stable blocker per code/shot and sort deterministically.
    blockers.sort(key=lambda item: (
        _HANDOFF_ORDER[item["code"]], item.get("shot_id", ""), item["message"]
    ))
    return {"blocked": bool(blockers), "blockers": blockers}
```

Use exact Chinese messages that are safe to show to an end user:

```python
_HANDOFF_MESSAGES = {
    "missing_locked_still": "请先为每个镜头锁定一张有效关键帧",
    "active_job": "仍有关键帧生成任务处理中，请等待完成",
    "refund_pending": "仍有关键帧退款待确认，请等待账本收口",
    "charge_attempt_pending": "仍有关键帧扣点记录处理中，请稍后重试",
    "ledger_inconsistent": "关键帧账本关联异常，请刷新后重试",
}
```

`build_production_snapshot` adds:

```python
handoff = build_phase_two_handoff(conn, project_id, project["ratio"])
return {
    # existing fields
    "handoff_blocked": handoff["blocked"],
    "handoff_blockers": handoff["blockers"],
}
```

- [ ] **Step 4: Make `confirm_stage` consume the structured decision**

After `reconcile_jobs` and before snapshot/CAS:

```python
handoff = build_phase_two_handoff(conn, project_id, project["ratio"])
if handoff["blocked"]:
    blocked_message = handoff["blockers"][0]["message"]
else:
    short_drama_voice.ensure_voice_workspace(
        conn, project_id, allowed_stages={"stills_review"}
    )
    # existing CAS update
```

Keep the existing deliberate commit-before-reject path for a durable refund intent; all ordinary exceptions continue to roll back.

- [ ] **Step 5: Run handoff and Phase 2 regression tests**

Run:

```powershell
python -m unittest tests.test_short_drama_production.ShortDramaProductionTests -v
python -m unittest tests.test_short_drama_production -v
git diff --check
```

Expected: the canonical blocker tests, reconciliation/refund recovery tests, and concurrent confirmation tests all pass.

- [ ] **Step 6: Commit the server decision model**

```powershell
git add server/content_domains/short_drama_production.py tests/test_short_drama_production.py
git commit -m "fix: expose short drama handoff blockers"
```

---

### Task 4: Make the production frontend consume server blockers

**Files:**

- Modify: `site/workbench/canvas/canvas-short-drama-production.js:110-155,234-274,455-462`
- Modify: `tests/test_canvas_short_drama_production.js:1-510`
- Modify: `site/workbench/canvas.html` through the asset stamper.

**Interfaces:**

- Consumes: `handoff_blocked` and `handoff_blockers` from Task 3.
- Produces normalized `state.handoff_blocked` and `state.handoff_blockers`.

- [ ] **Step 1: Add failing normalization, rendering, and mutation tests**

Add:

```javascript
const blocked = normalizeState({
  ...fixture,
  handoff_blocked: true,
  handoff_blockers: [
    {code: 'active_job', shot_id: fixture.shots[0].id, message: '关键帧任务仍在运行中'},
  ],
});
assert.equal(blocked.handoff_blocked, true);
assert.deepEqual(blocked.handoff_blockers.map((item) => item.code), ['active_job']);
const blockedHtml = renderWorkspace(blocked);
assert.ok(blockedHtml.includes('关键帧任务仍在运行中'));
assert.ok(/data-action="confirm-stage" disabled/.test(blockedHtml));
```

Add an async workspace test that calls `confirmStage()` with `handoff_blocked=true` and asserts no `POST /confirm` request was recorded, including the old-running/new-done fixture from Task 3.

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
node tests/test_canvas_short_drama_production.js
```

Expected: assertions fail because the normalized state drops the blocker fields and confirmation still depends on `shot.still.job`.

- [ ] **Step 3: Normalize and escape blockers**

Add:

```javascript
function normalizeBlockers(items){
  return (Array.isArray(items)?items:[]).map(function(item){
    item=item&&typeof item==='object'?item:{};
    return {
      code:text(item.code),
      shot_id:item.shot_id==null?null:text(item.shot_id),
      message:text(item.message)
    };
  });
}
```

In `normalizeState`:

```javascript
handoff_blocked:!!input.handoff_blocked,
handoff_blockers:normalizeBlockers(input.handoff_blockers),
```

Render each message through `escapeHtml` in an alert/list inside the inspector.

- [ ] **Step 4: Replace frontend inference with the server decision**

Change the confirmation predicate and mutation guard:

```javascript
var confirmable=writable&&allShotsLocked(state)&&!state.handoff_blocked;
```

```javascript
function confirmStage(){
  var state;
  try{ ensureWritable();state=view(); }catch(error){ return Promise.reject(error); }
  if(state.handoff_blocked){
    return Promise.reject(new Error(
      state.handoff_blockers[0]&&state.handoff_blockers[0].message||
      'short drama handoff is blocked'
    ));
  }
  if(!allShotsLocked(state)){
    return Promise.reject(new Error('every shot requires a locked current completed matching-ratio still'));
  }
  return mutation(CONFIRM_PATH,{
    project_id:serverState.project_id,
    revision:number(serverState.revision,0),
    stage:'stills_review'
  });
}
```

- [ ] **Step 5: Run frontend tests and update stamps**

Run:

```powershell
node tests/test_canvas_short_drama_production.js
node tests/test_canvas_short_drama.js
node --check site/workbench/canvas/canvas-short-drama-production.js
python scripts/stamp_assets.py
python scripts/stamp_assets.py --check
```

Expected: all commands pass and `canvas.html` contains the new production JS hash.

- [ ] **Step 6: Commit frontend blocker handling**

```powershell
git add site/workbench/canvas/canvas-short-drama-production.js site/workbench/canvas.html tests/test_canvas_short_drama_production.js
git commit -m "fix: honor server handoff blockers in canvas"
```

---

### Task 5: Align OpenAPI blocker and authorization semantics

**Files:**

- Modify: `docs/api/openapi.json:175-285`
- Modify: `tests/test_canvas_short_drama.js:1-90`

**Interfaces:**

- Documents Task 3 production fields.
- Defines voice 403 as account-level restriction and 404 as missing-or-undiscoverable project.

- [ ] **Step 1: Add failing contract assertions**

Add:

```javascript
const productionSchema = spec.paths['/api/gen/short-drama/production'].get
  .responses['200'].content['application/json'].schema;
assert.ok(productionSchema.required.includes('handoff_blocked'));
assert.ok(productionSchema.required.includes('handoff_blockers'));
assert.equal(productionSchema.properties.handoff_blocked.type, 'boolean');
assert.deepEqual(
  productionSchema.properties.handoff_blockers.items.required,
  ['code', 'message']
);

const voiceResponses = spec.paths['/api/gen/short-drama/voice'].get.responses;
assert.match(voiceResponses['403'].description, /密码|画布基础访问/);
assert.doesNotMatch(voiceResponses['403'].description, /项目权限/);
assert.match(voiceResponses['404'].description, /不存在|无权发现/);
```

- [ ] **Step 2: Run and verify RED**

```powershell
node tests/test_canvas_short_drama.js
```

Expected: blocker-schema and 403/404 description assertions fail.

- [ ] **Step 3: Update the OpenAPI schema**

Add the required production properties:

```json
"handoff_blocked": {"type": "boolean"},
"handoff_blockers": {
  "type": "array",
  "items": {
    "type": "object",
    "required": ["code", "message"],
    "properties": {
      "code": {
        "type": "string",
        "enum": [
          "missing_locked_still",
          "active_job",
          "refund_pending",
          "charge_attempt_pending",
          "ledger_inconsistent"
        ]
      },
      "shot_id": {"type": ["string", "null"]},
      "message": {"type": "string"}
    }
  }
}
```

Set descriptions exactly:

```json
"403": {"description": "必须修改初始密码，或账号没有画布基础访问能力"},
"404": {"description": "项目不存在，或当前用户无权发现该项目"}
```

- [ ] **Step 4: Validate JSON and contracts**

Run:

```powershell
python -m json.tool docs/api/openapi.json $null
node tests/test_canvas_short_drama.js
python scripts/ci_validate.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the API contract**

```powershell
git add docs/api/openapi.json tests/test_canvas_short_drama.js
git commit -m "docs: align short drama handoff contract"
```

---

### Task 6: Add an isolated browser acceptance fixture and execute all nine checks

**Files:**

- Create: `tests/fixtures/short_drama_voice_acceptance.py`
- Create: `tests/test_short_drama_voice_acceptance.py`
- Modify only if required by fixture injection: `scripts/dev_local.sh`

**Interfaces:**

- Produces: `build_acceptance_fixture(content_db, auth_db) -> dict`.
- Returned keys: `project_id`, `board_id`, `owner`, `viewer`, `unauthorized`, `voice_line_ids`.
- Fixture databases live under a generated temporary directory and are deleted after acceptance.

- [ ] **Step 1: Write failing fixture-isolation tests**

Create tests that assert:

```python
def test_fixture_builds_six_shot_voice_review_project_with_three_roles(self):
    result = build_acceptance_fixture(self.content_db, self.auth_db)
    self.assertEqual(6, result["shot_count"])
    self.assertEqual("voice_review", result["stage"])
    self.assertNotEqual(result["owner"], result["viewer"])
    self.assertNotEqual(result["viewer"], result["unauthorized"])
    self.assertGreater(len(result["voice_line_ids"]), 0)

def test_fixture_contains_narrator_and_silent_shot(self):
    result = build_acceptance_fixture(self.content_db, self.auth_db)
    self.assertTrue(result["has_narrator"])
    self.assertTrue(result["has_silent_shot"])

def test_fixture_paths_must_be_explicit_temporary_paths(self):
    with self.assertRaises(ValueError):
        build_acceptance_fixture(Path("server/content_jobs.db"), self.auth_db)
```

- [ ] **Step 2: Run and verify RED**

```powershell
python -m unittest tests.test_short_drama_voice_acceptance -v
```

Expected: import fails because the fixture builder does not exist.

- [ ] **Step 3: Implement the synthetic fixture builder**

The builder must:

1. reject paths inside tracked `server/`, `data/`, and repository database locations;
2. initialize auth/content schemas using existing initializers;
3. insert synthetic owner/viewer/unauthorized identities using random per-run passwords;
4. insert a board with owner and viewer membership;
5. insert one six-shot project and confirmed planning records;
6. call `ensure_voice_workspace` to create stable voice-line IDs;
7. set the project to `voice_review` without creating paid jobs or generated media;
8. return IDs/roles but never print password hashes, tokens, or cookies.

Use deterministic fixture labels:

```python
SHOT_KEYS = tuple("shot-%d" % index for index in range(1, 7))
NARRATOR_KEY = "narrator"
SILENT_SHOT_KEY = "shot-6"
```

- [ ] **Step 4: Run fixture tests**

```powershell
python -m unittest tests.test_short_drama_voice_acceptance -v
```

Expected: all tests pass and temporary directories are removed during teardown.

- [ ] **Step 5: Start the isolated local services**

Create a task-local temporary directory and point the local auth/content services to the fixture databases using their supported environment/config arguments. Start hidden/background processes only; do not use production database paths.

Have the fixture command write its actual generated values to `.superpowers/sdd/pr19-browser-acceptance.md` in this exact shape:

```markdown
# PR #19 Browser Acceptance

- Project ID: value emitted by `build_acceptance_fixture`
- Board ID: value emitted by `build_acceptance_fixture`
- Owner: generated synthetic owner username
- Viewer: generated synthetic viewer username
- Unauthorized: generated synthetic unauthorized username
```

Replace each descriptive value with the command's actual runtime value before the file is used as evidence; the evidence file is ignored and never committed.

- [ ] **Step 6: Execute and record the nine Chrome checks**

Open `http://127.0.0.1:8097/workbench/canvas.html` and record PASS/FAIL for:

1. voice workspace replaces still workspace;
2. six shots appear in storyboard order;
3. dialogue, character, voice key, speed, pitch, and volume match the fixture;
4. narrator displays the narration badge;
5. silent shot displays the silent state;
6. generate/save/lock/advance controls are absent or disabled;
7. refresh preserves every recorded voice-line ID and source snapshot text;
8. viewer can read and receives no write controls;
9. unauthorized user receives the same external 404 behavior as a missing project.

If any item fails, stop Ready conversion, capture the exact failure, implement a focused regression test/fix on the same branch, rerun affected automated suites, and repeat all nine browser checks.

- [ ] **Step 7: Commit only reusable fixture code**

```powershell
git add tests/fixtures/short_drama_voice_acceptance.py tests/test_short_drama_voice_acceptance.py
git commit -m "test: add short drama voice acceptance fixture"
```

Do not add `.superpowers/sdd/pr19-browser-acceptance.md`, temporary databases, screenshots with credentials, or generated media.

---

### Task 7: Run final gates, update PR #19, convert to Ready, and watch CI

**Files:**

- Verify all PR files.
- Update remote PR #19 metadata only; do not create another PR.

**Interfaces:**

- Consumes: passing Tasks 1-6 and browser evidence.
- Produces: Ready PR #19 with current branch, complete body, and green GitHub CI.

- [ ] **Step 1: Run cache, static, compilation, and syntax gates**

```powershell
python scripts/stamp_assets.py
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
python -m compileall -q server scripts worker
node --check site/workbench/cloud-shell.js
node --check site/workbench/canvas/canvas-short-drama.js
node --check site/workbench/canvas/canvas-short-drama-production.js
node --check site/workbench/canvas/canvas-short-drama-voice.js
git diff --check
```

Expected: every command exits 0. Commit any stamper-produced tracked HTML changes explicitly with the task that changed the stamped asset.

- [ ] **Step 2: Run related and full test suites**

```powershell
python -m unittest tests.test_short_drama_voice tests.test_short_drama_voice_acceptance tests.test_short_drama_projects tests.test_short_drama_planning tests.test_short_drama_production -v
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
node tests/test_canvas_short_drama_voice.js
python -m unittest discover -s tests -v
```

Expected: every suite passes. Record exact counts and any documented Windows environment exception; CI must still pass on Linux.

- [ ] **Step 3: Perform final scope and sensitive-data checks**

```powershell
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

Inspect the file list and reject any `.env`, `.db`, `content_out/`, `browser_data/`, `data/`, token, cookie, password, user-data, or unrelated file.

Expected: clean worktree and task-only files.

- [ ] **Step 4: Push the rebased branch safely**

Because Task 1 rebased an already-published branch, run:

```powershell
git push --force-with-lease origin codex/short-drama-phase3-voice-spec
```

Expected: remote PR #19 head equals local HEAD. Never use plain `--force`.

- [ ] **Step 5: Update PR #19 body**

Replace the previous “nine checks remain” section with a table containing:

```markdown
## Browser acceptance

| # | Check | Result |
|---|---|---|
| 1 | Voice workspace selected | PASS |
| 2 | Six-shot order | PASS |
| 3 | Dialogue/voice settings | PASS |
| 4 | Narration badge | PASS |
| 5 | Silent shot | PASS |
| 6 | Read-only controls | PASS |
| 7 | Refresh stability | PASS |
| 8 | Viewer read-only access | PASS |
| 9 | Unauthorized isolation | PASS |
```

Also include actual project ID, board ID, role names, exact automated commands/counts, file scope, task lock, and explicit no-deploy/no-merge statements. Do not include passwords or tokens.

- [ ] **Step 6: Convert the existing PR to Ready**

Run:

```powershell
gh pr ready 19 --repo LU-003/huangque-test-server
gh pr view 19 --repo LU-003/huangque-test-server --json isDraft,mergeable,mergeStateStatus,url
```

Expected: `isDraft=false`; PR URL remains `https://github.com/LU-003/huangque-test-server/pull/19`.

- [ ] **Step 7: Watch GitHub CI**

Run:

```powershell
gh pr checks 19 --repo LU-003/huangque-test-server --watch
gh pr checks 19 --repo LU-003/huangque-test-server
```

Expected: all required checks pass. If a check fails, resolve the most recent failed run ID and inspect its failed logs:

```powershell
$failedRunId = gh run list --repo LU-003/huangque-test-server --branch codex/short-drama-phase3-voice-spec --status failure --limit 1 --json databaseId --jq '.[0].databaseId'
gh run view $failedRunId --repo LU-003/huangque-test-server --log-failed
```

Fix only the concrete failure, rerun its local command, commit/push to the same branch, and watch the same PR again.

- [ ] **Step 8: Final handoff**

Report:

```text
Branch:
Commits:
PR URL:
PR status:
CI status:
Changed files:
Browser acceptance project/board:
Nine-check result:
Validation result:
Deployed: no
Services restarted: no
Remaining risks:
```

Do not merge PR #19. Merge remains the user's/reviewer's explicit action.

---

## Plan Self-Review

- Spec coverage: Tasks 2-5 close every database, handoff, frontend, and OpenAPI design requirement; Task 6 supplies the missing real-browser evidence; Tasks 1 and 7 enforce the approved PR workflow.
- Type consistency: `build_phase_two_handoff` returns `blocked/blockers`; the production API exposes `handoff_blocked/handoff_blockers`; frontend normalization consumes those exact names.
- Transaction consistency: reconciliation and blocker calculation remain inside `confirm_stage`'s existing `BEGIN IMMEDIATE`; only the durable refund-intent path intentionally commits before rejection.
- Scope: no Phase 3-B write route, TTS, timeline mutation, stage advancement, merge, or deployment is included.
- Content scan: the plan contains no unresolved implementation decisions or placeholder commands; runtime-generated acceptance values are written by the fixture command before evidence review.
