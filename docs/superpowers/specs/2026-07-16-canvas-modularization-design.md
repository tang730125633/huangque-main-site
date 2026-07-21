# 画布模块渐进式拆分设计

## 背景

`site/workbench/canvas.html` 当前约 3881 行，同时承载页面结构、样式、画布状态、节点渲染、交互、模板、本地存储、任务执行、导出和协作同步。功能虽然完整，但任何局部修改都需要理解大量共享变量和 DOM 副作用，容易产生冲突与回归。

本设计采用渐进式模块化，不重写产品、不引入前端框架，并通过三个串行 PR 将职责逐步拆开。

## 目标

- 将 `canvas.html` 收敛为页面结构和静态资源入口，目标 300–500 行。
- 按职责拆分状态、图算法、存储、渲染、交互、执行、协作、导出和 API。
- 每个模块保持单一职责，原则上不超过 800 行，不新造替代性大文件。
- 保持当前 DOM ID、接口路径、数据格式、localStorage 键和协作协议兼容。
- 每个迁移阶段都能独立验证、独立回滚，不把结构调整与新功能混在同一 PR。

## 非目标

- 不改用 React、Vue、React Flow 或其他框架。
- 不重新设计画布视觉和交互。
- 不修改服务端数据库结构或 API 协议。
- 不新增节点类型、版本历史、远端光标或评论功能。
- 不在本次拆分中处理与模块化无关的产品需求。

## 目标文件结构

```text
site/workbench/
├── canvas.html
└── canvas/
    ├── canvas.css
    ├── canvas-app.js
    ├── canvas-state.js
    ├── canvas-graph.js
    ├── canvas-storage.js
    ├── canvas-api.js
    ├── canvas-renderer.js
    ├── canvas-interactions.js
    ├── canvas-runner.js
    ├── canvas-collab.js
    └── canvas-export.js
```

现有 `canvas-collab-sync.js` 保持为底层同步算法模块；`canvas-collab.js` 负责把它接入页面状态、传输层和界面状态。

## 模块职责

### `canvas-app.js`

页面唯一启动入口。创建各模块、注入依赖、绑定顶层事件并协调页面模式切换。它不实现图算法、请求细节或节点业务逻辑。

### `canvas-state.js`

管理节点、连线、选择集、缩放、撤销栈和当前画布元数据。提供快照、恢复和订阅接口，不直接访问网络或 localStorage。

### `canvas-graph.js`

只包含可独立测试的纯函数：端口与连线索引、依赖图、环检测、拓扑排序、自动布局所需计算和内容边界计算。

### `canvas-storage.js`

统一维护现有草稿、画布、模板和活动画布键；负责兼容旧数据、配额错误、图片压缩和空间清理。其他模块不得直接写这些 localStorage 键。

### `canvas-api.js`

封装 `/api/auth/canvas/*`、`/api/gen/*` 和资产请求，统一 JSON、超时、HTTP 错误及取消行为。它不展示提示框或修改 DOM。

### `canvas-renderer.js`

根据状态渲染节点、连线、选区、缩略图、画布列表、模板列表和保存状态。用户输入通过回调或事件交给应用层，不直接执行任务或保存数据。

### `canvas-interactions.js`

管理拖拽、框选、连线、缩放、平移、快捷键、复制粘贴、上下文菜单和全屏工具栏。它只调用显式的状态动作，不直接修改共享状态对象。

### `canvas-runner.js`

管理节点输入校验、运行依赖、并发上限、任务轮询、重试、取消和运行状态。通过 `canvas-api.js` 发请求，通过事件报告进度和错误。

### `canvas-collab.js`

负责协作画布列表、成员管理、角色权限、自动保存、增量同步、在线心跳和断线重连。底层差异计算和操作应用继续复用 `canvas-collab-sync.js`。

### `canvas-export.js`

负责画布 JPG 预览、模板导入导出和导出资源加载。输入为快照，输出为 Blob、下载动作或结构化错误。

## 依赖方向

```text
canvas-app
  ├─ canvas-state
  ├─ canvas-storage
  ├─ canvas-graph
  ├─ canvas-renderer
  ├─ canvas-interactions
  ├─ canvas-runner ── canvas-api
  ├─ canvas-collab ── canvas-api + canvas-collab-sync
  └─ canvas-export
```

底层模块不得反向依赖 `canvas-app.js`。模块间共享数据必须通过快照、显式动作或事件传递，禁止依赖隐式全局变量。

## 加载与兼容策略

