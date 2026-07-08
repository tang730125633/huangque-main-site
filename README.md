# 黄雀 AI 创智 · 主站（huangque-main-site）

> 生产站：**https://huangquechuanmei.com** ｜ 运营后台：`/admin-console` ｜ 私有仓库，勿公开
>
> 一句话：**评论区获客 + AI 内容生产的一站式工作台**。前台给团队/客户做内容（作图、视频、配音、文案、采集、获客），后台给运营看健康度（接口拨测、日志监控、用户点数、充值审批）。为大鹏老板公司 AI 板块而建。

- AI 协作规则（Codex/Claude/Cursor 开工必读）：[`AGENTS.md`](AGENTS.md)
- 团队协作规矩（分支/锁/公共件）：[`docs/团队Git协作规矩.md`](docs/团队Git协作规矩.md)
- 生产环境清单与还原手册：[`deploy/生产环境清单与还原手册.md`](deploy/生产环境清单与还原手册.md)
- 视觉规范（改 UI 前必读）：[`DESIGN.md`](DESIGN.md)
- 获客/爬取运维细节与风控踩坑档案：[`docs/获客系统-douyin-leadgen.md`](docs/获客系统-douyin-leadgen.md)

---

## 一、产品全景（前台工作台 `site/workbench/`）

登录后进入工作台（平台账号体系 + 点数计费：提交即预扣，失败自动退点）。页面一览：

| 页面 | 文件 | 功能 |
|---|---|---|
| 今日 | `dashboard.html` | 工作台首页/概览 |
| 作图 | `banana.html` | AI 作图：文生图 / 图生图 / 局部修改（涂抹蒙版），**5 条渠道**可切换（见下方渠道表），多图画廊、清晰度与数量档位 |
| 视频模块 | `video.html` | 数字人**口播视频**（文案口播/音频口播，HeyGen 直连≈1min，失败自动回退泽龙中转）、**动作模仿**（照片+参考动作视频）、**换装/换背景**（RunningHub Wan2.2 Animate / VideoRefusion）、**文生/图生视频**（小乐渠道：果肉 Grok / 豆姐 seedance） |
| AI 配音 | `audio.html` | 豆包大模型 TTS（S_ 音色 + 试听样音）、**VIP 声音克隆**（上传样音复刻音色）、OpenAI TTS 兜底 |
| 编导 · 文案脚本 | `script.html` | 营销文案/分镜脚本生成（LLM），可一键转作图/转视频 |
| 内容爬取 | `collect.html` | 贴链接或关键词采集**抖音 / 小红书 / 视频号**内容：视频详情、无水印下载、评论区、**视频转文案口播**（ASR 转写）；视频号加密流自动解密 |
| 获客 | `leads.html` | 关键词 → 搜视频/笔记 → 扒评论区 → **意图过滤出精准客户名单**（B端/C端区分，带主页链接/属地/需求原文） |
| 资产库 | `assets.html` | 生成产物与上传素材统一管理：形象库、收藏与标签、批量下载/删除 |
| 灵感案例 | `inspiration.html` | 案例灵感库（`inspirations.json`） |
| 画布 | `canvas.html` | 节点式生产画布（实验） |
| 飞书 Bot 矩阵 | `bots.html` | 团队飞书机器人一览 |
| 成本账本 | `cost.html` | 各能力点数成本表 |
| 充值 | `recharge.html` | 用户提交充值申请 → 管理员后台审批到账 |
| 设置 | `settings.html` | 账号设置/改密 |

> 前端唯一正本是 `site/workbench/`；`huangque-web/` 是历史遗留副本，勿在里面改。

## 二、运营后台（`/admin-console`，role=admin 才能进）

`site/admin/index.html` + `server/admin_api.py`（:8098）。五个标签页：

1. **接口调试**：
   - 服务健康：内部服务逐个探活（点「测试」单独 ping，200 即正常）
   - 外部 API 密钥：**11 个渠道**全可测——带密钥的真调一次上游验证密钥有效性；签名类（COS/豆包等）测连通与延迟（标注"仅连通"）；**拨测顺带显示余额**（HeyGen 额度 / RunningHub 币 / TikHub 美元）
   - 每个渠道卡可展开「接口清单」：**51 条业务接口**，带用途与 计费/免费 标记（计费接口不提供一键调用，防误触烧额度）
   - 渠道开关/配置（写审计流水）
