# PR #19 审核闭环实施计划

> **供执行代理使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐项实施本计划。所有步骤均使用复选框（`- [ ]`）跟踪状态。

**目标：** 关闭 PR #19 的全部审核问题，记录 9 项真实浏览器验收结果，并在 CI 全绿后将现有 PR 从 Draft 转为 Ready。

**架构：** 在现有配音数据库初始化器中加入确定性的完整性触发器迁移；由服务端集中生成从关键帧阶段进入配音阶段的唯一交接判定，并让写操作与前端共同消费该判定。PR 3-A 继续保持只读，同时统一 OpenAPI 的防枚举语义；更新现有 PR 前，使用隔离的浏览器验收夹具验证最终分支。

**技术栈：** Python 3.12 标准库、SQLite、浏览器 JavaScript UMD 模块、CSS、Node.js 24.18.0 契约测试（用户已确认沿用本机版本）、Python `unittest`、Git/GitHub CLI。

## 全局约束

- 仅在 `codex/short-drama-phase3-voice-spec` 上工作；更新 PR #19，不新建其他 PR。
- 实施前先 rebase 到最新 `origin/main`，rebase 后重新执行全部质量门禁。
- PR 3-A 继续保持只读：不得提交 TTS 任务、增加扣点接口、修改字幕/时间线、锁定镜头、生成配音版本，或推进 `voice_review -> video_review`。
- 保持 Phase 2 关键帧计费、幂等、对账、恢复、退款及单一胜出确认行为不变。
- 数据库触发器负责内部身份一致性；鉴权和画布角色校验仍由服务层负责。
- 项目不存在和无权访问均返回 404；403 只用于账户级限制。
- 浏览器验收只使用本地合成数据；不得提交数据库、令牌、Cookie、密码、包含敏感信息的截图或生成媒体。
- 每个任务必须使用明确列出文件的 `git add` 命令；禁止使用 `git add -A`。
- 不合并、不部署、不重启服务，也不通过 SSH 修改任何服务器。

---

## 文件清单

### 新建

- `tests/fixtures/short_drama_voice_acceptance.py` — 确定性的六镜头合成验收夹具构建器，仅用于本地测试环境。
- `tests/test_short_drama_voice_acceptance.py` — 覆盖夹具隔离、身份、角色、旁白/静音场景及清理行为。

### 修改

- `server/content_domains/short_drama_voice.py` — 迁移并强化快照、报价、任务、扣点尝试和版本的身份一致性触发器。
- `server/content_domains/short_drama_production.py` — 实现标准交接判定及事务内复核。
- `site/workbench/canvas/canvas-short-drama-production.js` — 规范化、渲染并消费服务端交接阻塞项。
- `docs/api/openapi.json` — 增加制作交接阻塞字段并修正配音接口 403/404 描述。
- `tests/test_short_drama_voice.py` — 覆盖触发器迁移及反向更新约束。
- `tests/test_short_drama_production.py` — 覆盖标准阻塞项、对账、回滚和并发。
- `tests/test_canvas_short_drama_production.js` — 覆盖前端对服务端阻塞项的处理。
- `tests/test_canvas_short_drama.js` — OpenAPI 契约断言。
- `site/workbench/canvas.html` — 仅在前端修改导致资源戳变化时更新。

---

### 任务 1：将 PR #19 rebase 到最新 `origin/main` 并建立基线

**文件：**

- 本任务仅验证；如有 rebase 冲突，只处理 PR #19 已涉及的文件。

**接口：**

- 输入：当前分支 `codex/short-drama-phase3-voice-spec` 与远端 `origin/main`。
- 输出：已完成 rebase、工作区干净且变更前基线通过的本地分支。

- [ ] **步骤 1：确认环境、Git 身份、分支及工作区状态**

执行：

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

预期：

