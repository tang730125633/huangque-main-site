---
name: compose-business
description: "Business rules for the Huangque compose sub-agent: video-compose editing, matrix template videos (single and batch) with revision chains and template selection."
short_description: 成片业务规则（compose 域）。
short_description_zh: 黄雀成片子 Agent 业务规则：一键成片剪辑、模板成片（单条/批量）。
version: 1
updated: 2026-09-02T00:00:00Z
---

# compose-business：黄雀成片子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能（其「Template videos」段是模板成片的权威流程）。

## ① 负责哪些业务结果

- 一键成片剪辑：已有素材 → 分析 → 剪辑决策（保留/删除候选片段）→ 渲染成片。
- 模板成片：文案（顶部标题 + 底部行动文案）→ 素材库 + 原创模板 → 单条成片视频。
- 批量模板成片：同一模板 2~5 条一次报价一次确认。

## ② 能调哪些工具

**一键成片（video-compose 链，顺序推进）**

| 能力 id | 用途 | 要点 |
| --- | --- | --- |
| video-compose-create | 创建成片项目 | source_asset_id★（本人素材资产） |
| video-compose-analyze | 分析素材 | project_id★ + expected_revision★ |
| video-compose-review | 剪辑决策 | project_id★ + expected_revision★ + decisions★（candidate_* → keep/remove，1~200 项） |
| video-compose-render | 渲染成片 | project_id★ + expected_revision★ |
| video-compose-projects / project | 列表/详情 | 免费读 |
| video-compose-delete | 删除项目 | project_id★ + expected_revision★ |

**模板成片（matrix-template）**

| 能力 id | 用途 | 要点 |
| --- | --- | --- |
| matrix-template-capability / templates | 状态/模板目录（含字体） | 先查后选；template_id、font_family 只从实时结果取，绝不编造 |
| matrix-template-generate | 单条模板成片 | template_id★（1~64）、top_text★（2~60）、bottom_text★（2~80）、font_family（来自模板字体）；BGM 默认开；首次调用只报价 |
| matrix-template-batch-generate | 批量 2~5 条 | 同上 + count★（2~5）；整批一次报价一次确认 |
| one-click-video / matrix-template | 工作台导航 | 免费 |

## ③ 默认策略与容错逻辑

1. **路径选择**：已有素材资产 → 一键成片链（create→analyze→review→render，每步带 expected_revision）；只有文案 → 模板成片（先查 templates，选模板+字体）。
2. **剪辑决策**：analyze 产出的候选片段，decisions 必须经用户确认（列出候选摘要问 keep/remove），不擅自全 keep。
3. **批量规则**：batch 用 count=2~5 一次报价一次确认，**绝不逐条单独报价/确认**；字体锁定的模板只能单条（用 generate），批量自动降级为单条并说明。
4. **付费流程**：报价 → 报点数等确认 → 相同输入 + `--confirm --quote-token` 提交一次 → 保留全部 job_id 与原始 quote_token；批量部分失败/结果不确定 → 保留已接受的任务，按返回的结构化恢复指引处理，绝不新建整批。
5. **乐观锁**：每步 expected_revision 用上一步返回的最新值；409 冲突 → 重读 project 拿新 revision 再继续。
6. **失败容错**：render 失败 → 检查素材与决策完整性后重试 1 次同参数；仍失败 → failed 说明原因，不重复扣点。
