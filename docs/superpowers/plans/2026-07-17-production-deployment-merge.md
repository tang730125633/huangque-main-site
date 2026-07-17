# 主站部署合并到测试服务器实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将主站已部署提交 `fde51876fe5883497080b1170b63be2126f1f696` 合入测试服务器代码历史并部署，同时保留测试服务器 PR1、PR2、XAI 视频续跑、数据库、用户数据、产物和测试密钥。

**Architecture:** 双方提交历史无共同祖先，但测试仓库根提交 `dfe9419` 与主站提交 `ad82397` 的文件树完全相同。先在独立 worktree 中以 `ours` 策略建立双亲合并状态，再基于等价文件树核对主站 14 个路径的增量；12 个 XAI 路径已经与测试分支最终内容相同，只纳入主站独有的 `CLAUDE.md` 和 `deploy/setup-dev-server.sh`。测试通过后推送集成分支，把运行目录以 detached HEAD 切到已验证提交。

**Tech Stack:** Git 2.34、Bash、Python 3 unittest、systemd、Nginx、curl、SQLite 文件备份。

## Global Constraints

- 主站基线固定为 `fde51876fe5883497080b1170b63be2126f1f696`，执行期间不得静默升级到更新提交。
- 当前测试部署基线固定为 `2605cc5c494ca78e82edfe83f7afb47d00326839`。
- 保留测试仓库 PR1、PR2 和 XAI 视频续跑行为。
- 不复制或覆盖主站数据库、用户、点数、任务、生成产物、上传文件、密钥、Cookie、Token、证书或 xray 配置。
- 保留 `/etc/huangque-test/` 的现有值；任何检查只能输出变量名称和是否存在，不得输出密钥值。
- 无法明确判断的内容冲突必须停止并报告，不得自动选择一侧。
- 任一部署前门禁失败不得切换运行目录；任一部署后关键检查失败必须回滚到 `2605cc5c494ca78e82edfe83f7afb47d00326839`。

---

### Task 1: 创建隔离集成分支并锁定双方基线

**Files:**
- Existing repo: `/opt/huangque-test-server`
- Create worktree: `/opt/huangque-test-worktrees/production-deployment-merge`
- Branch: `codex/merge-production-deployment`

**Interfaces:**
- Consumes: 执行时 `codex/xai-video-resume` 的最新文档提交（包含部署提交 `2605cc5`、设计和本计划）、远端引用 `upstream/main`
- Produces: 干净的独立集成 worktree，且两个等价快照已经被机器验证

- [ ] **Step 1: 验证当前运行目录没有未跟踪或未提交变化**

Run:

```bash
cd /opt/huangque-test-server
sudo -u admin git status --short
sudo -u admin git rev-parse HEAD
```

Expected: 第一条无输出；第二条输出 `2605cc5c494ca78e82edfe83f7afb47d00326839`。

- [ ] **Step 2: 验证主站只读引用没有漂移**

Run:

```bash
cd /opt/huangque-test-worktrees/xai-video-resume
sudo -u admin git rev-parse upstream/main
```

Expected: `fde51876fe5883497080b1170b63be2126f1f696`。

- [ ] **Step 3: 创建独立集成 worktree**

Run:

```bash
test ! -e /opt/huangque-test-worktrees/production-deployment-merge
cd /opt/huangque-test-server
base_sha=$(sudo -u admin git rev-parse codex/xai-video-resume)
printf '%s\n' "$base_sha" > /tmp/huangque-production-merge-base
sudo -u admin git worktree add -b codex/merge-production-deployment /opt/huangque-test-worktrees/production-deployment-merge "$base_sha"
cd /opt/huangque-test-worktrees/production-deployment-merge
sudo -u admin git status --short --branch
```

Expected: 新分支为 `codex/merge-production-deployment`，工作区干净；`/tmp/huangque-production-merge-base` 记录本次不可变第一父提交。

- [ ] **Step 4: 验证人工三方合并基线确实是同一文件树**

