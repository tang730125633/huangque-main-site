# 黄雀 AI · 后端架构 + API 文档

> 单一事实源在 GitHub(`design-sync` 分支)。**任何人改后端：先 `git pull` → 改 → `git commit` → `git push` → 再部署。禁止直接在服务器上改文件**（会互相覆盖、且没备份）。
> 服务器：腾讯云 `dapeng-server`（129.204.166.13）。网站：https://huangquechuanmei.com

---

## 一、4 后端架构（各自独立，互不影响）

设计原则：**一个能力 = 一个文件 + 一个端口 + 一个 systemd 服务**。改一个不碰别人，谁都能独立部署/重启/回滚。

| 后端服务 | 端口 | 负责人 | 文件 | 能力 |
|---|---|---|---|---|
| **采集获客** | 8100 | Tang | `server/leadgen_api.py` | 采集 collect · 获客 leads · 关键词搜 |
| **视频下载** | 8097 | Tang | `server/dl_service.py` | 无水印视频下载代理 |
| **作图(nano banana)** | 8101 | Tang | `server/imggen_api.py` | Gemini 出图(NB2/NB Pro)·走 mihomo 代理出墙 |
| **内容生成 + 公共核心** | 8096 | 共用 | `server/content_api.py` | 作图 image · 文案 copy · 配音 audio(豆包) · 任务轮询 · 资产/历史 · 文件服务 |
| **登录鉴权** | 8095 | 共用 | `auth_server.py` | 登录 · 点数 |
| (旧)抖音获客 | 8090 | — | leadgen(legacy) | 历史遗留，逐步下线 |

> **音频/豆包** 目前还在 8096 里（同事的活）；建议同事也拆成独立服务 `8098`，并把代码 push 进 git。在那之前，因 Tang 的采集/获客/下载已搬出，**同事改 8096 也伤不到 Tang**。

### 共享基础设施（所有服务共用，不重复造）
- **任务库** `content_jobs.db`（在 `/home/ubuntu/content-api/`）：所有异步任务(collect/leads/image/copy/audio)都写这一个库 → 前端轮询 `/api/gen/job/{id}` 和「资产/历史」由 8096 统一读。
- **点数/用户** `users.db`（auth 服务）：所有服务扣同一个点数池，统一。
- **清道夫**：8096 跑一个 reaper，把 running 超 6 分钟的僵尸任务判失败+退点（管全库）。
- **TikHub 客户端** `server/tikhub.py`：抖音/小红书/视频号 统一封装，自带**全局限流(~7/s, 防撞 TikHub 10 QPS)** + id 校验 + 重试。8100 import 它。

---

## 二、nginx 路由（按路径分发到不同端口）

```
location = /api/gen/collect          → 8100  (采集)
location = /api/gen/collect/search   → 8100  (关键词搜)
location = /api/gen/leads            → 8100  (获客)
location = /api/gen/dl               → 8097  (下载)
location ^~ /api/gen/                → 8096  (其余：image/copy/audio/job/history/file)
location ^~ /api/auth/               → 8095  (登录)
```
> `location =` 精确匹配优先级最高，所以采集/获客/下载先被截到各自服务，其余才落 8096。

---

## 三、API 文档（前端就调这些）

所有 `/api/gen/*` 需带 `Authorization: Bearer <hq_token>`（登录拿到）。异步能力：POST 提交拿 `job_id` → 轮询 `GET /api/gen/job/{id}` 直到 `status=done`。

### 登录（8095）
- `POST /api/auth/login` body `{username, password}` → `{token, user:{username,points,role,...}}`
- `GET /api/auth/me`（带 Bearer）→ `{user:{username,points,role}}`

### 采集获客（8100）
- **采集** `POST /api/gen/collect`
  body：`{ url 或 (platform+id+note_type), want:["copy","comments","transcript"] }`（贴链接时 url 即可，平台自动识别）
  → `{job_id, cost, points_left}`；轮询 result：
  ```
  { type:"collect", platform, video:{title,author,profile_url,cover,play_url,url,duration,stats},
    copy:{title,desc,tags}, images:[...], transcript:{text}|null,
    comments:[{user,user_id,text,ip,likes,profile_url}], comments_more }
  ```
