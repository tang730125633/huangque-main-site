# PR #1436 视频 Agent 共享数据库迁移与回滚手册

## 状态与适用范围

本手册覆盖 `tang730125633/huangque-main-site#1436` 对生产共享数据库
`/home/ubuntu/content-api/content_jobs.db` 的结构升级。目标服务器为 `dapeng-server`。

当前状态：**业务与 Schema 已重新固定，旧审批因业务修复及 `main` 合成失效；等待 LU-003 对新固定对象重新批准，以及精确 HEAD CI/审核。本文件不是合并、部署或重启授权。**

本次结构变化包括：

- 新建 `video_agent_pending_actions` 表及 `idx_video_agent_pending_user` 索引；
- 新建或替换唯一部分索引 `idx_video_agent_pending_live_input`，其活动状态范围为
  `awaiting_confirmation`、`confirming`、`result_unknown`；
- 兼容早期候选库，为 `video_agent_pending_actions` 增加 `submission_key TEXT`、
  `payload_json TEXT`；
- 为权威 `jobs` 任务账本增加 `submission_key TEXT` 及部分索引
  `idx_jobs_submission_key(username, submission_key) WHERE submission_key IS NOT NULL`。

以上均为向后兼容的增量结构。历史 `jobs.submission_key` 保持 `NULL`，不得用账号、时间、
价格或任务类型推测其归属。应用回滚时保留新增表、列和索引，不执行破坏性逆向 DDL。

## 固定审批对象

- PR：`tang730125633/huangque-main-site#1436`
- 固定业务与 Schema 提交：`59d696be08719df5104908a21c6de3a13c92f2e0`
- 基线：`main@36d2693f72359cb3c8252f98958a604261a1bb48`
- 维护窗口：`2026-09-06 02:00–03:00 Asia/Shanghai`
- 执行、备份、验证、失败恢复负责人：`@LU-003`
- 已失效阶段一审批评论：
  [issuecomment-5537682411](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5537682411)
  - 评论 ID：`5537682411`
  - Node ID：`IC_kwDOS66oj88AAAABShJT6w`
  - 作者：`LU-003`
  - 作者关联：`COLLABORATOR`
  - 创建及更新时间：`2026-09-04T08:13:51Z`
  - 该评论绑定旧业务与 Schema 提交 `5713c83ecaf7b23d133f5da6b0c16a5afa3e1aa7`，
    已因本轮业务修复和 `main` 合成失效，不得用于当前候选。
- 已失效阶段一审批证据更正：
  [issuecomment-5537746449](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5537746449)
  - 评论 ID：`5537746449`
  - Node ID：`IC_kwDOS66oj88AAAABShNOEQ`
  - 作者：`LU-003`
  - 作者关联：`COLLABORATOR`
  - 创建及更新时间：`2026-09-04T08:20:08Z`
  - 绑定计数更正治理提交：`8f1a52b2033610601d61ae7f8a59796bfa38cf3d`；该证据不能批准当前候选。
- 当前阶段一审批：等待 `@LU-003` 明确批准上述固定业务与 Schema 提交、基线、数据库路径、维护窗口、负责人、备份、验证及回滚方案。
- 阶段二最终 HEAD 审批：待一次性推送、精确 HEAD CI 和最新审核通过后，由 `@LU-003` 对最终 HEAD 发布。
- 已失效历史审批：
  [issuecomment-5536582840](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5536582840)、
  [issuecomment-5536612768](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5536612768)。
  二者绑定旧业务 SHA `24e67cd43b082a646586ea02f1b4241c5dae30d1`，因本轮业务与测试修复而失效。

固定提交后只允许追加审批评论元数据和验证证据。若业务代码、测试、Schema 或合入的 `main`
再次变化，阶段一审批立即失效，必须重新固定 SHA、重跑验证并取得新审批；不得把 `main` 同步视为自动无影响。

## 部署前门禁

1. PR 精确 HEAD 的 required CI、最新审核、阶段一和阶段二审批全部通过，并另行收到部署授权。
2. 窗口开始前停止所有连接该数据库的写入，包括内容生成、视频 Agent、后台任务、退款和付费任务提交；
   确认运行中的 Provider 提交、未提交事务及迁移进程均为零。
