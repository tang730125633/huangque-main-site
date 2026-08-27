# Digital-human v2 release preflight

The reviewed checkout's versioned gate is
`scripts/digital_human_v2_release_preflight.py`. A release transaction must run
it as the `ubuntu` service user **before copying any runtime file**. The same
gate is installed as `huangque-content.service` `ExecStartPre`, so a later
restart cannot expose a runtime whose dependencies have drifted.

The test server `8.148.158.106` path
`/home/ubuntu/material-libraries/huangque-media` is the only material-library
source of truth. Production requests never read that server directly. Before
release, a trusted operator copies an exact snapshot into the production
read-only mirror at the same absolute path, verifies every indexed file, and
atomically installs `.huangque-mirror-source.json`. That record has exactly
`schema_version`, `source_host`, `source_root`, `mirror_root`, `entry_count`,
and `index_sha256`; the gate recomputes the local `index.jsonl` digest and
rejects a missing, extra, malformed, drifting, or differently sourced record.
The previous complete mirror remains the rollback when staging or validation
fails. No customer request may select a server path.

The gate is fail-closed and checks that locked 204-entry mirror, the production
`ubuntu` user's existing offline faster-whisper small cache at
`/home/ubuntu/.cache/huggingface/hub`, the installed `Noto Sans SC` Chinese
glyph coverage, required FFmpeg encoders and filters, output access, and the
no-charge HeyGen upload preflight. It prints no credential values. Install the
reviewed script and
`deploy/systemd/huangque-content.service.d/digital-human-v2-preflight.conf`
before deploying the digital-human runtime. A failed gate leaves the previous
runtime and service untouched; roll back the script/drop-in to their previously
reviewed bytes if the release platform itself must be reverted.

The release transaction invokes the gate with the same locked service
environment. The gate rejects any runtime that does not resolve to model
`small`, cache `/home/ubuntu/.cache/huggingface/hub`, and font
`Noto Sans SC`; changing those values requires a new reviewed release contract.
