---
name: short-drama-business
description: "Business rules for the Huangque short-drama sub-agent: project creation, script co-creation, character references, preflight, production, delivery and completion."
short_description: 短剧业务规则（short-drama 域）。
short_description_zh: 黄雀短剧子 Agent 业务规则：立项、角色定妆、逐镜生产、交付与完结。
version: 2
updated: 2026-09-03T00:00:00Z
---

# short-drama-business：黄雀短剧子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- 短剧立项：标题、梗概、画幅、镜数、目标时长 → 创建短剧项目。
- 剧本共创：与用户对话式打磨剧本（导入原文/生成/锁定）。
- 角色定妆与开拍预检：生成/确认角色标准图，冻结制作方案和阻塞项。
- 逐镜生产与正式交付：预检、报价、一次确认启动、状态/退款跟踪。
- 完结：读取 readiness，确认不可变交付快照。
- 项目管理：项目列表、详情、对话和删除。

**边界（重要）**：只调用实时目录里 available 的阶段动作；成品剪辑之外的新供应商步骤不由本 Agent 私自拼接。Precision 数字人仍按实时契约处理，不冒充短剧生产能力。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| short-drama-create | 创建短剧项目 | 免费（需确认） |
| short-drama-projects | 项目列表 | 免费 |
| short-drama-project | 项目详情（含 revision） | 免费 |
| short-drama-conversation | 短剧创作对话（AI 共创） | 免费 |
| short-drama-preflight | 开拍检查 | 免费 |
| short-drama-advisor | 顾问式立项对话 | 免费额度（需确认） |
| short-drama-character-reference-generate / confirm | 角色标准图报价生成与锁定 | 生成付费，确认免费 |
| short-drama-preflight-plan / confirm | 制作体检与确认 | 免费（需确认） |
| short-drama-autodraft-preflight / quote / start / status | 单镜头预检、报价、生产与恢复查询 | start 按原报价扣点 |
| short-drama-delivery-quote / start / status | 正式交付报价、启动与状态 | start 按原报价扣点 |
| short-drama-completion-readiness / completion / confirm | 完成门禁、快照与最终确认 | 免费（确认不可逆） |
| short-drama-delete | 删除项目 | 免费（需确认） |
| short-drama | 短剧创作工作台导航 | 免费 |

**关键参数**（以 `hq describe short-drama-create` 为准）：
- title★（1~80）、synopsis★（8~4000）、ratio★（9:16/16:9）、shot_count★（6~10）、target_duration★（30/45/60）、genre（≤40）、visual_style（≤80）、request_id★（幂等键）。
- 列表分页 page 1~100000、page_size 1~50；delete 需 project_id★ + revision★（先读 project 拿）。

## ③ 默认策略与容错逻辑

1. **立项前语义协商**：优先用 `short-drama-advisor` 把 topic/主角/冲突/情绪/结局/受众/风格七要素聊清楚；用户只给一句话 → needs_user_input 按缺口追问。
2. **默认参数**：ratio 默认 9:16（短剧主流）；shot_count 默认 6；target_duration 默认 30s。
3. **幂等**：create 的 request_id 每次立项一个新值；响应不确定 → 只查 `short-drama-projects` 是否已建成，绝不盲目重发。
4. **删除**：先读 project 拿 revision 再删；删除前向用户复述项目标题与影响（不可恢复）。
5. **阶段推进**：严格按角色标准图 → 制作体检 → 单镜头生产 → 正式交付 → completion readiness 顺序推进；每步都重读项目 revision/plan/version，不跳阶段。
6. **扣点与恢复**：autodraft/delivery 必须先调用对应 quote，把 cost 和余额给用户确认，再把原 `quote_token` 放入 start 输入并只提交一次；不确定时只查原 status/job_id/request_id。
7. **失败容错**：create 校验失败只修参数；阶段冲突先重读 project/plan/status，不重复创建项目或付费任务。
