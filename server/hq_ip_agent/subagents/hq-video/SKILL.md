---
name: video-business
description: "Business rules for the Huangque video sub-agent: video business outcomes, hq capabilities, default channels and fallback logic."
short_description: 出视频业务规则（video 域）。
short_description_zh: 黄雀出视频子 Agent 业务规则：视频类业务结果、可用 hq 能力、默认渠道与容错。
version: 1
updated: 2026-09-02T00:00:00Z
---

# video-business：黄雀出视频子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- 文生视频 / 图生视频：描述（或参考图+描述）→ 短视频（5 个渠道）。
- 口型同步：已有视频 + 已有音频 → 对口型视频（lipsync）。
- 动作模仿：电影化身模仿参考视频动作。
- 电影化身开放式生成：化身 + 描述 → 电影级短片。
- 换装：人物照/视频 + 衣服图 → 换装视频（快速/经典两条线路）。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| video-generate | 文生/图生视频，5 渠道 | 付费（先报价再确认） |
| video-lipsync | 原视频口型同步 | 付费 |
| cinematic-open-generate | 电影化身开放式生成 | 付费 |
| cinematic-motion-generate | 电影化身动作模仿 | 付费 |
| tryon-fast-generate | 快速换装 | 付费 |
| tryon-classic-generate | 经典换装 | 付费 |
| video-upload | 私有参考视频上传 → upload_id | 免费（需确认） |
| prompt-optimize | 提示词优化，kind=video | 不扣点，调外部 AI（需确认） |
| video | 视频工作台导航 | 免费 |

**video-generate 渠道规则**（channel 必选其一，以 `hq describe video-generate` 为准）：

| channel | 时长 | 分辨率 | 比例 | 参考图上限 | 备注 |
| --- | --- | --- | --- | --- | --- |
| grok | 1~15s | 480p/720p | 1:1/16:9/9:16/4:3/3:4/3:2/2:3 | 7 | model 可选 grok-imagine-video / -1.5；禁用 seconds、generate_audio |
| micro | 4~15s | 480p/720p/1080p | 21:9/16:9/4:3/1:1/3:4/9:16/adaptive | 9 | 禁用 seconds、model |
| omni | 3~10s | 720p | 9:16/16:9 | 6 | 禁用 seconds、model、generate_audio |
| minimax | 4~15s | 2k（仅此档） | 21:9/16:9/4:3/1:1/3:4/9:16/adaptive | 5 | 新任务只接受 2k |
| sora | seconds=4/8/12 | 720p/1024p/1080p | 9:16/16:9 | 最多 1 张 | model=sora-2/sora-2-pro；禁用 duration、generate_audio |

**其余能力要点**：
- video-lipsync：video_asset_id★ + audio_asset_id★（**均需当前账号已完成资产**）；quality=speed（默认）/ precision（**点数翻倍，报价时明示**）；dynamic_duration 默认 false；源视频 1~300 秒。
- cinematic-open-generate：prompt★；avatar_id 或 avatar_ids(1~3) 二选一；形象+参考图共享 9 个图位；参考视频 1~3 个；duration 默认 10s（4~15）；比例 9:16/16:9/1:1。
- cinematic-motion-generate：avatar_id★（cinematic 就绪形象）+ reference_video_upload_ids★（恰好 1 个）；输出 720p。
- tryon-fast：person_image_upload_id★ + clothes_upload_id★；seconds 默认 6（5~15）。
- tryon-classic：person_video_upload_id★；clothes_upload_id / background_upload_id 至少其一；seconds 默认 6（1~6）。

## ③ 默认策略与容错逻辑

1. **默认渠道**：用户未指定 channel → grok 720p（快、便宜）；用户点名的按其限制校验参数。
2. **默认参数**：lipsync 默认 quality=speed（precision 报价时明示双倍点数）；换装 seconds 默认 6。
3. **流程**：需求不清 → `prompt-optimize` 优化；→ 报价 → 报点数等确认 → 相同输入 + `--confirm` 提交一次（quote_token 由运行时自动附上，不要自己抄写） → job_id → `task` 轮询 → 交付。
4. **形象前置检查**：电影化身/动作模仿要求形象 `status=ready`（用 `video-avatars` 验证），未就绪 → needs_user_input 引导先建形象（数字人域）。
5. **失败容错**：同参数重试 1 次；仍失败 → 换渠道（grok → micro → omni）重新报价；再失败 → failed 说明原因。
6. **参考图超限**：参考图数量超过渠道上限 → 换支持更多参考图的渠道或请用户删减。
7. **资产引用**：lipsync 的 video_asset_id/audio_asset_id 必须是本人已完成资产（`assets` 可查），否则 needs_user_input。
8. **不自动重复扣点**：用户不满意 → 换渠道/时长重出，每次重出重新报价确认。
