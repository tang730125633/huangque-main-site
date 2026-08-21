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

`text-video`、`short-drama`、`pricing-page`、`invite`、`recharge` 和 `bots` 是页面入口：运行后只返回固定黄雀主站链接，除非再加 `--open-browser`，否则连浏览器都不会打开，更不会生成内容、创建订单或付款。设备授权页只由 `hq login` 的登录流程使用，不作为普通页面入口。

这批新增的直接 API 以安全读取为主：

- `digital-ip-projects`、`digital-ip-project`、`digital-ip-report`
- `text-video-capability`、`text-video-templates`、`text-video-styles`、`text-video-voices`
- `pricing`
- `inspiration-catalog`、`inspiration-likes`
- `leads-crm`、`video-avatars`、`audio-slots`
- `short-drama-projects`、`short-drama-project`、`short-drama-conversation`、`short-drama-preflight`

`inspiration-like` 和 `leads-crm-upsert` 会修改当前账号的数据，因此必须显式使用 `--confirm`；它们不会调用 AI 或扣点。

## 给 Agent 的安全工作流

1. 运行 `hq capabilities --json` 发现能力。
2. 运行 `hq describe <能力名> --json` 读取输入约束与副作用。
3. 准备 UTF-8 JSON，先执行只读或报价阶段。
4. 只有用户确认后，才执行带 `--confirm` 的写入；付费任务还必须复用同一输入与服务器返回的 `quote_token`。

```sh
printf '%s\n' '{"prompt":"一只金色黄雀","provider":"openai","ratio":"1:1","quality":"hd","count":1}' > image.json
hq run image-generate --input @image.json --json
# 用户核对费用后，再原样重试：
hq run image-generate --input @image.json --confirm --quote-token '<quote_token>' --json
```

项目同时提供可安装的 Codex Skill：[use-huangque-cli](skills/use-huangque-cli/SKILL.md)。

## 客户大白话对照

这些视频动作直接复用上面的服务器报价、确认和生成流程，不会在第一次命令时扣点或提交任务：

| 客户说法 | CLI 能力 | 必填输入 | 素材边界 |
|---|---|---|---|
| “保留原视频动作，只替换声音并让嘴型同步” | `video-lipsync` | `video_asset_id`、`audio_asset_id` | 两项都必须来自本人已完成资产；`speed` 便宜快速，`precision` 精度更高；默认保持原视频时长 |
| “用我的数字人形象和这段文案做一条口播视频” | `digital-ip-text-generate` | `avatar_id`、`text`、`voice` | 单个本人已就绪形象；不接收人物图片上传 |
| “用我的数字人形象和资产库这条音频做口播视频” | `digital-ip-audio-generate` | `avatar_id`、`audio_file` | `audio_file` 最长 500 字符且必须原样取自本人资产结果；不接收 URL、本机路径或音频上传 |
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

## 内容采集与获客

CLI 可以直接执行采集页和获客页的核心动作，不必先打开网页：

| 想做什么 | CLI 能力 | 输入 | 完成后去哪里拿结果 |
|---|---|---|---|
| 把一条内容的文案和评论采下来 | `collect-content` | 抖音或小红书公开内容 `url` | `task.result` 的完整文案和评论；`assets` 只存摘要 |
| 保存一条内容的原视频 | `collect-video` | 抖音或小红书公开内容 `url` | `assets` 的 `collect` 视频链接；`task.result` 也保留结果 |
| 提取视频里的口播文字 | `collect-transcript` | 抖音或小红书公开内容 `url` | `task.result` 的完整口播文字；`assets` 只记录是否已有口播 |
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
- 图片、视频、音频生成与提示词优化；`image-generate` 包含最多 14 张参考图的 Banana nb2/pro，`video-generate` 包含 Sora 2/Pro。
- 数字 IP 单条文案、本人资产音频与 2–5 个形象批量生成；电影化身开放式和动作模仿生成。
- 快速图片换装与经典视频换装。
- 私有图片/视频上传、画布创建、画布 Agent 方案与受限写入。
- 任务、流水、资产、音色、收藏与标签。
- 灵感案例与收藏、获客跟进、数字人形象、声音克隆槽位，以及短剧项目的安全读取。
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
- 上传只接受本人指定的 PNG/JPG/WebP 图片或 MP4/MOV/WebM 视频，要求绝对路径并拒绝符号链接；上传请求不回显本地路径和原始文件名。

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
