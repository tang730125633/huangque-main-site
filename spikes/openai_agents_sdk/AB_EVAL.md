# IP12 Runtime A/B Spike · 2026-08-24

## Decision

**暂不采用 OpenAI Agents SDK。**

The mixed architecture remains technically promising, but the adoption gate is
not met because neither B1 nor B2 could run a real provider eval in the current
protected environment. Keep A and B isolated; do not deploy either spike.

## Fixed business boundary

```text
Huangque Project / AgentRun / Production / Job / Artifact
  -> private SDK RunState (one or more cognitive runs)
  -> read-only Huangque function tools
  -> vendor-neutral AgentDecision
```

The SDK state is not a balance, quote, Job, Artifact, or Project source of truth.
The serialized SDK state contains tool-call arguments and trace metadata, so it
must remain in the private AgentRun envelope and never be returned by customer APIs.

## Official capability mapping

| Need | SDK evidence | Spike result |
|---|---|---|
| Model → tool loop | `Runner.run/run_streamed` | PASS offline |
| Master owns reply | Specialist through `Agent.as_tool()` | PASS |
| Function tools | `function_tool` | PASS: three fixture reads |
| Approval pause | `needs_approval=True` interruptions | PASS |
| Serialize/resume | `RunState.to_json/from_json` | Same-process PASS; nested Agent-as-Tool fresh-process FAIL |
| Streaming | `stream_events()` / raw text delta | PASS |
| Trace | custom `TracingProcessor` | PASS; IDs/types only |
| Session continuation | SDK sessions / input list | Supported, not provider-tested |

Official references:

- https://platform.openai.com/docs/quickstart/make-your-first-api-request
- https://openai.github.io/openai-agents-python/multi_agent/
- https://openai.github.io/openai-agents-python/human_in_the_loop/
- https://openai.github.io/openai-agents-python/running_agents/
- https://openai.github.io/openai-agents-python/tools/
- https://openai.github.io/openai-agents-python/models/

## Eval evidence

- Call budget: **49 / 50 scripted model calls**.
- Real provider calls: **0**.
- Huangque paid/media calls: **0**.
- Runtime scenarios: **9 / 9** offline PASS.
- Semantic fixture contract coverage: **26 / 26**.
- Provider decision accuracy and illegal-combination rate: **BLOCKED**; no key.
- Scripted status/intent mismatches: **0 / 8**; this is not a full cross-field legality metric.
- Unit tests: **12 / 12 PASS**, including a permanent known-failure probe.

Covered scenarios: specialist selection, missing avatar, missing voice, assets
ready, multiple-production ambiguity, changed source target, approval interruption
and serialized resume, streaming delta, tool timeout, invalid tool structure, and
text confirmation without any paid tool.

Critical limitation reproduced in a real second Python process: an approval raised
inside nested `Agent.as_tool()` does not resume to final output in SDK 0.8.4. The
fresh process re-runs the three read tools and pauses on the quote approval again.
The same pattern works only when the Specialist is the top-level Agent, which would
violate this product decision that the Master must own the final reply.

## Provider comparison

### B1 · OpenAI Responses

- Baseline: `gpt-5.6-terra`.
- Status: `provider_blocked` because `OPENAI_API_KEY` is absent.
- The SDK uses Responses by default and exposes native streaming, RunState,
  approvals, sessions, and tracing.
- Responses application state has a default retention period documented by the
  OpenAI platform; a production decision needs an explicit data-retention review.

### B2 · DashScope qwen-plus

- Status: `provider_blocked` because `DASHSCOPE_API_KEY` is absent.
- Low-cost adapter path exists through `OpenAIChatCompletionsModel` and an
  OpenAI-compatible `AsyncOpenAI` client.
- It does not prove Responses-only continuation semantics; OpenAI trace export
  must be disabled without an OpenAI key, and provider-specific streaming plus
  structured-output behavior still requires a live eval.

## Complexity

| Candidate | Measured code |
|---|---:|
| A checkpoint `7a6d91f9` | 6 files, +1215/-190, net +1025 lines |
| B integration surface | 4 files, 395 lines |
| B offline model/eval harness | 398 lines |
| B fresh-process known-failure probe | 47 lines |
| B permanent tests | 186 lines |

B removes a large part of the inner model/tool/approval/stream/trace loop, but
does **not** remove Huangque's durable worker, quote, idempotency, Job polling,
Artifact verification, refunds, Project writeback, or SSE business protocol.
Adopting it would be beneficial only if it replaces A's cognitive-loop code
instead of being wrapped around the entire A runtime.

## Dependency risks observed

`openai-agents==0.8.4` needed the release-lock versions
`openai==2.20.0` and `pydantic==2.12.3` in this spike. Installing the newest
allowed `openai`/`pydantic` versions caused an import/runtime validation failure.
Python 3.9 also needed `eval-type-backport`; this machine's SOCKS environment
needed `socksio`. These are isolated venv findings, not production changes.

## Adoption gate

Not passed:

1. Same-eval provider accuracy is unverified.
2. Real latency, token use, cost, and data retention are unverified.
3. B2 feature parity is unverified.
4. Nested Agent-as-Tool approval state is not durable across a fresh process.

Passed offline:

1. Agent-as-tool keeps the Master as final responder.
2. Approval cannot call a Huangque paid tool because none is exposed.
3. Same-process SDK state resumes without embedding the fixture script.
4. Public Huangque contract is vendor-neutral and redacted.

Privacy boundary: serialized SDK state does retain the raw user turn and tool-call
arguments. The Spike now returns it only behind `_private_state_json` when a test
explicitly opts in; normal callers receive only `public_run`. Production storage
would need encryption/access control and must never expose this blob via customer
API, SSE, logs, or ordinary events.

Trace boundary: the first implementation replaced the process-global trace
processor per run and mixed concurrent traces. The Spike now installs one
process-wide, thread-safe processor and partitions events by Huangque `run_id`;
a 10-run concurrent test verifies one start/end pair per outer run.

Next decision point: run a bounded provider eval only after an already-provisioned
test credential and data-retention approval are available. Until then, retain A
and B as separate, undeployed candidates.
