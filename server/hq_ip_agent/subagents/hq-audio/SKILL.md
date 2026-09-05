---
name: audio-business
description: "Business rules for the Huangque audio sub-agent: audio business outcomes, hq capabilities, voice defaults and fallback logic."
short_description: 音频业务规则（audio 域）。
short_description_zh: 黄雀音频子 Agent 业务规则：音频类业务结果、可用 hq 能力、默认音色与容错。
version: 1
updated: 2026-09-02T00:00:00Z
---

# audio-business：黄雀音频子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- AI 配音：文字 → 音频（可选音色、语速、音调、音量）。
- 口播音频：为后续数字人口播准备的声音资产。
- 个人声音克隆：查槽位 → 上传样音 → 克隆训练 → 用自己声音配音。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| audio-generate | 文字 → 音频 | 付费（先报价再确认） |
| voices | 可用音色列表 | 免费 |
| audio-slots | 声音克隆槽位查看 | 免费 |
| voice-clone-create | 创建声音克隆 | 免费（需确认） |
| voice-clone-status | 声音克隆状态 | 免费 |
| audio-upload | 私有参考音频上传 → upload_id | 免费（需确认） |
| audio | 音频工作台导航 | 免费 |

**关键参数**（以 `hq describe <id>` 为准）：
- audio-generate：text★（1~1000）、voice（1~128）、pitch=-12~12、speed=0.5~2、volume=-50~100。
- voice-clone-create：audio_upload_id★（36 位）、name★（1~40）、slot_id★；**必须带 8~128 位幂等键，不同样音绝不能复用同一幂等键**（冲突返回 409）。
- voice-clone-status：slot_id★。

## ③ 默认策略与容错逻辑

1. **默认音色**：用户未指定 voice → 先 `voices` 列表，选一个公共中文音色并告知"可换"，报价时说明。
2. **克隆流程**：用户要自己的声音 → `audio-slots` 确认有空槽位 → 用户上传样音（`audio-upload`）→ `voice-clone-create`（生成幂等键）→ 用 `voice-clone-status` 轮询到就绪 → 用克隆音色配音。无空槽位 → needs_user_input 说明需购买槽位（引导到价格/会员页）。
3. **授权**：克隆必须用用户本人明确授权的声音样音，不接受他人声音。
4. **流程**：报价 → 报点数等确认 → 相同输入 + `--confirm` 提交一次（quote_token 由运行时自动附上，不要自己抄写） → job_id → `task` 轮询 → 交付音频资产。
5. **失败容错**：克隆失败 → 检查样音格式/长度（mp3/wav/m4a），让用户重传后重试一次；生成失败 → 调 pitch/speed 或换音色重出，重新报价。
6. **不满意**：用户不满意 → 调 pitch/speed/volume 重出或换音色，不自动重复扣点。
