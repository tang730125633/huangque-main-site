---
name: image-business
description: "Business rules for the Huangque image sub-agent: image business outcomes, hq capabilities, default engines and fallback logic."
short_description: 出图业务规则（image 域）。
short_description_zh: 黄雀出图子 Agent 业务规则：图片类业务结果、可用 hq 能力、默认引擎与容错。
version: 1
updated: 2026-09-02T00:00:00Z
---

# image-business：黄雀出图子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- 文生图：一句话/一段描述 → 1~4 张图片（海报、封面、头像、概念图等）。
- 图生图：参考图 + 描述 → 新图；多图：2 张及以上参考图（合影/多参考）→ 新图，**参考图必须全部带入，绝不丢参考图做纯文生图**。
- 局部重绘：蒙版 + 参考图（仅黄雀引擎 2/openai 支持）。
- 看图：描述一张图片的内容（image_url + 可选 question）。
- 提示词优化：口语化需求 → 生图提示词（kind=image）。
- 画布图片节点生成：生成结果交给画布域写回节点（本域只负责生成）。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| image-generate | 文生图/图生图/局部重绘 | 付费（先报价再确认） |
| image-upload | 私有参考图上传 → upload_id | 免费（需确认） |
| prompt-optimize | 提示词优化，kind=image | 不扣点，调外部 AI（需确认） |
| image | 图片工作台导航（只预填不提交） | 免费 |

**image-generate 关键参数**（以 `hq describe image-generate` 为准）：

| 参数 | 必填 | 取值/范围 |
| --- | --- | --- |
| prompt | ★ | 1~2000 字 |
| provider | | openai / xiaole / seedream / banana |
| model | | nb2 / pro（仅 provider=banana 有效） |
| variant | | std / pro（仅 provider=seedream 有效） |
| quality | | std / hd |
| count | | 1~4 |
| ratio | | 1:1 / 2:3 / 3:2 / 3:4 / 4:3 / 4:5 / 5:4 / 9:16 / 16:9 / 21:9 |
| image_upload_id | | 与 reference_upload_ids 互斥 |
| reference_upload_ids | | 数组；上限按引擎：openai=16、seedream=10、xiaole=4、banana=14 |
| mask_upload_id | | 仅 openai + PNG 蒙版 + count=1 + 需 image_upload_id |

## ③ 默认策略与容错逻辑

1. **默认引擎**：用户未指定 provider → `provider=banana + model=nb2`（纳米香蕉 2：快、便宜、中文理解强）；用户点名引擎则用点名的，并按其限制校验参考图数量。
2. **默认比例**：短视频场景 9:16；封面/头像 1:1；不确定时按场景取默认并在报价时说明。
3. **流程**：需求不清 → 先 `prompt-optimize` 优化；→ 报价（不带 --confirm）→ 把点数报给用户等确认 → 用完全相同输入 + `--confirm --quote-token` 提交恰好一次 → 拿 job_id → `task` 轮询到终态 → 交付资产。
4. **多张**：count 1~4 一次调用生成；超过 4 张分多次，每次重新报价确认。
5. **失败容错**：同参数重试 1 次；仍失败 → 换引擎按 banana → seedream → openai 顺序重新报价；再失败 → failed（retryable=false）并说明原因。
6. **参考图超限**：当前引擎参考图数量不够 → 换支持更多参考图的引擎（如 openai 16 张）。
7. **URL 转上传**：图生图收到 http 图片 URL 而无 upload_id → 先下载并 `image-upload` 拿 upload_id（>10MB 先压缩到最长边 1280 的 JPEG）。
8. **不自动重复扣点**：用户不满意 → 提示换引擎/换 model/改 ratio 重出，每次重出都必须重新报价并等确认。
9. **点数与价格**：只认服务端报价返回的 cost/points，不本地估算（banana 与其他引擎点数体系不同）。
