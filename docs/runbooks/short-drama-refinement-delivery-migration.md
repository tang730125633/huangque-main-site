# 短剧精修与完整时长交付迁移手册

## 范围

本手册覆盖短剧精修、媒体偏好和免费重新装配功能对共享 `content_jobs.db` 的增量结构变更。代码来源为测试仓库 PR #96 与 PR #98，经主站最新 `main` 重新集成。

本文件不构成合并或部署授权。生产维护窗口、数据库绝对路径和四类负责人必须在部署前由主站负责人确认；未确认前不得迁移或部署。

## 结构变更

- `short_drama_characters` 增加角色参考图 pending/stale 状态字段，全部可空或带默认值。
- `short_drama_refinement_jobs` 增加 `replacement_provider_version_id`。
- `short_drama_refinement_versions` 增加 `preview_file_hash` 与 `media_json`。
- 新增 `short_drama_refinement_media_preferences`，支持 `voice_timeline`、`provider_audio` 与 `silent`；旧偏好表会在单一 SQLite 事务中兼容迁移。
- 新增 `short_drama_reassembly_operations`，以 `UNIQUE(project_id, source_version_id)` 保证同一精修来源只有一个重新装配操作，并记录跨 Worker 租约、心跳、渲染和结果版本。

以上均为向后兼容的增量结构。旧应用可忽略新增列和表；应用回退时保留增量 Schema，不在线执行破坏性逆向 DDL。

## 部署前必须确认

- 生产数据库实际绝对路径。
- 带时区的维护开始和结束时间。
- 执行负责人、备份负责人、验证负责人、失败恢复负责人。
- 当前稳定应用提交与待部署提交。
- 停写范围：所有连接 `content_jobs.db` 的 content、短剧生成、精修、重新装配、交付及后台任务写入。
- 没有运行中的付费任务、重新装配 operation 或未提交事务。

任一项不明确时取消维护窗口。

## WAL 一致备份

1. 停止全部写入，记录数据库、`-wal`、`-shm` 的状态、大小、所有者和权限。
2. 使用 SQLite Online Backup API、`sqlite3.Connection.backup()` 或 `.backup` 创建一致副本；禁止只复制 WAL 模式的主数据库文件。
3. 记录备份文件 SHA-256。
4. 在备份副本执行 `PRAGMA integrity_check`（必须为 `ok`）与 `PRAGMA foreign_key_check`（必须为空）。
5. 记录短剧项目、角色、参考图任务、精修任务/版本/偏好、重新装配、验收和交付记录的迁移前计数。

## 执行与验证

1. 先在空数据库运行 `short_drama.init_db` 与 `short_drama_refinement.init_db`，核对新增列、表、外键、CHECK 和唯一约束。
2. 在包含既有数据的数据库副本运行升级，确认原业务记录与计数不变。
3. 连续运行两次初始化，确认第二次不报错、不重复建表、不改写数据。
4. 部署已审核提交并执行初始化，再以 `PRAGMA table_info`、`PRAGMA foreign_key_list` 和 `sqlite_master` 核对结构。
5. 对比迁移前后业务计数；重新装配表在首次启用前应为空。
6. 再次执行完整性与外键检查。
7. 验证 active/pending 角色参考图切换、三种媒体偏好、精修验收、完整时长预览和免费重新装配；重复请求必须复用 operation，不调用视频 Provider、不重复扣点。
8. 全部通过后才恢复写入，并记录恢复时间、验证人和最终计数。

自动化回归至少包括：

```powershell
python -m unittest tests.test_short_drama_pr94_schema_migration -v
python -m unittest tests.test_short_drama_projects tests.test_short_drama_planning tests.test_short_drama_conversation tests.test_short_drama_autodraft tests.test_short_drama_refinement -v
node tests/test_short_drama_center.js
node tests/test_short_drama_workspace.js
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

## 回滚和数据丢失边界

失败时保持全部写入暂停，保存现场数据库、WAL/SHM、应用日志和失败步骤。优先回退到迁移前稳定应用提交并保留向后兼容的增量 Schema。

仅在数据库损坏、完整性或外键检查失败、业务计数不一致，或稳定应用无法读取时，才由恢复负责人使用已校验的一致性备份整体恢复。恢复后重新执行完整性、外键和业务计数检查，全部通过才恢复流量。

数据丢失边界为零：备份前停止全部写入，验证或恢复完成前不恢复写流量。任何前向修复都必须重新固定提交、复跑空库/旧库/幂等/兼容/计数验证，并进入新的已批准维护窗口。
