# AI 协作规则

这份文件给 Codex、Claude、Cursor 等 AI agent 读取。团队完整协作说明见 `docs/团队Git协作规矩.md`，生产环境说明见 `deploy/生产环境清单与还原手册.md`。

## 开工前

每次改代码前先执行并向用户说明当前状态：

```bash
git fetch origin --prune
git status --short --branch
git branch --show-current
git log --oneline -5
```

- **`main` 已上分支保护**：禁止直接 push，必须开 PR；CI「代码与安全门禁」绿了才能合，合并后分支自动删。
- 用自己的分支（从最新 `main` 开）：`codex/<任务>`、`claude/<任务>`、`feature/<任务>`。`design-sync` 已废弃删除，别用。
- 完整流程：`git checkout main && git pull` → 开分支 → 改 → `commit && push` → 开 PR → CI 绿 → 合并 → 从 `main` 部署。
- 如果发现本地有别人未提交的改动，不要覆盖、reset、checkout 或删除，先说明。

## 修改范围

- 只改本次任务需要的文件，不做无关重构。
- 公共文件要特别谨慎：`server/content_api.py`、`site/workbench/assets.html`、`site/workbench/audio.html`、`site/workbench/cloud-shell.js`、`site/api-admin/index.html`、`site/api-docs/openapi.json`、数据库 schema。
- 前端工作台唯一正本目录是 `site/workbench/`。
- 后端服务按文件和端口拆分，具体归属见 `docs/团队Git协作规矩.md`。

## 禁止事项

- 禁止把服务器当正本直接改代码。
- 禁止提交密钥、密码、cookie、数据库、用户数据和生成产物：`*.env`、`*.db`、`content_out/`、`browser_data/`、`data/`。
- 禁止整站 rsync 旧目录覆盖线上页面。
- 禁止在未确认的情况下改公共数据库表结构。

## 提交与部署

- 改完先 commit，再 push 到自己的分支。
- 需要部署时，只部署本次改过的文件，并从已经 push 的 commit 部署。
- 部署后说明是否重启服务、验证了什么。

收工汇报必须包含：

```text
分支：
提交：
修改文件：
是否部署：
部署文件：
是否重启服务：
验证结果：
风险/未完成：
```
