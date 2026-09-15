# Redis TTL 缓存 M3F：TikHub 采集缓存切换手册（草稿）

## 边界

本批把「TikHub 采集/获客结果缓存」从 SQLite（`content-api/tikhub_cache.db`，表
`cache(k TEXT PRIMARY KEY, v TEXT, exp INTEGER)`）迁到 **Redis TTL**。

- 本域**不是** PostgreSQL 域：缓存可丢，**没有 Alembic 迁移、没有回填器、没有审计表**。
- 源文件全程保留只读，任何时刻都能秒级回退；回退只损失命中率，不损失数据。
- 目标键前缀 `hq:tikhub:cache:`（与轮次旁路的 `hq:turn:` 同库不冲突）。

## 源库事实（2026-09-16 线上实测）

| 项 | 值 |
|---|---|
| 源文件 | `/home/ubuntu/content-api/tikhub_cache.db`（925696 B，2026-09-14 13:48） |
| 进程 | `huangque-content`（`content_api.py`）、`huangque-leadgen-api`（`leadgen_api.py`）都 `WorkingDirectory=/home/ubuntu/content-api`，**共享同一份** `tikhub.py` 与同一个缓存文件 |
| 其余副本 | `/home/ubuntu/huangque-main-site/server/tikhub_cache.db`（12 KB，git 检出内自测用）、部署备份目录里的历史副本 |
| 表 | 仅 `cache(k, v, exp)`；`exp` = 秒级 Unix 时间戳（`int(time.time()) + ttl`） |
| 数据 | 8 行、全部已过期（`exp` 均 < now），键前缀 `srch:`；单值最大 ~12.9 KB |
| 空间 | SQLite 只增不减：8 行却占 925 KB（清过期行靠 `DELETE`，页不回收） |

TTL 由调用点决定，源里没有默认值：

| 入口 | 键 | TTL |
|---|---|---|
| `search()` | `srch:<平台>:<关键词>:<页>:<video_only 0\|1>` | 1800s |
| `detail()` | `det:<平台>:<id 或分享链>` | 3600s（`channels` 平台读写都不缓存） |
| `comments()` | `cmt:<平台>:<id>:<cursor>:<count>` | 3600s |

读方/写方（同一个进程既读又写，无独立读者）：

- `server/content_domains/core.py`（采集搜索）→ `tikhub.search`
- `server/content_domains/leads.py`（获客详情/评论/搜索）→ `tikhub.detail/comments/search`
- `server/content_domains/breakdown.py`（拆解时取详情）→ `tikhub.detail`
- `server/leadgen_api.py`（leadgen 服务直连）→ `tikhub.detail/comments/search`
- `huangque-web/content-api/content_api.py`（线上 `content_api.py` 与 `server/content_domains` 同源）

## 运行时开关

| 变量 | 取值 | 说明 |
|---|---|---|
| `HQ_TIKHUB_CACHE` | `sqlite`（默认）/ `redis` | 缓存权威；**非法值直接抛错**（与 M3A `HQ_FLAGS_STORE` 同纪律，绝不静默落回 sqlite） |
| `HQ_REDIS_URL` | `redis://user:pass@host:port/db` | 仅 `redis` 模式需要；复用轮次旁路那把凭据（只落在服务器受保护 env，不进 git、不进聊天） |
| `HQ_TIKHUB_CACHE_PREFIX` | 默认 `hq:tikhub:cache:` | **只给测试隔离用**，生产不要改 |
| `TIKHUB_CACHE_DB` | 默认 `<tikhub.py 同目录>/tikhub_cache.db` | sqlite 模式的源文件路径，本次不动 |

后端模块 `server/content_domains/tikhub_cache_redis.py` 的失败语义与源 SQLite 路径**一致**：
缓存永远不炸主流程（读→未命中、写→丢弃、只打一次告警）。差别只有一处有意的：
`HQ_TIKHUB_CACHE` 取值非法 → `mode()` 抛错；`HQ_REDIS_URL` 未配置 → 业务路径降级并在
journal 打 `HQ_REDIS_URL is not configured`（运维自检入口 `client()` 直接抛错）。

