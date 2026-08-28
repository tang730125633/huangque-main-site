# PR #1313 历史短剧媒体恢复运行手册

## 状态与固定范围

本手册覆盖 `tang730125633/huangque-main-site#1313` 对生产共享数据库
`/home/ubuntu/content-api/content_jobs.db` 与受控媒体目录
`/home/ubuntu/content-api/content_out/video/` 的一次性、项目级历史媒体恢复。

该操作不新增或修改数据库 Schema，不调用视频 Provider，不扣点，也不会自动扫描全部
项目。只有项目所有者在工作台明确确认后，服务端才处理该项目中“历史上报告为 2K、
但缺少现代原生媒体证据”的已采用 MP4 镜头。

- 计划维护窗口：`2026-08-30 02:00–03:00 Asia/Shanghai`。
- 执行、备份、验证与失败恢复负责人：`@LU-003`。
- 固定业务提交：以本 PR 的 LU-003 批准评论明确列出的完整 40 位 SHA 为准。
- 当前状态：最终业务 SHA 已固定，LU-003 重新批准已发布，审批后最新 main 已纯同步；等待
  最终候选推送、CI 与最新审核。本文不是合并、部署或生产执行指令。

若批准后业务代码、测试语义、数据库写入逻辑或媒体生命周期规则发生变化，原批准立即
失效，必须重新固定业务 SHA 并重新审批。批准后只允许追加评论 URL、评论 ID、作者、
关联、时间和最终验证结果。

## 生产前置门禁

1. PR 已包含批准时最新 `main`，固定业务 SHA 的 CI 与双轴审查均通过。
2. LU-003 批准评论必须同时写明：PR、固定业务 SHA、上述维护窗口、生产数据库与媒体
   目录绝对路径、负责人、备份/验证/回滚要求；缺一项即不得执行。
3. 收到独立部署和生产操作指令；本 PR 的合并或批准本身不授权部署、重启或数据操作。
4. 固定业务 SHA 必须覆盖本 PR 的完整业务 patch。候选/发布 HEAD 如因严格主线门禁位于其后，
   只允许包含已验证的纯 `main` 同步或本手册审批证据变化；必须记录最终 HEAD，并用提交差异证明
   固定业务 SHA 之后没有本 PR 的业务代码、测试语义、数据库写入或媒体生命周期变化。
5. 目标项目不存在 `billing`、`queued`、`submitting`、`running`、`submit_unknown` 的
   Provider 任务，不存在活动候选采用、精修或合成任务。恢复接口会在文件处理前检查一次，并在
   每个镜头写库的 `BEGIN IMMEDIATE` 事务内再次检查；任一活动任务都会以 HTTP 409
   `legacy_media_recovery_busy` 拒绝，且本次镜头不得留下数据库或媒体文件副作用。
6. 记录目标项目 ID、所有者、已确认 plan/version、候选镜头、当前采用版本、源文件路径、
   SHA-256、大小、mtime，以及数据库主文件、`-wal`、`-shm` 的大小和 mtime。
7. 任一路径、所有者、提交 SHA、维护窗口或候选集合与批准不一致时立即取消。

## WAL 一致性在线备份

在窗口开始后暂停连接该共享库的短剧写入和后台任务，等待在飞事务结束。使用 SQLite
Online Backup API、`sqlite3.Connection.backup()` 或 SQLite CLI `.backup` 创建一致
副本；WAL 模式下禁止用 `cp`、`scp`、`rsync` 单独复制 `content_jobs.db`。

备份文件放入受控备份目录，名称包含 UTC 时间、PR 号和固定业务 SHA；权限不得宽于源库。
记录源库 `PRAGMA journal_mode`、备份 SHA-256，并在备份副本执行：

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

`integrity_check` 必须只返回 `ok`，`foreign_key_check` 必须为空。还要核对目标项目、
`short_drama_provider_shot_jobs`、`short_drama_provider_shot_versions`、
`short_drama_provider_shot_selections` 和共享 `jobs` 的行数及目标行快照。任一检查失败即取消。

## 执行步骤

1. 只部署已经 push 且与批准对象一致的提交，按独立部署指令重启 `huangque-content`；先做
   健康检查和未登录路由检查，确认新路由存在但未泄漏项目。
2. 由目标项目所有者登录工作台，确认警告列出的镜头集合与备份前候选一致。
3. 点击“验证并恢复历史原片”，再次确认提示。不得使用管理员、editor、服务器脚本或
   直接 SQL 绕过 owner-only HTTP 入口。
4. 服务端逐镜头创建稳定 MP4 快照，复验源文件身份、原生 2K、可听音轨、SHA-256、大小，
   再生成 faststart 派生文件。单镜头失败保持数据库与采用版本原状并清理本次临时文件。
5. 保存响应中的 `recovered_shot_keys`、`failed_shots`、`skipped_shot_keys`，以及服务日志
   时间段；不得在同一窗口内对失败镜头做直接 SQL 修补。

## 完整性、幂等与生命周期验证

1. 恢复成功镜头的采用版本指向新 faststart MP4；任务结果同时包含 raw/derived 文件、
   两份 SHA-256/大小、分辨率、可听音轨和
   `legacy_media_recovery.operation_version=legacy-native-media-recovery-v1`。
