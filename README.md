# 黄雀 AI 测试环境（ubuntu-fang-server）

> 本仓库是黄雀 AI 主站代码在独立测试服务器上的开发与验证仓库，必须保持为私有仓库。
>
> 测试站：<http://8.134.216.162/>
>
> 生产主站：<https://huangquechuanmei.com/>（仅作对照，禁止从测试分支直接部署）

这里用于先开发、先验证，再由团队通过 Git 合并到主线。日常修改只能落在本仓库分支和测试服务器，不得直接修改黄雀传媒生产主站。

## 环境边界

| 项目 | 测试环境 | 生产环境 |
|---|---|---|
| 用途 | 开发、联调、验收 | 正式对外服务 |
| GitHub 仓库 | `kong74007-ui/ubuntu-fang-server` | 团队生产主线 |
| 服务器 | `8.134.216.162` | 见生产环境手册 |
| 代码检出目录 | `/home/admin/ubuntu-fang-server` | 见生产环境手册 |
| 静态网页目录 | `/var/www/html/` | `/var/www/huangquechuanmei/` |
| 部署来源 | 已 push 的测试分支提交 | 合并并通过 CI 的 `main` 提交 |
| 修改权限 | 可以按本 README 部署测试 | 未经团队合并流程禁止修改 |

测试服务器当前核心运行目录：

- 认证服务：`/home/ubuntu/auth-service`（`huangque-auth`）
- 内容生成服务：`/home/ubuntu/content-api`（`huangque-content`）
- 前端页面：`/var/www/html/`

服务器运行目录不是代码正本。所有修改必须先在本地仓库完成、提交并 push，再部署到测试服务器。

## 开发与协作流程

开始改代码前先阅读：

- [AGENTS.md](AGENTS.md)：AI/Codex/Claude/Cursor 必须遵守的规则
- [团队 Git 协作规矩](docs/团队Git协作规矩.md)：分支、PR 和公共文件约定
- [DESIGN.md](DESIGN.md)：界面修改规范

标准流程：

```bash
git fetch origin --prune
git status --short --branch
git checkout main
git pull
git checkout -b codex/<任务名>

# 修改并测试
git add <本次文件>
git commit -m "<提交说明>"
git push -u origin codex/<任务名>
```

协作原则：

- `main` 有分支保护，禁止直接 push，必须通过 PR 和 CI。
- 使用 `codex/<任务>`、`claude/<任务>` 或 `feature/<任务>` 分支。
- `design-sync` 已废弃，禁止继续使用。
- 测试分支可以部署到测试服务器验证，但不得部署到生产主站。
- 同事统一合并时，以 Git 提交和 PR 为准，不以服务器上的文件为准。
- 发现别人的未提交改动时，不覆盖、不 reset，先沟通。

## 测试站部署

只部署本次修改的文件，并且必须从已经 push 的提交部署。禁止整站 `rsync --delete`，禁止用旧目录覆盖整个站点。

前端单文件示例：

```bash
TEST_SERVER=admin@8.134.216.162

scp site/workbench/video.html "$TEST_SERVER:/tmp/video.html"
ssh "$TEST_SERVER" \
  'sudo install -m 0644 /tmp/video.html /var/www/html/workbench/video.html'
```

后端部署要求：

1. 只上传本次修改的后端文件。
2. 安装到对应运行目录。
3. 仅重启受影响的 systemd 服务。
4. 检查服务状态、接口响应和页面功能。
5. 汇报分支、提交、部署文件、重启服务和验证结果。

生产部署不使用上面的测试命令。生产环境只能在团队完成合并后，按[生产环境清单与还原手册](deploy/生产环境清单与还原手册.md)执行。

## 产品模块

前端工作台唯一正本目录是 `site/workbench/`。

| 模块 | 主要文件 | 功能 |
|---|---|---|
| 灵感设计 | `inspiration.html` | 案例与创作灵感 |
| 平台获客 | `leads.html` | 搜索、评论采集和意向客户筛选 |
| 内容爬取 | `collect.html` | 抖音、小红书、视频号内容采集与转写 |
| 图片生成 | `banana.html` | 文生图、图生图和图片编辑 |
| 视频生成 | `video.html` | 数字人口播、电影化身、换装换背景及多渠道视频生成 |
| 音频生成 | `audio.html` | TTS、公共音色、个人音色和声音复刻 |
| 文案编导 | `script.html` | 营销文案和分镜脚本 |
| 无限画布 | `canvas.html` | 节点式内容生产画布 |
| 我的资产 | `assets.html` | 图片、视频、音频和个人形象管理 |
| 教程视频 | `tutorials.html` | 使用教程 |
| 通用设置 | `settings.html` | 账号和工作台设置 |

`huangque-web/` 是历史遗留副本，不是当前前端正本，不要在里面开发工作台页面。

## 核心架构

测试站沿用主站的服务拆分，但使用测试服务器自己的运行文件和数据库：

```text
浏览器
  └─ nginx /var/www/html
      ├─ /api/auth/*  → huangque-auth    :8095
      ├─ /api/gen/*   → huangque-content :8096
      └─ 静态工作台   → site/workbench/
```

主要代码位置：

- `server/auth_server.py`：账号、登录、点数和充值
- `server/content_api.py`、`server/content_domains/`：生成任务与业务能力
- `site/workbench/`：工作台前端
- `site/admin/`：运营后台
- `tests/`：自动化测试
- `deploy/`：生产配置和参考手册，不等于测试站部署命令

## 本地验证

```bash
python -m unittest discover -s tests
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

修改 JavaScript 后还应执行语法检查；修改页面后应在测试站实际打开并验证目标流程。

## 数据与密钥红线

以下内容不得提交到 Git：

- API Key、密码、Cookie、Token 和任何 `.env`
- `*.db` 数据库
- `content_out/`、`browser_data/`、`data/` 等运行数据和生成产物
- 用户数据、点数流水、客户名单及其他隐私数据

测试服务器的密钥和数据库只存在于服务器运行目录。可以按任务需要在测试环境验证，但不得把它们复制进仓库，也不得用测试数据库覆盖生产数据库。

## 生产资料说明

- [生产环境清单与还原手册](deploy/生产环境清单与还原手册.md)描述的是黄雀传媒生产主站，不是本测试服务器。
- [团队 Git 协作规矩](docs/团队Git协作规矩.md)中的生产地址和 `dapeng-server` 命令仅用于生产流程。
- 如果旧文档仍出现 `design-sync`，以 [AGENTS.md](AGENTS.md) 为准：该分支已经废弃。
- 测试站的开发成果应通过分支和 PR 交给同事合并，不直接反向覆盖生产服务器。

## 目录结构

```text
server/          后端服务与领域模块
site/            前端正本、运营后台和接口文档
tests/           Python/JavaScript 自动化测试
scripts/         CI、校验、部署辅助和维护脚本
deploy/          生产 nginx/systemd 配置与生产手册
docs/            团队协作和专项说明
design-system/   设计系统
worker/          获客采集 worker
huangque-web/    历史遗留副本（勿改工作台）
```