TTL 取值理由：**完全沿用源逻辑**（写入方给的 1800/3600 秒），不引入新的保守 TTL——
源里 `exp` 列就是「写入时刻 + ttl」，Redis 的 `SET ... EX ttl` 与之逐秒等价，且 Redis
到期自动删除，源那种「写入时顺手扫过期行」的清理不再需要（过期条目在源里也读不到，
不存在「Redis 更早失手」的语义差）。`ttl <= 0`：源等于写一条立即过期且覆盖旧值的行，
Redis 侧等价地删除该键（绝不写 0/负 TTL，Redis 会直接报错）。

## 切换前（staging / 生产演练）

1. 依赖：内容服务进程用 `/usr/bin/python3`（服务器已装 `redis 4.6.0`，实测可用）。
   `deploy/requirements-content.txt` 需补 `redis>=4.6,<6`（该文件由主 Agent 收编，
   M3F 域不改）；未装 redis 库时 redis 模式会降级，不会崩，但等于没切。
2. 部署文件：`server/tikhub.py`、`server/content_domains/tikhub_cache_redis.py`
   （`ship`/`drift_sentinel` 对 `server/content_domains/*.py` 是通用映射，无需新增白名单；
   但 `ship` 的兜底重启服务只有 `huangque-content`，**leadgen 必须手工重启**）。
3. 自检（切换前，仍 sqlite）：
   ```bash
   cd /home/ubuntu/content-api
   /usr/bin/python3 -c "from content_domains import tikhub_cache_redis as m; print(m.describe())"
   # 期望 mode=sqlite, available=False（此时不该连 Redis）
   ```
4. Redis 侧确认：`redis-cli -n <db> --scan --pattern 'hq:tikhub:cache:*' | head` 应无输出
   （切换前不该有本域键）；确认 Redis `maxmemory` 与 `maxmemory-policy` 能容纳
   最多 1 小时的搜索结果（单值 <15 KB，量级可忽略）。

## 切换步骤（低峰维护窗口，可秒级回滚）

1. 确认在途采集任务：`huangque-content`、`huangque-leadgen-api` 的 collect/leads
   任务没有 `pending/running`。
2. `/home/ubuntu/content-api/content.env` 追加（密码沿用既有 Redis 凭据）：
   ```
   HQ_TIKHUB_CACHE=redis
   HQ_REDIS_URL=redis://:<password>@127.0.0.1:6379/0
   ```
3. 重启两个消费进程：`sudo systemctl restart huangque-content huangque-leadgen-api`
   （admin/imggen 不 import tikhub，不必重启；但若同版本发布涉及它们，按各自窗口处理）。
4. **回读进程环境**（不能只看 env 文件）：
   ```bash
   tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value huangque-content)/environ | grep HQ_TIKHUB_CACHE
   sudo /usr/bin/python3 /home/ubuntu/m3a-verify-full/scripts/check_store_authority.py
   ```
   校验器里 `HQ_TIKHUB_CACHE` 相关行必须 `env=redis proc=redis OK`（其余域仍
   sqlite 属正常）；任何 ERROR 立即回滚（见 `postgresql-cutover-authority-guard.md`）。
5. 启动即自检：
   ```bash
   cd /home/ubuntu/content-api
   /usr/bin/python3 -c "from content_domains import tikhub_cache_redis as m; print(m.describe())"
   # 期望 mode=redis, url_configured=True, available=True
   ```
6. 业务验证（真实采集一次）：调一次抖音/小红书搜索 → 再调同关键词 → 第二次应显著更快；
   同时 `redis-cli -n 0 --scan --pattern 'hq:tikhub:cache:*' | head` 应出现
   `hq:tikhub:cache:srch:...`，`redis-cli -n 0 ttl 'hq:tikhub:cache:<键>'` 应返回
   ≤1800（搜索）/ ≤3600（详情、评论）。
7. journal 检查：`journalctl -u huangque-content -u huangque-leadgen-api --since '-10min'`
   无 `HQ_REDIS_URL is not configured`、无 `Redis 缓存` 降级告警、无 tikhub 缓存 traceback。
8. 观察 48 小时：采集/获客成功率与延迟不劣化；Redis 侧 `hq:tikhub:cache:*` 键数随
   1 小时 TTL 自然回落（不应单调增长——若单调增长说明 TTL 没落上，立即回滚排查）。
