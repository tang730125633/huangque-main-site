# IP12 OpenAI Agents SDK Spike

Disposable, offline-first comparison spike. It never imports the Hermes server,
never calls Huangque production tools, and never reads a real Project.

Boundary:

`Huangque AgentRun -> SDK RunState -> read-only Huangque fixture tools`

- Huangque Project remains the long-term customer/IP/content source of truth.
- Huangque AgentRun remains the durable business work-order source of truth.
- Production/Job/Artifact remain the billing and delivery source of truth.
- SDK state is an inner cognitive loop only and may be deleted/recreated.

Run:

```bash
python -m venv /tmp/huangque-agents-sdk-spike-venv
/tmp/huangque-agents-sdk-spike-venv/bin/pip install -r requirements.txt
/tmp/huangque-agents-sdk-spike-venv/bin/python -m unittest discover -s tests -p 'test_*.py'
/tmp/huangque-agents-sdk-spike-venv/bin/python eval_ab.py
```

Provider modes:

- `scripted`: default; no network, no secrets, deterministic SDK loop.
- `openai`: requires an already-present `OPENAI_API_KEY`; never creates or prints one.
- `dashscope`: requires an already-present `DASHSCOPE_API_KEY`; uses the SDK's
  OpenAI-compatible Chat Completions model and disables OpenAI trace export.

The provider-backed modes are probes only. They expose no paid Huangque tool.
