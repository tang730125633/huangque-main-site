# Short Drama Production Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从主站最新 `main` 合入单一的短剧策划功能，补齐项目生命周期、组合校验和扣点一致性，并提交不含测试服务器配置的干净 Draft PR。

**Architecture:** 以 `server/content_domains/short_drama.py` 作为短剧领域边界，共享 `core.py` 只负责鉴权、任务提交和路由分发。项目详情和预算统一从 `jobs` 付费任务计算净扣点；画布通过独立的短剧 JS/CSS 模块调用分页、创建、保存、删除和策划接口。

**Tech Stack:** Python 3 标准库（HTTPServer、SQLite、unittest）、原生 JavaScript/Node test runner、HTML/CSS、GitHub Actions。

## Global Constraints

- 分支必须基于 `tang730125633/huangque-main-site@f50b3aa512d9c6de4d357e399202ece74f13870c` 或执行时更新后的最新 `origin/main`。
- 每用户最多 50 个未删除项目；环境变量 `HQ_SHORT_DRAMA_MAX_PROJECTS_PER_USER` 非正整数时回退到 50。
- 分页默认 `page=1&page_size=20`，`page_size` 最大 50。
- 合法组合满足 `5 * shot_count <= target_duration <= 10 * shot_count`。
- `jobs` 中 `cost > 0` 且 `refunded != 1` 的绑定短剧策划任务计入累计已用点数。
- 不修改或新增 `deploy/test-server/`、Nginx/systemd、本地代理、测试服务器路径、账号、数据库和真实凭证。
- 所有生产代码行为先写失败测试并观察预期失败，再做最小实现。

---

### Task 1: 导入短剧领域后端和既有契约测试

**Files:**
- Create: `server/content_domains/short_drama.py`
- Modify: `server/content_domains/core.py`
- Modify: `server/content_domains/text.py`
- Create: `tests/test_short_drama_planning.py`
- Create: `tests/test_short_drama_projects.py`
- Modify: `tests/test_content_domains.py`
- Modify: `tests/test_sora_video.py`

**Interfaces:**
- Consumes: `core.jdb`、主站 `verify()`、`points.cost_of()`、付费任务 `jobs` 表和电影感视频引用查询。
- Produces: `short_drama.init_db()`、`dispatch_http()`、策划规范化、项目 CRUD 基础能力和短剧策划任务模式。

- [x] **Step 1: 只导入既有短剧测试并保持生产代码未改**

从 `codex/sync-test-server-main-20260722` 提取两个短剧测试文件，以及共享测试中仅含 `short_drama` 契约的测试方法；不得整体覆盖主站共享测试文件。

- [x] **Step 2: 运行测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_short_drama_planning tests.test_short_drama_projects -v
```

Expected: 因 `server.content_domains.short_drama` 尚不存在而失败；失败原因必须是缺少短剧领域模块，不是语法或测试夹具错误。

- [x] **Step 3: 导入最小后端实现**

从旧分支提取 `server/content_domains/short_drama.py`，并对共享文件只应用以下短剧相关逻辑：

```python
# core.py
def _short_drama_domain():
    from . import short_drama
    return short_drama

# 数据库初始化
_short_drama_domain().init_db(jdb)

# POST/GET/PUT 入口先交给短剧 dispatch_http；copy/short_drama
# 提交继续走 create_paid_job、Idempotency-Key 和队列退款路径。
```

`text.py` 只加入 `format == "short_drama"` 的策划生成分支；`video.py` 只加入短剧分镜选择电影感引用所需的拥有者查询，不携带其他渠道或部署变化。

- [x] **Step 4: 运行后端重点测试并确认 GREEN**

Run:

```powershell
python -m unittest tests.test_short_drama_planning tests.test_short_drama_projects -v
```

Expected: 全部通过，0 failures，0 errors。

- [x] **Step 5: 提交基础后端**

```powershell
git add server/content_domains/short_drama.py server/content_domains/core.py server/content_domains/text.py tests/test_short_drama_planning.py tests/test_short_drama_projects.py
git commit -m "feat: add short drama planning backend"
```

### Task 2: 增加项目上限、分页和软删除

**Files:**
- Modify: `server/content_domains/short_drama.py`
- Modify: `tests/test_short_drama_projects.py`

**Interfaces:**
- Consumes: `short_drama_projects.deleted`、请求 URL 查询参数和项目 `revision`。
- Produces: `ProjectLimitExceeded`、`list_projects(db_factory, username, page, page_size)`、`delete_project(...)` 和分页 HTTP 响应。

- [x] **Step 1: 写项目生命周期失败测试**

加入以下独立测试：

```python
def test_create_project_rejects_fifty_first_active_project(self):
    for index in range(50):
        short_drama.create_project(self.db, "alice", valid_project(title="短剧%02d" % index))
    with self.assertRaises(short_drama.ProjectLimitExceeded):
        short_drama.create_project(self.db, "alice", valid_project(title="第51个"))