9. 归档（可选，不删）：`tikhub_cache.db` 保留原地；确认无进程再打开：
   `lsof /home/ubuntu/content-api/tikhub_cache.db`（sqlite 模式下每次调用即连即关，
   空闲时本来就没有句柄，只有持续有采集时才看得到）。

## 回滚（秒级）

```bash
# content.env：HQ_TIKHUB_CACHE=redis → sqlite（HQ_REDIS_URL 可留着，sqlite 模式不读它）
sudo systemctl restart huangque-content huangque-leadgen-api
```

- 回退后立即回到旧权威，SQLite 文件全程未被删改（redis 模式下它一个字节都不写）。
- Redis 里的 `hq:tikhub:cache:*` 键留着无副作用（1 小时内自然过期）；要立刻清：
  `redis-cli -n 0 --scan --pattern 'hq:tikhub:cache:*' | xargs -r redis-cli -n 0 del`。
- 完整回退：部署上一个 commit（`tikhub.py` + 新模块一起回退）。
- 回滚代价只有命中率：缓存是可丢数据，**没有数据一致性风险**。

## 停止条件（任一触发立即停并报告）

- journal 出现 `HQ_REDIS_URL is not configured` 或持续的 `Redis 缓存` 降级告警
  （= 以为切了其实没切，或 Redis 不可用）。
- 采集/获客延迟或失败率劣化；Redis 连接超时拖慢 collect 请求。
- `hq:tikhub:cache:*` 键数单调增长（TTL 未生效）。
- Redis OOM / 被其他业务挤掉（`maxmemory` 打满、evicted_keys 激增）。
- 同一时刻出现「一半进程 redis、一半 sqlite」的双权威（只影响命中率，仍要求收敛）。
- 权威校验器对 `HQ_TIKHUB_CACHE` 报 ERROR（进程与配置不一致 = 双权威风险）；
  SQLite 冰冻监控 1 小时内 `tikhub` 旧缓存 mtime 前进或行数增长（见
  `postgresql-cutover-authority-guard.md`）。

## 附：egress-router 的 cache.db（**建议，未实施**）

**先纠正一个前提**：`huangque-egress-router` 那个 `cache.db` 不是业务 SQLite 缓存，
是本机 Mihomo 代理自己的内部存储（bbolt，文件头 `ed da 0c ed`），由 systemd
`StateDirectory=huangque-egress-router` 落在 `/var/lib/huangque-egress-router/cache.db`
（16 KB，2026-09-08）。它里面不是 `k/v/exp` 这种可迁移的键值，**没有 Redis 后端**，
也不属于任何业务库清单：

- `deploy/systemd/huangque-egress-router.service`（`ExecStart=/usr/local/bin/mihomo -d /var/lib/huangque-egress-router`）
- `/etc/huangque/mihomo-egress-failover.yaml`（前门线路与 30s 探针；缓存/存储项不是业务数据）
- 同类文件还有 `/home/ubuntu/.config/mihomo/cache.db`、`/home/ubuntu/.config/mihomo-new/cache.db`

结论：**不改**。理由：① 迁它需要改 Mihomo 本体，不是本仓库能做的事；② 它是代理的连接/
线路缓存，丢了只是重探一次，与「省 TikHub 调用」无关；③ 生产上 `/home/ubuntu/huangque-egress-router/`
目录并不存在（任务书里的路径是 `/var/lib/...` 的误记）。

若后续真的想让出境前门的状态集中化，**正确的落点是运维层而不是本域代码**：

1. 要动的文件：`deploy/systemd/huangque-egress-router.service`（数据目录 / 服务依赖）、
   `/etc/huangque/mihomo-egress-failover.yaml`（如启用 `profile`/`store` 相关项）、
   `deploy/egress/README.md`（部署说明）。
2. 验证法：`systemctl restart huangque-egress-router` 后
   `curl -m 10 -x http://127.0.0.1:10813 -o /dev/null -w '%{http_code}\n' https://api.openai.com/v1/models`
   期望 401（TLS/路由可达）；再按 `deploy/egress/README.md` 的「生产验收」跑主备两条线路
   各 3 轮图片/视频探针，并确认 journal 无 SSLEOFError。
3. 风险：出境前门是作图/采集的**唯一**出口，改它比改缓存危险得多，必须单独开窗口、单独回滚。

本域交付不包含以上任何改动。
