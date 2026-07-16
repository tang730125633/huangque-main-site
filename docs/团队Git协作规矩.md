# 黄雀团队 · Git 协作规矩（一页版）

> 给两个人（+各自的 AI：Claude Code / Codex）看的协作约定。**大白话，照着做就不打架。**
> **单一事实源 = GitHub `main` 分支（已上分支保护）。** 三处永远一致：**GitHub `main` = 服务器代码 = 线上**。
> 线上：https://huangquechuanmei.com ｜ 服务器：`dapeng-server`（129.204.166.13）

---

## 三条铁律（先记这个）

1. **🚫 绝不直接在服务器上改代码。** 所有改动先走 git（本地改 → push），再从 git 部署到服务器。GitHub 是唯一"正本"，服务器只是"跑正本的地方"。（教训：服务器上曾堆出 24 个 `content_api.py.bak`。）
2. **⚠️ 动手前先 `git pull`（AI 也不例外）。** 没 pull 就改 = 用旧版盖掉别人的新活。
3. **🔴 密钥绝不进 git。** API key / 密码 / cookie 一律放服务器 `content.env`（600 权限），代码只读环境变量。`*.env`、`*.db`、`browser_data/`、`data/` 已 gitignore。仓库保持 **private**。

---

## 正式工作流：GitHub Flow（每次都这样）

**核心：`main` 永远保持"可部署"（拉下来就能跑）。没测好的留在自己分支，绝不直接进 main。**

```bash
1. git checkout main && git pull           # 拉最新主线
2. git checkout -b feature-zelong-<任务>    # 从最新 main 开自己的分支
3. …改代码…
4. git commit && git push                   # 先 push 进 git（活先有备份）
5. （可选：从你的分支部署到服务器测）
6. 开 PR：你的分支 → main                    # CI 自动跑
7. CI「代码与安全门禁」绿 → 合并              # 合并后 feature 分支自动删
8. git checkout main && git pull            # 主线更新
9. rsync 改过的文件上服务器 + 重启服务        # 从 main 部署，生产 = main
```

口诀：**改在分支 → 先 push 再部署 → PR 合回 main → 从 main 部署。**

---

## main 分支保护（GitHub 已强制，不靠自觉）

- **禁止直接 push `main`** —— 必须开 PR（直推会被 GitHub 拦住）。
- **CI「代码与安全门禁」必须绿**才能合（自动查：密钥/PII 没进 git、Python/JS 语法没破、HTML 链接没断）。
- 合并前分支必须是**最新 main**（strict）。
- 禁止 force-push、禁止删除 main。
- **审核是建议、不强制**：CI 绿了自己也能合（两人快跑模式）。动公共件最好喊对方瞄一眼。
- 合并后 **feature 分支自动删**，不留废分支。
- （Tang 是 admin，紧急时可兜底；平时也走 PR。）

---

## 分工 & 各管各的文件（改不同文件 = 不冲突）

| 谁 | 负责 | 前端页（`site/workbench/`） | 后端 |
|---|---|---|---|
| **Tang** | 图片 + 抓取 | `banana` · `collect` · `leads` · `inspiration` | `imggen_api`(8101) · `leadgen_api`(8100) · `tikhub` · `dl_service`(8097) |
| **强哥** | 音频 + 视频 | `audio` · `assets` · `video` | `content_api`(8096) 音频/豆包部分 · 视频后端 |
| 共用 | 别乱动 | `cloud-shell.js`（侧栏/顶栏/登录） | `auth_server`(8095) |

**部署命令**（把 XXX 换成你的文件/服务）：
```bash
rsync -az --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/dapeng_server_ed25519" \
  server/XXX.py dapeng-server:/home/ubuntu/content-api/
ssh dapeng-server "sudo systemctl restart huangque-XXX"
```
> 前端唯一正本目录 = `site/workbench/`。**只部署改过的那个文件**，别整站 rsync 旧目录盖掉别人的新页面。

---

## 共享契约（碰这些先在群里说一声）

- **改共用库表结构先打招呼**：`users.db`（点数）、`content_jobs.db`（任务）是所有服务共用的契约。给 `jobs`/`users` 表加列、改字段前先通知——否则别人读它的代码会崩。私有库（`tikhub_cache.db`、`audio_assets.db`）随便改。
- **公共件同时动先对一下**：`content_api.py`、`cloud-shell.js`、`api-admin`/`api-docs`、nginx、本协作文档。
- **生产完整可还原**：服务/端口/部署配置/上游工具/从零还原步骤都记在 `deploy/生产环境清单与还原手册.md`，要和生产保持一致。

---

## 冲突 & 回滚（为什么这套值得）

- **冲突**：各开各分支 + 各管各文件，冲突很少；真撞了也是在**你自己的 PR 里解决，不会弄坏 main**——main 永远是能跑的。
- **回滚**：每次改动都是 main 上一个独立 PR/commit。坏了直接 `git revert` 那一个，干净利落。

---

## AI Agent（Codex / Claude / Cursor）

AI 是团队成员，不是临时终端。每次让 AI 改代码前，让它**先读根目录 `AGENTS.md`**（里面是给 AI 的硬规则：开工启动检查、用自己的分支、不碰服务器正本、不顺手重构、收工 7 项汇报）。AI 同样走上面的 GitHub Flow，分支名带身份：`codex/音频资产修复`、`claude/后端拆分`。

---

_有疑问当面对一遍，达成共识即可。最后更新：2026-06-29_