- Python 输出 3.12.x。
- Node 输出 24.18.0；该版本由用户明确确认沿用，不下载或切换 Node 22。
- Git 身份为 `kongli` / `kong74007@gmail.com`。
- 当前分支为 `codex/short-drama-phase3-voice-spec`，且工作区干净。

- [ ] **步骤 2：拉取并 rebase 到最新 main**

执行：

```powershell
git fetch origin --prune
git rebase origin/main
```

如发生冲突，只编辑 PR #19 范围内的冲突文件，然后精确暂存 Git 仍标记为未解决的路径并继续：

```powershell
$conflictedPaths = git diff --name-only --diff-filter=U
git add -- $conflictedPaths
git rebase --continue
```

预期：rebase 成功结束，且 `git merge-base HEAD origin/main` 与 `git rev-parse origin/main` 输出相同。

- [ ] **步骤 3：执行变更前基线门禁**

执行：

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

预期：所有命令退出码均为 0。仅由 Windows 环境导致的失败需单独记录，并在获准环境中执行完全相同的命令；不得把环境失败视为门禁通过。

- [ ] **步骤 4：记录 rebase 后的基线**

执行：

```powershell
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

预期：工作区干净，只包含 PR #19 的提交和文件；本任务不创建新提交。

---

### 任务 2：补齐配音账本反向更新与身份一致性约束

**文件：**

- 修改：`server/content_domains/short_drama_voice.py:51-318`
- 修改：`tests/test_short_drama_voice.py:220-490`

**接口：**

- 输入：`short_drama_voice.init_db(db_factory) -> None` 及现有六张配音表。
- 输出：幂等的标准触发器迁移，保护快照、报价、任务、扣点尝试及版本的身份一致性，同时允许合法编辑者作为计费主体。

- [ ] **步骤 1：编写会失败的迁移及反向更新测试**

新增测试，精确覆盖以下外部行为：

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

扩展 `_insert_editor_voice_ledger()`，使报价、任务和扣点尝试共享编辑者主体以及合法的项目/镜头/台词/任务身份。补充 actor、project、line、shot、quote token、旧 job ID 与 consumed job ID 的反向错误用例。

- [ ] **步骤 2：运行测试并确认处于 RED 状态**

执行：

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
```

预期：新测试失败，因为当前仍允许报价/任务/快照的反向更新以及版本与台词不一致。

- [ ] **步骤 3：加入确定性的触发器替换及不变量保护**

在 `short_drama_voice.py` 中扩展触发器重置逻辑，纳入全部标准身份触发器：

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

在建表/建索引之后、提交事务之前调用 `_replace_voice_triggers(conn)`。

使用 `BEFORE UPDATE OF` 保护，在存在关联记录时拒绝变更。报价反向保护必须保证：

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

版本 INSERT/UPDATE 保护必须保证：

```sql
SELECT CASE WHEN NOT EXISTS (
  SELECT 1
  FROM short_drama_voice_jobs AS job
  WHERE job.job_id = NEW.job_id
    AND job.voice_line_id = NEW.voice_line_id
) THEN RAISE(ABORT, 'voice version job does not belong to line') END;
```

使用 `BEFORE UPDATE OF` 触发器冻结配音镜头和配音台词的来源身份；任何受保护字段的 `OLD` 与 `NEW` 不同时立即中止。

- [ ] **步骤 4：运行专项测试与兼容性测试**

