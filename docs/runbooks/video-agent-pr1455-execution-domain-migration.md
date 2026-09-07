# PR #1455 视频 Agent `execution_domain` 共享数据库迁移手册

## 状态与适用范围

本手册覆盖 `tang730125633/huangque-main-site#1455` 对生产共享数据库
`/home/ubuntu/content-api/content_jobs.db` 的增量结构升级。

当前状态：**业务与 Schema 已固定并取得 LU-003 阶段一批准；等待本证据版本化后的最终
HEAD 阶段二批准及有效 GitHub 审核。本文件不是合并、部署、迁移、重启或数据库恢复授权。**

本次结构变化仅为已有 `video_agent_pending_actions` 表增加：

```sql
execution_domain TEXT NOT NULL DEFAULT 'remote'
```

该列区分本地主站提交与远端 CLI 提交。历史行按默认值 `remote` 解释，不进行基于用户、时间、
任务类型或报价内容的推测回填。变更为向后兼容的增量列；应用回滚时保留该列，不执行
`DROP COLUMN`、重建表或破坏性逆向迁移。

## 固定审批对象

- PR：`tang730125633/huangque-main-site#1455`
- 固定业务与 Schema 提交：`98aa650e10955c5dce5c91d98d2915a3352ae9f9`
- 基线：`main@3197f4608c03112094f384840ce3ea713a1e2a61`
- 生产数据库：`/home/ubuntu/content-api/content_jobs.db`
- 维护窗口：`2026-09-07 10:00–23:30 Asia/Shanghai`
- 执行、备份、验证与失败回滚负责人：`@LU-003`
- 已失效阶段一批准：
  [issuecomment-5565362511](https://github.com/tang730125633/huangque-main-site/pull/1455#issuecomment-5565362511)
  - 评论 ID：`5565362511`
  - Node ID：`IC_kwDOS66oj88AAAABS7ixTw`
  - 作者：`LU-003`
  - 作者关联：`COLLABORATOR`
  - 创建及更新时间：`2026-09-07T05:13:00Z`
  - 绑定旧业务/Schema SHA `03ea21d69cd6e19c9a70a6a2001a3e35a6923f15`；因后续
    HeyGen billing 路由修复失效，不得用于当前候选。
- 已失效阶段二批准：
  [issuecomment-5565420409](https://github.com/tang730125633/huangque-main-site/pull/1455#issuecomment-5565420409)
  - 绑定旧最终 HEAD `b3592c820665c3d8921f9ae4230b1e8d0e1b561b`；因业务代码
    变化失效，不得用于当前候选。
- 当前阶段一批准：
  [issuecomment-5565543319](https://github.com/tang730125633/huangque-main-site/pull/1455#issuecomment-5565543319)
  - 评论 ID：`5565543319`
  - Node ID：`IC_kwDOS66oj88AAAABS7tzlw`
  - 作者：`LU-003`
  - 作者关联：`COLLABORATOR`
  - 创建及更新时间：`2026-09-07T05:32:15Z`
  - 明确绑定当前业务/Schema SHA `98aa650e10955c5dce5c91d98d2915a3352ae9f9`、
    main 基线、数据库路径、维护窗口、停写、WAL 一致性备份、升级与幂等验证及失败回滚，
    并明确取代旧阶段一批准。
- 阶段二最终 HEAD 批准：待本文版本化、一次性推送及精确 HEAD CI 后，由 `@LU-003`
  对新的最终 HEAD 发布。

固定业务提交后只允许追加本治理文件和对应批准元数据。若业务代码、测试、Schema 或合入的
`main` 再次变化，阶段一批准立即失效，必须重新固定 SHA、重跑验证并取得新批准。维护窗口过期
只影响实际迁移/部署资格；不得凭本记录在窗口外操作生产数据库。

## 部署前门禁

以下条件必须同时成立；当前对话已明确暂停部署，因此现在不得执行这些生产步骤：

1. PR 精确 HEAD 的 required CI、Standards/Spec 双轴审核、阶段一与阶段二批准全部有效，PR 已合并，
   并另行收到明确的“部署上线”指令。
2. 当前时间处于批准维护窗口内；窗口过期须重新取得包含新窗口的 LU-003 批准。
3. 停止所有连接 `content_jobs.db` 的写入，包括内容生成、视频 Agent、后台任务、退款、对账及付费任务提交；
   确认 queued/running 任务、Provider 提交、未提交事务和迁移进程均为零。
4. 核对服务器、绝对路径、已合并提交和批准记录完全一致；任一不符即取消。
5. 记录主库、`-wal`、`-shm` 的所有者、权限、大小、修改时间及源库 SHA-256。
6. 记录迁移前业务计数：`jobs` 总数与各状态数、queued/running 数、
   `video_agent_pending_actions` 总数与各状态数，以及其中已有 `execution_domain` 的列定义（若存在）。

## WAL 一致性备份

1. 保持所有写入暂停，使用 SQLite Online Backup API、`sqlite3.Connection.backup()` 或 `.backup`
   创建一致副本；WAL 模式下禁止只复制主数据库文件。
2. 记录 `PRAGMA journal_mode` 以及源库 `-wal`、`-shm` 的存在状态和大小。
3. 计算备份文件 SHA-256；在备份副本执行 `PRAGMA integrity_check`（必须为 `ok`）和
   `PRAGMA foreign_key_check`（必须为空）。
4. 在一致备份副本核对迁移前全部业务计数。任何不一致均停止，不得启动新版本。

## 升级与幂等验证

1. 先在临时空库执行视频 Agent 初始化，确认列定义为
   `execution_domain TEXT NOT NULL DEFAULT 'remote'`。
2. 在包含历史 `video_agent_pending_actions` 行但缺少该列的生产一致备份副本上执行初始化：
   - `ALTER TABLE` 只在列缺失时执行；
   - 所有历史行的 `execution_domain` 均为 `remote`；
   - 行数、主键、用户名、报价、状态、费用、幂等键和结果字段均不改变；
   - 不调用真实 Provider，不创建任务、不扣点、不退款。
3. 对同一副本连续执行两次初始化，确认第二次无 DDL 错误、无重复列、无业务数据改写，且
   `PRAGMA table_info(video_agent_pending_actions)` 只返回一个 `execution_domain`。
4. 只有收到独立部署授权后，才可从已合并的精确提交部署对应文件并重启内容服务一次。
5. 启动后复核列定义、完整性、外键、迁移前后计数和 queued/running 数；验证新的本地卡片写入
   `local`、远端 CLI 卡片写入 `remote`，历史行保持 `remote`。
6. 所有检查通过后才恢复写入；恢复后观察首批请求、任务状态、对账、退款和错误日志，
   不用真实付费 Provider 作为验收手段。

自动化验证至少包括：

```powershell
python -m unittest tests.test_video_agent_tools tests.test_video_agent tests.test_video_parameters -v
python -m unittest tests.test_heygen_admin_oauth tests.test_heygen_mcp_oauth -v
python -m unittest discover -s tools/hq-cli/tests -p "test_*.py" -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
git diff --check
```

本地候选验证证据：相关实现与负向回归 `246/246` 通过，HQ CLI `87/87` 通过，director
regression gate `5/5` 通过；仓库 CI 文件隔离测试清单、Python/JavaScript 语法、前端运行时、
资源版本戳及 `git diff --check` 均通过。新增负向测试覆盖显式 API Wallet 模式在保留 OAuth
凭据时仍全程使用 API 上传/创建/轮询，以及套餐模式全程使用 MCP；相关测试已加入 required CI
的逐文件隔离清单。仓库测试共享模块名和全局 mock，单进程全量发现会发生
顺序污染；最终结论以精确 HEAD 的 required CI 隔离执行结果为准。

## 失败回滚

失败时继续停止写入，保留现场数据库、WAL/SHM、服务日志、失败步骤和迁移前后计数。优先切回
已知稳定的应用提交，同时保留向后兼容的 `execution_domain` 列；旧应用忽略该列。禁止执行
`DROP COLUMN`、重建共享表或删除历史行。

只有数据库损坏、完整性/外键检查失败或业务计数不一致，并由 `@LU-003` 针对该次整体恢复再次
明确授权时，才可停止所有连接并从已校验的一致备份整体恢复。恢复后必须复核 SHA-256、完整性、
外键和全部业务计数，全部通过才恢复写入。数据丢失边界为零：备份前停止全部写入，验证或恢复
完成前不恢复流量。