- 继续采用仓库当前可直接由 Nginx 提供的普通脚本，不增加构建步骤。
- 可复用的纯逻辑模块使用与 `canvas-collab-sync.js` 一致的 UMD 形式，同时支持浏览器全局对象和 Node 测试。
- 页面专用模块挂载到单一 `window.HQCanvas` 命名空间，避免增加散落的全局变量。
- 保留所有现有 DOM ID，迁移阶段不改变测试依赖的文本和控件名称。
- 保留 `hq_canvas_draft_v2`、`hq_canvas_templates_v2`、`hq_canvas_boards_v1`、`hq_canvas_active_id` 等现有键。
- 保留协作画布快照与操作协议，旧页面和服务端无需同步发布才能读取已有数据。

## 三阶段迁移

### PR 1：机械提取

- 将内联 CSS 原样移动到 `canvas/canvas.css`。
- 将内联业务脚本原样移动到 `canvas/canvas-app.js`。
- `canvas.html` 只调整资源引用，不改变功能和执行顺序。
- 更新共享资源缓存戳并补充静态结构测试。

验收重点：页面加载、所有核心控件存在、脚本顺序正确、原有画布与协作测试全部通过。

### PR 2：纯逻辑模块

- 拆出 `canvas-state.js`、`canvas-graph.js`、`canvas-storage.js`、`canvas-api.js` 和 `canvas-export.js`。
- 先迁移无 DOM 或少 DOM 的代码，再由 `canvas-app.js` 调用。
- 每迁移一个模块先补 Node 单元测试，再删除旧实现。

验收重点：快照兼容、撤销恢复、环检测、自动布局、模板读写、空间不足、请求错误和导出行为。

### PR 3：交互与业务编排

- 拆出 `canvas-renderer.js`、`canvas-interactions.js`、`canvas-runner.js` 和 `canvas-collab.js`。
- `canvas-app.js` 最终只负责初始化、依赖注入和页面模式协调。
- 清除已经迁出的共享变量和重复事件绑定。

验收重点：节点增删拖拽、连线、框选、快捷键、批量运行、任务失败、离线重连、角色降级和多人同步。

三个 PR 必须串行，均属于 `E-canvas` 冲突组；后一个 PR 只从前一个已合并的最新 `main` 开始。

## 状态与事件约定

状态模块暴露只读快照和动作，例如：

```text
getSnapshot()
restoreSnapshot(snapshot)
dispatch({ type, payload })
subscribe(listener)
```

首期不引入完整 Redux 风格框架。动作只覆盖迁出的共享状态，避免为了“纯架构”重写每个表单输入。渲染、保存和协作模块通过订阅或应用层调度响应状态变化。

## 错误处理

- API 模块返回带 `status`、`code` 和安全提示文案的结构化错误。
- 存储模块区分配额不足、数据损坏和浏览器禁用存储。
- Runner 保留节点级失败状态，单节点失败不得让队列永久停在运行中。
- 协作模块保留现有离线队列和退避重连；无法恢复时请求权威快照。
- 应用层统一决定弹窗、节点提示和保存状态文字，底层模块不直接 `alert`。

## 测试策略

### 每个 PR 的基础门禁

```text
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
python -m compileall -q server scripts worker
node --check <本次新增或修改的 JS>
git diff --check
```

### 保留的回归测试

- `tests/test_canvas_realtime_sync.js`
- `tests/test_canvas_board_card_layout.js`
- `tests/test_auth_canvas_collab.py`

### 新增测试方向

- HTML 引用和加载顺序。
- 图算法、快照与撤销栈。
- 旧 localStorage 数据兼容和配额错误。
- API 超时与 HTTP 错误归一化。
- Runner 并发、依赖失败和终态收敛。
- 协作保存、远端更新、离线恢复和只读权限。

提交每个 PR 前运行相关测试；PR 3 完成后运行全量测试。

## 风险控制与回滚

- 不在同一个 PR 中同时迁移文件和改变产品行为。
- 每次只迁移一个职责，测试通过后删除旧实现，禁止新旧路径长期双写。
- PR 1 可直接恢复内联资源回滚；PR 2、PR 3 可按模块逐个回退调用点。
- 若拆分暴露现有 bug，先添加回归测试；行为修复单独提交，不混入机械迁移提交。
- 部署必须在 PR 合并后从最新 `main` 进行，并只部署本次变更文件。

## 完成标准

- `canvas.html` 仅保留页面结构和资源引用，达到 300–500 行目标。
- 没有新增超过 1000 行的 JavaScript 文件。
- 模块职责和依赖方向符合本设计，没有跨模块直接操作私有状态。
- 现有本地画布、模板和协作画布无需迁移即可继续使用。
- 三个 PR 均通过 CI，且核心交互和多人协作验收通过。
