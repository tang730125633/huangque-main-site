# 切写权威校验与 SQLite 冰冻监控（所有域通用）

配套工具：`scripts/check_store_authority.py`（权威校验器，服务器上 sudo 运行）。
本文回答老板 2026-09-16 的批评：**「HQ_<DOMAIN>_STORE 默认 sqlite/json，漏配即
静默退回旧存储」** —— 三层防护确保任何一次切写都不可能「静默」。

## 三层防护总览

=============  ========================================================  =========
层              机制                                                      何时生效
=============  ========================================================  =========
一 · 声明日志   store 模块 mode() 首次解析时打一条日志；env 缺失/为空       每次进程
                走默认值时是 **WARNING**（默认日志级别可见），显式配置是     启动
                INFO。进程内一次性（`_MODE_ANNOUNCED` 去重），不刷屏。
二 · 权威校验   切写后逐服务读 `/proc/<pid>/environ`，与各单元             每次切写
                EnvironmentFile 逐开关比对，输出模式表；任一 ERROR 即
                退出码 1，切写流程不得继续。
三 · 冰冻监控   切写后盯 SQLite mtime/行数：旧库必须「冻住」。              切写后 1h
                影子核对 cron（`shadow_sync_all.sh` 活源行数比对）兜底      + 长期
                抓「SQLite 仍在涨」= 仍有进程在写旧库。
=============  ========================================================  =========

## 第一层：声明日志（已内建于各 store 模块，无需操作）

生效文件（2026-09-16 起）：主站 `content_domains/{flags,observability,channel,
leads,admin_config}_store.py`、`tikhub_cache_redis.py`、
`creator_agent/pg_store.py`、agent-metrics `metrics_store.py`、hq-ip-agent
`agent/pg_session_store.py`。

判读：切写后每次重启服务，`journalctl -u <单元>` 里应看到一条 INFO
「authority announced: mode=postgres」。若看到 WARNING「not set, falling back
to default …(legacy storage)」，说明该进程根本没配开关 —— 立即停止切写。

## 第二层：权威校验器（切写必跑，作为门禁）

**位置**：`/home/ubuntu/m3a-verify-full/scripts/check_store_authority.py`
（`git pull` 后即最新；该目录是生产部署的验证镜像，不直接被服务 import）。

**切写后必跑**（维护窗口内、重启完成后立刻）：

```bash
sudo /usr/bin/python3 /home/ubuntu/m3a-verify-full/scripts/check_store_authority.py
sudo /usr/bin/python3 /home/ubuntu/m3a-verify-full/scripts/check_store_authority.py --expect postgres
```

判读：

- 每个（服务，开关）组合输出一行：`env=`（单元 EnvironmentFile 里的值）、
  `proc=`（进程 /proc/<pid>/environ 实际值）、结论。
- `ERROR: env 配了 postgres，进程没吃到（多半没重启）` → 该服务重启不生效，
  **不得继续切写**。
- `ERROR: env=postgres 进程=sqlite 不一致` → 环境文件与进程分叉，排查后重跑。
- `--expect postgres` 要求所有相关开关实际生效值 = postgres（含未配置开关会
  报错），全绿才准进入观察期。
- 退出码非 0 一律视为切写失败，按域 Runbook 回滚。

切写前基线：用 `--expect sqlite`（hq-ip-agent 用 `--expect postgres` 单独查）
跑一遍留档，确认切写前全集群一致旧权威。

## 第三层：SQLite 冰冻监控（切写后 1 小时 + 长期）

切写完成、校验器全绿后，旧 SQLite 库必须「冻住」——行数不变、mtime 不再动。

1. **基线**：切写前记下旧库各表行数与文件 mtime：
   ```bash
   stat -c '%Y %s %n' <sqlite.db>
   sqlite3 <sqlite.db> 'SELECT COUNT(*) FROM <表>;'   # 关键表即可
   ```
2. **切写后 1 小时内**：每 10 分钟查一次 `stat` mtime/大小 + 关键表行数。
   mtime 前进 = 仍有进程在写旧库 = **立即停**，按域 Runbook 停止条件上报。
   （常见漏网：没重启的服务、cron 脚本、手动工具直连 sqlite。）
3. **长期兜底**：影子核对 cron（01:17/13:17 flags、02:17/14:17 全域）的活源
   快照行数比对会持续抓「SQLite 行数在涨」；任何不一致照域 Runbook 上报处置。
4. 归档只在冰冻确认后进行：SQLite 文件改名保留、`lsof` 确认无进程再打开。

## 与各域 Runbook 的衔接

- 每个域的「生产切换步骤」在重启服务那一步之后，插入：
  「跑权威校验器，`--expect postgres` 全绿（见
  postgresql-cutover-authority-guard.md），否则立即回滚」。
- 每个域的「停止条件」补一条：「SQLite 冰冻监控 1 小时内 mtime 前进或行数
  增长（=仍有进程写旧库）」。

## 关于 M6（users.db）的提前说明

auth-service（`huangque-auth`）的 `HQ_IDENTITY_STORE` / `HQ_LEDGER_STORE`
开关在 M6 切写阶段才引入；此前校验器对这两项输出「OK（env 未配置）」，不视为
错误。切写时同样走本文三层防护，账务域另加「余额重算核对需老板批准」条款。