2. **日志监控**：任务记录（用户/功能/耗时/点数）与 nginx 每一次 `/api/` 请求（路径/状态码/IP）**合并成一条时间线**；筛选＝任务/HTTP、成功/失败/进行中、关键词；密钥类查询参数自动打码
3. **功能开关**：按模块启停平台功能（禁用后用户提交返回维护提示，不扣点）
4. **用户管理**：账号列表、加减点（原子+流水审计）、点数流水
5. **充值审批**：确认到账自动加点 / 驳回

安全铁律：后台任何接口**不返回密钥真值**；全部写操作留审计；非 admin 一律 403。

## 三、架构

```
用户浏览器 ── https://huangquechuanmei.com（nginx, 443）
   │
   ├─ 静态页  /var/www/huangquechuanmei/（site/ 部署产物）
   ├─ /api/auth/*    → 127.0.0.1:8095  auth 服务（用户/点数/充值订单）
   ├─ /api/gen/*     → 127.0.0.1:8096  内容生成主力（jobs 异步任务模型）
   │    ├ banana/reverse → 127.0.0.1:8101  作图独立服务（nano banana）
   │    ├ dl            → 127.0.0.1:8097  无水印下载代理（CDN 白名单+SSRF 防护）
   │    ├ collect/leads → 127.0.0.1:8100  采集/获客
   │    └ 视频号解密     → 127.0.0.1:3001  Isaac64 解密服务
   ├─ /api/admin/*   → 127.0.0.1:8098  运营后台 API
   └─ /admin-console、/api-admin、/api-docs（团队密码门）

出墙：mihomo 代理 127.0.0.1:7897（OpenAI 官方/HeyGen 直连/Gemini 官方走它；
      泽龙系中转、TikHub、RunningHub 必须直连——代理会被 Cloudflare 拦）
同机：小探 :8501（抖音深采，只监听 docker 网桥 172.17.0.1，探 127.0.0.1 会误报离线）、
      MediaCrawler ×2（:8091/:8092）、获客老站 nginx :8090（Mac 住宅 IP worker 抢单爬取）
```

### systemd 服务一览

| 服务 | 端口 | 代码 | 职责 |
|---|---|---|---|
| huangque-auth | 8095 | `server/auth_server.py` | 账号/token/点数（预扣+失败退点）/充值订单/审计 |
| huangque-content | 8096 | `server/content_api.py` → `server/content_domains/` | 生成任务主力：作图/视频/配音/文案/采集/获客的 jobs 队列 |
| huangque-imggen-api | 8101 | `server/imggen_api.py` | nano banana（Gemini）作图 + 反推 |
| huangque-dl | 8097 | `server/dl_service.py` | 无水印视频下载代理 |
| huangque-leadgen-api | 8100 | `server/leadgen_api.py` | 采集/获客后端 |
| huangque-admin | 8098 | `server/admin_api.py` | 运营后台（拨测/日志/用户/审批） |
| mihomo | 7897 | — | 出墙代理（content/imggen 依赖它） |

数据库均为 SQLite：`auth-service/users.db`（用户/点数）、`content-api/content_jobs.db`（任务表，**已建 `idx_jobs_created` 索引；payload 含大块数据，查询只取前缀，勿全表扫**）、`admin_config.db`（渠道开关/审计）。密钥全部走服务器 env 文件（`content.env` 等，600 权限），**绝不进 git**。

### 外部渠道（11 组，51 条业务接口）

| 渠道 | 用在哪 | 连接方式 |
|---|---|---|
| OpenAI（经泽龙中转 `OPENAI_BASE`） | gpt-image-2 作图、文案 LLM、口播 ASR 转写、TTS 兜底 | 中转直连；官方域名走 mihomo |
| Gemini | nano banana 作图（nb2/pro） | 经泽龙中转直连 |
| 泽龙 Ai（api.xiaoleai.team） | gpt-image-2 作图渠道 | 直连 |
| 泽龙 2 生图号池（api.zelong.vip/image-pool） | gpt-image-2 号池轮询，坏号自动切 | 直连 |
| 果肉/豆姐（api.xiaolevideo.cn） | 文生/图生视频（Grok/seedance）+ 果肉生图 | 直连；海外成片 CDN 经法兰克福中转下载 |
| HeyGen 直连 | 数字人口播（≈1min）、动作模仿 | mihomo 代理 |
| HeyGen 中转（泽龙 relay） | 口播回退链路 + 成片 CDN 反代 | 直连 |
| RunningHub | 换装（Wan2.2 Animate）/换背景（VideoRefusion） | 直连 |
| 豆包/火山引擎 | TTS、声音克隆 | 直连（签名请求） |
| TikHub | 抖音/小红书/视频号：搜索、详情、评论、账号采集、短链解析 | 直连（代理会 403） |
| 腾讯云 COS | 产物/采集转存对象存储 | SDK 签名 |

