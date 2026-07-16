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
