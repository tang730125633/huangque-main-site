---
name: canvas-business
description: "Business rules for the Huangque canvas sub-agent: board management, node ops, canvas agent plans (plan-only), and in-node generation with version locks."
short_description: 无限画布业务规则（canvas 域）。
short_description_zh: 黄雀画布子 Agent 业务规则：画布管理、节点写入、创作计划与节点内生成。
version: 1
updated: 2026-09-02T00:00:00Z
---

# canvas-business：黄雀画布子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- 画布管理：创建、列表、读取、删除。
- 节点与连线写入：拖拽、连线、撤销等画布编辑操作（canvas-ops）。
- 画布 Agent 创作计划：读画布快照 → 返回创作计划（**只计划，绝不自动应用**）。
- 节点内生成：图片节点 / 视频节点内生成作品并写回节点。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| canvas-list / canvas-get | 画布列表/读取（get 含最新版本号） | 免费 |
| canvas-create | 创建画布 | 免费（需确认） |
| canvas-delete | 删除画布 | 免费（需确认） |
| canvas-ops | 写入画布操作（节点/连线/撤销） | 免费（需确认） |
| canvas-agent-plan | 画布 Agent 生成创作计划 | 付费（先报价再确认） |
| canvas | 画布工作台导航 | 免费 |

**关键参数**（以 `hq describe <id>` 为准）：
- canvas-create：name★（1~48）、prompt（≤2000）。
- canvas-ops：board_id★ + expected_version★ + op_id★（`hqcli-` 前缀唯一 id）+ ops★（1~12 项）。
- canvas-agent-plan：prompt★（1~2000）+ project_id★ + scope★（local/collab）+ nodes（≤60）+ edges（≤120）+ selected_node_ids（≤30）+ snapshot_digest★ + history（≤10）。
- 节点内生成复用 image-generate / video-generate（**用前在实时目录验证存在**），参数默认沿用：图片 banana+nb2、视频 grok 720p（见 image/video 域技能）。

## ③ 默认策略与容错逻辑

1. **创作计划只计划不应用**：canvas-agent-plan 返回计划后**必须等用户确认才写回**；把计划要点用中文列给用户（要建哪些节点/连哪些线/生成什么），确认后再经 canvas-ops 落盘。绝不在用户确认前应用计划。
2. **版本锁**：写 ops 前先 canvas-get 拿最新 expected_version；409 冲突 → 重读再写，不盲写。
3. **op_id**：每次写入生成新的 `hqcli-<唯一后缀>` op_id，不重复使用。
4. **节点内生成**：先生成（报价→确认→提交→轮询）拿到资产，再用 ops 写回对应节点；结果不确定时只查原 job_id，不重复生成。
5. **权限**：只有画布 owner 能发起付费操作与删除（viewer 只读、editor 可写不可付费）；操作前确认用户身份是 owner。
6. **并行约束**：同账号并行任务上限 5（MAX_USER_ACTIVE_JOBS），多节点生成**串行**逐个提交并轮询，不无脑并发（429 时稍等重读状态）。
7. **短剧节点**：创建/连接短剧节点保留上下文；短剧生产本身仍在短剧域/web 工作台完成。
8. **删除**：删除画布前向用户复述画布名与不可恢复性；只删用户点名的画布。
