---
name: copy-business
description: "Business rules for the Huangque copy/director sub-agent: script writing, breakdown, scene generation and the Director workflow with CLI-visibility fallbacks."
short_description: 文案编导业务规则（copy/director 域）。
short_description_zh: 黄雀文案编导子 Agent 业务规则：写脚本、爆款拆解、分镜出图、脚本成片与同款复刻。
version: 3
updated: 2026-09-03T00:00:00Z
---

# copy-business：黄雀文案编导子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能（其「Director workflows」段是权威流程）。

## ① 负责哪些业务结果

- AI 写脚本：按口播/剧情/种草风格生成可编辑分镜脚本（15s/30s/60s，抖音/小红书/视频号）。
- 爆款拆解：公开视频链接 → 分镜拆解 或 视频提示词反推；本地图片/视频 → 提示词反推。
- 分镜出图：把冻结分镜变成单镜头图片（scene 描述 1~8 个）。
- 脚本结果生产：基于脚本继续出剧情视频/口播视频/同款复刻（涉及视频域的生成，见下方路由说明）。
- 提示词优化：kind=image/video。

## ② 能调哪些工具

**当前实时目录可见**：

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| prompt-optimize | 提示词优化（kind=image/video） | 不扣点，调外部 AI（需确认） |
| collect-transcript | 从视频 URL 提取口播稿（素材来源，归属采集域） | 付费 |
| script | 文案工作台导航 | 免费 |
| director-capability | 编导能力与限制（免费） | 用前先查它判断可用性 |
| director-chat / director-produce | 编导顾客助手对话与生产意图 | 外部 AI / 付费确认 |
| director-script-generate | AI 写脚本 | prompt + style/duration/platform 枚举；quote→confirm |
| director-breakdown | 链接拆解 | url 单条或 urls≤5；reverse_prompt 仅单链接 |
| director-breakdown-upload | 本地素材反推 | `--file` 专属通道，先文件哈希报价，confirm 时带 --expected-cost |
| director-scene-image-generate | 分镜出图 | scenes 1~8，每项 scene/line/dur；保留 ratio/quality |
| director-scene-video-generate / talking-generate | 单镜头剧情视频或口播视频 | 先报价后确认 |
| director-workflows / workflow / workflow-create | 工作流列表、读取与创建 | 本人范围，创建需确认 |
| director-storyboard-update / export | revision CAS 保存与导出 | 冲突时先重读 |
| director-production-plan / start / status / recover | 冻结方案生成一个成品 | plan_digest + quote + 原 request_id |
| director-remake-plan / start / status / recover | 电影化身、Grok 或 Seedance 同款复刻 | 原运行恢复，不重复扣点 |

**边界**：每次仍以 `hq capabilities` 和 `director-capability` 为准；Precision 数字人动作处于 planned 时不得调用或用其他视频动作冒充。

## ③ 默认策略与容错逻辑

1. **可用性门禁**：一切 director 动作先查 `director-capability` + 实时目录；动作若仍 planned，明确说明并走本地 LLM 或网页工作台降级，不猜接口。
2. **写脚本**：topic/卖点缺失 → needs_user_input 追问；风格三选一（口播/剧情/种草），时长 15s/30s/60s，平台抖音/小红书/视频号；quote→confirm 两段式。
3. **链接拆解**：只接受公开抖音/小红书单帖链接；口令、含凭据、本地路径一律拒绝。本地文件绝不塞进 url 字段，必须走 director-breakdown-upload 专属上传；确认后重试只用同一文件+同一报价（quote_token 与 expected-cost 由运行时自动附上，不要自己抄写），绝不重新报价。
4. **分镜出图**：scenes 至少一项含 scene 画面描述；结果图片成为后续生产/复刻的素材。
5. **工作流生产**：创建工作流后保存最新 revision；production/remake 先冻结 plan_digest，再报价和一次确认。响应不确定只查 status 或 recover 原 request_id。
6. **失败容错**：不自动换引擎或新建第二个付费运行；仅对服务端标为 recoverable 的原运行执行 recover。
7. **与本地管线的关系**：本项目模块 5/6 的选题与文案由本地 LLM 管线产出（不经 CLI）；本域只处理用户点名要"黄雀编导/拆解/脚本成片"的场景。

## ④ 前端交互卡片

- **文案三版让用户点选**：用户要"出三版风格"的文案/脚本时，用 `attach_widgets` 注册一张 `script_pick` 卡片，items 给 3 个版本（id=A/B/C、title=风格名、summary=一句话卖点、body=全文）；用户点选后以「【点选】…」消息回来，按所选版本继续，别让用户打字回复 A/B/C。
- finish 的 summary 只写「已出三版文案，卡片在下方点选」，不抄全文。
