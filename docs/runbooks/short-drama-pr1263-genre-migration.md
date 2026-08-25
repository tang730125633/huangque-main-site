# PR #1263 短剧题材字段共享数据库迁移与回滚手册

## 状态与范围

本手册覆盖 `tang730125633/huangque-main-site#1263` 对生产共享数据库
`/home/ubuntu/content-api/content_jobs.db` 的唯一新增结构：
`short_drama_projects.genre TEXT NOT NULL DEFAULT ''`。该变更只增列，不删除、重命名
或重写既有字段；旧应用可以忽略该列，因此应用回滚时保留增量 Schema，不在线执行
破坏性逆向 DDL。

当前状态：**等待 LU-003 阶段一 Owner 审批；本文不是合并或部署指令。**

## 固定审批证据

- PR：`tang730125633/huangque-main-site#1263`。
- 固定业务与 Schema 提交：`PENDING_BUSINESS_SHA`。
- 阶段一批准评论：`PENDING_LU003_APPROVAL_URL`。
- GitHub 评论作者及关联：`LU-003`，需由 GitHub API 证明作者身份和时间。
- 生产服务器：`dapeng-server`。
- 生产数据库绝对路径：`/home/ubuntu/content-api/content_jobs.db`。
- 当前稳定应用提交：`629851345851f84b603e9d94bfd4e9f7a4aec372`。
- 维护窗口：`PENDING_APPROVED_WINDOW`，时区必须为 `Asia/Shanghai`。
- 执行、备份、验证、失败恢复负责人：均为 `@LU-003`。

业务与 Schema 提交冻结后，只允许追加本节的批准评论 URL、评论 ID/作者/时间和最终
验证证据；若再修改业务代码或 Schema，原阶段一批准立即失效，必须重新固定提交并获
得新批准。最终 HEAD 还需阶段二批准，证明固定提交之后只有本手册审批证据变化。

## 部署前门禁

1. PR 审查、精确 HEAD CI、阶段一及阶段二批准全部通过，并收到独立部署指令。
2. 窗口开始前暂停所有连接 `content_jobs.db` 的 content、短剧、后台任务和付费任务
   写入；确认没有运行中 Provider 提交、退款、未提交事务或迁移进程。
3. 记录当前稳定提交、待部署提交、数据库主文件及 `-wal`/`-shm` 的大小、所有者、
   权限和最后修改时间。
4. 记录迁移前计数：`short_drama_projects` 总数、未删除数、各 stage 数量、
   `short_drama_scripts`、`short_drama_shots`、`jobs` 总数及 pending/running 数量。

任一门禁缺失、实际路径或 SHA 不一致、窗口过期时立即取消，不得边部署边补审批。

## WAL 一致性备份

1. 保持写流量暂停，使用 SQLite Online Backup API、`sqlite3.Connection.backup()` 或
   `.backup` 创建一致副本；WAL 模式下禁止只复制主数据库文件。
2. 记录源库 `journal_mode` 以及 `content_jobs.db-wal`、`content_jobs.db-shm` 状态。
3. 对备份文件计算 SHA-256。
4. 在备份副本执行 `PRAGMA integrity_check`（必须为 `ok`）和
   `PRAGMA foreign_key_check`（必须为空），并核对上述业务计数。

## 执行与验证

1. 空库执行 `short_drama.init_db`，确认 `genre` 存在、`NOT NULL`、默认值为空字符串。
2. 对不含 `genre` 且带历史项目的数据库副本执行升级，确认旧项目自动得到空字符串，
   其他字段及全部业务计数不变。
3. 连续执行两次初始化，确认第二次无错误、无重复 DDL、无数据改写。
4. 部署冻结提交并启动一次 `huangque-content`；用 `PRAGMA table_info` 核对列定义。
5. 创建带题材的新项目并读取详情/列表；旧项目读取、更新和旧应用已知列查询均正常。
6. 再次执行完整性、外键和迁移前后业务计数核对；全部通过后才恢复写流量。

自动化验证至少包括：

```powershell
python -m unittest tests.test_short_drama_pr94_schema_migration -v
python -m unittest tests.test_short_drama_projects tests.test_short_drama_conversation tests.test_short_drama_autodraft -v
python -m unittest tests.test_private_assets tests.test_ci_validate -v
node tests/test_short_drama_center.js
node tests/test_short_drama_workspace.js
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

## 失败回滚

失败时继续停写，保存现场数据库、WAL/SHM、服务日志、失败步骤和计数。优先切回稳定
应用提交并保留新增 `genre` 列；旧应用忽略该列，不执行 `DROP COLUMN`。仅在数据库损坏、
完整性/外键失败或计数不一致，且失败恢复负责人明确批准时，才停止所有连接并用已校验
备份整体恢复。恢复后重复 SHA-256、完整性、外键和业务计数检查，全部通过才恢复流量。

数据丢失边界为零：备份前停止全部写入，验证或恢复完成前不恢复写流量。任何前向修复
都必须重新固定业务与 Schema SHA、重跑空库/历史库/幂等/计数验证并进入新的审批窗口。
