# Hermes IP12 Flask

This directory is the Git source for the existing Hermes Flask application.
Runtime data stays outside Git under `data/`, `media_library/`, and `knowledge/`.
Secrets are supplied through the systemd `EnvironmentFile`; never add them here.

Production keeps the original flat-module layout:

```bash
cd /home/ubuntu/hermes-web
python3 -c 'import server; server.app.run(host="127.0.0.1", port=3102, debug=False)'
```

`/` serves the current Project workbench. `/classic` is a compatibility alias
for the same page; the former session interface is retired.

## IP12 T1–T4 acceptance gates

The frozen product and safety contract is in
`docs/IP12-Agent验收合同-T1-T4.md`. The permanent stateful dialogue corpus is
`tests/fixtures/ip12_semantic_router_cases.json`; every Cognitive Engine and
Provider must use the same cases and thresholds.

Zero-cost contract and compatibility checks:

```bash
python3 -m pytest -q \
  tests/test_ip12_eval_contract.py \
  tests/test_ip12_provider_compat.py \
  tests/test_cognitive_engine.py \
  tests/test_semantic_master_router.py
```

Hard gates are: Schema and safety 100%, no tool/reference hallucinations, no
chat-to-production misfire, and at least 90% exact intent/delegate/tool routing.
An average score never overrides one payment, privacy, idempotency, or business
state failure.

Provider transcripts are graded by `provider_compat.py`. HTTP 200 without
behavioral evidence remains `unknown` or `fail`. Current live evidence and the
official baseline are documented in `docs/IP12-Provider兼容矩阵-T2.md`.

Real Provider Eval is never enabled by the default test command. It requires
separate protected credentials, an explicit model/cost approval, and zero
Huangque production tools. T2 passing does not authorize T3 or a Zelong SDK
Canary; keep `HERMES_AGENTS_SDK_ENABLED=0` until those gates are separately
approved and verified.

After explicit approval, `cognitive_live_eval.py` is the only T3/T4 entrypoint.
It re-runs the correlated T2 contract and evaluates custom plus Agents SDK on
the same corpus under one durable request/CNY ledger. A PASS artifact is mode
`0600`, release/corpus/provider/model-bound, and expires within seven days.

```bash
python3 cognitive_live_eval.py --mode t3 \
  --release-sha "$RELEASE_SHA" --model gpt-5.6-terra \
  --max-requests 120 --max-cny 12 \
  --budget-ledger /home/ubuntu/hermes-preview-data/terra-t3-budget.json \
  --output /home/ubuntu/hermes-preview-data/terra-conformance.json
```

T4 enables the SDK only for the configured canary Project, runs one read-only
decision without saving the Project, then restores `custom/0`. Any invalid or
expired artifact, missing usage, budget exhaustion, prepare/write tool, or
Project byte change is HOLD. Do not use this runner to confirm a quote or poll
a production Job.

## One-time artifact ownership migration

Before the first deployment that enables owner-isolated artifact storage, map
all pre-isolation assets to the existing account that created them. Do not guess
the username: confirm it in the account service first. Stop Hermes while the
migration runs so the media index cannot change concurrently. The release
package installs the migration tool under `scripts/` before invoking it.

```bash
sudo systemctl stop hermes-ip12-preview
python3 scripts/migrate_hermes_artifacts.py \
  --root-dir /home/ubuntu/hermes-web \
  --data-dir /home/ubuntu/hermes-web/data \
  --legacy-owner CONFIRMED_USERNAME \
  --dry-run
python3 scripts/migrate_hermes_artifacts.py \
  --root-dir /home/ubuntu/hermes-web \
  --data-dir /home/ubuntu/hermes-web/data \
  --legacy-owner CONFIRMED_USERNAME
sudo systemctl start hermes-ip12-preview
```

The migration copies legacy media, knowledge, videos, analyses, and uploads;
the originals are deliberately retained. It is idempotent and records a
checksum manifest in `data/.migrations/`. After verifying `/healthz`, media
search, and historical video URLs, archive the legacy directories according to
the normal backup policy.

Quota preflight runs before the manifest or any artifact is written. All active
storage below `data/` counts toward the runtime quota, including `agnes_lab/`,
`team_workbench/`, `users/`, `media_library/`, and `knowledge/`. Only the
retained top-level `data/videos/`, `data/analyses/`, and `data/uploads/`
rollback copies are excluded. If preflight reports insufficient space,
increase `HERMES_DATA_QUOTA_MB` explicitly and rerun the dry-run before
migration.

To roll back, stop Hermes first. Rollback refuses to overwrite a media index or
remove a migrated file that changed after migration.

```bash
sudo systemctl stop hermes-ip12-preview
python3 scripts/migrate_hermes_artifacts.py \
  --data-dir /home/ubuntu/hermes-web/data \
  --rollback
sudo systemctl start hermes-ip12-preview
```
