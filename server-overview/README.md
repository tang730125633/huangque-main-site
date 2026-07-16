# 🖥️ 服务器全貌 — 129.204.166.13

> 腾讯云 Ubuntu 22.04，8.4G 磁盘占用，24/7 运行

## OpenClaw 架构（3实例+1子区）

| 实例 | 路径 | 大小 | 身份 | 飞书App | GitHub |
|------|------|------|------|---------|--------|
| 主实例 | `~/.openclaw/` | 397M | 🥶 小冬·美业获客+运营 | cli_aabaa4d43cf81bd0 | tang730125633/OpenClaw_AI-Memory |
| 第二实例 | `~/.openclaw-second/` | 55M | 📝 文案策划 | cli_aabe46e2d0b8dbe4 | — |
| 视觉实例 | `~/.openclaw-visual/` | 122M | 🎨 视觉设计/小秋 | cli_aabc1d3f9e789bec | — |
| 东晟子区 | `~/.openclaw/workspace-dongsheng/` | — | 🏥 东晟AI健康管家 | (共享主实例) | — |

## 获客系统组件

| 组件 | 路径 | 端口 | 说明 |
|------|------|------|------|
| 获客队列服务 | `~/leadgen-server/` | :8090 | Node.js，抖音获客/视频号解析入口 |
| 小探API | — | :8501 | Python FastAPI，抖音/TikTok/B站解析 |
| 蓝绿部署A | `~/leadgen-A/` | — | 获客worker实例A |
| 蓝绿部署B | `~/leadgen-B/` | — | 获客worker实例B |
| Worker进程 | `~/worker_1/` `~/worker_2/` `~/worker_3/` | — | 爬虫worker池 |

## 爬虫工具

| 工具 | 路径 | 大小 | 说明 |
|------|------|------|------|
| douyin-leadgen | `~/douyin-leadgen/` | 752K | 获客主仓库（本仓库） |
| douyin-scraper | `~/douyin-scraper/` | 147M | 抖音专用爬虫 |
| MediaCrawler | `~/MediaCrawler/` | 1.2G | 多平台爬虫（抖音/小红书/快手/B站/微博） |
| douyin-leadgen-skill | `~/douyin-leadgen-skill/` | 232K | 获客技能包 |

## Web项目

| 项目 | 路径 | 大小 | 说明 |
|------|------|------|------|
| hermes-web | `~/hermes-web/` | 2.1G | Hermes Web 项目 |
| dify-proxy | `~/dify-proxy/` | 8K | Dify API 代理 |

## 关键脚本与配置

| 文件 | 路径 | 说明 |
|------|------|------|
| xiaole_img.py | `~/xiaole_img.py` | gpt-image-2 XiaoLe中转脚本 |
| secret.xiaole.env | `~/secret.xiaole.env` | XiaoLe API密钥 |
| dy_cookies.json | `~/dy_cookies.json` | 抖音登录Cookie |
| watchdog_openclaw.sh | `~/watchdog_openclaw.sh` | OpenClaw守护进程 |
| leadgen-deploy.sh | `~/leadgen-deploy.sh` | 获客蓝绿部署脚本 |
| leadgen-bluegreen-README.md | `~/leadgen-bluegreen-README.md` | 蓝绿部署文档 |

## 飞书群聊

| 群名 | chat_id |
|------|---------|
| 主群（10人） | oc_90de26e8f56516ceae60aca7c7009bb3 |
| 测试群 | oc_adf388bcde69e89da07569251a807db8 |

## 定时任务

```bash
# 每天23:00 GitHub自动同步
cron: daily-git-sync (小冬workspace → tang730125633/OpenClaw_AI-Memory)
```

## 备份

路径：`~/backups/`，按日期存储关键配置文件快照。
