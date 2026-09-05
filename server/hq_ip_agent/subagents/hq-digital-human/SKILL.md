---
name: digital-human-business
description: "Business rules for the Huangque digital-human sub-agent: avatars, voices, talking videos, text-video, presenter and one-click digital human runs."
short_description: 数字人业务规则（digital-human 域）。
short_description_zh: 黄雀数字人子 Agent 业务规则：形象、音色、口播成片、文案成片、讲解员与一键生成。
version: 4
updated: 2026-09-03T12:00:00Z
---

# digital-human-business：黄雀数字人子 Agent 业务规则

> 能力目录以 `hq capabilities --json` 实时结果为准；下表是 2026-09-02 调研快照，冲突时以实时目录为准。CLI 用法细节一律走 `use-huangque-cli` 技能（其「Digital-human one-click runs」段是权威流程，字段路由表照它执行，不自己发明）。

## ① 负责哪些业务结果

- 数字人形象资产：创建形象（本人肖像）、形象列表、口播人物导入。
- 声音资产：音色列表、个人声音克隆（与音频域共享，克隆由音频域或本域按场景执行）。
- 数字人口播成片：单条文案 / 批量文案 / 本人音频驱动的数字人出镜视频。
- 文案成片（text-video）：文案 → 旁白 + 画面 + 模板的成片，可指定分镜启用人物口播。
- 数字人讲解员（presenter）：画布内的口播讲解项目（创建/更新/删除）。
- 数字人一键生成（oneclick + precision）：待 CLI 补全发布后接管；当前降级拆步执行。

## ② 能调哪些工具

**形象资产**

| 能力 id | 用途 | 要点 |
| --- | --- | --- |
| video-avatars | 数字人形象列表 | limit 1~120 |
| video-avatar-create | 创建数字人形象 | image_data★（jpg/png/webp data URL，本人正面清晰肖像，32KB~12MB）、name（1~40）；失败（无脸检测）不扣点 |
| text-video-avatar-import | 导入口播人物 | image_upload_id★ |
| image-upload | 肖像/参考图私有上传 | 免费（需确认） |

**声音资产**（与音频域共享）：audio-slots / voice-clone-create / voice-clone-status / voices / audio-upload。

**口播成片**

| 能力 id | 用途 | 要点 |
| --- | --- | --- |
| digital-ip-text-generate | 单条文案口播 | text★（1~1000，压平单行）、voice★；avatar_id 或 image_upload_id；motion=low/medium/high；ratio=9:16/16:9/1:1/4:5/5:4；subtitle(bool)/subtitle_position=top/upper/center/lower/bottom/subtitle_style=white/variety/bar；输出固定 1080p |
| digital-ip-audio-generate | 本人音频驱动口播 | avatar_id 或 image_upload_id **恰选其一** + audio_file（本人资产 mp3/wav/m4a）或 audio_upload_id **恰选其一** |
| digital-ip-batch-generate | 批量口播 | avatars★（2~5 个 ready 形象，可带 label）+ text★ + voice★，共享同一套参数 |

**文案成片（text-video）**

| 能力 id | 用途 | 要点 |
| --- | --- | --- |
| text-video-capability / templates / styles / voices | 状态/模板/样式/音色 | 先查后选 |
| text-video-plan | 规划口播分镜 | text★（2~1000）、template★、style★、voice★；mode=generate/fixed；ratio=0.1~0.5；speech_rate=0.5~2.0；返回 plan_id/source_hash |
| text-video-generate | 文案成片生成 | 需 talking_material（来自 text-video-plan 的 plan_id/source_hash/ratio/人物 asset_id/逐分镜 enabled）；首次调用只返回 scene_count+cost_breakdown 不扣点 |

**数字人讲解员（画布内）**：digital-presenter-capability（状态）/ digital-presenter-create（board_id★+request_id★+title/script_text≤20000/ratio/resolution=1080p/voice_key/target_duration=30~180）/ digital-presenter-project / update / delete（写需 revision）。

**一键生成（oneclick，普通模式已开通；用前验证实时目录）**：digital-human-oneclick-capability（状态/限制）/ plan（冻结方案，返回 plan_digest）/ consent（授权，绑定 run_id+plan_digest）/ audio-upload、material-upload（专属上传通道）/ start（报价→确认→提交）/ status / recover / abandon / history。字段路由表照官方 `use-huangque-cli` 的「Digital-human one-click runs」段执行（plan/start 不收 run_id；audio 模式先上传拿 audio_upload_id）。
**Precision 真人模式（digital-human-oneclick-precision-*）仍为 planned**：实时目录未出现前不得调用，也不得用普通 oneclick 冒充。

## ③ 默认策略与容错逻辑

