---
name: leads-business
description: "Business rules for the Huangque leads sub-agent: platform lead generation and CRM follow-up management."
short_description: 获客业务规则（leads 域）。
short_description_zh: 黄雀获客子 Agent 业务规则：平台获客名单与线索跟进 CRM。
version: 1
updated: 2026-09-02T00:00:00Z
---

# leads-business：黄雀获客子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- 平台获客：抖音/小红书/视频号 → 潜在客户名单（搜索评论区意向用户）。
- 线索跟进 CRM：查看线索、保存跟进状态与备注、删除线索。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| leads-generate | 生成获客名单 | 付费（先报价再确认） |
| leads-crm | 获客跟进列表 | 免费 |
| leads-crm-upsert | 保存客户跟进 | 免费（需确认） |
| leads-delete | 删除获客跟进 | 免费（需确认） |
| channels | 视频号渠道目录（channels_targets 用） | 免费 |
| leads | 获客工作台导航 | 免费 |

**关键参数**（以 `hq describe <id>` 为准）：
- leads-generate：platforms★（数组，douyin/xhs/channels，1~3 项不重复）；keyword（1~120，**douyin/xhs 时必填**）；channels_targets（1~20 项，**channels 时必填**，从 channels 目录取）；count=1~30（默认 20）；pages=1~3。
- leads-crm：lead_ids（≤100 项）。
- leads-crm-upsert：lead_id★（16~40）+ follow_status（待跟进/跟进中/已加微/已成交/无效）+ intent（高意向/咨询/价格敏感/围观）+ follow_note（≤300）。
- leads-delete：lead_ids★（1~100）。

## ③ 默认策略与容错逻辑

1. **需求澄清**：用户要名单 → 先问平台与关键词（或视频号目标账号）；缺 keyword/channels_targets 按平台必填规则 needs_user_input 追问；视频号目标账号先从 `channels` 目录里选。
2. **付费流程**：报价 → 报点数等确认 → 相同输入 + `--confirm --quote-token` 提交一次 → job_id → `task` 轮询 → 名单交付。
3. **CRM 衔接**：名单产出后主动提议导入 CRM 跟进；用户口述"这条加微了/无效"等 → 转成 upsert 的枚举值（follow_status/intent）保存。
4. **删除**：删除前向用户复述要删的线索条数与不可恢复性。
5. **失败容错**：生成失败重试 1 次同参数；仍失败 → failed 说明原因；平台限流（429）→ 稍等重读状态，不重复下单。
