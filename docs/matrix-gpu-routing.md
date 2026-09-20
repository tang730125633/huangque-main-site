# GPU-only template video routing

Requires the generation service's `webgpu-native` contract v1, covering all 22
public template IDs. Merely seeing an NVIDIA GPU or a live heartbeat is not proof
that the worker has the composition backend installed.

The updated poller reads `/health.gpu_render` from its local render service and
includes that contract in heartbeat and claim requests. Contracts expire with
node liveness. Runtime fingerprints and supported template IDs are validated;
unverified, stale, software-only and incompatible nodes cannot claim required
GPU jobs. Priority/load balancing ignores incapable nodes.

Enable only after the matching renderer is installed and probed on the workers:

```text
RELAY_REQUIRE_GPU=1
RELAY_GPU_QUEUE_MAX=500
```

Compatibility default is enforcement off. With enforcement on, no compatible
GPU returns a preflight error before a job is admitted. Busy compatible workers
can queue jobs up to the configured limit. The relay stores a per-job GPU claim
contract in its own SQLite database (`gpu_contract`, default empty for historical
jobs). This additive migration preserves in-flight legacy job completion.

Required jobs can publish only with matching runtime evidence and node identity.
Binary upload must complete before the metadata report can mark success. Relay
delivery URLs remain relay-owned; rendering metadata cannot replace them.
Turning enforcement off does not erase existing jobs' GPU requirements.

Rollout: deploy the compatible relay/poller first with enforcement off; stage
and probe immutable GPU renderer releases on every worker; wait for active jobs
before switching workers; confirm the 22-template capabilities; enable relay
enforcement; verify website/CLI output and billing behavior. No service restart
or remote deployment is performed by this code change.

Before reverting to old relay code, drain or resolve required GPU jobs through
the normal failure/refund workflow. Do not silently run those jobs on CPU and do
not resubmit already completed jobs. A CPU-only central service may continue
serving metadata and preflight, but is excluded from the GPU claim pool.

Tests: `python -m pytest tests/test_render_relay_gpu.py tests/test_render_relay_manifest.py`.