3. 核对服务器、绝对路径、待部署提交与审批记录完全一致；窗口过期或任一值不符即取消。
4. 记录主库、`-wal`、`-shm` 的大小、所有者、权限、修改时间和源库 SHA-256。
5. 记录迁移前计数：`jobs` 总数及各状态数、付费任务 pending/running 数、
   `video_agent_pending_actions`（若存在）总数及各状态数、`submission_idempotency` 总数。

## WAL 一致性备份

1. 保持写入暂停，使用 SQLite Online Backup API 或 `.backup` 创建一致副本；WAL 模式下禁止只复制主文件。
2. 记录 `PRAGMA journal_mode` 以及源库 `-wal`、`-shm` 状态。
3. 计算备份文件 SHA-256，并在副本执行 `PRAGMA integrity_check`（必须为 `ok`）与
   `PRAGMA foreign_key_check`（必须为空）。
4. 在备份副本核对所有迁移前计数。任一不一致时停止，不得启动新版本。

## 执行与验证

1. 在临时空库执行 `core.init_db()` 和视频 Agent 初始化，核对新表、两项新增列及三个索引定义。
2. 在包含历史 `jobs`、早期 pending 表和旧 live 索引的数据库副本上升级：
   - 历史 `jobs` 行及业务计数不变，新增 `submission_key` 为 `NULL`；
   - 未提交的旧指纹报价安全取消；
   - `confirming` 在恢复流程中转为 `result_unknown`，不得被再次提交；
   - 重复活动卡片仅保留优先级最高的一张作为硬阻塞，其余标记取消；
   - 非等待/确认状态的旧报价令牌被清空。
3. 连续执行两次初始化，确认第二次无错误、无重复 DDL、无业务数据改写。
4. 从已固定且已 push 的提交部署本次文件，并仅在收到独立授权后重启对应内容服务一次。
5. 启动后用 `PRAGMA table_info`、`sqlite_master` 核对列和索引 SQL；再次执行完整性、外键和迁移前后计数核对。
6. 验证精确对账：新付费任务在插入 `jobs` 的同一事务写入请求 `submission_key`；
   `result_unknown` 只按 `username + submission_key + capability 允许的 kind` 命中，错误 key、空 key及历史 `NULL` 均不得认领。
7. 验证素材门禁与真实报价合同：talking 的当前账户已核验私有图片加文案和音色可报价；
   talking 不得同时传 `avatar_id` 与 `image_upload_id`；story 的普通参考图片加剧本仍不得 ready，
   ready avatar 加剧本必须 ready；无源视频 compose、尺寸或 SHA-256 不一致上传均被拒绝。
8. 所有检查通过后才恢复写入；恢复后观察首批请求、任务状态、退款和错误日志，不调用真实付费 Provider 做验收。

自动化验证至少包括：

```powershell
python -m unittest tests.test_video_agent tests.test_video_agent_tools tests.test_video_agent_capability_contract tests.test_cli_media_uploads tests.test_cli_image_uploads tests.test_video_batch tests.test_jobs_store tests.test_hq_cli_content -v
python -m unittest tests.test_hq_cli_api -v
python -m unittest discover -s tools/hq-cli/tests -p "test_*.py" -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

本地候选验证证据：视频 Agent、委托令牌、CLI 执行器、素材所有权、报价确认、幂等、退款及
Provider 密钥相邻回归共 `368/368` 通过；HQ CLI 回归 `87/87` 通过；仓库静态门禁、资源版本戳、
Python 语法及 `git diff --check` 均通过。仓库历史测试会修改 `sys.path` 并复用 `server` 等通用模块名，
因此单进程全量发现存在顺序污染，不能作为可信结论；required CI 改为每个文件使用独立解释器执行
本 PR 全部变更测试及相邻安全回归。受支持 Linux 环境的最终结论必须以一次性推送后的精确 HEAD
required CI 为准，不得用 Windows 结果替代。

## 失败回滚

失败时继续停止写入，保留现场数据库、WAL/SHM、服务日志、失败步骤和计数。优先切回已知稳定的应用提交，
保留向后兼容的新增表、列和索引；旧应用会忽略这些结构，不执行 `DROP TABLE`、`DROP COLUMN` 或历史行回填。

只有在数据库损坏、完整性/外键检查失败或业务计数不一致，并由 `@LU-003` 对该次恢复明确重新授权时，
才停止所有连接并从已校验的一致备份整体恢复。恢复后复核 SHA-256、完整性、外键和全部业务计数，
全部通过才恢复流量。数据丢失边界为零：备份前停止全部写入，验证或恢复完成前不恢复写入。
