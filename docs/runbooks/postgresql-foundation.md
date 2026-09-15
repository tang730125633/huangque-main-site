# PostgreSQL 基础底座运行手册

## 当前状态

本目录只建立 PostgreSQL 连接池、版本化 Schema 和迁移审计表。所有现有业务仍以 SQLite
为权威；合并本代码不会自动连接、迁移或切换生产数据库。

## 环境变量

- `HQ_DATABASE_URL`：必需，完整连接串只放权限 `600` 的服务端环境文件。
- `HQ_DB_POOL_MIN`：默认 1。
- `HQ_DB_POOL_MAX`：默认 10。
- `HQ_DB_POOL_TIMEOUT`：默认 10 秒。

禁止把连接串放进 Git、systemd `Environment=`、浏览器、日志或测试产物。

## Staging 初始化

```bash
export HQ_DATABASE_URL='postgresql://...'
alembic upgrade head
python -m unittest discover -s tests -p 'test_postgres_foundation.py' -v
```

初始迁移创建十个业务 Schema，以及：

- `ops.data_migration_runs`：一次迁移的代码版本、源指纹、计数和终态。
- `ops.data_migration_items`：分表/分块回填计数与校验和。

## SQLite 禁增门禁

```bash
python scripts/sqlite_inventory.py --check
```

该门禁是单向棘轮：已有 SQLite 调用允许减少；新增 SQLite 文件或增加现有调用会失败。
基线只在首次建立时生成，后续不得为绕过门禁而扩写。

## 失败与回滚

- 连接池未配置时不会打开 PostgreSQL。
- Schema 升级失败时保持现有 SQLite 权威，不切换任何服务。
- 不提供破坏性 `alembic downgrade`；失败时保留现场，恢复已验证备份。
- 本底座不授权安装生产 PostgreSQL、修改生产数据或删除 SQLite。
