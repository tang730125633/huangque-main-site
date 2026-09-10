# PR 1485 渠道管理与运行证据数据库治理记录

本记录覆盖 PR [#1485](https://github.com/tang730125633/huangque-main-site/pull/1485)
新增的两个跨服务私有 SQLite 数据库。当前状态：**业务与 Schema 已固定并取得 LU-003
阶段一批准；等待本证据版本化后的最终 HEAD 阶段二批准及最终审核**。本文不是合并、
部署、迁移、重启、真实 Provider 调用或数据库整体恢复授权。

## 固定对象与审批

- 目标分支：`main`
- 审核基线：`main@e19d45e3976442045cbfd3ede52811f2caa4e699`
- 业务/Schema SHA：`5d1af39aca49e19bf0c79863eb7c2b9968a89cae`
- 维护窗口：`2026-09-11 02:00–03:00 Asia/Shanghai`
- Owner：`LU-003`
- 执行负责人：`LU-003`
- 回滚负责人：`LU-003`
- 阶段一批准：`LU-003` 于 `2026-09-10` 在本次 Codex 审核任务中明确批准上述准确
  业务/Schema SHA、main 基线、两个生产数据库路径和维护窗口，并批准停写、WAL 一致性
  备份、幂等初始化与升级验证及失败回滚；同时授权将批准信息版本化并发布到 PR 1485。
  批准明确不授权部署、真实 Provider 调用或数据库整体恢复。批准原文如下：

  > 批准 PR 1485 业务/Schema SHA `5d1af39aca49e19bf0c79863eb7c2b9968a89cae`，
  > 基于 `main@e19d45e3976442045cbfd3ede52811f2caa4e699`，沿用两个数据库路径及
  > `2026-09-11 02:00–03:00 Asia/Shanghai` 维护窗口、停写、WAL 备份、幂等验证和
  > 回滚方案；授权版本化并发布到 PR，不授权部署、真实 Provider 调用或数据库整体恢复。
- 阶段二最终 HEAD 批准：等待阶段一证据版本化、精确 HEAD CI 与最终双轴复审后，由
  `LU-003` 对最终 HEAD 明确批准。阶段二评论不回写仓库，避免审批自引用。

若业务代码、Schema、数据库路径、维护窗口或 `main` 在批准后变化，相关批准立即失效，
必须重新固定、验证和批准。合并仍不等于部署；生产操作必须另有明确“部署上线”指令。

## 数据库边界

- 渠道配置与执行状态：`/home/ubuntu/content-api/channel_management.db`
- 运行证据与通知队列：`/home/ubuntu/content-api/runtime_observability.db`
- 连接服务：`huangque-content`、`huangque-imggen-api`、`huangque-admin`
- 文件权限：服务账号独占读写，数据库主文件必须为 `0600`；同目录不得出现宽权限备份。
- 禁止把数据库、WAL/SHM、真实密钥、用户输入、Provider 原始响应或生成产物提交 Git。

`channel_management.db` 首次创建：`channels`、`versions`、`mappings`、`runs`、`events`、
`schedule`、`settings`、`channel_incidents`，以及 runs/events 查询索引。
`runtime_observability.db` 首次创建：`task_trace`、`alert_outbox`。迁移均使用
`CREATE TABLE/INDEX IF NOT EXISTS`，不得删除或改写已有业务数据。

## 停写与前置检查

1. 固定已合并待部署提交，确认它与批准 SHA/最终 HEAD 的关系及 required CI、审核和可合并证据。
2. 确认维护窗口尚未过期，磁盘空间足够容纳两个数据库、WAL/SHM 与至少两份一致性备份。
3. 确认 `content_jobs.db` 中 queued/running 为零；本迁移不授权终止、退款、重发或恢复真实任务。
4. 对 `huangque-content` 先停止接收新任务并自然排空；随后停止
   `huangque-content`、`huangque-imggen-api`、`huangque-admin`，确认三个进程均不再持有数据库。
5. 记录两个数据库及 `-wal`、`-shm` 的存在性、大小、所有者、权限与 SHA-256；任何路径或
   连接进程不符即取消操作。

## WAL 一致性备份

1. 保持停写，分别使用 SQLite Online Backup API 或 `sqlite3 .backup` 创建带 UTC 时间戳的
   一致副本；WAL 模式下禁止只复制主数据库文件。
2. 对源库与备份执行 `PRAGMA quick_check`（必须为 `ok`）、`PRAGMA foreign_key_check`
   （必须为空），并记录页数、表/索引清单、行数、备份大小和 SHA-256。
3. 备份置于受限目录并设为 `0600`。任何检查失败时不得初始化、启动服务或开放写流量。

## 升级、幂等与兼容验证

1. 使用与待发布服务相同的 Python 解释器和固定代码，显式设置上述两个绝对路径；分别调用
   `channel_manager.db()` 与 `runtime_observability.database()` 完成一次初始化并关闭连接。
2. 重复执行相同初始化，比较两次 `sqlite_master`、表/索引列定义、全部业务行数与
   `PRAGMA quick_check`；第二次不得新增重复对象、改变现有配置/运行记录或报错。
3. 在空白隔离路径重复两次初始化，验证完整 Schema、索引和 `0600` 权限；删除该隔离副本，
   禁止用空库替换生产库。
4. 用迁移前稳定代码对一致备份执行只读兼容检查；旧代码不得删除、重命名或误解释新增数据。
5. 启动服务前确认数据库中没有明文密钥，任务证据只含允许字段，通知 URL/渠道密钥仍为密文。
6. 启动 `huangque-admin` 后先做只读健康与列表检查，再启动生成服务并检查日志；不运行完整
   生成测试，不调用真实 Provider。确认原有任务数量、状态、退款标记及用户点数未变化后才恢复流量。

## 失败回滚

任一步失败时继续停写，保留现场数据库、WAL/SHM、日志、失败步骤和计数。优先回退到迁移前
稳定应用提交；新增独立数据库为向后兼容附加物，旧应用不使用它们，因此默认保留数据库供审计，
禁止为“清理”而删除。若确认必须恢复数据库，须再次取得明确的数据库整体恢复授权，并在停写下
对当前现场再做一致性备份，然后用已验证的一致备份整体恢复对应数据库；禁止混搭主库与其他时点
WAL/SHM。恢复后重复完整性、Schema、行数和权限检查，再按管理员、生成服务、流量的顺序恢复。

数据丢失边界为零。任何真实 Provider 调用、历史任务处理、点数扣退、数据库删除或整体恢复都不在
本记录授权范围内。
