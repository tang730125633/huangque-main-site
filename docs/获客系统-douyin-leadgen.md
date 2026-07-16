# 获客系统（douyin-leadgen）· 完整档案

> 本文是仓库早期 README 的完整存档（平台化后主 README 已重写，2026-07-09）。
> 获客/爬取的运维细节、MediaCrawler 风控安全参数、踩坑速查都在这里，仍然有效。


多平台数据采集，三个主力场景：
- **🔍 关键词获客**：输行业关键词 → 搜视频/笔记 → 扒评论区 → 过滤出**精准潜在客户名单**（抖音 + 小红书）
- **👤 账号深扒**：输主页链接 → 拉该账号**画像 + 全部作品 + 评论** → 导出 xlsx
- **🎯 两阶段抓取**：全量扫 → 自动筛爆款 → 高量补抓 → 评论覆盖率翻倍

为大鹏老板公司 AI 板块的获客/对标场景而建。网页自助使用，结果可发飞书群。

---

## 一、网页怎么用

公网地址：`http://129.204.166.13:8090`（口令见内部）。两个 tab：

### 🔍 关键词搜索
关键词 → 出**评论区精准客户名单**（网页表格 + CSV 下载，客户带抖音号/属地/需求原文/**主页超链接**）。
- 内置**关键词库**（美业/电商/IP/品牌孵化/AI/美业项目C端，上百词，点一下填入）
- 自动剔除同行中介刷的广告，只留真实需求（"怎么拓客/想做/多少钱"）
- 边爬边从视频话题标签自动挖「🔥发现的词」，词库自增长
- ⚠️ 区分 **B端**（获客/拓客→门店老板）vs **C端**（瘦身/幼态脸→消费者），两类客户

### 👤 按账号爬取
贴**主页链接**（抖音 app 分享→复制链接，一行一个支持批量）：
- 单个账号 → 网页显示**画像 + 作品封面预览 + CSV 下载**
- 批量账号 → 每个账号一个 **xlsx**（画像/全部作品/评论 三个 sheet）自动发飞书群
- 账号模式评论**爬全**（每视频上限 100 条一级评论，不爬子回复，安全档）

---

## 二、架构（混合：服务器 + Mac worker）

```
①伙伴浏览器 ──公网IP:8090──> ②服务器(腾讯云CVM,广州)
                               · FastAPI 后端 + SQLite 任务队列(systemd leadgen)
                               · 网页前端 + 关键词库
                               · 结果展示 / 发飞书
                                    ↑ ↓ HTTP 抢单
                               ③Mac worker(LaunchAgent 开机自启+防休眠)
                               · 跑 MediaCrawler 实际爬取(住宅IP)
                               · 关键词→评论过滤 / 账号→creator 深扒
                               · 导出 + lark-cli 发飞书
```

**为什么是混合**：抖音对**机房 IP 的搜索接口**风控（返回空），但放行深采。所以**爬取必须在 Mac（住宅 IP）**，服务器只做队列/前端/发飞书。代价：**Mac 需开机联网**。

### 依赖的上游工具（不在本仓库分发）
- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：关键词搜索 + 评论 + 账号(creator) 采集
- [Douyin_TikTok_Download_API（小探）](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)：账号深采 API（服务器 :8501）

---

## 三、命根子：活账号 + 干净 IP

系统本质是**用一个抖音账号的登录态（Cookie）+ 住宅 IP** 在抓。所以：
- **账号会失效**（cookie 过期几天~几周，或爬太猛被风控）→ 失效后需重新扫码登录
- **建议用专用小号**，别用主号（封了不心疼）
- **登录态健康检查**（`scripts/login_health_check.py` + LaunchAgent）每 6 小时自检，失效自动飞书私信告警

---

## 四、本地 / 运维

```bash
# 本地全套(后端+worker都在本机,自己用)
bash run_local.sh        # 然后开 http://localhost:8090
bash stop_local.sh

# 生产 worker(连公网服务器,正式给伙伴用)
bash run_worker.sh       # Mac 需保持开机联网

# 命令行/飞书Bot 调用(关键词获客)
python scripts/crawl_cli.py "美业获客" --count 10

# 🎯 两阶段抓取(爆款深挖)
python scripts/viral_hunter.py data/douyin/jsonl/search_contents_*.jsonl        # 筛爆款列表
python scripts/viral_hunter.py data/douyin/jsonl/search_contents_*.jsonl --command  # 生成补抓命令
python scripts/viral_hunter.py data/douyin/jsonl/ --merge --out merged.xlsx         # 合并数据导出

# 🎙️ 视频转口播文案(ASR)
python scripts/transcribe_video.py data/douyin/jsonl/search_contents_*.jsonl --top 10
python scripts/transcribe_video.py data/douyin/jsonl/creator_contents_*.jsonl --all
```

LaunchAgent（开机自启，已装）：
- `com.tang.leadgen-worker`：worker 主体（KeepAlive + caffeinate 防休眠）
- `com.tang.leadgen-health`：登录态 6 小时自检告警

---

## 五、目录

```
server/    app.py(后端API+队列) · index.html(网页) · keywords.json(关键词库)
worker/    worker.py(领单→爬取→过滤/导出→发飞书)
scripts/   leads_filter(意图过滤) · export_videos_xlsx · export_account_xlsx
           · resolve_douyin_id(抖音号→sec_uid) · crawl_cli(飞书Bot桥)
           · validate_keywords · login_health_check(健康检查)
           · viral_hunter(爆款识别+两阶段抓取,支持抖音/小红书)
           · export_xhs_xlsx(小红书数据导出)
           · transcribe_video(视频→口播文案faster-whisper ASR)
docs/      部署记录(playbook) · 成果 ; keywords.md(关键词库说明)
```

## 六、踩坑速查（建系统时的硬经验）
1. 机房 IP 封搜索、放行深采 → 搜索必须住宅 IP（Mac）。
2. **抖音号→sec_uid 解析被验证码挡**（playwright 无头/有头都弹验证码）→ 网页**改输主页链接**绕过；裸抖音号靠真实 Chrome 人工解析。**连续搜会被限流。**
3. 飞书发送：lark-cli 加 `--profile xiaoqiu`（默认 app 授权坏；open_id 按 app 隔离）。
4. CDP 连真实 Chrome：Chrome149+Playwright ws 404 → 用标准模式 + `SAVE_LOGIN_STATE`。
   - **已验证的根因+修复**：`CDP_CONNECT_EXISTING=True` 时 MediaCrawler 连的是
     `ws://localhost:9222/devtools/browser`(裸路径)，新版 Chrome 返回 404。
     改为从 `http://127.0.0.1:9222/json/version` 读 `webSocketDebuggerUrl` 再连即可。
   - 取地址的 httpx 须 `trust_env=False`(否则系统代理拦截 localhost 返回 502)；
     运行加 `NO_PROXY=localhost,127.0.0.1` 让 playwright 连接也绕过代理。
   - 启动调试 Chrome 不能用默认用户目录(Chrome 拒绝)，要 `--user-data-dir=独立目录`。
5. MediaCrawler 安全参数（住宅 IP + 小号 + 单并发，实测不触发风控）：
   | 参数 | 关键词搜索 | 账号深扒 | 说明 |
   |------|:---------:|:-------:|------|
   | CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES | 100 | 100 | 每视频一级评论上限 |
   | ENABLE_GET_SUB_COMMENTS | False | False | 不爬子评论 |
   | MAX_CONCURRENCY_NUM | 1 | 1 | 单并发 |
   | CRAWLER_MAX_SLEEP_SEC | 3 | 3 | 请求间隔(秒) |
   | HEADLESS | True | True | Mac 无头/本地调试改 False |
   | ENABLE_CDP_MODE | False | False | 标准模式(CDP 有 ws404 bug) |
   
   > 💡 以上参数在住宅 IP + 小号场景下已验证安全。搜索模式约 16 请求/分钟，创作者 77 作品~8 分钟跑完，均不触发风控。调大需谨慎。
6. 切爬取账号：清 `browser_data/dy_user_data_dir` → 跑触发二维码 → 扫；**登录后别立刻杀进程**，等存盘。
7. **网络抖动整任务崩**：抓 creator 几十个视频时，单次 httpx `ConnectError`/TLS 握手失败
   会让整个进程退出。给 client `request()` 加连接/超时类错误重试(retry 3~4 次)即可扛过抖动；
   creator 模式失败重跑会自动跳过已抓视频(去重)，补齐剩余即可。
8. **creator 的 jsonl 是追加写**：连爬多个账号，`creator_contents_*.jsonl` 会混入多账号数据。
   导出时按 `works[0].sec_uid` 过滤 + `aweme_id`/`comment_id` 去重(见 `export_account_xlsx.py`)。
9. **小红书风控安全参数**（标准 Playwright 模式即可，无需 CDP）：
   | 参数 | 关键词搜索 | 说明 |
   |------|:---------:|------|
   | CRAWLER_MAX_NOTES_COUNT | 30 | 每次搜索笔记数 |
   | CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES | 100 | 每笔记一级评论上限 |
   | CRAWLER_MAX_SLEEP_SEC | 3 | 请求间隔(秒) |
   | ENABLE_GET_SUB_COMMENTS | False | 不爬子评论 |
   | MAX_CONCURRENCY_NUM | 1 | 单并发 |
   | ENABLE_CDP_MODE | False | 标准模式(小红书 CDP 不需要) |

   > 小红书互动量级比抖音小,爆款阈值也应调低: 点赞>5000/评论>200 即为爆款，
   > detail 补抓 200 条/笔记。实测 20 笔记 ~14 分钟，约 9 请求/分钟，无风控。
10. **小红书导出**：字段名和抖音不同 — `video_url`(不是 video_download_url),
   `image_list`(不是 cover_url), `note_id`(不是 aweme_id), `user_id`(不是 sec_uid)。
11. **ASR 模型下载**：faster-whisper small(484MB) 从 huggingface.co 下载在国内很慢/被墙，
   设 `HF_ENDPOINT=https://hf-mirror.com` 或手动 curl 到 `models/faster-whisper-small/`。
   ffmpeg 用 `pip install imageio-ffmpeg`(自带完整静态二进制,免系统安装)。
   口播型视频识别极好；新闻剪辑/多声道/背景音乐大的视频识别差。

## 七、路线图
- [x] 关键词→评论区精准客户（网页表格+CSV+主页链接）
- [x] 关键词库多垂类 + C端 + 自动发现词
- [x] 按账号爬取（画像/全部作品/评论 xlsx → 飞书）
- [x] 单账号网页预览 + CSV
- [x] 全量评论（500/视频 + 子评论）
- [x] 公网上线 + Mac worker 开机自启 + 登录态健康告警
- [ ] 飞书 Bot 小秋接入（`crawl_cli.py` 就绪，待接 `~/.lark-channel`）
- [x] 🎙️ 视频转口播文案（faster-whisper ASR，可热榜/账号批量）
- [ ] 住宅代理 → 爬取搬服务器、脱离 Mac
- [x] 🎯 两阶段抓取(抖音/小红书双平台: search全量→viral_hunter筛爆款→detail高量补抓→合并去重)
- [x] 📕 小红书全流程(搜索→导出xlsx, 20笔记+1548评论实测,登录态复用免扫码)
- [ ] 跨爬取去重 / 客户浓度排名

## 八、安全与合规
- cookie / 真实名单(PII) / jobs.db / xlsx：**永不进 git**（见 `.gitignore`），私有仓库
- 仅用于正当商业触达，遵守平台规则；上游工具各遵其开源协议

## 九、CI 自动检查

GitHub Actions 会在提交到 `main`、发起 Pull Request 或手动触发时运行：

- 拦截 `browser_data/`、`data/`、`.env`、数据库和密钥文件；
- 检查 HTML 本地链接和静态资源是否丢失；
- 检查 Python / JavaScript 语法；
- 使用锁定依赖构建 React/Vite 设计系统。

本地可在提交前运行同等检查：

```bash
python3 scripts/ci_validate.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts server worker tests
find site -type f -name '*.js' -print0 | xargs -0 -n1 node --check
cd design-system && npm ci && npm run build
```
