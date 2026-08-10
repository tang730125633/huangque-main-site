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

需要 Python 3.10+：

```sh
curl -fsSL https://raw.githubusercontent.com/tang730125633/huangque-cli/v0.7.0/install.sh | sh
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

`hq login` 使用浏览器设备授权。CLI 不接触账号密码或网页 Cookie；访问令牌只保存在本机 `~/.config/hq-cli/credentials.json`，权限为 `0600`，并可通过 `hq logout` 撤销。

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

## 当前能力

- 账号、点数、权限和渠道目录读取。
- Hermes IP12 项目、进度、报告与显式确认对话。
- 图片、视频、音频生成与提示词优化；`image-generate` 包含最多 14 张参考图的 Banana nb2/pro，`video-generate` 包含 Sora 2/Pro。
- 私有图片上传、画布创建、画布 Agent 方案与受限写入。
- 任务、流水、资产、音色、收藏与标签。
- 灵感案例与收藏、获客跟进、数字人形象、声音克隆槽位，以及短剧项目的安全读取。
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
- 上传只接受本人指定的 PNG/JPG/WebP，拒绝符号链接，不回显本地路径和原始文件名。

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
