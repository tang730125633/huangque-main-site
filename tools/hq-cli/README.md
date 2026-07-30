# 黄雀主站 HQ CLI 0.3

`hq` 是给用户和 Agent 使用的黄雀主站命令行工具。它固定连接
`https://huangquechuanmei.com`，不接受自定义服务器、任意 HTTP 方法、密码或 Cookie。

## 一条命令安装

```sh
curl -fsSL https://huangquechuanmei.com/downloads/hq/install.sh | sh
```

需要 Python 3.9+。安装脚本会校验版本化 wheel 的 SHA-256，把程序放在
`~/.local/share/hq-cli/0.3.0/`，并创建 `~/.local/bin/hq`。如果该目录不在 PATH，按安装结果提示补一次即可。

## 给一个没有上下文的 Agent

只要把安装命令和下面四步发给它：

```sh
hq login --json
hq status --json
hq capabilities --json
hq describe ip12-projects --json
```

`hq login` 使用浏览器设备授权：用户在黄雀主站登录并查看权限后同意，CLI 不接触账号密码或网页 Cookie。
访问令牌仅保存在本机 `~/.config/hq-cli/credentials.json`，权限为 `0600`，8 小时后失效；`hq logout` 会在服务端撤销。
从 V0.2 升级后需重新执行一次 `hq login`，才能取得新增的 `ip12:chat` 和 `assets:upload` 权限。

## 能力

- 读取账号资料、点数和授权范围。
- 创建/读取主站当前 Hermes IP12 诊断项目，读取基础资料、对话、进度和已有模块报告；授权中包含独立 `ip12:chat` 权限，显式确认后可继续一轮诊断对话。
- 真实调用图片或视频提示词优化。
- 创建并读取画布，可把提示词放入首个文本节点。
- 读取任务详情、点数流水、图片/音频/视频等资产与可用音色；可收藏资产并管理标签。
- 流式上传本人本地 PNG/JPG/WebP，得到短期私有 `upload_id`，用于单参考图、果肉多参考图或 OpenAI PNG 蒙版生成。
- 图片、视频、音频生成：先取服务器报价，再以相同输入、`quote_token` 和 `--confirm` 二次提交并扣点。
- 返回黄雀主站各工作台的安全深链接。

所有能力和参数均可机器读取：

```sh
hq capabilities --json
hq describe canvas-create --json
hq describe ip12-message --json
```

输入只接受 UTF-8 JSON 对象文件或标准输入，最多 64 KiB：

```sh
printf '%s\n' '{"name":"客户内容规划","prompt":"为餐饮老板规划一周短视频"}' > canvas.json
hq run canvas-create --input @canvas.json --confirm --json
```

图片字节不进入这段 JSON。先显式上传绝对路径文件（单图最多 10 MiB）：

```sh
hq run image-upload --file /absolute/path/reference.png --confirm --json
```

上传不扣点、不返回公开 URL，结果中的 `upload_id` 默认一小时失效；每个账号同时最多保留 8 个、合计 60 MiB。把它写入图片生成参数：

```sh
printf '%s\n' '{"prompt":"保持人物一致，生成电影海报","provider":"openai","image_upload_id":"img_替换为上传结果","ratio":"9:16","quality":"hd","count":1}' > image.json
hq run image-generate --input @image.json --json
# 核对报价后，原样重试：
hq run image-generate --input @image.json --confirm --quote-token '<quote_token>' --json
```

果肉多参考图使用最多 4 项的 `reference_upload_ids`；OpenAI 局部修改同时传 `image_upload_id` 和 PNG `mask_upload_id`。提交返回 `job_id`，用已有 `task` 能力轮询，任务完成后的 `result.url` / `result.urls` 就是成品地址。

继续 IP12 对话会写入本人项目并调用 AI，必须显式确认：

```sh
printf '%s\n' '{"project_id":"项目ID","message":"我的核心客户是本地餐饮老板","request_id":"turn-20260730-001"}' > ip12-message.json
hq run ip12-message --input @ip12-message.json --confirm --json
```

同一轮网络超时只能原样复用同一个 `request_id`；新一轮必须换新值。若返回“结果未知”，先读取项目，避免重复写入。

付费生成必须分两次：

```sh
printf '%s\n' '{"prompt":"一只金色黄雀","provider":"openai","ratio":"1:1","quality":"hd","count":1}' > image.json
hq run image-generate --input @image.json --json
# 核对返回的 cost、points 和 quote_token 后：
hq run image-generate --input @image.json --confirm --quote-token '<quote_token>' --json
```

报价绑定账号、能力、完整输入、价格和有效期；改参数、过期或服务端价格变化都会拒绝扣点并要求重新报价。

## 稳定边界

- 这是黄雀主站 CLI，不是泽龙 CLI；只有 `main` 环境。
- Agent 只能运行内置能力，不能借 CLI 请求任意 URL 或旧业务 API。
- 读取、外部 AI、普通写入、付费写入分别声明；IP12 对话使用独立 `ip12:chat` 权限，提示词优化、创建 IP12/画布和付费生成都要求显式确认。
- 本地图片只经固定主站上传端点进入当前账号的临时私有区；CLI 不读取目录、不上传符号链接、不回显本地路径或原始文件名。
- V0.3 支持资产收藏和标签，不提供删除资产、删除项目、管理员接口、充值或批量破坏性操作。
- 成功与错误都输出一个带 `schema`、`cli_version` 的 JSON；用进程退出码判断结果。
