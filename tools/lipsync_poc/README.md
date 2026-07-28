# 短剧口型 Provider PoC 工具

该目录承载阶段 0-A 的离线评测框架和阶段 0-B 的真实 Provider 对比适配器。它只用于受控 PoC，不接入生产画布、项目扣点或用户数据。

## 当前候选

- `sync-labs`：直接上传无声视频和主音轨，异步查询并下载结果。
- `fal-latentsync`：通过 fal 队列运行 LatentSync，支持查询、取消和结果重拉。
- `mock`：离线合同测试，不访问网络、不收费，也不产生真实口型。

HeyGen 仍属于现有数字人链路；其公开 Avatar + Audio 合同不等同于“任意短剧画面 + 主音轨”，因此不作为本轮横向候选。

## 安全与资金边界

- API Key 只从环境变量读取，不得写入清单、日志、报告或 Git。
- 私有样本、Provider 响应、媒体和报告必须放在仓库外，或默认的 `.local-content-out/lipsync-poc` 忽略目录。
- 工具不调用项目扣点服务；真实 Provider 可能直接产生供应商费用。
- 每个样本会先落盘 `submitting` 状态，拿到 Job ID 后立即原子更新。已有状态默认拒绝重复创建任务。
- 两个候选均未在本实现中假设服务端支持幂等键。提交响应丢失且没有 Job ID 时，报告会标记 `requires_reconciliation`，不得自动重提。
- 下载结果只接受 HTTPS，限制最大响应体，并使用临时文件原子替换。
- Provider 输出音轨会被 FFmpeg 移除；最终媒体必须经 FFprobe 确认为零音轨。
- Token、密钥、Cookie、Authorization、查询参数 URL 和畸形 URL 会在报告中统一脱敏。

## 环境变量

Sync Labs：

```text
SYNC_API_KEY                 必填
SYNC_API_BASE                可选，默认 https://api.sync.so
SYNC_LIPSYNC_MODEL           可选，默认 lipsync-2
SYNC_LIPSYNC_COST_PER_SECOND_USD
                             验收前必须配置，用于统一估算成本
```

fal.ai LatentSync：

```text
FAL_KEY                      必填
FAL_QUEUE_BASE               可选，默认 https://queue.fal.run
FAL_LIPSYNC_MODEL            可选，默认 fal-ai/latentsync
FAL_LIPSYNC_COST_PER_SECOND_USD
                             验收前必须配置，用于统一估算成本
```

成本变量只用于报告估算，不代表供应商最终账单；最终仍需人工对账。

## 样本清单

清单必须符合 `sample_manifest.schema.json`。媒体只允许引用 `assets_root` 下的相对路径。`visible` 样本必须提供 `character_key` 和明确的 `face_target`。

建议先运行：

```powershell
python -m tools.lipsync_poc.run_poc `
  --manifest C:\private-lipsync\manifest.json `
  --assets-root C:\private-lipsync\assets `
  --validate-only
```

## 真实 Provider 冒烟

先对每个 Provider 各运行 5 条样本，确认提交、轮询、下载、静音化、报告和费用归属正确，再扩大到 20～30 条：

```powershell
python -m tools.lipsync_poc.run_poc `
  --manifest C:\private-lipsync\manifest.json `
  --assets-root C:\private-lipsync\assets `
  --provider sync-labs `
  --sample-id front-normal-01
```

```powershell
python -m tools.lipsync_poc.run_poc `
  --manifest C:\private-lipsync\manifest.json `
  --assets-root C:\private-lipsync\assets `
  --provider fal-latentsync `
  --sample-id front-normal-01
```

中断或轮询异常时只能使用原 Job ID 恢复：

```powershell
python -m tools.lipsync_poc.run_poc <原参数> --resume
```

任务已成功、只需重新下载时：

```powershell
python -m tools.lipsync_poc.run_poc <原参数> --refetch
```

禁止删除状态文件后重新提交来绕过费用核对。

## 产物与人工评分

每个 Provider 使用独立目录：

```text
<output-dir>/<provider>/
  state/<sample-id>.json
  media/<sample-id>.mp4
  reports/<sample-id>.json
```

报告包含输入哈希、Job ID、耗时、媒体规格、输出静音化结果、估算成本、恢复能力和脱敏元数据。人工复核需要填写口型、身份、画质、整句错位、AV 偏移、审核人和审核时间；未填写时系统不会伪造评分。

汇总评测：

```powershell
python -m tools.lipsync_poc.evaluation `
  --output-dir .local-content-out\lipsync-poc `
  --providers sync-labs fal-latentsync
```

汇总文件为 `evaluation-summary.json`。只有质量、成功率、成本配置、静音输出、人工复核和费用核对门槛全部通过的候选才会得到 `go` 并参与默认 Provider 选择。
