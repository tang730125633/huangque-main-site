# IP12 Provider 兼容矩阵（T2）

状态：`T2 PASS / T3–T4 HOLD`
当前授权模型：`gpt-5.6-terra`
Terra T2 调用上限：55 次 / 2 元
黄雀报价、任务、生成、扣点：0

## 统一探针

代码：`server/hermes_ip12/provider_compat.py`
永久测试：`tests/test_ip12_provider_compat.py`

逻辑探针固定覆盖：

1. Strict Structured Output / JSON Schema；
2. 强制 `tool_choice` 与严格函数参数；
3. SSE delta 与 terminal event；
4. 连续状态：有存储时使用 `previous_response_id`，`store=false` 时原样回传上一轮 `response.output`；
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
| OpenAI 官方 | Responses | `gpt-5.6-terra` 经 Zelong→Novix 受控出口完成 10/10 live probes，usage、模型身份与请求相关性完整 | `PASS` |
| DashScope | Agents SDK + Chat Completions | 历史 Sol 目录不含目标模型；未偷换模型 | 历史 `model_unavailable` |
| 泽龙中转 | 尚无独立 SDK adapter | 历史 Sol 探针证明 HTTP 200 不保留完整 Responses 合同 | 历史 `HOLD` |

## 2026-08-24 Terra 官方兼容结果

最终单轮使用 OpenAI 官方 `gpt-5.6-terra` 与 Zelong→Novix 出口；13 次请求、205 input / 430 output Token，估算 0.041775 元。Structured Output、强制工具、SSE、无存储续轮、reasoning、`store=false`、usage、模型身份、结构化 4xx 和 timeout/cancel 全部 PASS，`evidence_source=live_capture`、`evidence_correlated=true`。

为修复预期 4xx usage 与客户端代理 timeout 两项 fail-closed 探针问题，四轮累计 49 次请求、估算 0.15978 元，未超过 55 次/2 元授权。T2 PASS 只解除 Provider 合同门禁，不自动授权 T3、Agents SDK 依赖安装或 T4 Canary。

## 2026-08-24 Sol 真实预检与兼容结果

Tang 明确授权三端使用 `gpt-5.6-sol`，最多 1000 次、100 元；通过后仍不允许开启 Canary。

| Provider | 免费模型目录 | 真实兼容探针 | 结果 |
|---|---|---|---|
| OpenAI 官方 | 凭证存在，但泽龙服务器访问 `api.openai.com:443` 超时 | 未进入模型调用 | `network_blocked` |
| DashScope | 目录 HTTP 200、241 个模型，但没有 `gpt-5.6-sol` | 按“不换模型”规则不发调用 | `model_unavailable` |
| 泽龙中转 | 目录 HTTP 200、5 个模型，包含 `gpt-5.6-sol` | 两轮共 22 个 Responses 兼容请求；HTTP 均为 200（timeout 探针除外），但无 Responses model/usage/typed output；stream 只返回 error | `HOLD` |

泽龙中转关键能力结果：Structured Output FAIL、强制工具 FAIL、SSE FAIL、continuation FAIL、model identity FAIL、usage FAIL、reasoning UNKNOWN、store FALSE UNKNOWN、结构化错误合同 FAIL，仅 timeout terminal PASS。它证明“返回 200”同时仍可删除、改写或不实现 Responses 语义。

中转没有返回 usage，因此不能报告实际 Token 费用为 0。按所有 22 个请求均消耗 `gpt-5.6-sol` 最大 512 输出 Token 的本地预算模型估算约 1.78 元；该数不是中转真实账务上界。Runner 已改为跨进程 0600 耐久账本，命令行不能把授权提高到 1000 次/100 元以上，并在任何 2xx 缺 usage 时立即停止后续模型调用。

以上 Sol 结果是历史证据，已由 Terra 官方 T2 PASS supersede；中转和 DashScope 的失败结论仍保留，不能外推为可用。

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

## 下一门禁

T3 真实同语料质量、延迟、usage 与成本对比需独立预算；T4 仍需 T3 PASS、锁定 SDK 依赖、有效 conformance artifact 和 Tang 单独授权。任何真实 Eval 都不得触发黄雀确认、生成、任务或点数。