Run:

```bash
sudo -u admin git rev-parse dfe941972779c1cb44ef59b0a000e884e4754a48^{tree}
sudo -u admin git rev-parse ad82397f75ebe04e457616679f31335b5f8d7ec7^{tree}
sudo -u admin git diff --quiet dfe941972779c1cb44ef59b0a000e884e4754a48 ad82397f75ebe04e457616679f31335b5f8d7ec7
```

Expected: 两个 tree SHA 都是 `16f29b8ba829d5601afae3fc7279093b03707066f`，最后一条退出码为 0。

---

### Task 2: 建立双亲合并并纳入主站独有增量

**Files:**
- Modify: `CLAUDE.md`
- Create: `deploy/setup-dev-server.sh`
- Verify unchanged: `server/content_domains/core.py`
- Verify unchanged: `server/content_domains/startup_recovery.py`
- Verify unchanged: `server/content_domains/video.py`
- Verify unchanged: `server/content_domains/video_xai.py`
- Verify unchanged: `scripts/recover_xai_video_job.py`
- Verify unchanged: `tests/test_job_refund_cas.py`
- Verify unchanged: `tests/test_recover_xai_video_job.py`
- Verify unchanged: `tests/test_video_failed_asset_sync.py`
- Verify unchanged: `tests/test_video_xai.py`
- Verify unchanged: `tests/test_xiaole_video.py`
- Verify unchanged: `docs/superpowers/plans/2026-07-16-xai-video-resumable-polling.md`
- Verify unchanged: `docs/superpowers/specs/2026-07-16-xai-video-resumable-polling-design.md`

**Interfaces:**
- Consumes: Task 1 的等价树基线和固定 `upstream/main`
- Produces: 同时以测试分支与主站 `fde5187` 为父提交的集成提交

- [ ] **Step 1: 再次列出主站从等价基线以来的完整增量**

Run:

```bash
sudo -u admin git --no-pager diff --name-status ad82397f75ebe04e457616679f31335b5f8d7ec7..upstream/main
```

Expected: 恰好 14 个路径：`CLAUDE.md`、`deploy/setup-dev-server.sh`、两份 XAI 文档、`scripts/recover_xai_video_job.py`、四个 `server/content_domains/` 文件和五个 XAI/退款测试文件。若路径集合不同，停止执行并更新计划。

- [ ] **Step 2: 验证 12 个重叠路径的最终内容已经一致**

Run:

```bash
sudo -u admin git diff --quiet HEAD upstream/main -- \
  docs/superpowers/plans/2026-07-16-xai-video-resumable-polling.md \
  docs/superpowers/specs/2026-07-16-xai-video-resumable-polling-design.md \
  scripts/recover_xai_video_job.py \
  server/content_domains/core.py \
  server/content_domains/startup_recovery.py \
  server/content_domains/video.py \
  server/content_domains/video_xai.py \
  tests/test_job_refund_cas.py \
  tests/test_recover_xai_video_job.py \
  tests/test_video_failed_asset_sync.py \
  tests/test_video_xai.py \
  tests/test_xiaole_video.py
```

Expected: 退出码 0。非零时停止，不覆盖测试分支版本，先输出逐文件差异供审查。

- [ ] **Step 3: 建立可追踪的双亲合并状态**

Run:

```bash
sudo -u admin git merge --allow-unrelated-histories -s ours --no-commit upstream/main
test "$(sudo -u admin git rev-parse MERGE_HEAD)" = "fde51876fe5883497080b1170b63be2126f1f696"
```

Expected: Git 报告合并成功并停在提交前；`MERGE_HEAD` 等于固定主站提交。

- [ ] **Step 4: 从主站纳入两个尚未存在的内容变化**

Run:

```bash
sudo -u admin git checkout upstream/main -- CLAUDE.md deploy/setup-dev-server.sh
sudo -u admin bash -n deploy/setup-dev-server.sh
sudo -u admin git --no-pager diff --cached --name-status
```

