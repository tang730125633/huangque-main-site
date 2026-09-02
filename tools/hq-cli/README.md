<p align="center">
  <img src="./assets/readme/hero-zel-v1.webp" width="100%" alt="Zel and the orange cat guiding discovered capabilities through a quote, confirmation, task, and result gate">
</p>

# HQ CLI

[![CI](https://github.com/tang730125633/huangque-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/tang730125633/huangque-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
[![License: MIT](https://img.shields.io/badge/License-MIT-c65b3e.svg)](LICENSE)

把黄雀主站的能力目录、参数约束和任务操作带到命令行，让人和 Agent 使用同一套可检查、可确认的入口。

```text
$ hq doctor --json
{"checks":[{"service":"auth","status":"ok"}, ...], "schema":"hq.doctor/v1"}

$ hq capabilities --json
{"capabilities":[...], "schema":"hq.capabilities/v1"}
```

## 为什么做成 CLI

- 先发现能力，再读取严格参数，不让 Agent 猜接口。
- 读取、普通写入、外部 AI 和付费操作使用不同安全门槛。
- 所有结果都带稳定 JSON schema 和退出码，方便脚本与 Agent 判断。
- 固定连接黄雀主站，不接受任意服务器、HTTP 方法、密码或 Cookie。

## 安装

需要 Python 3.10+。

Windows 10/11（PowerShell 5.1 或 7）：

```powershell
irm https://huangquechuanmei.com/downloads/hq/install.ps1 | iex
```

安装后重新打开 PowerShell，运行 `hq version --json`。程序安装到 `%LOCALAPPDATA%\Huangque\hq-cli`，安装器会幂等更新当前用户 PATH。卸载时下载同版本 `uninstall.ps1` 后运行；默认保留登录凭据，加 `-PurgeCredentials` 才会删除。

macOS / Linux：

```sh
curl -fsSL https://huangquechuanmei.com/downloads/hq/install.sh | sh
```

安装脚本会校验版本化 wheel 的 SHA-256，将程序放到 `~/.local/share/hq-cli/`，并创建 `~/.local/bin/hq`。

## 第一次使用

```sh
hq version --json
hq doctor --json
hq login --json
hq status --json
hq capabilities --json
hq describe ip12-projects --json
```

`hq login` 使用浏览器设备授权。CLI 不接触账号密码或网页 Cookie；访问令牌在 macOS/Linux 保存到权限为 `0600` 的 `~/.config/hq-cli/credentials.json`，在 Windows 保存到 `%APPDATA%\Huangque\hq-cli\credentials.json` 并由当前 Windows 用户的 DPAPI 加密。可通过 `hq logout` 撤销。

## 页面入口不等于直接执行

`text-video`、`matrix-template`、`short-drama`、`pricing-page`、`invite`、`recharge` 和 `bots` 是页面入口：运行后只返回固定黄雀主站链接，除非再加 `--open-browser`，否则连浏览器都不会打开，更不会生成内容、创建订单或付款。设备授权页只由 `hq login` 的登录流程使用，不作为普通页面入口。

这批新增的直接 API 以安全读取为主：

- `digital-ip-projects`、`digital-ip-project`、`digital-ip-report`
- `text-video-capability`、`text-video-templates`、`text-video-styles`、`text-video-voices`
- `pricing`
- `inspiration-catalog`、`inspiration-likes`
- `leads-crm`、`video-avatars`、`audio-slots`
- `short-drama-projects`、`short-drama-project`、`short-drama-conversation`、`short-drama-preflight`

`inspiration-like` 和 `leads-crm-upsert` 会修改当前账号的数据，因此必须显式使用 `--confirm`；它们不会调用 AI 或扣点。

## 0.15.0 网页能力对等

0.15.0 把目录扩展到 247 项：正常用户网页中已有后端 API 的 Creator Agent、邀请、通知、好友、账号资料、资产批量操作、画布成员、数字 IP 报告、短剧项目/剧本/角色/场景/自动制作/精修，以及头像、H3 视频、ZIP、PDF 专用传输均可直接调用。每项使用固定主站路由和独立严格 schema，不提供任意 URL/方法代理。

修改密码、点数转赠和最终付款仍交给浏览器：这些流程需要密码或支付确认，CLI 不接收密码、Cookie、支付凭据或 OTP。网页本地的主题偏好、剪贴板、二维码与 JPG 导出也不伪装成服务器 API。

## 给 Agent 的安全工作流

1. 运行 `hq capabilities --json` 发现能力。
2. 运行 `hq describe <能力名> --json` 读取输入约束与副作用。
3. 准备 UTF-8 JSON，先执行只读或报价阶段。
4. 只有用户确认后，才执行带 `--confirm` 的写入；付费任务还必须复用同一输入与服务器返回的 `quote_token`。

从 0.11.0 起，每个 capability 都包含 `agent` 字段：

- `resource`、`operation`：告诉 Agent 当前是 list/get/create/update/delete/execute 还是页面导航。
- `required_inputs`：说明每个必填 ID 应从哪个读取或上传能力取得。
- `resource_operations`、`missing_crud`：列出同一资源已有和缺失的 CRUD。
- `website_operations`、`website_access`：说明网页对应操作和真实执行方式；API 支持的正常用户动作直接调用，密码/支付/浏览器本地动作明确交接。
- `workflow`、`success_evidence`、`recovery`：约束报价、确认、幂等、轮询和失败恢复。

例如 `ip12-project` 会同时告诉 Agent：先用 `ip12-projects` 取 ID，可用 `ip12-create` 创建、`ip12-message` 更新、`ip12-delete` 删除。删除必须先读取目标并显式确认。

```sh
printf '%s\n' '{"prompt":"一只金色黄雀","provider":"openai","ratio":"1:1","quality":"hd","count":1}' > image.json
hq run image-generate --input @image.json --json
# 用户核对费用后，再原样重试：
hq run image-generate --input @image.json --confirm --quote-token '<quote_token>' --json
```

## Omni 参考图视频

先把用户明确指定的 JPEG、PNG 或 WebP 上传为临时私有 `upload_id`，再把该 ID 写入 Omni 请求。Omni 支持 1-6 张参考图、`9:16` 或 `16:9`、3-10 秒，当前固定输出 `720p`。

```sh
hq describe image-upload --json
hq run image-upload --file /absolute/path/reference.png --confirm --json

cat > omni-video.json <<'JSON'
{
  "channel": "omni",
  "prompt": "Use @图片1 as the identity and opening composition.",
  "ratio": "9:16",
  "duration": 5,
  "resolution": "720p",
  "reference_upload_ids": ["<upload_id>"]
}
JSON

hq describe video-generate --json
hq run video-generate --input @omni-video.json --json
# 用户核对报价后，原样复用同一输入：
hq run video-generate --input @omni-video.json --confirm --quote-token '<quote_token>' --json
hq run task --input @- --json <<'JSON'
{"job_id": 123}
JSON
```

上传成功但报价返回参考图格式错误时，不要反复上传、转换格式或绕过 CLI 调用私有接口；保留 `upload_id`、MIME 和 SHA-256 作为证据并报告服务端兼容问题。报价失败不会扣点。

## 文案成片

先读取当前可用模板、素材风格和音色，再使用同一份 UTF-8 JSON 完成报价与确认提交：

```sh
hq run text-video-templates --json
hq run text-video-styles --json
hq run text-video-voices --json

cat > text-video.json <<'JSON'
{
  "text": "AI 培训如何帮助团队提高工作效率",
  "template": "1080x1920/image_default.html",
  "mode": "generate",
  "style": "realistic_commercial",
  "voice": "public:zh-CN-YunjianNeural",
  "speech_rate": 1.0
}
JSON

hq run text-video-generate --input @text-video.json --json
# 核对 scene_count、cost_breakdown 和 cost 后确认：
hq run text-video-generate --input @text-video.json --confirm --quote-token '<quote_token>' --json
hq run task --input @- --json <<'JSON'
{"job_id": 123}
JSON
```

`mode=generate` 根据主题创作，`mode=fixed` 原样使用完整文案并自动拆分分镜。

## 模板成片

模板成片使用固定的“顶部标题 + 平台素材 + 底部行动文案”结构，不调用 AI 生图或视频模型。先读取服务状态和模板目录：

```sh
hq run matrix-template-capability --json
hq run matrix-template-templates --json
```

从目录选择一个 `template_id`；需要指定字体时，同时复制 `fonts[].value` 作为可选 `font_family`。准备 UTF-8 JSON：

```sh
cat > matrix-template.json <<'JSON'
{
  "top_text": "真正拉开差距的，不是工具",
  "bottom_text": "评论区留下关键词，领取完整方案",
  "template_id": "full-overlay-bold",
  "font_family": "AaHouDiHei"
}
JSON

hq run matrix-template-generate --input @matrix-template.json --json
# 核对固定点数报价后，用完全相同的输入确认提交：
hq run matrix-template-generate --input @matrix-template.json --confirm --quote-token '<quote_token>' --json
hq run task --input @- --json <<'JSON'
{"job_id": 123}
JSON
```

时长由文案自动计算，背景音乐默认开启，素材固定来自平台已审核素材库。拿到 `job_id` 后只轮询 `task`，不要再次提交生成命令。

同一文案与模板需要一次生成 2–5 条时，增加 `count` 并使用批量能力：

```json
{
  "top_text": "真正拉开差距的，不是工具",
  "bottom_text": "评论区留下关键词，领取完整方案",
  "template_id": "full-overlay-bold",
  "font_family": "AaHouDiHei",
  "count": 5
}
```

```sh
hq run matrix-template-batch-generate --input @matrix-template-batch.json --json
# 核对总价、单价和 count 后，只确认一次：
hq run matrix-template-batch-generate --input @matrix-template-batch.json --confirm --quote-token '<quote_token>' --json
```

批量确认返回 `job_ids`；每个子任务仍沿用单条模板成片的幂等、失败退款和资产合同，只轮询这些原始 Job，不重新提交整批。

需要混入口播视频素材时，先上传并导入一个或多个人物，再生成分镜方案：

```sh
hq run image-upload --file /absolute/path/avatar.png --confirm --json
printf '%s\n' '{"image_upload_id":"img_<32位十六进制>"}' > avatar.json
hq run text-video-avatar-import --input @avatar.json --confirm --json

cat > talking-plan.json <<'JSON'
{
  "text": "完整文案",
  "template": "1080x1920/image_default.html",
  "mode": "fixed",
  "style": "realistic_commercial",
  "voice": "public:zh-CN-YunjianNeural",
  "speech_rate": 1.0,
  "ratio": 0.3
}
JSON
hq run text-video-plan --input @talking-plan.json --confirm --json
```

核对返回的 `scenes` 后，将 `plan_id`、`source_hash`、人物 `asset_id` 和逐镜头选择加入原生成参数：

```json
{
  "talking_material": {
    "enabled": true,
    "plan_id": "talking_plan_<32位十六进制>",
    "source_hash": "<64位十六进制>",
    "ratio": 0.3,
    "default_avatar_asset_id": "local_avatar_<32位十六进制>",
    "scenes": [
      {"scene_id": "scene_01", "enabled": true},
      {"scene_id": "scene_02", "enabled": false},
      {"scene_id": "scene_03", "enabled": true, "avatar_asset_id": "local_avatar_<32位十六进制>"}
    ]
  }
}
```

把 `talking_material` 合并到与规划时完全一致的文案成片 JSON，再执行原有报价和确认命令。人物与方案均为当前账号私有的短期资产；最终提交前仍会校验方案、人物、参数、分镜和价格。

## Agent Skill 与 MCP

Agent 使用方法由独立公开仓库 [`huangque-agent-skill`](https://github.com/tang730125633/huangque-agent-skill) 维护，避免在 CLI 仓库复制第二份 Skill。CLI 0.12.0 起可安装同一份版本化 Skill：

```sh
hq skill install deepseek
hq skill install codex
hq skill install openclaw
hq skill install pi
```

标准 MCP 服务由当前 CLI 直接提供：

```json
{
  "mcpServers": {
    "huangque": {
      "command": "hq",
      "args": ["mcp"]
    }
  }
}
```

`hq skill install mcp` 返回同一配置。MCP 根据当前版本的固定能力目录生成带参数约束的工具，不提供任意命令执行；写入、上传与付费确认规则和 CLI 完全相同。

## 客户大白话对照

这些视频动作直接复用上面的服务器报价、确认和生成流程，不会在第一次命令时扣点或提交任务：

| 客户说法 | CLI 能力 | 必填输入 | 素材边界 |
|---|---|---|---|
| “保留原视频动作，只替换声音并让嘴型同步” | `video-lipsync` | `video_asset_id`、`audio_asset_id` | 两项都必须来自本人已完成资产；`speed` 便宜快速，`precision` 精度更高；默认保持原视频时长 |
| “用我的数字人形象或临时人物照片和这段文案做口播视频” | `digital-ip-text-generate` | `avatar_id` 或 `image_upload_id`、`text`、`voice` | 二选一；临时照片先通过 `image-upload` 上传 |
| “用我的形象和已有/临时音频做口播视频” | `digital-ip-audio-generate` | `avatar_id` 或 `image_upload_id`；`audio_file` 或 `audio_upload_id` | 两组分别二选一；临时素材先上传，不接收 URL、本机路径或 base64 |
| “让 2–5 个我的数字人分别讲同一段文案” | `digital-ip-batch-generate` | `avatars`、`text`、`voice` | `avatars` 每项是本人已就绪的 `avatar_id`，可带 `label`；共用文案、音色和字幕设置 |
| “让 1–3 个电影化身按描述生成，也可以参考我的图或视频” | `cinematic-open-generate` | `avatar_id` 或 `avatar_ids`、`prompt` | 形象和参考图共用 9 张额度：1/2/3 个形象最多再传 8/7/6 个图片 `upload_id`；另可传 3 个视频 `upload_id`；时长 4–15 秒 |
| “让我的电影化身模仿这段视频的动作” | `cinematic-motion-generate` | `avatar_id`、`reference_video_upload_ids` | 必须且只能放 1 个本人短期私有视频 `upload_id` |
| “用人物照片和衣服图快速做换装视频” | `tryon-fast-generate` | `person_image_upload_id`、`clothes_upload_id` | 两项都先通过 `image-upload` 上传；时长 5–15 秒 |
| “以人物视频为底片，更换衣服或背景” | `tryon-classic-generate` | `person_video_upload_id` | 衣服图、背景图至少提供一项；人物视频先通过 `video-upload` 上传；时长 1–6 秒 |

先用 `hq run assets --input @assets.json --json` 查本人资产中的 `audio_file`，用 `hq run video-avatars --json` 查本人可用的 `avatar_id`。本地参考视频只接受 MP4、MOV 或 WebM，使用绝对路径、最大 32 MiB，并需显式确认：

```sh
hq run video-upload --file /absolute/path/reference.mp4 --confirm --json
```

Windows PowerShell 使用完整驱动器路径，例如：

```powershell
hq run video-upload --file "C:\Users\Alice\Videos\reference.mp4" --confirm --json
```

上传只取得短期私有 `upload_id`；真正生成仍需先获取报价，再以完全相同的输入携带 `--confirm --quote-token` 提交。

## 音频上传与声音克隆

本地样音先通过专用上传能力取得当前账号私有的 `upload_id`，再把它作为 `audio_upload_id` 使用；有效期以返回的 `expires_in` 为准。CLI 只接受绝对路径，不会把本机路径或原文件名发给服务器：

```sh
hq run audio-slots --json
hq run audio-upload --file /absolute/path/voice-sample.mp3 --confirm --json
```

从 `audio-slots` 复制可用 `slot_id`，再把上传结果写入克隆输入：

```json
{
  "slot_id": "slot_<当前账号槽位>",
  "name": "我的克隆音色",
  "audio_upload_id": "aud_<32位十六进制>"
}
```

```sh
hq run voice-clone-create --input @voice-clone.json --confirm --json
hq run voice-clone-status --input @- --json <<'JSON'
{"slot_id":"slot_<当前账号槽位>"}
JSON
```

状态为 `ready` 后调用 `voices` 取得 `voice_key`，再用于 `audio-generate`。音频上传支持 MP3、WAV、M4A、AAC、OGG，最大 10 MiB、最长 300 秒；声音克隆会在服务端规范化最多 60 秒清晰语音。为避免供应商判断“有效语音太短”，克隆样音应包含 30–60 秒连续、清晰、单人说话，文件总时长不能代替有效语音时长。若状态为 `failed`，先读取原槽位错误；有效语音不足时上传新的合格样音，并使用新的 `audio_upload_id` 发起新操作。上传和克隆本身不扣点，使用已有音色生成语音仍须先报价再确认。

## 编导工作流

CLI 可以直接调用主站编导的 AI 脚本生成、公开链接拆解、工作流和成品生成，不再只是打开 `/workbench/script`：

```sh
hq run director-capability --json
hq describe director-script-generate --json
hq describe director-breakdown --json
hq describe director-breakdown-upload --json
hq describe director-scene-image-generate --json
hq describe director-scene-video-generate --json
hq describe director-scene-talking-generate --json
hq describe director-workflow-create --json
hq describe director-production-plan --json
hq describe director-remake-plan --json
```

`director-script-generate` 接收 `prompt`，以及可选的 `style`、`duration`、`platform`；`director-breakdown` 接收一个 `url` 或最多五条 `urls`。单镜头图片、剧情视频和口播视频都复用主站已有报价与生成能力，先报价，再以完全相同输入、`quote_token` 和 `--confirm` 提交一次。

已完成的 `copy` / `breakdown` 任务或显式分镜可以创建本人工作流。分镜更新必须携带当前 `revision`，冲突时拒绝覆盖。`director-production-plan` 和 `director-remake-plan` 冻结工作流 revision、输入与价格；启动时必须复用返回的 `plan_digest`、`quote_token` 和唯一 `request_id`。每次生产运行生成一个成品，网络结果不确定时只查状态或恢复原运行，不新建第二个付费任务。

本地图片或视频反推必须先报价，首次调用只在本地校验文件并计算 SHA-256，不上传文件：

```sh
hq run director-breakdown-upload --file <绝对路径> --json
```

审核返回的 `cost` 后，复用同一文件和 `quote_token`，并把该费用作为 `--expected-cost` 明确确认：

```sh
hq run director-breakdown-upload --file <绝对路径> --confirm \
  --quote-token <quote_token> --expected-cost <cost> --json
```

CLI 会为同一 `quote_token` 生成稳定的 `Idempotency-Key`。若上传响应不确定，必须用同一文件、同一报价令牌和同一费用重试；不要重新报价。拿到 `job_id` 后只使用 `task` 轮询。

Precision 数字人的多阶段完整运行仍按实时契约显示为 planned；这不影响本节已经开放的编导视频、口播、生产和复刻动作。

## 短剧五阶段与无水印下载

短剧项目支持顾问对话、角色定妆、预飞计划、自动草稿、交付与完结。每个确认或启动动作都必须使用 `hq describe` 返回的输入契约；付费阶段先报价，再复用原输入、报价令牌和请求 ID。状态不确定时只轮询原项目或运行，不重复提交。

本人资产或黄雀允许的结果链接可用 `dl` 下载到绝对路径：

```sh
hq describe dl --json
hq run dl --input @download.json --output /absolute/path/result.mp4 --json
```

下载固定访问黄雀主站，不跟随重定向，不覆盖已有文件；临时文件校验并原子落盘，解码密钥只放请求头，不写入 URL。

## 内容采集与获客

CLI 可以直接执行采集页和获客页的核心动作，不必先打开网页：

| 想做什么 | CLI 能力 | 输入 | 完成后去哪里拿结果 |
|---|---|---|---|
| 把一条内容的文案和评论采下来 | `collect-content` | 抖音、小红书、视频号、B 站或 X 单帖公开 `url` | `task.result` 的完整文案和评论；`assets` 只存摘要 |
| 保存一条内容的原视频 | `collect-video` | 抖音、小红书、视频号或 B 站公开内容 `url` | `assets` 的 `collect` 视频链接；`task.result` 也保留结果 |
| 提取视频里的口播文字 | `collect-transcript` | 抖音、小红书、视频号或 B 站公开内容 `url` | `task.result` 的完整口播文字；`assets` 只记录是否已有口播 |
| 按关键词搜索平台内容 | `collect-search` | `platform=douyin|xhs`、`keyword`，可选 `page` | `task.result` 的任务结果 |
| 从多平台评论里筛选潜在客户 | `leads-generate` | 平台，以及对应的关键词 / 视频号目标；数量和页数可选 | `assets` 的 `leads` 资产 |

这五项都是付费异步任务：先运行一次看报价，用户确认后用**完全相同的 JSON**和返回的 `quote_token` 提交一次。拿到 `job_id` 后只轮询任务，不要再次提交。三个链接采集任务完成后都会写入资产库，但资产只保存摘要和视频链接；完整评论与口播文字必须从 `task.result` 读取。关键词搜索结果直接保留在 `task.result`，获客结果同时写入完整的 `leads` 资产：

```sh
printf '%s\n' '{"url":"https://v.douyin.com/abc123/"}' > collect.json
hq run collect-video --input @collect.json --json
hq run collect-video --input @collect.json --confirm --quote-token '<quote_token>' --json

printf '%s\n' '{"job_id":123}' > task.json
hq run task --input @task.json --json
printf '%s\n' '{"kind":"collect","limit":20}' > assets.json
hq run assets --input @assets.json --json
```

三个按链接采集的能力只接受完整的抖音 / 小红书公开 HTTP(S) URL，端口只能省略或使用 80 / 443；不接受口令、分享文案、账号密码、本机路径或其他网站链接。`leads-generate` 支持 `douyin`、`xhs`、`channels`：包含抖音或小红书时必须提供 `keyword`，包含视频号时必须提供 `channels_targets`，混合平台时两者都要提供；`count` 和 `pages` 可省略。

## 当前能力

- 账号、点数、权限和渠道目录读取。
- Hermes IP12 项目、进度、报告与显式确认对话。
- 图片、视频、音频、文案成片和平台素材库模板成片生成与提示词优化；`image-generate` 包含最多 14 张参考图的 Banana nb2/pro，`video-generate` 包含 Sora 2/Pro。
- 数字 IP 单条文案、本人资产音频与 2–5 个形象批量生成；电影化身开放式和动作模仿生成。
- 快速图片换装与经典视频换装。
- 私有图片/视频/音频上传、画布创建、画布 Agent 方案与受限写入。
- 任务、流水、资产、音色、收藏与标签。
- 灵感案例与收藏、获客跟进、数字人形象、声音克隆槽位，以及短剧五阶段生产。
- 编导工作流、分镜更新/导出、单镜头生成、冻结方案生产、同款复刻与无水印下载。
- 抖音 / 小红书内容、原视频、口播文案和关键词结果采集，以及多平台评论获客。
- 一键成片项目的创建、分析、审核与渲染。
- 数字人口播项目的能力检查、创建、读取与基础设置。
- 黄雀主站工作台的安全深链接。

精确能力、参数和副作用以当前 CLI 输出为准：

```sh
hq capabilities --json
hq describe <能力名> --json
```

## 安全边界

- 只允许内置能力和固定黄雀主站路径，拒绝任意 URL 与跨域重定向。
- 外部 AI 和写操作需要显式确认；付费生成必须先报价再确认。
- 幂等写入保留 `request_id`，并发更新保留 `revision` / `base_version`。
- 不提供管理员、自动充值或付款、批量删除、任意文件读取或任意 HTTP 请求能力。
- 上传只接受本人指定的 PNG/JPG/WebP 图片、MP4/MOV/WebM 视频或 MP3/WAV/M4A/AAC/OGG 音频，要求绝对路径并拒绝符号链接；上传请求不回显本地路径和原始文件名。

## 本地开发

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/hq version --json
```

CLI 客户端源码位于本仓库；服务端权限、计费和任务实现仍由黄雀主站维护。

## License

[MIT](LICENSE) © 2026 Tang Zelong

“黄雀”名称与品牌标识归其权利人所有。
