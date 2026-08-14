# PR #102 短剧问题镜头候选采用与 `content_jobs.db` 运行手册

适用范围：[PR #102](https://github.com/LU-003/huangque-test-server/pull/102) 的问题镜头候选采用、统一重新合成和验收门禁。本手册不构成合并或部署授权；PR 当前最终 HEAD 审核通过、人工合并且收到用户明确部署指令前，不执行数据库维护或应用更新。

## 固定证据与结构变更

- 数据库绝对路径：`/opt/huangque-test-server/server/content_jobs.db`。执行前必须在目标主机重新解析并记录实际路径；不一致立即停止。
- 适用治理版本：`583293ae3e456e4fc82502d04da9db37d3b2faf4`。
- 最终固定业务提交：待本地代码、测试、8100 验证和双轴技术复审完成后回填。
- Owner 批准评论、API `updated_at`、作者/角色与 controls digest：待固定业务提交推送后，由仓库 Owner 独立确认并回填；代理不得代替 Owner 作出批准。
- 受影响表：`short_drama_refinement_jobs`。
- 新增列：`defer_reassembly INTEGER NOT NULL DEFAULT 0`。
- 迁移入口：`server/content_domains/short_drama_refinement.py:init_db()`；既有库通过幂等 `ALTER TABLE ... ADD COLUMN` 增加列，空库由版本化 `_SCHEMA` 直接创建。
- 兼容语义：旧任务迁移后取默认值 `0/false`；既有 `/refinement/jobs` 保留即时精修语义。新工作区只通过独立 `/refinement/candidates/adopt` 接口采用问题镜头候选，该接口由服务端强制 `defer_reassembly=true`，即使客户端提交 `false` 也不能提前重建整片。
- 不新增或修改索引、触发器、外键和既有约束；不删除、重命名或回填既有业务行。

## 执行窗口与职责

下列内容必须由 Owner 批准评论明确确认，并在证据提交中原样回填；任一项未确认时保持 Draft，不得合并或部署：

- 具体执行日期与维护窗口：待 Owner 确认。
- 执行负责人：待 Owner 确认。
- 备份负责人：待 Owner 确认。
- 验证负责人：待 Owner 确认。
- 故障恢复负责人：待 Owner 确认。
- 停写范围：窗口内暂停测试服务器全部连接 `content_jobs.db` 的 content 服务、短剧项目、Provider 镜头任务、精修、重新装配、交付和后台任务写入。
- 开始前必须确认没有运行中的付费任务、Provider 提交、精修/重装配租约、扣点结算或未提交事务；无法安全停写时取消窗口。
- 数据丢失边界：零数据丢失；该承诺必须由 Owner 在批准评论中确认。

## WAL 一致备份

1. 记录待部署 SHA、当前稳定应用 SHA、数据库绝对路径、所有者/权限，以及主库、`-wal`、`-shm` 的存在状态、大小和 journal mode。
2. 完成停写后，使用 SQLite online backup API、`sqlite3.Connection.backup()` 或 `.backup` 创建一致副本；WAL 模式禁止只复制主数据库文件。
3. 记录备份文件名、大小和 SHA-256；恢复前后重新核对摘要。
4. 对备份执行 `PRAGMA integrity_check`（必须为 `ok`）和 `PRAGMA foreign_key_check`（必须为空）。
5. 记录下列表的总数及状态分组：`short_drama_projects`、`short_drama_refinement_jobs`、`short_drama_refinement_versions`、`short_drama_refinement_acceptances`、`short_drama_reassembly_operations`、`short_drama_provider_shot_jobs`、`short_drama_provider_shot_versions`、`short_drama_delivery_jobs`、`short_drama_delivery_versions`。
6. 单独记录 queued/running 精修任务、processing 重装配租约、未失效验收和已确认精修版本；任一非零活动任务未安全收敛时不得迁移。

## 迁移与验证

1. 在空数据库启动 content 服务两次，确认新表定义直接包含 `defer_reassembly NOT NULL DEFAULT 0`，重复初始化无结构或数据漂移。
2. 在带历史数据的生产副本上移除该列以构造旧 Schema，保留旧任务行；运行新 `init_db()` 两次，确认仅新增列，旧任务读取为 `false`，任务 ID、状态、幂等键、结果/错误和全部业务计数不变。
3. 运行迁移前后 `PRAGMA integrity_check`、`PRAGMA foreign_key_check` 和上述表计数/状态分组；结果必须与允许的新增 Schema 变化一致。
4. 对一个问题镜头调用 `/refinement/candidates/adopt`，并刻意提交 `defer_reassembly=false`，确认服务端返回的任务字段仍为 `true`、仅采用候选、不调用整片渲染，并保持旧预览 URL/hash；同时回归 `/refinement/jobs` 的既有即时精修兼容路径。
5. 同一幂等键重放必须返回同一任务；不同项目、镜头、候选版本或有效 defer 语义不得错误重放。
6. staged 候选媒体缺失、损坏或 probe 失败时，`reassembly_required` 必须保持 true，验收必须失败且旧预览不得被锁定。
7. 仍有任一 issue 时直接调用 `/refinement/candidates/reassemble` 必须返回 409，且不得启动 FFmpeg、创建 operation、幂等残留或新 refinement version；既有 `/refinement/reassemble` 仅保留旧客户端的免费重装配兼容语义。
8. 所有问题镜头逐一采用后，只允许一次基于最新 refinement version 的免费统一合成；并发调用必须由既有 DB lease/CAS 保证唯一成功，0 扣点、0 Provider 调用，失败或失租清理临时输出且可安全重试。
9. 合成成功后 `staged_replacements` 清空，最新预览含全部候选镜头；验收前再次校验源 hash、物理媒体、音轨/字幕和最新版本。
10. 使用旧版本/旧 source、运行中精修任务、陈旧租约或变化中的 HEAD/base 执行必须 fail closed。

自动化验证至少包括：

```powershell
python -m unittest tests.test_short_drama_refinement -v
node --test --test-isolation=none tests/test_short_drama_center.js tests/test_short_drama_workspace.js
python -m unittest tests.test_stamp_assets -v
python scripts/stamp_assets.py --check
python scripts/ci_validate.py
git diff --check
```

本地真实页面统一使用 `http://127.0.0.1:8100/workbench/short-drama.html`，并记录实际服务的 HEAD、HTML 中四个短剧资源戳、问题镜头入口、候选采用、剩余问题阻断、统一合成按钮和验收门禁结果。

## 回滚与前向修复

失败时保持全部 `content_jobs.db` 写入暂停，保存现场数据库、WAL/SHM、应用日志、失败步骤、任务/租约状态和迁移前后计数。先回退应用到执行前已验证 SHA。

新增列是 additive 且旧应用会忽略，但应用回退不等于数据库恢复。出现完整性/外键失败、业务计数变化、任务状态漂移、重复合成、错误验收或无法证明零数据丢失时，必须停止全部数据库连接，核验备份 SHA-256 后整体恢复 WAL 一致备份；稳定应用启动并复核全部基线后才能恢复写入。

仅当数据库完整、业务计数一致、旧应用已验证可忽略新增列且没有新代码写入的待处理任务需要保留时，才可在负责人书面记录后选择“只回退应用、保留新增列”。禁止在生产窗口临时 `DROP COLUMN`。前向修复必须重新固定提交、复跑迁移/兼容/竞态/回滚验证并重新获得 Owner 批准。