执行：

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
python -m unittest tests.test_short_drama_voice tests.test_short_drama_projects -v
git diff --check
```

预期：全部测试通过；编辑者主体写入仍然合法；所有身份不一致和反向更新均被拒绝。

- [ ] **步骤 5：提交账本迁移**

```powershell
git add server/content_domains/short_drama_voice.py tests/test_short_drama_voice.py
git commit -m "fix: close short drama voice ledger identities"
```

---

### 任务 3：生成唯一的服务端交接判定

**文件：**

- 修改：`server/content_domains/short_drama_production.py:1048-1167,1235-1302`
- 修改：`tests/test_short_drama_production.py:1550-1785,3180-3230`

**接口：**

- 输出：`build_phase_two_handoff(conn, project_id, ratio) -> dict`。
- 返回结构：`{"blocked": bool, "blockers": list[dict]}`。
- 制作态读取接口新增：`handoff_blocked: bool`、`handoff_blockers: list[dict]`。
- `confirm_stage` 在事务内完成对账后调用同一个构建函数。

- [ ] **步骤 1：编写会失败的标准阻塞项测试**

新增：

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

保留并扩展已有测试，覆盖延迟终态成功、持久化退款意图、快照回滚、交接前已准备完成以及并发确认。

- [ ] **步骤 2：运行测试并确认处于 RED 状态**

执行：

```powershell
python -m unittest tests.test_short_drama_production.ShortDramaProductionTests -v
```

预期：新的快照断言失败，因为响应尚未包含 `handoff_blocked` 或完整历史阻塞项。

- [ ] **步骤 3：用结构化判定替换单消息辅助函数**

实现：

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

使用以下可直接展示给终端用户的中文文案：

```python
_HANDOFF_MESSAGES = {
    "missing_locked_still": "请先为每个镜头锁定一张有效关键帧",
    "active_job": "仍有关键帧生成任务处理中，请等待完成",
    "refund_pending": "仍有关键帧退款待确认，请等待账本收口",
    "charge_attempt_pending": "仍有关键帧扣点记录处理中，请稍后重试",
    "ledger_inconsistent": "关键帧账本关联异常，请刷新后重试",
}
```

`build_production_snapshot` 增加：

```python
handoff = build_phase_two_handoff(conn, project_id, project["ratio"])
return {
    # existing fields
    "handoff_blocked": handoff["blocked"],
    "handoff_blockers": handoff["blockers"],
}
```

- [ ] **步骤 4：让 `confirm_stage` 消费结构化判定**

在 `reconcile_jobs` 之后、快照/CAS 之前：

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

保留“持久化退款意图后再拒绝”的既有特殊提交路径；其他普通异常仍必须回滚。

- [ ] **步骤 5：运行交接及 Phase 2 回归测试**

执行：

```powershell
python -m unittest tests.test_short_drama_production.ShortDramaProductionTests -v
python -m unittest tests.test_short_drama_production -v
git diff --check
```

预期：标准阻塞项、对账/退款恢复及并发确认测试全部通过。

- [ ] **步骤 6：提交服务端判定模型**

```powershell
git add server/content_domains/short_drama_production.py tests/test_short_drama_production.py
git commit -m "fix: expose short drama handoff blockers"
```

---

### 任务 4：让制作前端消费服务端阻塞项

**文件：**

- 修改：`site/workbench/canvas/canvas-short-drama-production.js:110-155,234-274,455-462`
- 修改：`tests/test_canvas_short_drama_production.js:1-510`
- 通过资源戳脚本修改：`site/workbench/canvas.html`。

**接口：**

- 输入：任务 3 提供的 `handoff_blocked` 与 `handoff_blockers`。
- 输出：规范化后的 `state.handoff_blocked` 与 `state.handoff_blockers`。

- [ ] **步骤 1：新增会失败的规范化、渲染及写操作测试**

新增：

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

新增异步工作区测试：在 `handoff_blocked=true` 时调用 `confirmStage()`，断言没有记录到 `POST /confirm` 请求；测试数据需包含任务 3 的“旧任务运行中、最新任务已完成”场景。

- [ ] **步骤 2：运行测试并确认处于 RED 状态**

执行：

```powershell
node tests/test_canvas_short_drama_production.js
```

预期：断言失败，因为当前规范化状态会丢弃阻塞字段，且确认逻辑仍依赖 `shot.still.job`。

- [ ] **步骤 3：规范化并转义阻塞项**

新增：

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

在 `normalizeState` 中：

```javascript
handoff_blocked:!!input.handoff_blocked,
handoff_blockers:normalizeBlockers(input.handoff_blockers),
```

在检查器的提示框/列表中，通过 `escapeHtml` 渲染每条消息。

- [ ] **步骤 4：用服务端判定替换前端推断**

修改确认条件及写操作保护：

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

- [ ] **步骤 5：运行前端测试并更新资源戳**

执行：

```powershell
node tests/test_canvas_short_drama_production.js
node tests/test_canvas_short_drama.js
node --check site/workbench/canvas/canvas-short-drama-production.js
python scripts/stamp_assets.py
python scripts/stamp_assets.py --check
```

预期：所有命令通过，且 `canvas.html` 包含新的制作模块 JS 哈希。

- [ ] **步骤 6：提交前端阻塞项处理**

```powershell
git add site/workbench/canvas/canvas-short-drama-production.js site/workbench/canvas.html tests/test_canvas_short_drama_production.js
git commit -m "fix: honor server handoff blockers in canvas"
```

---

### 任务 5：统一 OpenAPI 阻塞字段与鉴权语义

**文件：**

- 修改：`docs/api/openapi.json:175-285`
- 修改：`tests/test_canvas_short_drama.js:1-90`

**接口：**

- 文档化任务 3 新增的制作态字段。
- 将配音接口 403 定义为账户级限制，将 404 定义为项目不存在或对调用者不可发现。

- [ ] **步骤 1：新增会失败的契约断言**

新增：

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

- [ ] **步骤 2：运行测试并确认处于 RED 状态**

```powershell
node tests/test_canvas_short_drama.js
```

预期：阻塞字段 schema 以及 403/404 描述断言失败。

- [ ] **步骤 3：更新 OpenAPI schema**

增加以下必需的制作态属性：

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

将描述精确设置为：

```json
"403": {"description": "必须修改初始密码，或账号没有画布基础访问能力"},
"404": {"description": "项目不存在，或当前用户无权发现该项目"}
```

- [ ] **步骤 4：验证 JSON 与契约**

执行：

```powershell
python -m json.tool docs/api/openapi.json $null
node tests/test_canvas_short_drama.js
python scripts/ci_validate.py
git diff --check
```

预期：所有命令退出码均为 0。

- [ ] **步骤 5：提交 API 契约**

```powershell
git add docs/api/openapi.json tests/test_canvas_short_drama.js
git commit -m "docs: align short drama handoff contract"
```

---

### 任务 6：新增隔离的浏览器验收夹具并执行 9 项检查

**文件：**

- 新建：`tests/fixtures/short_drama_voice_acceptance.py`
- 新建：`tests/test_short_drama_voice_acceptance.py`
- 仅在注入夹具确有需要时修改：`scripts/dev_local.sh`

**接口：**

- 输出：`build_acceptance_fixture(content_db, auth_db) -> dict`。
- 返回键：`project_id`、`board_id`、`owner`、`viewer`、`unauthorized`、`voice_line_ids`。
- 夹具数据库存放在动态创建的临时目录中，验收结束后删除。

- [ ] **步骤 1：编写会失败的夹具隔离测试**

创建测试并断言：

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

- [ ] **步骤 2：运行测试并确认处于 RED 状态**

```powershell
python -m unittest tests.test_short_drama_voice_acceptance -v
```

预期：导入失败，因为夹具构建器尚不存在。

- [ ] **步骤 3：实现合成验收夹具构建器**

构建器必须：

1. 拒绝使用受版本控制的 `server/`、`data/` 目录及仓库数据库路径；
2. 使用现有初始化器创建鉴权与内容数据库 schema；
3. 插入合成的 owner/viewer/unauthorized 身份，每次运行使用随机密码；
4. 插入一个包含 owner 和 viewer 成员关系的画布；
5. 插入一个六镜头项目及已确认的规划记录；
6. 调用 `ensure_voice_workspace` 创建稳定的配音台词 ID；
7. 将项目设为 `voice_review`，但不创建付费任务或生成媒体；
8. 返回 ID/角色，但绝不输出密码哈希、令牌或 Cookie。

使用确定性的夹具标签：

```python
SHOT_KEYS = tuple("shot-%d" % index for index in range(1, 7))
NARRATOR_KEY = "narrator"
SILENT_SHOT_KEY = "shot-6"
```

- [ ] **步骤 4：运行夹具测试**

```powershell
python -m unittest tests.test_short_drama_voice_acceptance -v
```

预期：所有测试通过，临时目录在 teardown 阶段被删除。

- [ ] **步骤 5：启动隔离的本地服务**

创建任务专用临时目录，并通过本地鉴权/内容服务支持的环境变量或配置参数指向夹具数据库。服务只能以隐藏后台进程启动，不得使用生产数据库路径。

让夹具命令把实际生成值按以下固定格式写入 `.superpowers/sdd/pr19-browser-acceptance.md`：

```markdown
# PR #19 浏览器验收

