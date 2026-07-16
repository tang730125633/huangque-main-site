# OpenClaw Agents 完整架构（广州服务器）

> 盘点时间：2026-06-23 ｜ 来源：远程实查服务器 `openclaw.json` + `openclaw channels status` + agents 目录 + `ps aux`
> ⚠️ 本文档**只记录架构、配置与 App ID**。飞书 App Secret、服务器密码、中转站 Token 等**一律不入库**（见文末"机密存放位置"）。

## 服务器
- 腾讯云 CVM（广州），公网 `129.204.166.13`，登录用户 `ubuntu@`，主机名 `VM-0-15-ubuntu`，Ubuntu 22.04（4 核 / 3.6G 内存 / 59G 盘 → 实际 118G / 30%）
- OpenClaw 版本：`2026.6.6`，安装路径：`/home/ubuntu/.npm-global/lib/node_modules/openclaw`
- 多开方式：**多 home 隔离**，每套独立数据目录 + 独立网关端口

---

## 总览：3 实例 / 24 Agent / 4 飞书 App（全部 `running` ✅）

| # | 实例 (home) | 网关端口 | Agent 数 | 进程 PID | 磁盘占用 | 飞书 App 数 |
|---|------------|---------|---------|---------|---------|------------|
| 1 | `.openclaw`（主） | **18789** | 2 | 833797 | 341M | **2** |
| 2 | `.openclaw-second` | **1890** | 11 (+ 残留 11) | 1085586 | 54M | 1 |
| 3 | `.openclaw-visual` | **1891** | 11 | 2053261 | 77M | 1 |

> 跨实例合计：**24 个已激活 Agent / 4 个飞书渠道全部 `running`**。`bot_bot*` 与 `visual` 为残留目录，未在 agents.list 中激活。

---

## 实例 1：`.openclaw`（主实例） — gateway :18789

两个子 agent，靠 `bindings` 路由分流到不同飞书 App。

### 全局配置

| 字段 | 值 |
|------|-----|
| 默认模型 | 无（沿用 OpenClaw 全局默认 = deepseek/deepseek-v4-pro） |
| gateway | 18789 |
| 绑定规则 | 飞书账号 `dongsheng` → 路由给 `ai`（东晟AI健康管家） |

### Agent 明细

| ID | 身份 | Emoji | 模型 | 工具范围 | 飞书 App ID | 备注 |
|----|------|-------|------|---------|------------|------|
| `main` | **小冬** | 🥶 | deepseek/deepseek-v4-pro | denylist 模式：仅允许 `douyin__*` + `message` + `web_search` + `web_fetch` + `memory_search` | `cli_aabaa4d43cf81bd0`（default） | 获客助手核心 agent，走 default 飞书 |
| `ai` | **东晟AI健康管家** | — | xiaole/gpt-5.4 | 继承实例默认（无额外限制） | `cli_aabfb56d0b781cd9`（account: dongsheng） | 独立 workspace `workspace-dongsheng` |

### 数据占用

| 目录 | 大小 |
|------|------|
| agents/ (`main` + `ai`) | ~341M |
| workspace-dongsheng | — |

### 工具权限（小冬）

小冬使用 `profile: "minimal"` + `deny` + `alsoAllow` 三阶段控制：

```json
{
  "deny": ["fs","runtime","read","exec","write","edit","process",
           "apply_patch","code_execution","cron","gateway","nodes",
           "browser","subagents","sessions_spawn","sessions_send"],
  "profile": "minimal",
  "alsoAllow": ["douyin__douyin_crawl", "douyin__douyin_crawl_result",
                "message", "web_search", "web_fetch", "memory_search"]
}
```

---

## 实例 2：`.openclaw-second` — gateway :1890

### Agent 明细

| ID | 身份 | Emoji | 模型 | 沙箱 | 备注 |
|----|------|-------|------|------|------|
| `main` | **文案策划** | 📝 | 默认（deepseek-v4-pro） | Docker 1G / sandbox mode=all | default=true |
| `bot1` ~ `bot10` | bot1 ~ bot10 | — | deepseek/deepseek-v4-flash | Docker 1G / sandbox mode=all | 完全一致配置 |

### 共性配置

所有 agent（含 main）共用以下 sandbox 与工具配置：

- **sandbox**: Docker backend, network=bridge, capDrop=ALL, tmpfs 512M, pidsLimit=256, readOnlyRoot=true
- **tools**: `profile: minimal`, deny 类清单同小冬（无 alsoAllow），exec security=full
- **context**: `contextInjection: always`, `bootstrapMaxChars: 20000`, `bootstrapTotalMaxChars: 60000`

### 残留目录（未激活，可清理）

| 残留项 | 大小 | 说明 |
|--------|------|------|
| `agents/visual` | 20K | 视觉设计搬迁到 `.openclaw-visual` 后的残留 |
| `agents/bot_bot1` ~ `bot_bot10` | 各 16K | 疑似重复生成的 bot 目录，不在 agents.list 中 |
| `workspace-visual` | 2.0M | 视觉设计旧 workspace |

### 数据占用

| 分类 | 大小 |
|------|------|
| agents/ (已激活 11 个) | ~3.3M |
| 残留 agents/ (11 个) | ~176K |
| workspace 目录 (12 个) | ~640K |

---

## 实例 3：`.openclaw-visual（出图实例） — gateway :1891

> 环境变量：`IMAGE_OUT_DIR=/home/ubuntu/.openclaw-visual/media/outbound`

### Agent 明细