- **关键词搜** `GET /api/gen/collect/search?platform=douyin|xhs|channels&keyword=&page=1`（扣 1 点）
  → `{items:[{id,platform,title,cover,author,url,note_type,stats:{like,comment}}], points_left}`
- **获客** `POST /api/gen/leads`
  body：`{keyword, platforms:["douyin","xhs"], count, pages, channels_targets:[]}`
  → `{job_id, cost, points_left}`；轮询 result：
  ```
  { type:"leads", keyword, leads_count, spam, chat, total,
    leads:[{nickname,user_unique_id,ip_location,content,title,platform,profile_url}] }
  ```

### 视频下载（8097）
- `GET /api/gen/dl?url=<无水印play_url>&name=<文件名>` + Bearer → 直接下载 mp4（附件，强制下载，限定视频CDN域名防SSRF）

### 作图 nano banana（8101）
- `POST /api/gen/banana` body `{prompt, model:"nb2"|"pro", ratio}` → `{job_id, cost, points_left}`；轮询 result `{type:"image", url, ratio, model}`
  - `nb2`=gemini-3.1-flash-image(主力,10点)·`pro`=gemini-3-pro-image(精品,18点)；ratio 支持 1:1/9:16/16:9/3:4 等
  - 大陆服务器走 mihomo 代理(content.env 的 HTTPS_PROXY)出墙到 Google；key=`GEMINI_API_KEY`(env)
  - 作图页 gpt-image-2 仍走 `/api/gen/image`(8096)，引擎可切

### 内容生成 / 公共（8096）
- `POST /api/gen/image`（作图，12点）· `POST /api/gen/copy`（文案，3点）· `POST /api/gen/audio`（配音，4点）
- `GET /api/gen/job/{id}`（轮询任意任务状态）
- `GET /api/gen/history?kind=collect|image|...`（资产/最近作品）
- `GET /api/gen/file/{name}`（取生成文件；真人视频/参考图/个人音色试听需 Bearer 且校验归属）
- `GET /api/gen/health` → `{caps:[...]}`（看 8096 当前装了哪些能力）

> 豆包音色克隆相关接口在 8096（同事维护），细节问同事 / 待其补文档。

---

## 四、部署 / 重启 / 查状态（Claude Code 或人都能操作）

每个服务独立，互不影响：
```bash
# 部署某个后端（本机 → 服务器，只覆盖自己的文件）
rsync -az --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/dapeng_server_ed25519" \
  server/leadgen_api.py dapeng-server:/home/ubuntu/content-api/
ssh dapeng-server "sudo systemctl restart huangque-leadgen-api"

# 查某个服务状态 / 日志
ssh dapeng-server "systemctl is-active huangque-leadgen-api"
ssh dapeng-server "sudo journalctl -u huangque-leadgen-api -n 50"

# 服务名对照
#   huangque-leadgen-api (8100)  采集获客
#   huangque-dl          (8097)  下载
#   huangque-content     (8096)  内容生成+核心
```
- 前端整站部署：`bash scripts/deploy_site.sh`
- 健康自检：`curl https://huangquechuanmei.com/api/gen/health`

---

## 五、并发 / 扩容路线（以后用户多了再上，按需）

真正的瓶颈是 **TikHub 10 QPS（一个 key）**，不是服务器。量上来时从便宜到贵：
1. **缓存**：同一条链接别人爬过 → 直接给缓存，省 ~90% TikHub 调用 + 秒出。**性价比最高，最先上。**
2. **TikHub 提套餐 / 多 key**：把 10/s 提到 50/100/s。
3. **排队提示**："前面 N 人，约 X 秒"，高峰不崩。
4. **SQLite → Postgres**：上百人同时写不锁库。
5. **多 worker 进程**：网页本身扛更多人。
6. **测试用户看缓存样例，付费用户才走真实抓取**：成百上千人测试也烧不了几个钱。

> 现有「异步任务 + 点数 + 限流 + 独立服务」就是为扩容准备的地基——扩是"往上加"，不是重写。

---

_最后更新：2026-06-27（小秋/Claude Code）_