- 项目 ID：`build_acceptance_fixture` 实际输出值
- 画布 ID：`build_acceptance_fixture` 实际输出值
- Owner：生成的合成 owner 用户名
- Viewer：生成的合成 viewer 用户名
- Unauthorized：生成的合成 unauthorized 用户名
```

将上述说明值替换为命令运行时的实际值后，方可将文件作为验收证据；该证据文件必须被忽略且永不提交。

- [ ] **步骤 6：执行并记录 9 项 Chrome 检查**

打开 `http://127.0.0.1:8097/workbench/canvas.html`，逐项记录 PASS/FAIL：

1. 配音工作区已替换关键帧工作区；
2. 六个镜头按分镜顺序显示；
3. 台词、角色、音色 key、语速、音调和音量与夹具一致；
4. 旁白镜头显示旁白标识；
5. 静音镜头显示静音状态；
6. 生成、保存、锁定和推进控件不存在或已禁用；
7. 刷新后每条配音台词 ID 及来源快照文本保持不变；
8. viewer 可读取，但看不到任何写操作控件；
9. unauthorized 用户与项目不存在时得到相同的外部 404 行为。

如任一项失败，立即停止转为 Ready；记录精确失败现象，在同一分支增加针对性回归测试并修复，重新运行受影响的自动化测试，然后重新执行全部 9 项浏览器检查。

