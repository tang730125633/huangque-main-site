# PR 1564 统一渠道映射数据库治理记录

本记录覆盖 PR [#1564](https://github.com/tang730125633/huangque-main-site/pull/1564)
对共享渠道数据库的追加式 Schema 变更。合并不等于部署；本文及 Owner 批准均不授权连接生产、
调用真实 Provider、处理历史任务、变更用户点数或整体恢复数据库。

## 固定对象与批准

- 目标分支：`main`
- 阶段一基线：`main@b620e50c4fc956c9ae5076db23c26ae313ef7911`
- 业务/Schema SHA：`08ee6786f04600163f87af8c57253b0e189900b4`
- 阶段一合成候选：`240f39318f569adcc0030a3c8531dc5e4127a63e`
- 数据库：`/home/ubuntu/content-api/channel_management.db`
- 维护窗口：`2026-09-15 02:00–03:00 Asia/Shanghai`
- Owner、执行负责人、回滚负责人：`LU-003`
- 阶段二最终 HEAD：等待本记录提交、最终测试及 Owner 精确批准。

LU-003 于 2026-09-14 给出的当前阶段一批准原文：

> 我以 LU-003 负责人身份重新批准 PR 1564 业务/Schema SHA
> `08ee6786f04600163f87af8c57253b0e189900b4`，基于
> `main@b620e50c4fc956c9ae5076db23c26ae313ef7911`，沿用
> `2026-09-15 02:00–03:00 Asia/Shanghai` 维护窗口、
> `/home/ubuntu/content-api/channel_management.db` 数据库路径、停写、WAL 一致性备份、
> 三张新表及索引的幂等迁移、空库与旧库升级验证和失败回滚方案；授权版本化并发布到
> PR 1564。本批准取代旧阶段一批准，不授权部署、真实 Provider 调用或数据库整体恢复。

此前基于 `main@aac1934ba2612cc76b1d4d2eaa3640c9495f5d90` 的阶段一批准因
`main` 前进而失效，仅保留在 PR 评论审计历史中。业务代码、Schema、数据库路径、维护窗口、
目标分支或基线再次变化时，当前批准立即失效，必须重新固定、验证和批准。

## Schema 边界

本次只在现有 `channel_management.db` 中追加：

- `operation_mappings`：每个稳定 `operation_id` 的当前修订指针和状态。
- `operation_mapping_versions`：不可变的映射修订历史。
- `run_snapshots`：不含密钥的服务端执行或 shadow 观察快照。
- 索引 `operation_mapping_history`。

迁移使用 `CREATE TABLE IF NOT EXISTS` 和 `CREATE INDEX IF NOT EXISTS`；不删除、不重命名、
不改写既有 `channels`、`versions`、`mappings`、`runs`、`events`、`schedule`、`settings`、
`channel_incidents` 数据。数据库由 `huangque-content`、`huangque-imggen-api` 和
`huangque-admin` 共同连接，因此按共享数据库门禁处理。

## 停写与前置检查

1. 仅在批准窗口内，从已合并并再次固定的 `main` 精确提交执行；确认 required CI、最终审核、
   可合并/已合并证据及阶段二最终 HEAD 批准有效。
2. 确认磁盘空间可容纳数据库、WAL/SHM 和至少两份一致性备份；记录源文件及 `-wal`、`-shm`
   的存在性、大小、所有者、权限和 SHA-256。
3. 停止接收新任务并等待 `content_jobs.db` 中 queued/running 归零。本批准不授权终止任务、
   退款、重发或恢复历史任务；非零时取消操作并另行取得授权。
4. 依次停止所有写入者：`huangque-content` 先 graceful stop，随后停止
   `huangque-imggen-api` 和 `huangque-admin`；确认没有进程持有目标数据库后保持停写。
5. 任一 SHA、路径、窗口、服务集合或任务状态不符时立即取消，不执行初始化。

## WAL 一致性备份

1. 保持停写，使用 SQLite Online Backup API 或 `.backup` 创建带 UTC 时间戳的一致副本；
   WAL 模式下禁止只复制主数据库文件。
2. 对源库和备份分别执行 `PRAGMA quick_check`（必须为 `ok`）与
   `PRAGMA foreign_key_check`（必须为空），记录页数、表/索引清单、各表行数、文件大小和
   SHA-256。
3. 备份存放在受限目录并设为 `0600`。校验失败时不得迁移、启动服务或恢复写流量。

## 升级、幂等与兼容验证

1. 使用待发布提交及其 Python 解释器，显式设置目标数据库绝对路径后调用
   `channel_manager.db()` 一次并关闭连接。
2. 核对新增三表、索引、列定义和旧表行数；确认旧映射、渠道版本、运行记录及审计记录未变化。
3. 再次运行相同初始化，比较两次 `sqlite_master`、所有表/索引列定义和逐表行数；第二次不得
   新增重复对象、改变任何业务行或报错。
4. 在隔离空库和包含旧 Schema/哨兵数据的隔离副本各重复两次初始化，验证完整 Schema、
   `0600` 权限、旧数据保留及旧应用只读兼容；隔离库不得替换生产库。
5. 检查 `run_snapshots` 仅含 operation、映射修订、渠道版本、Adapter、实际模型和可信调用
   来源等允许字段，不得出现密钥、Authorization、Cookie、用户输入或 Provider 原始响应。
6. 启动 `huangque-admin` 后先做只读健康、列表和 shadow 证据查询，再启动生成服务并检查日志；
   不运行完整生成测试，不调用真实 Provider。核对任务计数、退款标记及用户点数未变化后才可
   恢复写流量。

仓库验证证据：共享渠道/任务/HTTP 安全组合 204 项、后台 E2E 71 项、HQ CLI 内容 38 项、
监控回归 3 项、管理前端渠道回归 12 项通过；`py_compile`、`node --check`、
`scripts/ci_validate.py`、`scripts/stamp_assets.py --check` 和 `git diff --check` 通过。
`tests.test_function_registry` 的音频 ffprobe 断言已在相同 `main` 基线独立复现，属于主线既有
失败而非本 PR 回归。

## 失败回滚

任一步失败时继续停写，保留现场数据库、WAL/SHM、服务日志、失败步骤、Schema 与行数证据。
优先回退到迁移前稳定应用提交；新增表是向后兼容附加物，默认保留供审计，禁止为“清理”而删除。
若确需整体恢复数据库，必须重新取得明确的整体恢复授权，并在停写状态下先对当前现场再做一致性
备份，然后仅使用已验证的一致备份恢复；不得混用不同时点的主库和 WAL/SHM。恢复后重复完整性、
Schema、行数、权限与应用只读检查，再按管理员、生成服务、写流量的顺序恢复。

数据丢失边界为零。本文不授权部署；部署仍需 PR 合并后另行收到明确“部署上线”指令。
