# 工单 · 内容爬取/获客采集管线提速（SCRAPER-SPEED）

> 供其他 agent 独立执行。先读《工单-其他agent实施》§0 +《任务看板》+ `DESIGN.md`。
> **冲突组：leadgen（`worker/server_worker.py`，采集 worker，与 A/content 无关）** —— 独立文件，抢 `lock/leadgen`（约定新锁名；改前 `git ls-remote origin refs/heads/lock/leadgen` 确认没被占再抢）。
> 来源：Issue #412（yuelei-dev，bug+enhancement）。

## 背景
关键词获客/内容爬取从提交到出结果通常 **2-10 分钟**，体验差。yuelei 给了瓶颈定位（行号以其提交时为准，实施时以当前代码为准复核）：

| # | 瓶颈 | 耗时 | 位置 |
|---|------|------|------|
| 1 | 每次冷启动 Chromium 准备登录态 | 30-45s | `worker/server_worker.py:112-124` |
| 2 | 爬取超时过长 | 480s/210s | `worker/server_worker.py:155` |
| 3 | 视频评论逐条串行 | 每视频 2-5s | MediaCrawler 内部 |
| 4 | 失败重试 3 次 | ×3 | `worker/server_worker.py:367` |
| 5 | 青果短效代理不稳（IP 活 2 分钟） | 额外重试 | — |

## 改动（`worker/server_worker.py` 为主，MediaCrawler 参数层）
1. **复用浏览器实例**：保持温热 context，不每任务重建 Chromium → 省 ~30s/任务。（注意并发安全/内存泄漏，长跑要有回收）
2. **缩减超时**：静态 IP 480→180s、青果 210→120s，失败更快进重试而非傻等。
3. **评论并发爬取**：MediaCrawler 开 2-3 并发爬评论 → 约 -40% 时间。（别把目标站打崩，留限速）
4. **预注入登录态**：定时脚本预热 cookie，worker 直接取用 → 省 ~35s/任务。
5. **跳过全量重试**：部分数据可用即用，不非等 3 次全失败。

> ⚠️ 这是获客系统核心链路（见 CLAUDE.md 方案卡 `~/AI-Memory/systems/douyin-leadgen.md`），改前先读方案卡，**命中即复用、禁止重写**；`browser_data/`、`data/`（PII）永不进 git。

## 验收标准
1. 典型关键词采集端到端耗时明显下降（目标从 2-10min 压到分钟内），有前后对比数据。
2. 采集结果数量/质量不下降（并发/缩超时不能漏采）。
3. 温热 context 长跑不泄漏、不串号；失败仍能重试、不因缩超时误杀正常慢任务。
4. 不触碰红线：cookie/名单不进 git。

## 部署
`worker/server_worker.py` → 部署到采集 worker 运行位（按方案卡路径），重启对应服务。⚠️ 涉及登录态/代理，改后实跑一轮真实关键词验证再收。
