# 隔离端到端测试设施

目的：在不接触生产的前提下，验证「后台切换渠道 → 用户页面提交 → 目标供应商
收到正确请求 → 返回成品」，并核对页面点数、服务端报价、扣费记录、任务记录一致。

## 组成

| 文件 | 作用 |
|---|---|
| `harness.py` | 假供应商（真实 HTTP，两条协议：openai-image / lechuang-image，能返回成品） |
| `stub_auth.py` | 受控替身：auth-service 的登录态、扣点、退点，并记录账本 |
| `local_service.py` | 隔离服务：**复用生产请求处理器与 worker**，只替换外部服务 |
| `test_http_entry_e2e.py` | 验收用例 |

## 关键点：复用生产代码，不重写受理

隔离服务**不自己实现** `/api/gen/*`：

* `/api/gen/banana` 由 `imggen_api.H`（生产处理器，nginx 把该路径路由到 8101）处理；
* 字段清洗、参数版本校验、报价、`channel_manager.capture` 渠道绑定、
  `jobs_store` 入库、worker 池执行，全部是生产代码；
* 只有**鉴权、扣费、供应商**换成受控替身（`stub_auth.py` + `harness.py`）。

## jobs 库只有一个

生产上 `core.JOB_DB` 与 `imggen_api.JOB_DB` 之所以是同一个文件，是因为生产
**没有设置** `CONTENT_JOB_DB`，两者都回落到 `content-api/content_jobs.db`。
隔离环境一旦只给 `imggen_api` 指路，建表与受理就会分裂成两个库
（症状：`no such table: jobs`）。所以这里显式把两处都对齐到同一个隔离文件。

## 运行

```bash
python -m unittest tests.isolated.test_http_entry_e2e -v
```

不连生产、不改生产路由、不产生任何真实费用。