Expected: shell 语法检查退出码 0；暂存差异只有 `CLAUDE.md` 和 `deploy/setup-dev-server.sh`。

- [ ] **Step 5: 创建集成合并提交**

Run:

```bash
sudo -u admin git commit -m "merge: integrate production deployment baseline"
merge_sha=$(sudo -u admin git rev-parse HEAD)
parents=$(sudo -u admin git show -s --format='%P' "$merge_sha")
test "${parents%% *}" = "$(cat /tmp/huangque-production-merge-base)"
test "${parents##* }" = "fde51876fe5883497080b1170b63be2126f1f696"
sudo -u admin git --no-pager show -s --format='%H%n%P%n%s' "$merge_sha"
```

Expected: 提交有两个父提交；第一父等于 `/tmp/huangque-production-merge-base` 记录值，第二父为 `fde51876fe5883497080b1170b63be2126f1f696`。

---

### Task 3: 执行部署前验证门禁

**Files:**
- Test: `tests/test_video_xai.py`
- Test: `tests/test_xiaole_video.py`
- Test: `tests/test_recover_xai_video_job.py`
- Test: `tests/test_job_refund_cas.py`
- Test: repository-wide `tests/`
- Verify: `site/workbench/canvas.html`
- Verify: `site/workbench/canvas/`

**Interfaces:**
- Consumes: Task 2 的集成提交
- Produces: 可部署提交及完整测试证据；任何失败都会阻止 Task 4

- [ ] **Step 1: 验证 Git 差异质量和数据保护**

Run:

```bash
base_sha=$(cat /tmp/huangque-production-merge-base)
sudo -u admin git --no-pager diff --check "$base_sha"..HEAD
sudo -u admin git --no-pager diff --name-only "$base_sha"..HEAD
```

Expected: `diff --check` 无输出；相对集成第一父的内容变化只有 `CLAUDE.md` 和 `deploy/setup-dev-server.sh`，没有 `*.db`、`server/content_out/` 或 `/etc/huangque-test/` 内容。

- [ ] **Step 2: 验证 PR2 画布资源没有被主站合并改变**

Run:

```bash
sudo -u admin git diff --quiet 2605cc5c494ca78e82edfe83f7afb47d00326839..HEAD -- site/workbench/canvas.html site/workbench/canvas/
```

Expected: 退出码 0。

- [ ] **Step 3: 运行 XAI 视频恢复与退款定向测试**

Run:

```bash
sudo -u admin python3 -m unittest \
  tests.test_video_xai \
  tests.test_xiaole_video \
  tests.test_recover_xai_video_job \
  tests.test_job_refund_cas -v
```

Expected: 全部通过，0 failures、0 errors。

- [ ] **Step 4: 运行完整 Python 测试套件**

Run:

```bash
sudo -u admin python3 -m unittest discover -s tests -v
```

Expected: 973 项或更多测试通过，0 failures、0 errors。

- [ ] **Step 5: 对全部 Python 服务入口做无副作用导入冒烟**

Run:

```bash
cd server
sudo -u admin env PYTHONDONTWRITEBYTECODE=1 python3 -c 'import auth_server, content_api, admin_api, imggen_api, leadgen_api, dl_service'
cd ..
```

Expected: 退出码 0，无 traceback。

- [ ] **Step 6: 验证现有 Nginx 和测试环境必需配置仍可用**

Run:

```bash
sudo nginx -t
for key in GEMINI_API_KEY OPENAI_API_KEY COS_SECRET_ID COS_SECRET_KEY COS_BUCKET TIKHUB_KEY RUNNINGHUB_API_KEY; do
  sudo grep -q "^${key}=." /etc/huangque-test/providers.env
done
```

Expected: Nginx 输出配置测试成功；循环退出码 0。命令只检查名称和非空状态，不输出任何密钥值。

---

### Task 4: 推送集成分支并创建部署前备份