| ID | 身份 | Emoji | 模型 | 沙箱 | 备注 |
|----|------|-------|------|------|------|
| `main` | **视觉设计** | 🎨 | 默认（deepseek-v4-pro） | Docker 1G / sandbox mode=all | default=true, tools profile=full |
| `v1` ~ `v10` | v1 ~ v10 | — | deepseek/deepseek-v4-pro | Docker 1G / sandbox mode=all | identity 为空占位符 |

### 工具权限

`main` 和 `v1`~`v10` 均使用 `profile: "full"`（比另两个实例更宽松）：

```json
{
  "profile": "full",
  "deny": ["gateway", "cron", "sessions_spawn", "sessions_send",
           "sessions_history", "subagents"]
}
```

### 数据占用

| 目录 | 大小 |
|------|------|
| agents/ (`main` + `v1`~`v10`) | ~22M |
| workspace (`main` + `v1`~`v10` + attestations) | ~2.7M |

---

## 进程状态验证

```
ubuntu    833797  node openclaw gateway --port 18789     # .openclaw   主实例
ubuntu   1085586  openclaw                                 # .openclaw-second
ubuntu   2053261  openclaw                                 # .openclaw-visual
```

3 个进程全部运行中。各实例飞书渠道通过 `openclaw channels status` 验证为 `running` ✅。

---

## 飞书 App 总览

| # | 实例 | 子 Agent | 飞书 App ID | 用途 |
|---|------|---------|------------|------|
| 1 | `.openclaw` | 小冬 | `cli_aabaa4d43cf81bd0` | 获客助手（default） |
| 2 | `.openclaw` | 东晟AI健康管家 | `cli_aabfb56d0b781cd9` | 健康咨询（account: dongsheng） |
| 3 | `.openclaw-second` | 文案策划 | `cli_aabe46e2d0b8dbe4` | 文案生成（default） |
| 4 | `.openclaw-visual` | 视觉设计 | `cli_aabc1d3f9e789bec` | 图片生成（default） |

---

## 架构拓扑

```
┌─────────────────────────────────────────────────────────────┐
│                    广州腾讯云 CVM                           │
│                   129.204.166.13                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌───── .openclaw ─────┐  ┌── .openclaw-second ──┐        │
│  │  Gateway :18789      │  │  Gateway :1890        │        │
│  │                      │  │                       │        │
│  │  ┌──────────────┐   │  │  ┌──────────────────┐ │        │
│  │  │ main         │   │  │  │ main (文案策划)   │ │        │
│  │  │ 小冬 🥶      │   │  │  ├──────────────────┤ │        │
│  │  │ model: v4-pro │   │  │  │ bot1  bot2  bot3│ │        │
│  │  │ Feishu App 1  │   │  │  │ bot4  bot5  bot6│ │        │
│  │  └──────────────┘   │  │  │ bot7  bot8  bot9│ │        │
│  │                      │  │  │ bot10            │ │        │
│  │  ┌──────────────┐   │  │  │ model: v4-flash  │ │        │
│  │  │ ai            │   │  │  │ Feishu App 3     │ │        │
│  │  │ 东晟AI健康管家│   │  │  └──────────────────┘ │        │
│  │  │ model: gpt-5.4│   │  └───────────────────────┘        │
│  │  │ Feishu App 2  │   │                                   │
│  │  └──────────────┘   │  ┌── .openclaw-visual ──┐         │
│  └─────────────────────┘  │  Gateway :1891        │         │
│                           │  IMAGE_OUT_DIR        │         │
│                           │                       │         │
│  ┌─── 其他服务 ────┐     │  ┌──────────────────┐ │         │
│  │ Leadgen :8090    │     │  │ main (视觉设计)   │ │         │
│  │ 小探    :8501    │     │  │ 🎨               │ │         │
│  │ Dify (11容器)    │     │  ├──────────────────┤ │         │
│  │ Hermes :3000     │     │  │ v1  v2  v3  v4  │ │         │
│  └───────────────────┘     │  │ v5  v6  v7  v8  │ │         │
│                           │  │ v9  v10          │ │         │
│  ┌─── OpenClaw ─────┐     │  │ model: v4-pro    │ │         │
│  │ 3 gateway 进程    │     │  │ Feishu App 4     │ │         │
│  │ 24 Agent / 4 飞书│     │  └──────────────────┘ │         │
│  └───────────────────┘     └───────────────────────┘         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 已知问题与建议

| 问题 | 建议 |
|------|------|
| `.openclaw-second` 残留 11 个 `bot_bot*` 目录 + `visual` | 可安全删除：`rm -rf agents/bot_bot* agents/visual workspace-visual` |
| `.openclaw-visual` 的 v1~v10 identity 为空占位符（`????`） | 如需对外展示，建议补全 identity.name + identity.emoji |
| 三套实例 `doctor --deep` 提示飞书插件未注册 | 飞书走内置渠道已 `running`，不影响使用；若需 doctor 干净通过，可补登记插件 |
| 24 个 Agent 共享 4 核 CPU / 7.4G 内存 | Docker sandbox 每 agent 限定 1G，高峰可能争抢；建议监控 load 是否 > 4 |

---

## 机密存放位置（不入库）

以下机密**只在服务器本地**，不写入本仓库：
- 飞书 App Secret：各 home 的 `~/.openclaw*/openclaw.json` → `channels.feishu.appSecret` / `channels.feishu.accounts.*.appSecret`
- 服务器登录密码：本机 `~/.ssh/.xiaodong-server-pass`
- 中转站（生图/模型网关）Token：本机 `~/.claude/settings1.json`（base_url `https://api.zelong.vip`）
