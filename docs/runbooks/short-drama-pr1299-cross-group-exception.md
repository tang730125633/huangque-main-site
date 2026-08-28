# PR #1299 跨组例外记录

## 适用对象

- 仓库：`tang730125633/huangque-main-site`
- PR：`#1299`
- 目标分支：`main`
- 审批时主线：`1165d66bffacf439d00c7142bfcc16ad7d2b8b4b`
- 固定业务提交：`0990c2275258f28fea004f2c06297c543a4320cb`
- 批准人与责任人：`@LU-003`

本记录只为上述固定业务提交保存一次性跨组批准证据，不修改或扩展业务范围。
固定业务提交之后只允许追加本记录；若业务代码、测试语义、Schema 或部署文件再次
变化，本批准立即失效，必须重新固定并审批。

## 版本化批准证据

- 批准评论：<https://github.com/tang730125633/huangque-main-site/pull/1299#issuecomment-5448053026>
- GitHub API 评论 ID：`5448053026`
- GitHub GraphQL 节点 ID：`IC_kwDOS66oj88AAAABRLqxIg`
- 作者：`LU-003`
- `author_association`：`COLLABORATOR`
- `created_at`：`2026-08-28T03:31:28Z`（`2026-08-28 11:31:28 +08:00`）
- `updated_at`：`2026-08-28T03:31:28Z`（发布后未编辑）

批准评论明确允许 PR #1299 针对 `CLAUDE.md`“一个 PR 只能动一个组”规则使用一次性
固定提交例外，同时明确该批准不构成后续 PR 的通用豁免。

## 批准范围

本次例外仅覆盖：

- 组 A：`server/content_domains/core.py`
- 组 B：`server/content_domains/video.py`
- 固定业务提交内与上述链路直接关联的短剧原生媒体、正式交付、场景文件归属模块和测试

固定提交中的跨组修改属于同一条安全与恢复链：

1. `core.py` 在 Worker 启动和周期恢复阶段调用原生媒体及正式交付清理；
2. `video.py` 负责原生视频下载、不可变原片、派生文件和孤儿媒体清理；
3. autodraft、正式渲染和 refinement 共同消费并复验 `native_media` 血缘；
4. 私人场景文件读取与资产图归属、锁定状态和 Provider preflight 共同构成 fail-closed 边界；
5. 单独移除任一组会形成启动恢复缺失、媒体证据不完整或安全语义不一致的中间状态。

因此，该固定业务提交按一个原子单元接受审核、测试和回滚。

## 固定提交验证证据

- 固定业务提交已包含审批时最新 `main`。
- 安全来源 `source` 与展示来源 `reference_source` 已分离。
- 私人场景文件鉴权、跨用户拒绝、URL/file 同索引配对及 Worker 二次复验保持 fail closed。
- `ai_generation`、`asset_library` 两类真实前端请求均通过设置、预览、锁定和公开 Provider preflight。
- GitHub Actions run `33137448618` 精确绑定固定业务提交：
  - `代码与安全门禁`：`SUCCESS`
  - `HQ CLI · Windows`：`SUCCESS`
- 批准时 PR 状态：`MERGEABLE / CLEAN`。

## 责任与授权边界

`@LU-003` 负责范围确认、最终验证和失败回滚判断。本记录不授权合并、部署、服务重启、
生产数据库操作或真实 Provider 调用；上述动作仍需各自独立指令。记录提交形成的新 HEAD
必须重新执行仓库门禁，并确认相对固定业务提交只有本文件发生变化。

## 最终重固定批准元数据

- 批准评论：<https://github.com/tang730125633/huangque-main-site/pull/1299#issuecomment-5448175482>
- GitHub API 评论 ID：`5448175482`
- GitHub GraphQL 节点 ID：`IC_kwDOS66oj88AAAABRLyPeg`
- 作者：`LU-003`
- `author_association`：`COLLABORATOR`
- `created_at`：`2026-08-28T03:51:38Z`（`2026-08-28 11:51:38 +08:00`）
- `updated_at`：`2026-08-28T03:51:38Z`（发布后未编辑）
