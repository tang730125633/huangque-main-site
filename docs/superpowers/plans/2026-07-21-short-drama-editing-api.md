# Short Drama Editing API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing authenticated short-drama project PUT route so the current review stage can persist characters, append a script version, or replace storyboard shots without losing revision, ownership, or reference integrity.

**Architecture:** Keep the public route unchanged and make `short_drama.update_project` dispatch exactly one content section to focused transactional service functions. Each function locks with `BEGIN IMMEDIATE`, validates owner/revision/stage before mutation, composes a complete current plan for reference checks, and increments the project revision once.

**Tech Stack:** Python 3 standard library, SQLite, existing `content_domains.short_drama` service and HTTP dispatcher, Python `unittest`.

## Global Constraints

- Continue using `PUT /api/gen/short-drama/project?id=<project_id>` and the existing authenticated HTTP dispatcher.
- Every request includes the current integer `revision` and contains either project-setting fields or exactly one of `characters`, `script`, and `shots`.
- `characters` is editable only in `characters_review`, `script` only in `script_review`, and `shots` only in `storyboard_review`.
- Successful content updates increment revision exactly once and never change the project stage or points.
- Character removal prunes only invalid `character_keys` from unconfirmed shots.
- Script saving appends a version, retargets current shots to it, and prunes only invalid `dialogue_line_ids`.
- Storyboard replacement requires 6–10 shots, each 5 or 10 seconds, with total duration equal to the project target and all references valid.
- All writes are owner-scoped, use optimistic revision checks, and are atomic under `BEGIN IMMEDIATE`.
- Invalid stage, mixed content sections, mixed project/content fields, malformed data, or invalid references return 400 without side effects.
- Content editing is free: it creates no job and changes neither `spent_points` nor account points.

---

### Task 1: Persist stage-gated character, script, and storyboard edits

**Files:**
- Modify: `server/content_domains/short_drama.py`
- Modify: `tests/test_short_drama_projects.py`

**Interfaces:**
- Consumes: `update_project(db_factory, username, project_id, revision, patch)` from the existing PUT dispatcher.
- Produces: `update_characters(db_factory, username, project_id, revision, characters)`, `update_script(db_factory, username, project_id, revision, script)`, and `update_shots(db_factory, username, project_id, revision, shots)`, each returning the complete project dictionary.
- Public request bodies contain exactly `{revision: integer, characters: array}`, `{revision: integer, script: object}`, or `{revision: integer, shots: array}`.

- [ ] **Step 1: Write failing service tests for all three content sections**

Extend `tests/test_short_drama_projects.py` with a helper that creates a 30-second project, applies a valid six-shot plan, and returns the `characters_review` project. Add these concrete assertions:

```python
def test_content_sections_save_only_in_their_review_stage(self):
    project = self.applied_project()
    edited = [dict(project["characters"][0], name="林默（新）")]
    project = short_drama.update_project(
        self.db, "alice", project["id"], project["revision"],
        {"characters": edited},
    )
    self.assertEqual(project["revision"], 3)
    self.assertEqual(project["stage"], "characters_review")
    self.assertEqual(project["characters"][0]["name"], "林默（新）")

    with self.assertRaisesRegex(ValueError, "当前阶段"):
        short_drama.update_project(
            self.db, "alice", project["id"], project["revision"],
            {"script": dict(project["script_versions"][-1])},
        )

    project = short_drama.confirm_stage(
        self.db, "alice", project["id"], project["revision"], "characters_review"
    )
    script = dict(project["script_versions"][-1], ending="新的结尾")
    project = short_drama.update_project(
        self.db, "alice", project["id"], project["revision"], {"script": script}
    )
    self.assertEqual(len(project["script_versions"]), 2)
    self.assertEqual(project["script_versions"][-1]["ending"], "新的结尾")
    self.assertTrue(all(
        shot["script_version"] == project["script_versions"][-1]["version"]
        for shot in project["shots"]
    ))

    project = short_drama.confirm_stage(
        self.db, "alice", project["id"], project["revision"], "script_review"
    )
    shots = [dict(shot, scene_description="修改后的场景") for shot in project["shots"]]
    project = short_drama.update_project(
        self.db, "alice", project["id"], project["revision"], {"shots": shots}
    )
    self.assertEqual(len(project["shots"]), 6)
    self.assertTrue(all(x["scene_description"] == "修改后的场景" for x in project["shots"]))
```

Add separate tests proving:

