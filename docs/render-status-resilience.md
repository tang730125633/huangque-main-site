# 节点状态跟踪瞬时故障恢复

背景：百条真实压测中的一条 Relay 任务在本地 MP4 完成之前因 `timed out`
提前报告失败。本地成片保留，不能靠重复生成解决。

## 本次范围

- 取得本地 job_id 后，仅重新 GET 同一任务；不重复 POST 渲染。
- 状态查询的 timeout、连接中断、HTTP 408/429/500/502/503/504 可退避恢复。
- 仍沿用 NODE_JOB_TIMEOUT_SECONDS 总跟踪预算，使用 monotonic；每次请求和等待
  使用剩余预算，不因错误重置期限。真实 failed 仍立即返回失败。
- 400/401/403/404/409 等永久状态不盲目重试，日志只含错误类别/状态码。
- 已有成片下载仅重试 GET，最多三次，沿用原下载总预算；Relay 上传 POST 不自动重发。
- 不改并发、渲染质量、素材源、计费、数据库或历史任务状态。

## 验证

`python -m unittest tests.test_node_status_resilience tests.test_node_admission_retry tests.test_render_relay_manifest tests.test_render_relay_gpu -q`

包含真实本地 HTTP 首次读取超时后恢复，以及同 job_id/单次提交、截止时间、永久错误、
下载尝试上限和未知上传确认不得重发。部署只更新四节点 node_poller.py，空闲后重启
poller；不重启 renderer、主站、Relay、HY，不产生收费任务。

## 边界

这不是完整的分布式结果恢复机制。长期失联超过总预算仍会终止跟踪；上传确认丢失、
poller 进程崩溃后的持久恢复仍需独立设计。不能把本次修复描述为任何故障下都绝不出现
前后端状态差异。历史失败任务不自动重试、修改或扣点。