def test_list_projects_returns_stable_page_metadata(self):
    for index in range(23):
        short_drama.create_project(self.db, "alice", valid_project(title="短剧%02d" % index))
    result = short_drama.list_projects(self.db, "alice", page=2, page_size=20)
    self.assertEqual((result["page"], result["page_size"], result["total"]), (2, 20, 23))
    self.assertEqual(len(result["items"]), 3)

def test_soft_delete_hides_project_and_releases_capacity(self):
    project = short_drama.create_project(self.db, "alice", valid_project())
    short_drama.delete_project(self.db, "alice", project["id"], project["revision"])
    with self.assertRaises(LookupError):
        short_drama.get_project(self.db, "alice", project["id"])
```

另加 HTTP 测试断言非法分页返回 400、上限返回 429/`short_drama_project_cap`、跨用户删除不泄露项目存在性、版本冲突返回 409。

- [x] **Step 2: 运行新测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_short_drama_projects.ShortDramaProjectTests.test_create_project_rejects_fifty_first_active_project tests.test_short_drama_projects.ShortDramaProjectTests.test_list_projects_returns_stable_page_metadata tests.test_short_drama_projects.ShortDramaProjectTests.test_soft_delete_hides_project_and_releases_capacity -v
```

Expected: 因缺少上限、分页返回结构和 `delete_project` 失败。

- [x] **Step 3: 实现事务上限、分页和软删除**

在 `short_drama.py` 中加入：

```python
DEFAULT_MAX_PROJECTS_PER_USER = 50
DEFAULT_PROJECT_PAGE_SIZE = 20
MAX_PROJECT_PAGE_SIZE = 50

class ProjectLimitExceeded(ValueError):
    def __init__(self, max_projects):
        super().__init__("短剧项目数量已达上限")
        self.max_projects = max_projects

def _validate_page(value, default, maximum=None):
    if value is None:
        return default
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError("分页参数无效")
    return value
```

`create_project()` 使用 `BEGIN IMMEDIATE`，在同一事务中统计并插入。`list_projects()` 先 `COUNT(*)`，再用 `LIMIT/OFFSET` 读取当前页。`delete_project()` 执行用户、版本和 `deleted=0` 约束下的软删除并递增 revision。`dispatch_http()` 解析查询参数，并增加 `POST .../project/delete` 路由。

- [x] **Step 4: 运行项目生命周期测试并确认 GREEN**

Run:

```powershell
python -m unittest tests.test_short_drama_projects -v
```

Expected: 全部通过，0 failures，0 errors。

- [x] **Step 5: 提交项目生命周期**

```powershell
git add server/content_domains/short_drama.py tests/test_short_drama_projects.py
git commit -m "feat: bound short drama project lifecycle"
```

### Task 3: 统一时长和镜头组合校验

**Files:**
- Modify: `server/content_domains/short_drama.py`
- Modify: `tests/test_short_drama_projects.py`
- Modify: `tests/test_short_drama_planning.py`

**Interfaces:**
- Consumes: 项目当前 `target_duration`/`shot_count` 和请求补丁。
- Produces: `_validate_planning_limits(target_duration, shot_count)` 在创建、部分更新、策划和应用阶段使用。

- [x] **Step 1: 写创建和部分更新失败测试**

```python
def test_create_project_rejects_impossible_duration_shot_pair(self):
    with self.assertRaisesRegex(ValueError, "时长与分镜数量不匹配"):
        short_drama.create_project(self.db, "alice", valid_project(target_duration=30, shot_count=10))

def test_partial_settings_update_validates_merged_pair(self):
    project = short_drama.create_project(self.db, "alice", valid_project(target_duration=45, shot_count=9))
    with self.assertRaisesRegex(ValueError, "时长与分镜数量不匹配"):
        short_drama.update_project(self.db, "alice", project["id"], project["revision"], {"target_duration": 30})
```

