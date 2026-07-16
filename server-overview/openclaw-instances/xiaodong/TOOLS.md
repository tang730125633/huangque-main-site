# TOOLS.md — 小冬的本机环境备注

> 🎯 核心能力封装为 skill，按需激活。业务知识/命令用法在各 skill 里，这里只登记**有什么**。

## 环境
- 我跑在大鹏老板公司的**服务器**上（云端），24/7 值守。
- 搜索：默认用 **Tavily**（已替代 Brave），支持 `tavily_search`（高级参数）+ `tavily_extract`（URL 提取）

---

## 🏗️ 后端服务（本机端口）

| 端口 | 服务 | 说明 |
|------|------|------|
| `:8090` | **获客队列服务** (Leadgen Server) | Node.js — 抖音获客/视频号解析/视频下载的入口 |
| `:8501` | **小探 API** (Douyin_TikTok_Download_API) | Python FastAPI — 抖音/TikTok/B站 视频/用户/评论解析 |

### :8090 获客队列 API

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/submit` | POST | 提交获客任务（关键词搜索/账号深扒） |
| `/api/keywords` | GET | 获取关键词库 |
| `/api/resolve` | POST | 解析抖音视频链接 |
| `/api/extract` | POST | 下载抖音视频 + 提取口播文案 |
| `/api/job/<id>` | GET | 查询任务状态 |
| `/api/download/<file>` | GET | 下载结果文件 |
| `/api/wxchannel` | POST | 📺 解析视频号分享链接（标题/封面/作者/无水印链接） |
| `/api/wxchannel_download` | POST | 📺 下载视频号无水印视频 |
| `/api/wxchannel_transcribe` | POST | 📺 下载视频号视频 + 提取口播文案 |
| `/api/wxchannel_comments` | POST | 抓视频号作品评论（分享链接→后端自动转 object_id→翻页，返回昵称/内容/点赞/IP属地） |

**口令：** 需 `password` 字段（环境变量 `LEADGEN_PASSWORD`）

### :8501 小探 API（精选端点）

| 端点 | 用途 |
|------|------|
| `/api/douyin/web/fetch_one_video` | 获取单个抖音视频数据 |
| `/api/douyin/web/fetch_user_post_videos` | 获取用户主页作品 |
| `/api/douyin/web/fetch_video_comments` | 获取视频评论 |
| `/api/douyin/web/fetch_video_comment_replies` | 获取评论回复 |
| `/api/douyin/web/handler_user_profile` | 获取用户信息 |
| `/api/douyin/web/fetch_user_like_videos` | 获取用户喜欢作品 |
| `/api/douyin/web/fetch_user_collection_videos` | 获取用户收藏作品 |
| `/api/douyin/web/fetch_user_mix_videos` | 获取用户合辑作品 |
| `/api/douyin/web/fetch_user_live_videos` | 获取用户直播流 |
| `/api/hybrid/video_data` | 混合解析单一视频（支持多平台） |
| `/api/download` | 在线下载视频/图片 |
| — 还有 TikTok / Bilibili 全部对应端点 | 完整列表见 `/docs` Swagger |

---

## 🐍 脚本工具（`skills/douyin-leadgen/scripts/`）

| 脚本 | 用途 |
|------|------|
| `leadgen.py` | ⭐ 获客主入口：关键词搜视频→扒评论→过滤客户 / 账号深扒 |
| `crawl_cli.py` | 轻量 CLI 桥（仅关键词，旧版，统一用 leadgen.py） |
| `leads_filter.py` | 评论区意图过滤器：jsonl → 精准客户/中介噪音/闲聊 |
| `viral_hunter.py` | 🎯 两阶段爆款深挖（抖音+小红书双平台） |
| `export_account_xlsx.py` | 账号数据导出：画像+作品+评论 → 一个 xlsx |
| `export_videos_xlsx.py` | 视频级数据导出：search_contents jsonl → 18列 xlsx |
| `export_xhs_xlsx.py` | 小红书数据导出：笔记+评论+摘要 → xlsx |
| `transcribe_video.py` | 视频转口播文案：下载→faster-whisper 转文字 |
| `login_health_check.py` | 登录态健康检查：Cookie 失效→飞书告警 Tang |
| `resolve_douyin_id.py` | 抖音号→sec_uid 解析（⚠️已弃用，撞验证码） |
| `validate_keywords.py` | 关键词抽样验证：确认搜得到视频+评论 |

---

## 📡 飞书通道
- **私聊：** 唐泽龙 `ou_d45fa42f49c14bc85abf18cc37f70391`
- **群聊：** `oc_90de26e8f56516ceae60aca7c7009bb3`（唐泽龙/大鹏/小方等10人群）
  - ⚠️ 不是 Mac 端的 Claw Bot 先锋项目讨论群，那是 Mac agent 的群
  - 成员名单见 USER.md 团队通讯录

---

## 🛠️ 技能清单（Skills）

### 飞书插件工具（`~/.openclaw/plugin-skills/`）
由飞书插件提供，调用方式为 JSON 参数：

| 工具名 | 功能 | SKILL.md |
|--------|------|----------|
| `feishu_chat` | 群聊管理：查群信息、成员列表、发消息 | — |
| `feishu_doc` | 文档读写：创建/读取/写入/追加/上传图片 | `~/.openclaw/plugin-skills/feishu-doc/SKILL.md` |
| `feishu_drive` | 云盘文件管理：上传/下载/搜索 | `~/.openclaw/plugin-skills/feishu-drive/SKILL.md` |
| `feishu_perm` | 权限管理：文档分享、协作者 | `~/.openclaw/plugin-skills/feishu-perm/SKILL.md` |
| `feishu_wiki` | 知识库导航：空间/节点/创建页 | `~/.openclaw/plugin-skills/feishu-wiki/SKILL.md` |
| `feishu_bitable_*` | 多维表格：创建/查/改/增字段和记录 | — |

### 系统通用技能（`~/.npm-global/lib/node_modules/openclaw/skills/`）
| 技能名 | 功能 |
|--------|------|
| `browser-automation` | 浏览器自动化（Playwright） |
| `canvas` | HTML 在连接节点上展示 |
| `diagram-maker` | SVG/HTML/Excalidraw 图表 |
| `healthcheck` | 主机安全审计（SSH/防火墙/备份） |
| `meme-maker` | 表情包生成 |
| `node-connect` | 节点连接诊断 |
| `node-inspect-debugger` | Node.js 调试 |
| `notion` | Notion API（页面/数据库/评论） |
| `python-debugpy` | Python 调试 |
| `skill-creator` | 创建/编辑技能 |
| `spike` | 快速原型验证 |
| `taskflow` | 多步骤持久任务编排 |
| `taskflow-inbox-triage` | 收件箱分类工作流 |
| `tmux` | tmux 会话/面板控制 |
| `video-frames` | ffmpeg 视频帧提取 |
| `weather` | 天气查询 |

### 🎯 我的业务技能（`workspace/skills/`）

| 技能名 | 触发词 | 功能 |
|--------|--------|------|
| `douyin-leadgen` | 爬客户/找客户/获客/抖音获客/关键词获客/爬评论/搜客户/leadgen/找线索/客户名单/账号深扒/看账号/爬这个账号 | 🎯 **抖音评论区获客**（核心！）关键词→搜视频→扒评论→客户名单；账号主页深扒；爆款挖掘 |
| `wxchannel-scraper` | 视频号/视频号链接/视频号爬取/视频号下载/视频号文案/微信视频号/视频号解析/视频号评论/视频号评论区/抓视频号评论/sph | 📺 **视频号全能力**（核心！）总钩子=「视频号」→判断意图选4能力：解析信息`/api/wxchannel`·无水印下载`/api/wxchannel_download`·口播文案`/api/wxchannel_transcribe`·评论区抓取`/api/wxchannel_comments`，全走 :8090（口令 $LEADGEN_PASSWORD） |
| `image-gen` | 出图/做海报/生成图片/产品海报/产品宣传图/三视图/风格迁移/AI画图/配图/帮我画 | 🎨 **AI 出图**（gpt-image-2 走 XiaoLe 中转，国内直连）参考图+提示词→海报/三视图/风格迁移，生成后发飞书。脚本 `~/xiaole_img.py`，key `~/secret.xiaole.env` |

---

## 💾 Git & 备份
- GitHub 仓库：`tang730125633/OpenClaw_AI-Memory`
- 推送方式：SSH key（`~/.ssh/xiaodong_github`）
- 自动同步：每天 23:00（`daily-git-sync` cron job）