**Files:**
- Push branch: `origin/codex/merge-production-deployment`
- Backup root: `/opt/huangque-test-backups/<timestamp>/`
- Backup: `/opt/huangque-test-server/server/*.db`
- Backup: `/etc/nginx/sites-available/huangque-test`
- Backup: `/etc/systemd/system/huangque-test-*.service`
- Backup manifests: `git-head.txt`, `sha256.txt`, `content-out-count.txt`

**Interfaces:**
- Consumes: Task 3 的已验证提交
- Produces: 远端可追踪分支和可用于回滚的数据/配置备份目录

- [ ] **Step 1: 推送集成分支**

Run:

```bash
sudo -u admin git push -u origin codex/merge-production-deployment
sudo -u admin git ls-remote origin refs/heads/codex/merge-production-deployment
```

Expected: 远端分支 SHA 等于本地 `HEAD`。

- [ ] **Step 2: 创建带时间戳的备份目录并记录旧部署提交**

Run:

```bash
stamp=$(date +%Y%m%d-%H%M%S)
backup=/opt/huangque-test-backups/$stamp
install -d -m 0700 "$backup/db" "$backup/config"
printf '%s\n' '2605cc5c494ca78e82edfe83f7afb47d00326839' > "$backup/git-head.txt"
printf '%s\n' "$backup" > /opt/huangque-test-backups/LATEST
printf '%s\n' "$backup"
```

Expected: 输出唯一备份目录路径，目录权限为 700。

- [ ] **Step 3: 在线一致性备份五个 SQLite 数据库**

Run:

```bash
backup=$(cat /opt/huangque-test-backups/LATEST)
for db in admin_config audio_assets content_jobs feature_flags users; do
  sqlite3 "/opt/huangque-test-server/server/${db}.db" ".backup '$backup/db/${db}.db'"
done
sha256sum "$backup"/db/*.db > "$backup/sha256.txt"
```

Expected: 五个数据库备份存在且非空，`sha256.txt` 有五行。使用 SQLite `.backup`，不得在服务运行时用普通 `cp` 复制数据库。

- [ ] **Step 4: 备份配置并记录产物目录计数**

Run:

```bash
backup=$(cat /opt/huangque-test-backups/LATEST)
cp -a /etc/nginx/sites-available/huangque-test "$backup/config/"
cp -a /etc/systemd/system/huangque-test-*.service "$backup/config/"
find /opt/huangque-test-server/server/content_out -type f | wc -l > "$backup/content-out-count.txt"
```

Expected: 配置备份存在；只记录产物计数，不复制、删除或覆盖产物。

---

### Task 5: 切换部署并验证线上状态

**Files:**
- Deploy checkout: `/opt/huangque-test-server`
- Verify script: `deploy/test-server/verify-full-environment.sh`

**Interfaces:**
- Consumes: Task 4 的已推送集成 SHA 和备份目录
- Produces: 运行目录指向集成提交，HTTP 与服务健康证据完整

- [ ] **Step 1: 最后确认运行目录仍处于旧基线且干净**

Run:

```bash
cd /opt/huangque-test-server
test "$(sudo -u admin git rev-parse HEAD)" = "2605cc5c494ca78e82edfe83f7afb47d00326839"
test -z "$(sudo -u admin git status --short)"
```

Expected: 两条均退出码 0。

- [ ] **Step 2: 以 detached HEAD 切换到已验证集成提交**

Run:

```bash
deploy_sha=$(sudo -u admin git -C /opt/huangque-test-worktrees/production-deployment-merge rev-parse HEAD)
sudo -u admin git checkout --detach "$deploy_sha"
test "$(sudo -u admin git rev-parse HEAD)" = "$deploy_sha"
```

Expected: checkout 成功，运行目录 HEAD 等于集成分支 SHA。

- [ ] **Step 3: 根据实际运行文件差异决定不重启服务**

Run:

