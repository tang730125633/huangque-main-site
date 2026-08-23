# IP12 Provider 兼容矩阵（T2）

状态：`provider_live_blocked`
本轮真实 Provider 请求：0
黄雀报价、任务、生成、扣点：0

## 统一探针

代码：`server/hermes_ip12/provider_compat.py`
永久测试：`tests/test_ip12_provider_compat.py`

逻辑探针固定覆盖：

1. Strict Structured Output / JSON Schema；
2. 强制 `tool_choice` 与严格函数参数；
3. SSE delta 与 terminal event；
4. `previous_response_id` 连续状态；
5. reasoning effort 的有效证据；
6. `store=false` 后结果不可检索；
7. usage；
8. 实际响应模型身份；
9. 非法请求的结构化 4xx；
10. 超时或取消终态。

单项结果只允许 `pass / fail / unknown / blocked`。HTTP 200 但缺乏参数行为证据时必须是 `unknown` 或 `fail`，不能是 `pass`。整套手写或内存 transport 最多得到 `OFFLINE_PASS`；只有带 Provider request ID、请求指纹和采集时间的 `live_capture` 才能得到 `PASS`。

## 当前矩阵

| 路径 | 协议/适配 | 当前证据 | 结论 |
|---|---|---|---|
| `custom` | OpenAI-compatible Chat Completions | 发送 `model/messages/stream=false/JSON Schema/timeout`；现有代码不保存实际响应 model、usage，也不使用 API tools、续轮状态或 `store` | 可作为当前回滚路径；不满足 T2 完整 Provider 证据 |
| OpenAI 官方 | Agents SDK + Responses | 代码有 `OpenAIResponsesModel`；官方 Responses 合同包含 Structured Output、tools/tool_choice、`store`、stream、`previous_response_id`、usage 和错误，但本轮未获真实调用费用授权 | `blocked` |
| DashScope | Agents SDK + Chat Completions | 当前 adapter 会使用 Pydantic Structured Output；严格 JSON Schema、Responses 续轮、reasoning 与 usage 尚未通过同一探针 | `blocked`；必须先完成 conformance，代码默认 fail closed |
| 泽龙中转 | 尚无独立 SDK adapter | 没有受控 `base_url`、协议、模型身份、参数改写、SSE、usage、reasoning、store 或续轮证据 | `blocked`；未知 provider 默认 fail closed |

## 官方基准

- OpenAI Responses API 的请求合同明确包含 `store`、stream options、Structured Output、tools/tool choice、`previous_response_id`、usage 和实际响应 model：<https://developers.openai.com/api/reference/cli/resources/responses/methods/create>
- OpenAI Agents SDK 的 Manager 模式允许 Master 通过 `Agent.as_tool()` 调用 Specialist，而 Master 保留最终回复权：<https://openai.github.io/openai-agents-python/multi_agent/>
- SDK 运行配置可以关闭 tracing 或排除敏感输入/输出，并区分 SDK 本地工具执行并发和 Provider 侧 parallel tool calls：<https://openai.github.io/openai-agents-python/running_agents/>

这些官方合同只证明官方基准能力，不证明任何中转实现保留了相同行为。

## 安全门

- `agents_sdk` 默认关闭。
- 未识别 Provider 不再自动落入 OpenAI 分支，而是 `agents_sdk_provider_unsupported`。
- DashScope 未设置已验证 conformance 标记时，初始化前即 `agents_sdk_dashscope_conformance_not_proven`。
- SDK 尝试、成功、回退和脱敏错误类型进入进程级 health metrics；不记录异常正文、客户原话或工具参数。
- T4 不能再只靠两个环境变量开启：运行时还要求一个 SHA-256 固定、未过期、绑定 release/provider/model/corpus 的 live conformance artifact；其中 Provider 报告必须是 correlated `live_capture PASS`，Eval 必须满足全部硬门槛。
- SDK 依赖尚未进入泽龙部署 requirements，因此当前 Canary 也被部署依赖门禁阻止。

## 解除 blocked 所需授权和证据

需要 Tang 明确批准：

1. 可使用的官方 OpenAI、DashScope 与泽龙中转测试凭证；
2. 每个 Provider 的模型和最大文本调用费用；
3. 是否允许在泽龙仅启用只读 SDK Canary；
4. 真实 Eval 期间不得触发黄雀报价、生成、任务或点数。

授权后按同一请求集执行，不为单个 Provider 改语料或降低门槛。
