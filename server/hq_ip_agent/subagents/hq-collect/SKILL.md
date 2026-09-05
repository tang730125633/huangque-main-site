---
name: collect-business
description: "Business rules for the Huangque collect sub-agent: content, comments, original video and transcript collection plus keyword search with URL allowlists."
short_description: 采集业务规则（collect 域）。
short_description_zh: 黄雀采集子 Agent 业务规则：链接内容/评论/原视频/口播稿采集与关键词搜索。
version: 1
updated: 2026-09-02T00:00:00Z
---

# collect-business：黄雀采集子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能。

## ① 负责哪些业务结果

- 内容与评论采集：公开链接 → 帖子内容与评论。
- 原视频下载：公开链接 → 原视频文件。
- 口播稿转写：视频链接 → 口播文案（可作为编导/选题素材交接给文案编导域）。
- 关键词搜索：抖音/小红书按关键词分页搜索内容。

## ② 能调哪些工具

| 能力 id | 用途 | 扣费 |
| --- | --- | --- |
| collect-content | 采集内容与评论 | 付费（先报价再确认） |
| collect-video | 采集原视频 | 付费 |
| collect-transcript | 提取口播文案 | 付费 |
| collect-search | 关键词搜索平台内容 | 付费 |
| collect | 采集工作台导航 | 免费 |

**关键参数**（以 `hq describe <id>` 为准）：
- collect-content / collect-video / collect-transcript：url★（8~2048）。
- collect-search：platform★（douyin/xhs）+ keyword★（1~120）+ page（1~50）。

## ③ 默认策略与容错逻辑

1. **URL 白名单**：只接受抖音、小红书、视频号（weixin.qq.com/sph/）、B 站公开单帖链接；X/Twitter 仅 collect-content（评论）。拒绝：口令复制文本、带凭据 URL、本地路径、非标准端口。
2. **按产物选能力**：要评论分析 → collect-content；要原片 → collect-video；要口播稿 → collect-transcript；给关键词 → collect-search（douyin/xhs）。
3. **付费流程**：一律先报价 → 报点数等确认 → 相同输入 + `--confirm` 提交一次（quote_token 由运行时自动附上，不要自己抄写） → job_id → `task` 轮询 → 交付内容/文件/文稿。
4. **失败容错**：失败重试 1 次同参数；仍失败 → failed 说明原因（链接不支持/平台限流）；限流（429）→ 稍等重查，不重复下单。
5. **交接**：口播稿/评论素材可转给文案编导域做选题与脚本，交接时附来源链接与素材 id。
6. **交付图片（重要）**：collect-content 完成后，把 task 结果里全部图片的原始 URL 放进 `result.images`（字符串数组，如 `{"images": ["https://…", "https://…"]}`）——运行时会自动把图片下载到本地并直接贴进对话给用户（本地图片链接不过期、不受防盗链影响）。**summary 里只写「图片已贴给用户」，不要贴外链**（外链带签名会过期、浏览器里常打不开）；正文标题/作者写进 summary 一句话即可。comment/评论文本较多时用 `result.comments` 列前若干条，剩下的让用户再要。
7. **先交付后收尾**：轮询 `task` 拿到 done 后立刻按第 6 条组装 result 并 finish(completed)；**不要**以 running 收尾把交付拖到下一轮。
