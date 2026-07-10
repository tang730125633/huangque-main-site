# 右上角铃铛通知中心查看任务跳转 PR

## 改动概述

修复了顶部栏铃铛无法正确跳转到指定获客任务的问题。现在铃铛点击后会弹出真实的获客任务列表，点击任务项可跳转到对应的任务详情并恢复运行中或已完成的结果。

## 修改的文件

### 1. `site/workbench/tasks.js` ✅
**重构为纯数据层任务存储**
- 移除了对已废弃 `.rail/.spacer` 结构的依赖
- 保留 `localStorage["hq_jobs"]` 作为任务元数据存储
- 新增 `normalize()` 统一任务记录格式
- 新增 `listRecent()` / `unreadCount()` / `markRead()` / `markAllRead()` 支持通知场景
- 暴露 `window.HQTasks` API：`list`, `get`, `upsert`, `remove`, `hrefFor`, `activeCount`, `unreadCount`, `markRead`, `markAllRead`, `onChange`

### 2. `site/workbench/leads.html` ✅
**新增 #task=<id> 深链接恢复能力**
- 引入 `tasks.js?v=1`（在 cloud-shell 之前加载）
- 新增 `taskIdFromLocation()` 解析 hash/query 中的 task 参数
- 新增 `hydrateTask(id)` 通过 `/api/gen/job/{id}` 恢复任务（done/running/failed 都支持）
- 新增 `focusTaskResult()` 滚动到结果区并高亮
- 新增 `syncTaskMeta(patch)` 在提交/轮询时写入 HQTasks 元数据
- 修改 `start()` 提交成功后同步写入 HQTasks
- 修改 `watch(id, t0, meta)` 轮询时持续更新任务状态到 HQTasks
- 新增 `bootTaskState()` 页面初始化时优先恢复 hash 任务，否则恢复 localStorage 活跃任务
- 新增 `hashchange` 监听支持同页内切换任务

### 3. `site/workbench/cloud-shell.js` ✅
**铃铛从硬跳 leads.html 改为弹出真实通知列表**
- 移除 `notifyBtn.onclick = () => location.href = 'leads.html'`
- 新增铃铛包装层 `notifyWrap` 和红点 `notifyDot`
- 新增通知浮层 `notifyPop`（340px 宽，最多 360px 高，显示最近 5 条获客任务）
- 新增 `bellStoreRead()` / `bellJobs(limit)` 读取任务列表（优先 HQTasks API，兜底 localStorage 直读）
- 新增 `bellSummary(job)` / `bellStatusColor(job)` 生成任务摘要与状态色
- 新增 `renderBell()` 渲染通知列表，空态显示"暂无获客任务"
- 新增 `markBellRead(id)` 点击任务项时标记已读
- 铃铛红点根据 `unread` 或活跃任务状态显示
- 绑定 `storage` / `HQTasks.onChange` / `Escape` / 点击外部关闭逻辑

### 4. `site/workbench/*.html`（14 个页面）✅
**统一更新 cloud-shell.js 缓存戳**
- 所有页面的 `cloud-shell.js?v=43c67c59` 已更新为 `?v=207ff35b`（基于修改后的 cloud-shell.js 内容 MD5）

## 技术细节

### 任务记录字段（`hq_jobs` localStorage）
```js
{
  id: string,              // job_id
  kind: 'leads',           // 任务类型（本次只支持 leads）
  status: 'queued|running|done|failed',
  title: string,           // 显示标题（优先 keyword）
  keyword: string,         // 用户输入的关键词
  t0: number,              // 任务开始时间戳
  href: 'leads.html#task=<id>',  // 深链接
  leads_count: number,     // 完成态：客户数
  deduped: number,         // 完成态：去重数
  error: string,           // 失败态：错误信息
  unread: boolean,         // 是否未读（新完成/失败的任务标为 unread）
  createdAt: number,
  updatedAt: number
}
```

### 深链接格式
- `leads.html#task=<job_id>`
- 也兼容 `leads.html?task=<job_id>`

### 铃铛通知列表
- 最多显示最近 5 条获客任务
- 按 `updatedAt` 倒序排列
- 进行中任务（queued/running）或 unread 任务时显示红点
- 点击任务项跳转到 `leads.html#task=<id>` 并标记已读

### 视觉规范
- 遵守 `DESIGN.md`：后台式、紧凑、低装饰
- 浮层：`rgba(16,24,39,.98)` 深色背景，`border-radius: 14px`
- 状态色：done=`#2dd4bf`，failed=`#f4708a`，running=`#e7b24c`

## 验收要点

### ✅ 提交新任务
1. 在 `leads.html` 输入关键词提交获客任务
2. 切换到其他工作台页面
3. 点击右上角铃铛，能看到刚才的任务（进行中状态）
4. 点击任务项，跳转回 `leads.html#task=<id>`
5. 页面显示"正在打开任务…"，然后继续轮询
6. 任务完成后正常渲染客户列表和 KPI

### ✅ 查看已完成任务
1. 等任务完成后，再次点击铃铛
2. 任务条目显示"已完成 · X 个客户"
3. 点击条目，跳转到 `leads.html#task=<id>`
4. 页面直接显示该任务的结果（不需要 `hq_active_leads_job`）

### ✅ 查看失败任务
1. 提交一个必然失败的任务（如关键词为空或后端返回错误）
2. 失败后铃铛显示红点
3. 点击铃铛，任务条目显示"失败 · xxx"
4. 点击条目，跳转到任务页并显示失败信息

### ✅ 空态
1. 清空 localStorage `hq_jobs`
2. 点击铃铛，显示"暂无获客任务"

### ✅ 回归
1. 现有 `hq_active_leads_job` 的刷新/切页恢复能力不回归
2. 顶栏其它功能（导航、点数、登录态）不受影响
3. 所有 workbench 页面正常加载新版 `cloud-shell.js?v=207ff35b`

## 未来扩展
- 本次只支持获客任务（`kind: 'leads'`）
- 要支持视频/音频/爬取/作图任务，需在各页面提交/轮询时调用 `HQTasks.upsert`，并在 `bellJobs()` 中增加 filter
- `tasks.js` 的 `hrefFor()` 可扩展为根据 `kind` 返回不同页面的深链接

## 提交信息建议
```
fix(workbench): 铃铛通知中心支持任务跳转

- 重构 tasks.js 为纯数据层（移除废弃 .rail 依赖）
- leads.html 支持 #task=<id> 深链接恢复任务
- cloud-shell.js 铃铛改为弹出获客任务列表
- 更新全站 cloud-shell.js 缓存戳 v=207ff35b

修复 #413
```

## 相关工单
- 工单-NAV-BELL通知铃铛接功能-20260709.md

---
生成时间：2026-07-10