1. **前置检查**：口播/文案成片前先确认：形象 `status=ready`（`video-avatars`）且音色存在（`voices`）；缺形象且用户要"自己的数字人" → 引导 `video-avatar-create`（需本人肖像，失败不扣点）；缺声音且要克隆 → 引导声音克隆（槽位 → 上传 → 克隆 → 就绪）。
2. **文案处理**：text 必须压平成单行（CLI 拒绝控制字符/换行）。
3. **默认路径**：单条文案 → `digital-ip-text-generate`；要分镜/模板成片 → `text-video-plan → text-video-generate`；批量多形象 → `digital-ip-batch-generate`；用户要"一键全流程"且 oneclick 可用 → 按官方 SKILL.md 字段路由表走 plan→consent→start→status；不可用 → 拆步（本域能力组合）并告知。
4. **付费流程**：一律先报价 → 报点数等确认 → 相同输入 + `--confirm` 提交一次（quote_token 由运行时自动附上，不要自己抄写） → job_id → `task` 轮询。
5. **授权**：肖像与声音必须用户明确授权；只接受本人素材、平台授权素材或用户批准的 AI 生成虚构素材，绝不推断他人授权。
6. **失败容错（区分两类）**：形象创建失败（无脸检测）不扣点，引导换清晰正面照；参数/素材类失败（形象未就绪、音色无效等）可重试 1 次同参数。**供应商超时退款**（`task` 报 error+refunded、错误含「超时」，如 HeyGen 生成超时）**不是普通失败：不要自动重试同参数**（重开一单=重新扣点）——按工具返回的 note 口径向用户说明：已全额退款（净扣 0）、供应商侧可能仍在渲染、成片可能稍后回主站原任务变 ready（站内可下载，CLI 读不到补回 URL）；是否重开由用户决定。
7. **讲解员**：create 需画布 board_id（与画布域协作）；update/delete 先读项目拿 revision。
8. **文案未确认绝不出片**：`digital-ip-text-generate` / `digital-ip-batch-generate` / `text-video-generate` 等任何生成调用前，必须已有一条用户确认的文案。用户没确认就先用 `attach_widgets` 注册三版 `script_pick` 让人点选，并以 finish(needs_user_input) 收尾问「文案用哪一版」；绝不允许带着未确认文案直接提交生成（页面上的「确认生成」按钮在文案未选时不可点，前端会兜底拦截，但你自己也不能越界先提）。

## ④ 前端交互卡片（不要只用文字）

页面会把以下内容渲染成可点击组件，用户**点选**后会以「【点选】<卡片标题>：<选项>」消息回来，把它当作用户的选择继续：

1. **形象查询自动成卡**：调用 `video-avatars` 后，运行时自动把形象列表渲染成缩略图卡片（id/名字/图片/状态），不用手动处理；回复用户时只需说「形象卡片在下方，点一下选一个」。**只推真人形象**：插画/原画/大师/patreon 类形象不是真人数字人素材（运行时已自动过滤），不要推荐、不要用于出片；优先引导「本人形象」。**卡片纪律**：只有用户明确要出片时才查询形象/音色（查询即渲染卡片），平时不要为了展示而查询；用户关掉卡片后不要重复查询注册同一批素材。
2. **音色查询自动成卡**：调用 `voices` / `text-video-voices` / `audio-slots` 后，运行时自动渲染音色卡片（带 ▶ 试听按钮），用户点卡片选中即可；**严禁让用户打字选音色**。`audio-slots` 返回的克隆槽位**没有名字字段**（只有 id 与试听），**严禁编造槽位名称**（如「苏芮音色」这类）；卡片统一显示「我的克隆音色」+ 创建日期，让用户点卡片 ▶ 试听来分辨。
3. **文案三版让用户点选**：口播/成片文案需要风格选择时，用 `attach_widgets` 注册一张 `script_pick` 卡片，items 给 3 个版本（id=A/B/C、title=风格名、summary=一句话卖点、body=全文）；用户点哪版就按哪版继续，别让用户回复"A/B/C"。用户没点选前按 ③-8 处理，绝不带着未确认文案出片。
4. **默认形象/音色自动勾选**：形象卡渲染时前端自动勾选「本人形象」，音色卡自动勾选「本人声音/我的克隆音色」（只自动选一次，用户撤掉不选回）；你不用在回复里替用户做选择，只需按 ③-8 收口。
5. **需要图片选形象时**：用户上传的图片（页面输入框贴图）会随消息给出服务器本地路径；用 `hq_run` 的 `file_path` 参数把它交给上传类能力（如 `image-upload`、`digital-human-oneclick-material-upload`、`video-avatar-create` 所需的 data URL 则先用本地读取转 base64），不要向用户索要路径。
6. **finish 的 summary 简短**：卡片已展示内容时，summary 只写「已展示 N 个形象/音色，等用户点选」，不抄列表。
