# Seedream 配置：真实任务入口接线验收

## 本次范围

- 普通 Seedream 图片请求：真实 HTTP 入口 → `create_paid_jobs` → 扣点 → jobs/队列 → 独立进程 worker。
- 数字人素材图片子任务：真实 HTTP 入口 → durable submission prepare → 扣点 → job；恢复复用原 attempt 的配置快照。
- 两条路径均在扣点前固定服务端配置版本，清除客户端传来的 `_provider_config`。配置不可用返回 503，未扣点、未入队。
- 补齐 `_gen_image_seedream` 到 `_seedream_one` 丢失的 `config_ref` 参数；仅有 payload 字段而未传到供应商调用不算接通。
- 已固定版本的任务不受后来发布、回滚、环境变量变更或关闭新任务接管开关影响。

## 实证与边界

前一版隔离回归为 195 项；其 SQLite 配置测试已被本轮 PostgreSQL-only 测试替代，不应继续把旧数字当作当前版本验收。

本轮 PostgreSQL-only 版本：Python 196 项整组回归 + admin config store 13 项 = 209 项通过，无跳过；前端配置弹窗 5 项通过。SQLite 禁增门禁原规则通过，未修改白名单。未进行生产、付费供应商或真实浏览器验收。

配置存储仅支持 PostgreSQL，必须配置 HQ_ADMIN_CONFIG_STORE=postgres 和 HQ_DATABASE_URL，运行时不建表、不回退 SQLite。旧业务的任务库保持不变。未启用接管时，旧后台启动不要求启用此功能。

入口测试使用 PostgreSQL 配置存储，真实执行 HTTP handler、任务数据库写入、入队及独立 worker。认证、扣点系统和供应商网络为隔离替身，断言 URL 与凭据摘要，不输出密钥、不真实扣点、不调用付费生成。

验证回归只替换 HTTP 运输，执行真实 save_draft → validate_draft → publish。404/429/5xx、超时、非成功响应均不得发布，失败复验撤销先前成功证据；旧验证器留下的非 2xx 假绿证据也不得发布。

覆盖：发布 B 后旧任务 A/新任务 B、回滚后的新任务、同请求幂等、快照失败无扣点、批次中途失败无部分扣点、客户端版本伪造、关闭开关后旧任务仍固定、数字人 durable attempt 恢复不重新选版本。

CI 已加入 PostgreSQL 与入口回归，使用 CI 自带的 PostgreSQL 16；本机验证使用隔离 embedded PostgreSQL。不得将测试数据库地址设为生产库，PG 用例会重建测试夹具。

## 未承诺的能力

- 未合并、未部署、未修改生产配置；尚未进行生产真实供应商和成片验收。
- imggen banana 路由仍为 Banana；script-to-video 当前 OpenAI 图片分支仍为 OpenAI，不声称这些产品已改用 Seedream。
- 其他供应商 URL/Key 接线仍不属于此单线路试点。
- runtime 上报是已观察到的进程加载证据，不代表所有生产实例均完成加载。
- 测试返回模拟图片内容，不证明真实生成文件可播放或图片质量。

## 回归命令

```text
python -m unittest tests.test_provider_config tests.test_provider_config_loop tests.test_provider_config_recovery tests.test_provider_config_submission tests.test_jobs_store tests.test_imggen_job_cas tests.test_seedream tests.test_matrix_template_submission tests.test_provider_key_pool -q
python -m unittest tests.test_provider_config_pg tests.test_provider_config_submission_pg -q
python -m unittest tests.test_provider_config_probe -v
python -m unittest tests.test_admin_config_store -q
```

所有配置测试都需要隔离 PostgreSQL；未提供 HQ_DATABASE_URL 时夹具自动启动本地 embedded 实例，无法启动即失败、不跳过。admin config store 组必须显式提供隔离 HQ_DATABASE_URL。测试夹具会清空 provider_config 的两张表，只能连接专用测试库。上线仍按原 PR 的迁移、代码发布和单线路灰度步骤独立审批执行。
