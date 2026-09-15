# 乐创 AI 图片视频统一渠道实施说明

## 目标

网页、Agent、后台测试和异步任务统一使用 `operation_id` 表示用户动作。渠道、模型与配置版本只表示执行选择，不能替代功能身份。完整术语见仓库根目录 `CONTEXT.md`。

## 控制链路

```text
请求参数
  → function_registry.classify_task(kind, payload)
  → operation_id
  → operation_mappings 当前修订
  → 渠道当前版本与兼容性/完整测试门槛
  → _channel_binding 执行快照
  → runs + run_snapshots 执行证据
```

`operation_mapping_versions` 保存不可变历史，`operation_mappings` 只保存当前指针与配置。并发修改通过 `expected_revision` 检查，旧页面不能覆盖新发布。

## 四种控制状态

- `legacy`：继续旧线路，仅用于尚未迁移或主动回退的功能。
- `shadow`：记录候选渠道快照，但不改变实际执行线路。
- `managed`：由统一渠道执行。发布及每次接单都要求当前渠道版本已启用，并有最近 24 小时通过的完整生成测试。
- `paused`：明确拒绝新任务，不回退旧线路。

托管路径不会自动使用备用渠道。付费创建请求是否被供应商接收可能无法确认，自动换渠道会造成重复扣费或重复成片；当前备用渠道只用于管理员明确切换并发布新修订。

## 能力与安全约束

- 图片与视频操作从 `function_registry` 生成统一目录，管理页不再手填 `kind/front`。
- Agent 的 `image-generate`、`video-generate` 直接映射到同一批 operation；Agent 请求进入内容服务后走同一解析器。
- 发布时校验图片/视频类型、参考图、蒙版及 Grok 1.5 参考图约束。
- 渠道配置变化后，新版本必须重新完成生成测试，旧测试不能为新版本背书。
- 客户端提交的 `_channel_binding` 和 `_channel_shadow` 会被剥离；任务只保存服务端重新生成的执行快照。
- 执行快照不含密钥，包含 `operation_id`、映射修订、渠道版本、适配器、实际模型和调用来源。

## 管理操作

渠道后台的“功能映射”页展示所有可接入 operation。发布 `managed` 前：

1. 保存并启用渠道；
2. 对当前渠道版本运行完整生成测试；
3. 先发布 `shadow` 并观察；
4. 发布 `managed`；
5. 异常时发布 `paused` 或新的 `legacy` 修订。

旧 `kind/front` 映射保留为只读兼容区，未迁移功能的行为不变。

## 数据兼容

迁移只新增表，不改公共业务数据库 schema，也不重写历史任务：

- `operation_mappings`
- `operation_mapping_versions`
- `run_snapshots`

原 `channels`、`versions`、`mappings`、`runs` 表继续可读；旧任务没有执行快照时，证据接口返回空的 operation 字段，不伪造历史归属。
