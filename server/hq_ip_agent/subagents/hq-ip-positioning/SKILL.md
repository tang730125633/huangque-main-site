---
name: ip-positioning-business
description: "Business rules for the Huangque IP-positioning sub-agent: IP12 and digital-IP project management, reports, messages and inspiration cases."
short_description: IP 定位业务规则（ip-positioning 域）。
short_description_zh: 黄雀 IP 定位子 Agent 业务规则：IP12/数字化 IP 项目、报告、对话与灵感案例。
version: 1
updated: 2026-09-02T00:00:00Z
---

# ip-positioning-business：黄雀 IP 定位子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- IP12 项目：创建、列表、资料、报告读取、继续对话、删除。
- 数字化 IP 项目：创建、更新、删除、列表、详情、报告。
- 灵感案例：案例目录、收藏状态、收藏/取消收藏。

**与本项目本地管线的边界**：本域只负责**黄雀主站侧**的项目与报告。本项目自己的 IP 定位管线（采集 → 8 模块诊断报告 → 模块 5 选题 → 模块 6 文案 → 模块 7/8）由本地代码与 LLM 完成，不经本域；用户要"打开 IP12 项目/读报告/继续主站对话"时才用本域。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| ip12-projects / ip12-project / ip12-report | 项目列表/资料/报告 | 免费 |
| ip12-create | 创建 IP12 项目 | 免费（需确认） |
| ip12-message | 继续 IP12 对话 | 不扣点，调外部 AI（需确认） |
| ip12-delete | 删除 IP12 项目 | 免费（需确认） |
| digital-ip-projects / digital-ip-project / digital-ip-report | 数字化 IP 项目列表/详情/报告 | 免费 |
| digital-ip-create / digital-ip-update / digital-ip-delete | 数字化 IP 项目管理 | 免费（需确认） |
| inspiration-catalog / inspiration-likes | 灵感案例目录/收藏状态 | 免费 |
| inspiration-like | 收藏/取消收藏案例 | 免费（需确认） |

**关键参数**（以 `hq describe <id>` 为准）：
- ip12-create：title★（1~120）；ip12-message：project_id★ + message★（1~4000）+ request_id★（幂等键）；ip12-delete：project_id★。
- digital-ip-create：title★（1~80）；digital-ip-update/delete：project_id★ + revision★（先读 project 拿）；update 可改 title。
- inspiration-like：id★ + favorite★。

## ③ 默认策略与容错逻辑

1. **幂等**：ip12-message 每次对话轮次用新的稳定 request_id；响应不确定 → 只查原 request_id/project，绝不重发消息。
2. **乐观锁**：digital-ip update/delete 先读项目拿 revision 再写；409 → 重读再写。
3. **删除**：删除项目前向用户复述项目标题与不可恢复性；只删用户点名的项目。
4. **灵感做同款**：用户看中案例 → 建议"做同款"并导航到对应工作台（预填提示词/参考），生成仍由对应域负责。
5. **失败容错**：ip12-message 失败（外部 AI 超时）→ 按 retryable 重试 1 次同 request_id；仍失败 → failed 说明原因。
6. **两套体系**：IP12（ip12-*，发起人对话）与数字化 IP（digital-ip-*，项目管理）并存；按用户所指的项目体系操作，不混淆。
