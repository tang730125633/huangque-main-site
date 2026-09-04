# PR #1436 视频 Agent 共享数据库迁移与回滚手册

## 状态与适用范围

本手册覆盖 `tang730125633/huangque-main-site#1436` 对生产共享数据库
`/home/ubuntu/content-api/content_jobs.db` 的结构升级。目标服务器为 `dapeng-server`。

当前状态：**最新审核修复已固定，等待受支持 Linux 完整回归及新的 LU-003 阶段一审批；本文件不是合并、部署或重启授权。**

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
- 固定业务、测试与 CI 提交：`52516e40534a6acf24b294703e7012c589fca403`
- 基线：`main@f7c2463c201946c2ce9344ae21f06d06e9433f61`
- 维护窗口：`2026-09-06 02:00–03:00 Asia/Shanghai`
- 执行、备份、验证、失败恢复负责人：`@LU-003`
- 最新审核：
  [issuecomment-5537924343](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5537924343)，
  其提示路由与 Linux 完整回归两项 P1 已在固定提交中修复或加入强制 CI 验证。
- 新阶段一审批：待 Linux 完整回归验证通过后由 `LU-003` 发布并绑定上述固定提交。
- 阶段二最终 HEAD 审批：待 PR 精确 HEAD CI 和最新审核通过后发布。
- 已失效历史审批：
  [issuecomment-5536582840](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5536582840)、
  [issuecomment-5536612768](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5536612768)、
  [issuecomment-5537682411](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5537682411)、
  [issuecomment-5537746449](https://github.com/tang730125633/huangque-main-site/pull/1436#issuecomment-5537746449)。
  前两者绑定业务 SHA `24e67cd43b082a646586ea02f1b4241c5dae30d1`，后两者绑定业务 SHA
  `5713c83ecaf7b23d133f5da6b0c16a5afa3e1aa7`；均因后续业务、测试或 CI 修复而失效。

固定提交后只允许追加审批评论元数据、验证证据或纯 `main` 同步。若业务代码、测试或 Schema
再次变化，阶段一审批立即失效，必须重新固定 SHA、重跑验证并取得新审批。

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
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

本地固定提交验证证据：任务相关模块 `193/193`、主线 HQ CLI API `72/72`，合计
`265/265`；HQ CLI 独立回归 `87/87`；仓库静态门禁、资源版本戳、Python 语法及
`git diff --check` 均通过。Windows 单进程全量发现运行 4715 项，结果为 52 failures、118 errors、
46 skipped；在隔离的 `main@f7c2463c` 工作树中，审核点名的短剧恢复与 Sora 恢复失败可同样复现，
而 PR 独有的 Seedance 顺序回归已修复并通过 19/19 模块测试。新增的
`仓库完整回归 · Linux` job 会在 Ubuntu/Python 3.12 安装真实浏览器与仓库约束依赖，并执行未缩减的
repo-root unittest discovery；该 job 绿色前不得取得阶段一审批或更新 PR，不得用 Windows 结果替代。

## 失败回滚

失败时继续停止写入，保留现场数据库、WAL/SHM、服务日志、失败步骤和计数。优先切回已知稳定的应用提交，
保留向后兼容的新增表、列和索引；旧应用会忽略这些结构，不执行 `DROP TABLE`、`DROP COLUMN` 或历史行回填。

只有在数据库损坏、完整性/外键检查失败或业务计数不一致，并由 `@LU-003` 对该次恢复明确重新授权时，
才停止所有连接并从已校验的一致备份整体恢复。恢复后复核 SHA-256、完整性、外键和全部业务计数，
全部通过才恢复流量。数据丢失边界为零：备份前停止全部写入，验证或恢复完成前不恢复写入。