- [x] **Step 2: 运行新测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_short_drama_projects.ShortDramaProjectTests.test_create_project_rejects_impossible_duration_shot_pair tests.test_short_drama_projects.ShortDramaProjectTests.test_partial_settings_update_validates_merged_pair -v
```

Expected: 两个无效组合被现有代码接受，测试失败。

- [x] **Step 3: 复用单一组合校验函数**

`validate_project_payload()` 在完整创建时调用 `_validate_planning_limits()`。`update_project()` 查询当前时长和镜头数，把补丁覆盖到当前值后调用同一函数，再执行 UPDATE。策划提交、结果规范化和分镜保存继续复用相同约束。

- [x] **Step 4: 运行策划与项目测试并确认 GREEN**

Run:

```powershell
python -m unittest tests.test_short_drama_planning tests.test_short_drama_projects -v
```

Expected: 全部通过，0 failures，0 errors。

- [x] **Step 5: 提交组合校验**

```powershell
git add server/content_domains/short_drama.py tests/test_short_drama_projects.py tests/test_short_drama_planning.py
git commit -m "fix: validate short drama duration and shot pairs"
```

### Task 4: 统一项目扣点展示与预算口径

**Files:**
- Modify: `server/content_domains/short_drama.py`
- Modify: `tests/test_short_drama_projects.py`

**Interfaces:**
- Consumes: `jobs(id, kind, username, cost, status, payload, refunded)`。
- Produces: `_charged_planning_points(conn, username, project_id)`，供项目详情和预算检查共用。

- [x] **Step 1: 写扣点一致性失败测试**

```python
def test_paid_planning_job_counts_before_apply(self):
    project = short_drama.create_project(self.db, "alice", valid_project())
    self.insert_planning_job(project, job_id=41, cost=3, status="pending", refunded=0)
    self.assertEqual(short_drama.get_project(self.db, "alice", project["id"])["spent_points"], 3)

def test_confirmed_refund_is_removed_from_spent_points(self):
    project = short_drama.create_project(self.db, "alice", valid_project())
    self.insert_planning_job(project, job_id=42, cost=3, status="error", refunded=1)
    self.assertEqual(short_drama.get_project(self.db, "alice", project["id"])["spent_points"], 0)

def test_apply_plan_does_not_double_count_paid_job(self):
    project, job = self.create_paid_completed_plan(cost=3)
    applied = self.apply_completed_plan(project, job)
    self.assertEqual(applied["spent_points"], 3)
```

- [x] **Step 2: 运行新测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_short_drama_projects.ShortDramaProjectTests.test_paid_planning_job_counts_before_apply tests.test_short_drama_projects.ShortDramaProjectTests.test_confirmed_refund_is_removed_from_spent_points tests.test_short_drama_projects.ShortDramaProjectTests.test_apply_plan_does_not_double_count_paid_job -v
```

Expected: 未应用任务显示 0，或应用后重复累计，至少一个断言失败。

- [x] **Step 3: 实现任务事实源聚合**

```python
def _charged_planning_points(conn, username, project_id):
    total = 0
    rows = conn.execute(
        "SELECT cost, payload, refunded FROM jobs WHERE username=? AND kind='copy' "
        "AND COALESCE(cost,0)>0 AND COALESCE(refunded,0)<>1",
        (username,),
    ).fetchall()
    for row in rows:
        payload = _json(row["payload"], {})
        if payload.get("format") == "short_drama" and payload.get("project_id") == project_id:
            total += int(row["cost"] or 0)
    return total
```

项目序列化时用该值覆盖响应中的 `spent_points`；预算判断使用同一值，不再将已应用和待应用任务分开相加。`apply_plan()` 保留应用任务登记，但移除 `spent_points=spent_points+?`。

- [x] **Step 4: 运行项目和退款相关回归并确认 GREEN**

Run:

```powershell
python -m unittest tests.test_short_drama_projects tests.test_job_refund_cas tests.test_content_domains -v
```

Expected: 全部通过，0 failures，0 errors。

- [x] **Step 5: 提交扣点一致性**

```powershell
git add server/content_domains/short_drama.py tests/test_short_drama_projects.py
git commit -m "fix: report charged short drama planning points"
```

### Task 5: 导入画布短剧工作区并补齐生产约束 UI

**Files:**
- Modify: `site/workbench/canvas.html`
- Modify: `site/workbench/canvas/canvas-app.js`
- Create: `site/workbench/canvas/canvas-short-drama.js`
- Create: `site/workbench/canvas/canvas-short-drama.css`
- Modify: `scripts/stamp_assets.py`
- Modify: `tests/test_canvas_api.js`
- Create: `tests/test_canvas_short_drama.js`
- Modify: `tests/test_canvas_asset_extraction.py`

**Interfaces:**
- Consumes: 分页项目接口、删除接口、项目 revision、策划任务和画布节点 API。
- Produces: 可创建、打开、保存、删除和恢复短剧策划的画布工作区。

- [x] **Step 1: 只导入画布测试并确认 RED**

从旧分支提取 `tests/test_canvas_short_drama.js` 和共享画布测试中的短剧断言，新增以下约束：

```javascript
test('30 second projects only offer six shots', () => {
  assert.deepEqual(shortDrama.validShotCounts(30), [6]);
});

test('delete sends project revision and removes the node after success', async () => {
  await shortDrama.deleteProject({id:'p1', revision:4});
  assert.deepEqual(api.deleted, {project_id:'p1', revision:4});
});
```

Run:

```powershell
node tests/test_canvas_short_drama.js
```

