# 黄雀子 Agent 集（v4 交付物）

本目录是 12 个黄雀子 Agent 业务 Skill 的**源文件**（可编辑版本）。每个子 Agent 只写三样：
①负责哪些业务结果 ②能调哪些工具 ③默认策略与容错逻辑。CLI 用法细节统一在官方工具底座技能 `use-huangque-cli` 里。

## 子 Agent 清单

| 子 Agent id | 名称 | 业务 Skill（源文件） | 负责的业务结果 |
| --- | --- | --- | --- |
| `hq-image` | 黄雀出图子 Agent | [hq-image/SKILL.md](hq-image/SKILL.md) | 文生图、图生图、多图、看图、提示词优化/反推 |
| `hq-video` | 黄雀出视频子 Agent | [hq-video/SKILL.md](hq-video/SKILL.md) | 文生/图生视频、口型同步、动作模仿、电影化身、换装 |
| `hq-audio` | 黄雀音频子 Agent | [hq-audio/SKILL.md](hq-audio/SKILL.md) | AI 配音、口播音频、声音克隆与管理 |
| `hq-copy` | 黄雀文案编导子 Agent | [hq-copy/SKILL.md](hq-copy/SKILL.md) | 写脚本、爆款拆解、分镜出图、脚本成片、同款复刻 |
| `hq-digital-human` | 黄雀数字人子 Agent | [hq-digital-human/SKILL.md](hq-digital-human/SKILL.md) | 形象、音色、口播成片、文案成片、讲解员、一键生成 |
| `hq-short-drama` | 黄雀短剧子 Agent | [hq-short-drama/SKILL.md](hq-short-drama/SKILL.md) | 立项、剧本共创、开拍预检、项目管理 |
| `hq-compose` | 黄雀成片子 Agent | [hq-compose/SKILL.md](hq-compose/SKILL.md) | 一键成片剪辑、模板成片（单条/批量） |
| `hq-canvas` | 黄雀画布子 Agent | [hq-canvas/SKILL.md](hq-canvas/SKILL.md) | 画布管理、节点写入、创作计划、节点内生成 |
| `hq-leads` | 黄雀获客子 Agent | [hq-leads/SKILL.md](hq-leads/SKILL.md) | 平台获客名单、线索跟进 CRM |
| `hq-collect` | 黄雀采集子 Agent | [hq-collect/SKILL.md](hq-collect/SKILL.md) | 链接内容/评论/原视频/口播稿采集、关键词搜索 |
| `hq-ip-positioning` | 黄雀 IP 定位子 Agent | [hq-ip-positioning/SKILL.md](hq-ip-positioning/SKILL.md) | IP12/数字化 IP 项目管理与报告、灵感案例 |
| `hq-system` | 黄雀系统子 Agent | [hq-system/SKILL.md](hq-system/SKILL.md) | 账号点数、任务轮询、资产库、价格、导航 |

## 与 Penguin 子 Agent 的对应关系

每个子 Agent 是一个 Penguin agent（`<app_data_dir>/agents/<id>/`），已按 `agent-initialization` 技能初始化：

- `agent_state/AGENTS.md`：角色 + 领域规则（六态返回协议、安全合同、实时发现）。
- `agent_state/skills/use-huangque-cli/`：官方工具底座技能（v0.3.0，从 `tang730125633/huangque-agent-skill` 移植并规范化；原 `agents/openai.yaml` 是外部适配器配置、无 Penguin 运行时，已丢弃）。
- `agent_state/skills/<domain>-business/`：本目录对应源文件的安装副本。
- `system_config.yaml`：name/description 已设；运行时继承主会话（provider+model_id 成对、thinking_level=medium，均不写入 Agent State）。

**同步约定**：本目录的 `SKILL.md` 是源，安装副本由源复制生成。改业务规则 → 改这里的源文件 → 重新复制到对应 agent 的 skills 目录。

## 路由与协议

主 Agent 路由表见 [`docs/subagent-routing.md`](../docs/subagent-routing.md)；CLI 补全清单见 [`docs/hq-cli-gap-list.md`](../docs/hq-cli-gap-list.md)；调研依据见 [`docs/hq-cli-tools-report.md`](../docs/hq-cli-tools-report.md)。
