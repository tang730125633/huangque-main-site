# PostgreSQL M2：IP Agent 会话回填手册

## 边界

本批只建立 `agent.sessions` JSONB 快照表和可审计回填器。它不改变 IP Agent 当前 JSON
读写路径，不启用 shadow-write，不切生产权威，也不删除任何会话文件。

## 生产只读 dry-run 基线

- 会话文件：233
- 主会话消息：6,697
- 缺完整 owner 的历史会话：112
- 会话集合校验和：`a81792a8b196a97b2b5e61e4b366e23ef03546a2b987cc582c37101b12fc6b1b`

缺 owner 的旧会话完整迁移并标记 `legacy_owner_missing=true`，不得猜测账号归属。后续用户
访问仍遵守现有权限逻辑；迁移本身不会让无归属会话变得可访问。

## Staging

```bash
export HQ_DATABASE_URL='postgresql://...'
alembic upgrade head
python scripts/migrate_ip_agent_sessions.py \
  --source-dir /path/to/read-only-session-snapshot
python scripts/migrate_ip_agent_sessions.py \
  --source-dir /path/to/read-only-session-snapshot \
  --code-sha <hq-ip-agent-commit> \
  --apply
```

不带 `--apply` 永远只输出聚合信息。Apply 规则：

- 第一次导入写入 `agent.sessions` 和 `ops.data_migration_*` 审计。
- 相同 session + 相同源 SHA 重跑只记为 skipped。
- 相同 session + 不同源 SHA 视为冲突，整个事务回滚。
- JSONB 回读后使用规范化 SHA-256 核对，禁止只比较行数。

## 生产切换前仍需

1. staging 回填与重复执行通过。
2. 实现 IP Agent shadow-write，JSON 继续权威。
3. 逐会话比较 owner、消息数、组件、选择、子 Agent 状态和最后事件。
4. 差异连续 48 小时为零。
5. 另行固定生产 commit、维护窗口、备份、回滚负责人和切换授权。