> 完整接口清单（含用途、计费标记）看运营后台「接口调试」页各渠道的展开列表；调用**大多计费**，测试一律走后台的渠道级拨测，不要手工调业务接口。

## 四、开发与协作

```bash
# 开工三板斧（详见 AGENTS.md）
git fetch origin --prune && git status -sb && git log --oneline -5
git checkout main && git pull
git checkout -b claude/<任务名>     # 或 codex/ feature/

# 本地校验（CI 同款）
python3 -m unittest discover -s tests       # 60+ 用例
python3 scripts/ci_validate.py              # 安全红线(密钥/db/数据不入库) + HTML 链接检查
python3 scripts/stamp_assets.py --check     # 静态资源缓存戳
find site -type f -name '*.js' -print0 | xargs -0 -n1 node --check
cd design-system && npm ci && npm run build # 设计系统构建（CI 也会跑）
```

- **main 有分支保护**：禁止直推，必须 PR + CI「代码与安全门禁」绿才能合并
- **服务器只跑 main、不当正本**；禁止把服务器当正本改代码、禁止整站 rsync 覆盖线上
- 公共文件（`content_api.py`、`cloud-shell.js`、`site/api-admin/index.html`、nginx、数据库 schema）动之前群里打招呼
- 每个工单一个 PR；历史工单沉淀在 `docs/工单-*.md`，收工汇报格式见 `AGENTS.md`

## 五、部署（合并 main 之后）

原则：**只部署本次改过的文件**，从已 push 的 commit 部署，改后端要重启对应 systemd 服务并验证。

```bash
# 例：部署运营后台（先备份；服务器文件属主多为 root，走 /tmp + sudo mv）
scp server/admin_api.py dapeng-server:/tmp/x && ssh dapeng-server \
  'sudo cp ~/content-api/admin_api.py ~/backups/admin_api.py.pre<PR号> &&
   sudo mv /tmp/x /home/ubuntu/content-api/admin_api.py &&
   sudo systemctl restart huangque-admin'
# 前端单文件同理 → /var/www/huangquechuanmei/…（属主 www-data）
# 验证：systemctl is-active + 未登录 curl 应 401 + 浏览器强刷
```

⚠️ `scripts/deploy_site.sh` 是整站 rsync（带 `--delete`），**日常禁用**，只在明确要全量对齐时用。
完整拓扑、env 清单、回滚步骤见 `deploy/生产环境清单与还原手册.md`。

## 六、目录结构

```
server/               后端各服务（单文件一服务 + content_domains/ 领域拆分）
site/                 前端正本（workbench/ 工作台、admin/ 运营后台、api-admin·api-docs 团队页）
tests/                unittest（CI 强制）
scripts/              ci_validate / 部署 / 采集 / 导出等工具脚本
deploy/               nginx 配置、systemd unit、生产手册、env 示例
docs/                 工单档案、协作规矩、获客系统档案、备案流程
worker/               Mac 端获客爬取 worker（住宅 IP 抢单跑 MediaCrawler）
design-system/        React/Vite 设计系统（CI 锁定依赖构建）
knowledge/ archive/ huangque-web/   资料 / 归档 / 遗留副本（勿改）
```

## 七、获客系统由来（douyin-leadgen）

本仓库从"抖音评论区获客工具"长出来：关键词 → MediaCrawler 搜视频 → 扒评论 → 意图过滤 → 客户名单。因抖音对机房 IP 的搜索接口风控，采用**服务器出题、Mac 住宅 IP worker 抢单爬取**的混合架构（worker 用 LaunchAgent 常驻）。老入口 `:8090` 仍在服务获客场景；新平台的获客/采集页（`leads.html`/`collect.html`）走 TikHub + 小探接口。

**运维细节、MediaCrawler 风控安全参数、11 条踩坑速查**（活账号+干净 IP、CDP ws404、ASR 模型下载等硬经验）完整保留在 [`docs/获客系统-douyin-leadgen.md`](docs/获客系统-douyin-leadgen.md)。上游工具（不随仓库分发）：[MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)、[小探 Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)。

## 八、安全红线（ci_validate 强制 + 人工自觉）

- 密钥/密码/cookie/`*.env`/`*.db`/用户数据/生成产物（`content_out/`、`browser_data/`、`data/`）**永不进 git**
- 仓库保持 **private**
- 后台/日志任何地方不显示密钥真值（请求日志已对 token/key/sign/dk 参数打码）
- 客户名单、评论数据含 PII：不外发、不进公开渠道；仅用于正当商业触达，遵守平台规则
