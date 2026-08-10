# PR #94 短剧共享数据库迁移与回滚手册

适用范围：PR [#94](https://github.com/LU-003/huangque-test-server/pull/94) 对共享 `content_jobs.db` 的增量变更。

## 上线门禁

当前状态：**协作确认已完成；仍须等待 PR 审核通过、合并及明确部署指令。**

- Tang/团队确认：LU-003 代表 Tang/团队确认
- 确认链接：[PR #94 迁移确认评论](https://github.com/LU-003/huangque-test-server/pull/94#issuecomment-5235811721)
- 确认时间：2026-08-10 12:19:08（Asia/Shanghai）
- 迁移窗口：PR #94 合并后首次部署时执行，计划 30 分钟；前 10 分钟暂停写入并备份，中间 10 分钟升级验证，最后 10 分钟预留回滚；具体日期由正式部署指令确定
- 执行人：LU-003
- 回滚负责人：LU-003
- 备份文件及校验值：迁移时填写

确认评论已覆盖迁移窗口、备份（含 WAL 一致性）和回滚方案，但不等于部署指令。只有 PR 审核通过并合并，且收到明确部署指令后，才能在约定窗口操作共享数据库。审核整改依据见 [PR #94 审核评论](https://github.com/LU-003/huangque-test-server/pull/94#issuecomment-5235152857)。

## 变更清单与兼容性

本次初始化只做可重复执行的增量建表/加列，不删除或重命名旧字段：

- `short_drama_projects.creation_status`
- `short_drama_script_imports.core_story_json`
- `short_drama_script_imports.core_story_confirmed_at`
- `short_drama_script_imports.character_contract_migration_json`
- `short_drama_characters.reference_source`
- `short_drama_characters.reference_asset_id`
- `short_drama_characters.reference_name`
- 新表 `short_drama_provider_shot_execution_overrides`
- 新表 `short_drama_provider_shot_selections`

旧版 `front_full / side_full / front_half` 合同初始化时保持原文，不会把 `front_half` 直接改写为 `back_full`。初始化增加 `back_full_confirmation_required` 迁移标记，并记录各角色当时的 `reference_version` 基线。只有可信 AI 三视图任务成功产出的新版本参考图，且任务、项目、角色、参考图版本完全匹配并由用户确认后，才会原子升级该角色合同并移出待迁移列表；普通上传或旧版半身参考图即使产生更高版本也不能作为迁移证据。合法删除未锁定、无付费任务的待迁移角色时，会同步清理该角色的基线和证据，最后一个待迁移角色被删除后迁移标记清空；已锁定或存在付费任务的角色仍禁止删除。仍有待迁移角色时禁止转为正式项目。旧程序可忽略新增列和表继续读取原有字段，因此首选代码回滚，不做破坏性降级 DDL。

## 迁移前准备

1. 在约定窗口暂停内容服务写入，确认没有运行中的付费短剧任务或数据库写事务。
2. 记录待部署提交 SHA、当前稳定提交 SHA、数据库绝对路径和执行人。
3. 使用 SQLite 在线备份接口或 `sqlite3 content_jobs.db ".backup '<timestamp>-content_jobs.db'"` 创建一致性备份。数据库处于 WAL 模式时不要只复制主文件；同时记录 `content_jobs.db-wal`、`content_jobs.db-shm` 是否存在及大小。
4. 对备份执行 SHA-256，保存校验值；在备份副本执行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`。
5. 保留当前稳定构建和启动命令，确认可以在窗口内切回。

## 执行与验证

1. 部署已审核提交，启动一次内容服务，让 `short_drama.init_db` 完成增量初始化。
2. 再执行一次初始化，确认重复执行无错误、无额外数据改写。
3. 用 `PRAGMA table_info` / `sqlite_master` 核对上方列、表和索引均存在。
4. 对比迁移前后项目、导入记录、角色和付费任务数量；确认初始化没有改写旧合同 JSON，仅产生包含角色版本基线的待补图迁移标记。
5. 执行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`，前者必须为 `ok`，后者必须为空。
6. 冒烟验证：恢复旧草稿；确认普通上传不能解除迁移门禁；确认可信 AI 三视图任务的新版本可逐角色解除门禁；确认合法删除最后一个待迁移角色会清空标记；确认已锁定角色删除保护、付费参考图任务编辑保护、MiniMax 免费预检不扣点且不提交外部任务。

自动化验证命令：

```powershell
python -m unittest tests.test_short_drama_pr94_schema_migration -v
python -m unittest tests.test_short_drama_projects tests.test_short_drama_autodraft -v
node --test tests/test_short_drama_center.js
```

## 回滚

优先采用功能回滚：

1. 暂停内容服务写入，记录失败现象、当前任务状态和最后成功写入时间。
2. 切回上方记录的稳定提交并重启。新增列/表是兼容性增量，保留它们，旧程序按原字段工作。
3. 验证健康检查、旧草稿读取和已有付费任务查询；不要删除新增表或列。

只有数据库损坏或增量数据本身不可用，且回滚负责人明确批准时，才做数据恢复。恢复会丢失备份之后的写入：

1. 停止所有连接 `content_jobs.db` 的进程，保存现场数据库及 WAL/SHM 文件供审计。
2. 再次核对备份 SHA-256 和 `integrity_check`，将一致性备份恢复到原路径。
3. 启动稳定版本，执行完整性、外键、项目数量及付费任务核对后再恢复流量。

## 前向修复

修复版再次上线时复用同一门禁和备份步骤。初始化是幂等的，可直接在保留新增列/表的数据库上再次执行；随后重复“执行与验证”全部检查，并把最终提交 SHA、备份校验值、验证结果和回滚状态补充到 PR #94。