```bash
sudo -u admin git --no-pager diff --name-only 2605cc5c494ca78e82edfe83f7afb47d00326839..HEAD
```

Expected: 只有合并设计/计划文档、`CLAUDE.md` 和 `deploy/setup-dev-server.sh`；没有 `server/`、`site/`、`deploy/test-server/` 或已安装配置变化。因此不执行 systemd restart、daemon-reload 或 Nginx reload。

- [ ] **Step 4: 运行完整测试环境校验**

Run:

```bash
sudo bash deploy/test-server/verify-full-environment.sh
```

Expected: `PASS: full test environment configuration and no-cost connectivity checks`。

- [ ] **Step 5: 验证核心接口和公网入口**

Run:

```bash
curl --fail --silent http://127.0.0.1/api/auth/health
curl --fail --silent http://127.0.0.1/api/gen/health
curl --location --fail --silent --output /dev/null --write-out '%{http_code}\n' http://8.138.143.64/
curl --location --fail --silent --output /dev/null --write-out '%{http_code}\n' http://8.138.143.64/workbench/canvas.html
```

Expected: 两个健康接口 JSON 含 `"ok": true`；两个公网请求均输出 `200`。

- [ ] **Step 6: 验证部署版本、数据和产物未变化**

Run:

```bash
backup=$(cat /opt/huangque-test-backups/LATEST)
test -z "$(sudo -u admin git status --short)"
sha256sum -c "$backup/sha256.txt"
test "$(find /opt/huangque-test-server/server/content_out -type f | wc -l)" -ge "$(cat "$backup/content-out-count.txt")"
sudo -u admin git --no-pager log -1 --format='%H %P %s'
```

Expected: Git 干净；五个数据库备份校验为 `OK`；产物文件计数未减少；最终日志显示集成提交及两个父提交。

---

### Task 6: 失败时执行回滚并验证恢复

**Files:**
- Restore checkout: `/opt/huangque-test-server` at `2605cc5c494ca78e82edfe83f7afb47d00326839`
- Restore config from: `/opt/huangque-test-backups/<timestamp>/config/`

**Interfaces:**
- Consumes: Task 4 的 `$backup` 路径；仅在 Task 5 任一关键检查失败时执行
- Produces: 恢复旧部署提交和健康服务；保留失败分支、提交与日志

- [ ] **Step 1: 切回旧部署提交**

Run:

```bash
cd /opt/huangque-test-server
backup=$(cat /opt/huangque-test-backups/LATEST)
sudo -u admin git checkout --detach 2605cc5c494ca78e82edfe83f7afb47d00326839
```

Expected: HEAD 回到 `2605cc5c494ca78e82edfe83f7afb47d00326839`。

- [ ] **Step 2: 仅在配置被修改过时恢复配置并重载服务**

Run:

```bash
cp -a "$backup/config/huangque-test" /etc/nginx/sites-available/huangque-test
cp -a "$backup/config/"huangque-test-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart huangque-test-auth huangque-test-content huangque-test-admin huangque-test-imggen huangque-test-leadgen huangque-test-dl
nginx -t
systemctl reload nginx
```

Expected: 仅当 Task 5 前后确实改过配置时执行；所有命令退出码 0。本次预期没有配置变化，因此正常路径不执行此步骤。

- [ ] **Step 3: 再次运行完整环境检查**

Run:

```bash
sudo bash deploy/test-server/verify-full-environment.sh
curl --location --fail --silent --output /dev/null --write-out '%{http_code}\n' http://8.138.143.64/
```

Expected: 完整环境检查 PASS，公网返回 `200`。不得删除或强推失败的集成分支。

---

## 最终交付证据

- 集成分支名和远端 SHA。
- 双亲合并提交的两个父 SHA。
- 定向测试与完整测试的通过数量。
- 备份目录路径和五个数据库校验结果。
- 部署后完整环境检查结果、两个健康接口结果和两个公网 HTTP 状态。
- 最终运行目录 HEAD 与干净状态。