- [ ] **步骤 7：只提交可复用的夹具代码**

```powershell
git add tests/fixtures/short_drama_voice_acceptance.py tests/test_short_drama_voice_acceptance.py
git commit -m "test: add short drama voice acceptance fixture"
```

不得暂存 `.superpowers/sdd/pr19-browser-acceptance.md`、临时数据库、包含凭据的截图或生成媒体。

---

### 任务 7：执行最终门禁、更新 PR #19、转为 Ready 并监控 CI

**文件：**

- 验证 PR 涉及的全部文件。
- 只更新远端 PR #19 的元数据，不新建其他 PR。

**接口：**

- 输入：已通过的任务 1-6 及浏览器验收证据。
- 输出：使用当前分支、描述完整、GitHub CI 全绿且状态为 Ready 的 PR #19。

- [ ] **步骤 1：执行缓存、静态检查、编译及语法门禁**

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

预期：所有命令退出码均为 0。资源戳脚本产生的受控 HTML 变更，必须与引发资源戳变化的任务一起明确提交。

- [ ] **步骤 2：运行相关测试及完整测试集**

```powershell
python -m unittest tests.test_short_drama_voice tests.test_short_drama_voice_acceptance tests.test_short_drama_projects tests.test_short_drama_planning tests.test_short_drama_production -v
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
node tests/test_canvas_short_drama_production.js
node tests/test_canvas_short_drama_voice.js
python -m unittest discover -s tests -v
```

预期：所有测试集通过。记录准确的测试数量及已说明的 Windows 环境例外；Linux CI 仍必须通过。

- [ ] **步骤 3：执行最终范围与敏感数据检查**