2. 对每个成功镜头重新计算文件 SHA-256、大小并执行媒体探测，结果必须与数据库证据完全
   相等；源历史文件、快照和派生文件均位于受控 `content_out/video/` 内。
3. 刷新工作台后，相应镜头从 `media_verification_missing_shot_keys` 消失；真实低分辨率镜头
   仍保持阻塞，不得被恢复路径误标为原生 2K。
4. 在不改变项目状态的情况下由同一所有者再次执行；必须返回零个恢复镜头，数据库目标行、
   采用版本和文件集合均不变化，以证明幂等。
5. 执行原生媒体孤儿清理验证：数据库仍引用的 `legacy_recovery_raw_*` 必须保留；构造于 QA
   目录、超过宽限期且无任何数据库引用的同前缀文件必须被删除。不得用生产用户文件造测试。
6. 再次执行源库 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`，并核对备份前后非目标
   项目及业务计数不变。全部通过后才恢复写流量。

## 失败恢复与回滚

单镜头校验失败时优先保留原状态并调查，不回滚整个共享库。若应用异常，先停止新的恢复
请求并切回稳定应用提交；旧应用会忽略新增 JSON 证据，不删除数据库或媒体文件。

只有发生数据库损坏、外键失败、非目标行变化或采用版本无法恢复，且 LU-003 对“共享库
整体恢复”另行明确批准时，才继续停写、停止所有数据库连接，用已验证的 online backup
整体恢复。恢复后重新执行 SHA-256、完整性、外键、目标行和全局业务计数核对。

数据库恢复后，根据本次响应和日志精确识别本次新建的 raw/derived 文件；确认备份数据库
无任何引用后，才可按显式清单清理。禁止按目录或通配符批量删除。数据丢失边界为零：
备份前停止写入，验证或恢复完成前不恢复流量。

## 历史批准证据（已失效，仅供追溯）

以下批准早于本轮工作台业务语义修复。根据本手册固定规则，该批准不再授权当前候选提交；
新的固定业务 SHA、批准评论和最终验证结果必须在发布后另行追加。

- 批准评论 URL：
  `https://github.com/tang730125633/huangque-main-site/pull/1313#issuecomment-5450348004`。
- GitHub 评论 ID / Node ID：`5450348004` / `IC_kwDOS66oj88AAAABRN215A`。
- 作者与关联：`LU-003 / COLLABORATOR`。
- 创建与更新时间：`2026-08-28T08:38:51Z` / `2026-08-28T08:38:51Z`；发布后未编辑。
- 审批对象核对：固定业务 SHA `ba1870ed0cf3831002248167e63041d556ecc05b`、
  strict-main 候选 HEAD `f728e6d54ed9801729873d2253c409ee01bd0442`、
  同步 main `c8e8d2bb94d8b65eadea6e60fc3c7d78d5cac6a1` 均与评论正文完全一致。
- 最终验证结果：Standards/Spec 双轴无 P0-P3；自动草稿 113、精修 116、媒体/CI/资源戳
  70、工作台 125、最新主线 Director/CLI 65 项均通过；两份 OpenAPI SHA-256 一致，
  `scripts/ci_validate.py`、资源戳、语法、`git diff --check` 与 strict-main 门禁通过。

## 当前批准证据（发布后追加）

- 批准评论 URL：
  `https://github.com/tang730125633/huangque-main-site/pull/1313#issuecomment-5451136589`。
- GitHub 评论 ID / Node ID：`5451136589` / `IC_kwDOS66oj88AAAABROm-TQ`。
- 作者与关联：`LU-003 / COLLABORATOR`。
- 创建与更新时间：`2026-08-28T09:58:26Z` / `2026-08-28T09:58:26Z`；发布后未编辑。
- 审批对象核对：固定业务 SHA 与候选 HEAD 均为
  `b04f4116c64258b175c2a56b6d233f15fd029482`，同步 main 为
  `a73ad605b9080b4b52d7bb915c95bda44ffb599d`，均与评论正文完全一致。
- 授权边界核对：评论明确取代旧批准，并重申本批准不等于合并、部署、服务重启或生产数据
  操作指令；后续业务逻辑变化将使本批准失效。
- 审批后 strict-main 同步：`main` 推进到 `4301c7f1eb6681c4877cc6cea04bf50843308929`，
  通过纯合并提交 `d91db54f39cbcaeb6d4b598be044cac927a14560` 纳入；上游只修改 HyperFrames、
  Creator Agent 与 HQ CLI 文件，不触及本轮短剧工作台业务文件。固定业务 SHA 与同步后候选的
  工作台 JS/CSS、短剧 HTML 和对应测试逐字节无差异。
- 固定业务 SHA 本地验证：Standards/Spec 双轴无 P0-P3；工作台 127、CI 正式 JavaScript
  清单 290、历史恢复/媒体生命周期/完成完整性 Python 158 项均通过；`scripts/ci_validate.py`、
  资源戳、JavaScript 语法及 `git diff --check` 通过。最终候选仍须以推送后的 required CI 和
  最新远端审核为准。