Expected: 因短剧画布模块不存在或缺少生产约束接口而失败。

- [x] **Step 2: 导入画布模块和最小共享集成**

从旧分支提取短剧 JS/CSS，并只把短剧入口、节点类型、API 方法和资源引用对应的 hunk 合入共享文件。不得整体覆盖主站 `canvas-app.js` 或带入测试服务器代理逻辑。

- [x] **Step 3: 实现组合过滤、分页和删除交互**

在 `canvas-short-drama.js` 导出并使用：

```javascript
function validShotCounts(duration) {
  return [6,7,8,9,10].filter(count => 5 * count <= duration && duration <= 10 * count);
}
```

列表请求显式发送 `page`/`page_size`；删除操作在用户确认后发送 `{project_id, revision}`，成功后关闭工作区并移除对应画布节点。页面累计点数标签使用“累计已扣”。

- [x] **Step 4: 运行画布测试并确认 GREEN**

Run:

```powershell
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
python -m unittest tests.test_canvas_asset_extraction -v
python scripts/stamp_assets.py
python scripts/stamp_assets.py --check
```

Expected: 所有测试通过，资源版本戳检查返回 `cache stamps OK`。

- [x] **Step 5: 提交画布功能**

```powershell
git add site/workbench/canvas.html site/workbench/canvas/canvas-app.js site/workbench/canvas/canvas-short-drama.js site/workbench/canvas/canvas-short-drama.css scripts/stamp_assets.py tests/test_canvas_short_drama.js
git commit -m "feat: add bounded short drama canvas workspace"
```

### Task 6: 更新短剧 API 文档并验证 PR 边界

**Files:**
- Modify: `docs/api/openapi.json`
- Modify: `docs/superpowers/plans/2026-07-22-short-drama-production-readiness.md`

**Interfaces:**
- Consumes: 最终 API 请求、响应、错误码和测试结果。
- Produces: 可审计的接口契约、完成勾选和干净 PR 文件清单。

- [x] **Step 1: 更新 OpenAPI**

只记录短剧项目、分页、删除、策划报价/任务/应用接口；明确 400、401、404、409、429 返回和 `spent_points` 为净实际扣点。

- [x] **Step 2: 运行重点验证**

```powershell
python -m unittest tests.test_short_drama_planning tests.test_short_drama_projects tests.test_canvas_asset_extraction -v
node tests/test_canvas_api.js
node tests/test_canvas_short_drama.js
node tests/test_cloud_shell_sidebar.js
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
Get-ChildItem -Path site -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { throw "JS syntax failed: $($_.FullName)" } }
git diff --check origin/main...HEAD
```

Expected: 所有命令退出码为 0。

- [x] **Step 3: 审计文件边界和敏感内容**

```powershell
git diff --name-only origin/main...HEAD
git log --oneline origin/main..HEAD
git diff origin/main...HEAD | rg -n "129\.204\.|password|passwd|api[_-]?key|secret|test-server|dev_proxy"
```

Expected: 文件列表只含设计、计划、短剧后端/画布/API 文档和对应测试；敏感扫描不出现服务器地址、凭证或测试服务器实现。文档中作为禁止范围出现的 `test-server`/`dev_proxy` 字样需人工确认仅为边界说明。

- [x] **Step 4: 提交文档**

```powershell
git add docs/api/openapi.json docs/superpowers/plans/2026-07-22-short-drama-production-readiness.md
git commit -m "docs: document short drama production API"
```

### Task 7: 推送并创建替代 Draft PR

**Files:**
- No repository file changes.

**Interfaces:**
- Consumes: 已验证的 `codex/short-drama-production` 分支。
- Produces: 指向主站 `main` 的新 Draft PR。

- [x] **Step 1: 完成提交后验证**

```powershell
git status --short --branch
git show -s --format="%H%n%P%n%s" HEAD
git diff --name-status origin/main...HEAD
```

Expected: 工作区干净；所有提交为主站 `main` 上的少量线性功能提交；文件清单没有服务器配置。

- [ ] **Step 2: 推送分支**

```powershell
git push -u origin codex/short-drama-production
```

- [ ] **Step 3: 创建 Draft PR**

PR 标题使用 `feat: add production-ready short drama planning`。正文写明：仅短剧功能、项目上限与分页、软删除、组合校验、扣点口径、数据库兼容、完整测试结果、未部署/未重启、旧 PR `#723` 被替代但未自动关闭。

- [ ] **Step 4: 核验远端 PR**

```powershell
gh pr view --repo tang730125633/huangque-main-site --json number,url,state,isDraft,baseRefName,headRefName,mergeStateStatus,statusCheckRollup
```

Expected: PR 为 OPEN/Draft，base 为 `main`，head 为 `codex/short-drama-production`，CI 已启动或完成。