```powershell
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

检查文件清单，拒绝任何 `.env`、`.db`、`content_out/`、`browser_data/`、`data/`、令牌、Cookie、密码、用户数据或无关文件。

预期：工作区干净，变更文件仅属于本任务范围。

- [ ] **步骤 4：安全推送 rebase 后的分支**

由于任务 1 对已发布分支执行了 rebase，运行：

```powershell
git push --force-with-lease origin codex/short-drama-phase3-voice-spec
```

预期：远端 PR #19 的 head 与本地 HEAD 一致。严禁使用普通 `--force`。

- [ ] **步骤 5：更新 PR #19 描述**

将原“9 项检查待执行”段落替换为包含以下内容的表格：

```markdown
## 浏览器验收

| # | 检查项 | 结果 |
|---|---|---|
| 1 | 已进入配音工作区 | PASS |
| 2 | 六镜头顺序正确 | PASS |
| 3 | 台词与音色参数正确 | PASS |
| 4 | 旁白标识正确 | PASS |
| 5 | 静音镜头状态正确 | PASS |
| 6 | 写操作控件只读或隐藏 | PASS |
| 7 | 刷新后数据稳定 | PASS |
| 8 | Viewer 只读访问正确 | PASS |
| 9 | 未授权访问隔离正确 | PASS |
```

同时写明实际项目 ID、画布 ID、角色名称、准确的自动化命令/数量、文件范围、任务锁定，以及明确的“不部署/不合并”声明。不得包含密码或令牌。

- [ ] **步骤 6：将现有 PR 转为 Ready**

执行：

```powershell
gh pr ready 19 --repo LU-003/huangque-test-server
gh pr view 19 --repo LU-003/huangque-test-server --json isDraft,mergeable,mergeStateStatus,url
```

预期：`isDraft=false`；PR 地址仍为 `https://github.com/LU-003/huangque-test-server/pull/19`。

- [ ] **步骤 7：监控 GitHub CI**

执行：

```powershell
gh pr checks 19 --repo LU-003/huangque-test-server --watch
gh pr checks 19 --repo LU-003/huangque-test-server
```

预期：所有必需检查通过。如有检查失败，获取最近一次失败运行的 ID 并查看失败日志：

```powershell
$failedRunId = gh run list --repo LU-003/huangque-test-server --branch codex/short-drama-phase3-voice-spec --status failure --limit 1 --json databaseId --jq '.[0].databaseId'
gh run view $failedRunId --repo LU-003/huangque-test-server --log-failed
```

只修复已确认的具体失败；重新执行对应本地命令，提交并推送到同一分支，然后继续监控同一个 PR。

- [ ] **步骤 8：最终交付说明**

报告以下信息：

```text
分支：
提交：
PR 地址：
PR 状态：
CI 状态：
变更文件：
浏览器验收项目/画布：
9 项检查结果：
验证结果：
已部署：否
已重启服务：否
剩余风险：
```

不得合并 PR #19；合并仍需由用户或审核者明确执行。

---

## 计划自检

- 规格覆盖：任务 2-5 覆盖数据库、交接、前端及 OpenAPI 的全部设计要求；任务 6 补充缺失的真实浏览器证据；任务 1 和任务 7 落实已确认的 PR 流程。
- 类型一致性：`build_phase_two_handoff` 返回 `blocked/blockers`；制作接口暴露 `handoff_blocked/handoff_blockers`；前端规范化逻辑消费完全相同的字段名。
- 事务一致性：对账及阻塞项计算仍位于 `confirm_stage` 现有的 `BEGIN IMMEDIATE` 事务中；只有需要持久化退款意图的路径会刻意先提交再拒绝。
- 范围：不包含 Phase 3-B 写接口、TTS、时间线修改、阶段推进、合并或部署。
- 内容扫描：计划中没有未决实现决策或占位命令；运行时生成的验收值会由夹具命令写入，之后再审核证据。