```python
def test_character_and_script_edits_prune_only_invalid_unconfirmed_references(self):
    project = self.applied_project_with_two_characters_and_dialogue()
    kept_key = project["characters"][0]["character_key"]
    project = short_drama.update_project(
        self.db, "alice", project["id"], project["revision"],
        {"characters": [project["characters"][0]]},
    )
    self.assertTrue(all(
        set(shot["character_keys"]) <= {kept_key} for shot in project["shots"]
    ))

    project = short_drama.confirm_stage(
        self.db, "alice", project["id"], project["revision"], "characters_review"
    )
    script = dict(project["script_versions"][-1])
    script["dialogue_lines"] = script["dialogue_lines"][:1]
    valid_dialogue = {line["id"] for line in script["dialogue_lines"]}
    project = short_drama.update_script(
        self.db, "alice", project["id"], project["revision"], script
    )
    self.assertTrue(all(
        set(shot["dialogue_line_ids"]) <= valid_dialogue for shot in project["shots"]
    ))
```

Add table-driven rejection tests for cross-owner calls, stale revisions, the wrong stage, two content keys in one request, content mixed with `title`, malformed section types, duplicate keys, and invalid shot count/duration/total/reference. Snapshot the project before each rejected call and assert revision, stage, characters, script versions, shots, and spent points remain unchanged.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& 'C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_short_drama_projects -v
```

Expected: failures showing `characters`, `script`, and `shots` are rejected as unsupported project fields and the three service functions do not exist.

- [ ] **Step 3: Implement exact section dispatch and transactional services**

In `update_project`, classify the patch before calling `validate_project_payload`:

```python
CONTENT_KEYS = {"characters", "script", "shots"}

content_keys = set(original_patch) & CONTENT_KEYS
if content_keys:
    if len(content_keys) != 1 or len(original_patch) != 1:
        raise ValueError("每次只能更新一个短剧内容分区")
    key = next(iter(content_keys))
    if key == "characters":
        return update_characters(db_factory, username, project_id, revision, original_patch[key])
    if key == "script":
        return update_script(db_factory, username, project_id, revision, original_patch[key])
    return update_shots(db_factory, username, project_id, revision, original_patch[key])
```

Implement a shared locked loader:

```python
def _begin_content_update(conn, username, project_id, revision, required_stage):
    if type(revision) is not int:
        raise ValueError("revision 必须是整数")
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM short_drama_projects "
        "WHERE id=? AND username=? AND deleted=0",
        (project_id, username),
    ).fetchone()
    if not row:
        raise LookupError("短剧项目不存在")
    if row["revision"] != revision:
        raise RevisionConflict("项目已在其他页面更新，请刷新后重试")
    if row["stage"] != required_stage:
        raise ValueError("当前阶段不能修改该内容")
    return row
```

Each public section service must:

1. Open one connection and call `_begin_content_update` with its exact stage.
2. Read the current complete project through `_project_detail` on the same connection.
3. Normalize the incoming section without coercing malformed scalar/container types.
4. Prune only the allowed unconfirmed references for character/script edits.
5. Validate the resulting complete characters/latest-script/shots bundle before any DELETE or INSERT.
6. Replace only the affected rows, append rather than overwrite scripts, update shot `script_version` after a script save, and execute one revision-checked project UPDATE.
7. Commit and return `_project_detail`; rollback on every exception.

Use this compare-and-swap for the single project revision increment:

```python
cur = conn.execute(
    "UPDATE short_drama_projects SET revision=revision+1, updated_at=? "
    "WHERE id=? AND username=? AND revision=? AND stage=? AND deleted=0",
    (int(time.time()), project_id, username, revision, required_stage),
)
if cur.rowcount != 1:
    _raise_cas_error(conn, username, project_id)
```

Do not call point functions, create a job, advance stage, or delete historical script rows.

- [ ] **Step 4: Add executable HTTP contract tests**

For each content section, use the existing live `ThreadingHTTPServer` test harness to PUT the section at the correct stage and assert 200 plus the new revision. Add requests proving anonymous malformed JSON returns 401 before parsing, wrong-stage content returns 400, a stale revision returns `409` with `code="revision_conflict"`, and mixed content keys return 400.

Query the `jobs` table and the project `spent_points` before and after the three successful edits; assert neither count/cost nor spent points changes.

- [ ] **Step 5: Run focused and backend regression suites**

Run:

```powershell
& 'C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_short_drama_projects tests.test_short_drama_planning tests.test_content_domains tests.test_auth_points -v
& 'C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile server/content_domains/short_drama.py tests/test_short_drama_projects.py
git diff --check
```

Expected: all tests pass, compilation succeeds, and diff check emits no errors.

- [ ] **Step 6: Commit the editing API slice**

```powershell
git add server/content_domains/short_drama.py tests/test_short_drama_projects.py
git commit -m "feat: persist short drama review edits"
```

## Completion Gate

This supplement is complete only when character, script, and shot edits survive a fresh GET; every rejection is side-effect free; script history is preserved; invalid unconfirmed references are pruned exactly as specified; and the full backend regression command passes.
