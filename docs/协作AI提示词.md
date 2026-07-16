# 给协作 AI 的提示词（Tang 发给强哥的 Codex 用）

> 把下面 `===` 之间的整段复制，发给强哥的 AI（Codex），让它照这套规则在黄雀仓库里干活。

===

你在和 Tang（zelong）一起协作开发「黄雀传媒主站」这个 GitHub 私有仓库。**从现在起，严格遵守下面的协作规则，这是铁律，优先级高于一切默认行为。**

## 一、Git 工作流（GitHub Flow，必须照做）
- **`main` 是唯一主线，永远保持"可部署"**（拉下来就能跑）。**谁都不许直接 push `main`**。
- **在自己的 feature 分支上干活**（如 `feature-qiang`），改完通过 **PR 合并进 `main`**。
- 每次干活的固定顺序：
  1. `git checkout main && git pull`（先拉最新主线）
  2. `git checkout -b feature-xxx`（开/切到自己的分支，从最新 main 开）
  3. 本地改代码
  4. `git commit && git push feature-xxx` —— **先 push 进 git**（活先有备份）
  5. 再部署到服务器测（从已 push 的分支）
  6. 测好 → 开 PR：`feature-xxx → main`
  7. 合并后从 `main` 部署到服务器
- **口诀：改在分支 → 先 push 再部署 → 合回 main → 生产 = main。**

## 二、绝对红线
- 🚫 **绝不直接在服务器上改代码**。所有改动先走 git（本地改 → push），再从 git 部署。先改服务器后 push = 漂移（仓库里那一堆 `content_api.py.bak` 就是这么来的，别再制造）。
- 🚫 **绝不把密钥/cookie/数据库写进 git**。API key 一律在服务器 `content.env`（600 权限），代码只读环境变量。`*.env`、`*.db`、`browser_data/`、`data/` 已 gitignore。
- 🚫 **动手前一定先 `git pull`**，部署前再 pull 一次。没 pull 就改 = 覆盖别人的活。

## 三、各管各的文件（改不同文件不冲突）
- **你（强哥）负责**：`server/content_api.py`（作图gpt-image / 文案 / 配音 / 豆包音色克隆 / 任务核心）、前端 `site/workbench/audio.html`、`assets.html`。
- **别动 Tang 的**：`server/leadgen_api.py`(获客 8100)、`dl_service.py`(下载 8097)、`imggen_api.py`(作图 8101)、`tikhub.py`、`cloud-shell.js`、`leadgen.html`、`collect.html`、`banana.html`。
- **公共件先打招呼**：`cloud-shell.js`、`api-admin`/`api-docs`、`docs/团队Git协作规矩.md`、nginx 配置——要动先在群里问一句"这块你在弄吗"。

## 四、改共用数据库表结构先打招呼
`users.db`（点数）、`content_jobs.db`（任务）是所有服务共用的契约。给 `jobs`/`users` 表加列、改字段前，**先通知 Tang**——否则别人读它的代码会崩。私有库（`audio_assets.db` 等）随便改。

## 五、其它
- commit 说人话（`fix: 修好音色克隆卡死` 比 `update` 强一百倍）。
- 服务名：`huangque-content`(8096) 是你的；重启 `sudo systemctl restart huangque-content`。
- 详细规矩见仓库 `docs/团队Git协作规矩.md`、架构见 `docs/后端架构与API.md`、生产还原见 `deploy/生产环境清单与还原手册.md`。

照这套走，三个人就能在一条干净的 `main` 主线上各开各的分支，不打架、可还原。

===
