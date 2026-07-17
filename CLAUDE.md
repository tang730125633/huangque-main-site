# CLAUDE.md — 黄雀 AI 主站

黄雀 AI：社交媒资内容工作台 + 抖音评论区获客引擎。

> 🧭 **获客系统**：先读 `~/AI-Memory/systems/douyin-leadgen.md` + `~/AI-Memory/SYSTEM.md`。
> **UI/视觉**：先读 `DESIGN.md`，不得偏离。

## 架构

| 服务 | 端口 | 主要文件 |
|------|------|----------|
| content_api | 8096 | `server/content_domains/core.py` |
| imggen_api | 8101 | `server/imggen_api.py`（Nano Banana 独立服务）|
| auth_server | 8095 | `server/auth_server.py` |
| leadgen_api | 8090 | `server/leadgen_api.py` |
| Mac Worker | 远程 | MediaCrawler + TikHub 爬虫 |

**前端**：`site/workbench/`（原生 JS + HTML，唯一正本目录）

## 组锁纪律（最重要规则）

一个 PR **只能动一个组**，跨组必被打回。

| 组 | 文件 |
|----|------|
| Shell | `cloud-shell.js`（排他）|
| A | `core.py` `points.py` `leads.py` `cos.py` `egress.py` `wavespeed.py` |
| B | `video.html` `video.py` `banana.html` `canvas.html` |
| C | `audio.html` `script.html` |
| E | `collect.html` `inspiration.html` `assets.html` |

## PR 流程

1. `git checkout main && git pull` → 开分支
2. 只改一个组的文件 + 关联测试
3. 改前端必跑 `python scripts/stamp_assets.py`
4. commit → push → 开 PR → 等 kong74007-ui 审核
5. CI 门禁绿了才能合并

详见 `.claude/commands/pr.md`。

## 红线

- ❌ 禁止直接 push main
- ❌ 禁止跨组 PR
- ❌ 禁止提交密钥 `.env` `.db` `content_out/` `browser_data/` `data/`
- ❌ 禁止改服务器代码
- ❌ **改源码必须同步更新相关测试文件**，不能只让测试追源码

### 支付红线（2026-07 微信支付上线后新增）

- ❌ 金额/点数只认服务端：套餐走 `RECHARGE_PACKAGES` 白名单，自定义走 `recharge_points_for(amount)`，**任何路径都不得采信客户端传来的 points**
- ❌ 支付/微信回调只能在主站测（回调域名绑死 huangquechuanmei.com），dev 环境充值测到"出二维码"为止
- ❌ 支付凭证只存服务器 env（`/home/ubuntu/.wxpay/`，600 权限），不进仓库
- ⚠️ 支付相关文件（`auth_server.py` `wxpay.py` `recharge.html`）未在组锁表内，动之前先在群里说一声

## 团队协作节奏（2026-07-15 定稿，dev 服务器到位后生效）

每人一台独立 dev 服务器，子域名 `拼音.huangquechuanmei.com`；每人独立 API Key + 额度上限，禁止复制生产 Key。

- **早**：群里认领当日模块，防重复劳动；dev 服务器自动拉 main 合入本人分支（冲突飞书通知本人自解）
- **白天**：各自分支上自由开发，dev 环境秒级反馈
- **傍晚**：AI 出验收报告 → 验收人过目 → **逐个**合并（合一个冒烟一次）→ 预生产冒烟
- **深夜**：自动 ship 主站 + 健康检查（命脉接口连挂两轮自动回滚 + 飞书告警）

分叉永不超过 24h。测试报 bug 必须标域名（四台跑不同分支）；dev 服务器是牲口不是宠物，每 1–2 周 setup 脚本推平重建。

## QA 协作（yuelei-dev）

- QA 提问题 → AI 分析根因 → 等确认 → 动手
- AI 不擅自 commit/push/创建 Issue-PR、不碰服务器
- 网络问题走代理 `127.0.0.1:7897`

## 改代码前检查清单

1. `grep` 搜所有引用（包括 `tests/` 目录）
2. 列出需同步的测试文件
3. 确认单组内
4. 确认没回退 upstream 代码
5. 跑 `stamp_assets.py`

## 已知坑

- **poll catch 为空**：banana/audio/script 轮询网络错误静默忽略
- **reaper 误杀**：talking 视频内部轮询不刷 `updated_at`，>9min 可能被杀
- **canvas 无服务端存储**：全量 localStorage，换浏览器即丢
- **点数分两套**：banana 自算点数（`imggen_api.py`），其余走 `points.py`
- **`MAX_USER_ACTIVE_JOBS=5`**：画布并行节点多时会被 429 拦截

## 获客架构

- 发现层：MediaCrawler（Mac 本地 `~/code/MediaCrawler`）
- 深采层：Douyin_TikTok_Download_API（服务器 `:8501`）
- 过滤层：`scripts/leads_filter.py`
