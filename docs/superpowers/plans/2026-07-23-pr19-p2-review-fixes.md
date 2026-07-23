# PR #19 P2 审核问题修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 PR #19 的 attempt/job/quote 绑定漏洞及 OpenAPI 3.0.3 联合类型问题。

**Architecture:** 继续使用 SQLite 触发器维护配音账本不变量，在 attempt 插入与更新入口统一校验 quote 消费任务；更新入口额外实施一次性任务绑定。OpenAPI 保持 3.0.3，并用可省略的字符串属性表达 blocker 的可选 `shot_id`。

**Tech Stack:** Python 3.12、SQLite、unittest、Node.js 24、OpenAPI 3.0.3。

## 全局约束

- 只修改审核指出的账本触发器、测试和 OpenAPI 契约。
- 不新增 Phase 3-B 写接口、TTS、扣点、时间线编辑或阶段推进。
- 更新现有 PR #19；不得新建 PR、合并、部署或重启服务。
- 使用用户现有 Node.js 24.18.0，不下载运行时。

---

### 任务 1：锁定扣费尝试的任务身份

**文件：**

- 修改：`tests/test_short_drama_voice.py`
- 修改：`server/content_domains/short_drama_voice.py`

**接口：**

- 消费：`short_drama_voice_charge_attempts.job_id`、`short_drama_voice_quotes.consumed_job_id`。
- 产出：首次 `NULL -> job_id` 合法，非空绑定不可替换，且 attempt 与已消费 quote 指向同一任务。

- [ ] **步骤 1：编写失败测试**

新增两个 schema 测试：

```python
def test_charge_attempt_job_can_bind_once_but_cannot_rebind(self):
    # 同一台词创建 job 101 与 202；attempt 先绑定 101，quote 再消费 101。
    # 重复写 101 成功，改绑 202 抛出 sqlite3.IntegrityError。

def test_charge_attempt_job_must_match_consumed_quote(self):
    # quote 已消费 101 后，插入 job 202 的 attempt 失败；
    # 无任务 attempt 首次绑定 202 失败，绑定 101 成功。
```

- [ ] **步骤 2：运行测试并确认 RED**

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
```

预期：两个新增测试因当前触发器允许同台词第二任务而失败。

- [ ] **步骤 3：最小修改插入与更新触发器**

在 quote join 中加入：

```sql
AND (NEW.job_id IS NULL OR quote.consumed_job_id IS NULL
  OR quote.consumed_job_id IS NEW.job_id)
```

在更新触发器的有效性查询中再加入：

```sql
AND (OLD.job_id IS NULL OR NEW.job_id IS OLD.job_id)
```

- [ ] **步骤 4：验证 GREEN**

```powershell
python -m unittest tests.test_short_drama_voice.ShortDramaVoiceSchemaTests -v
python -m unittest tests.test_short_drama_voice -v
git diff --check
```

- [ ] **步骤 5：提交账本修复**

```powershell
git add server/content_domains/short_drama_voice.py tests/test_short_drama_voice.py
git commit -m "fix: lock voice charge attempt job identity"
```

---

### 任务 2：修复 OpenAPI 3.0.3 可选 shot_id

**文件：**

- 修改：`tests/test_canvas_short_drama.js`
- 修改：`docs/api/openapi.json`

**接口：**

- 消费：`handoff_blockers[].shot_id` 可省略的服务端响应。
- 产出：合法的 OpenAPI 3.0.3 `type: string` schema。

- [ ] **步骤 1：编写失败契约测试**

```javascript
assert.equal(spec.openapi, '3.0.3');
const blockerShotId = productionSchema.properties.handoff_blockers
  .items.properties.shot_id;
assert.equal(blockerShotId.type, 'string');
assert.equal(Object.hasOwn(blockerShotId, 'nullable'), false);
assert.equal(productionSchema.properties.handoff_blockers.items.required.includes('shot_id'), false);
```

- [ ] **步骤 2：运行测试并确认 RED**

```powershell
node tests/test_canvas_short_drama.js
```

预期：`blockerShotId.type` 实际为数组，断言失败。

- [ ] **步骤 3：最小修复 schema**

```json
"shot_id": {"type": "string"}
```

- [ ] **步骤 4：验证 GREEN 并提交**

```powershell
node tests/test_canvas_short_drama.js
python scripts/ci_validate.py
git diff --check
git add docs/api/openapi.json tests/test_canvas_short_drama.js
git commit -m "docs: fix openapi blocker shot schema"
```

---

### 任务 3：最终验证与更新 PR #19

**文件：** 验证全部本次变更；只更新 PR #19 元数据。

- [ ] **步骤 1：运行完整门禁**

```powershell
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
python -m compileall -q server scripts worker
node tests/test_canvas_short_drama.js
python -m unittest discover -s tests -q
git diff --check origin/main...HEAD
git status --short
```

- [ ] **步骤 2：安全推送并更新 PR**

使用绑定已读取远端 SHA 的 `--force-with-lease` 推送同一分支，更新 PR #19 描述中的审核问题与测试数量，将 PR 保持为 Ready。

- [ ] **步骤 3：监控 CI**

```powershell
gh pr checks 19 --repo LU-003/huangque-test-server --watch
```

预期：PR 为 Ready、MERGEABLE/CLEAN，全部必需检查通过。
