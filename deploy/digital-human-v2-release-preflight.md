# Digital-human v2 release preflight

The reviewed checkout's versioned gate is
`scripts/digital_human_v2_release_preflight.py`. A release transaction must run
it as the `ubuntu` service user **before copying any runtime file**. The same
gate is installed as `huangque-content.service` `ExecStartPre`, so a later
restart cannot expose a runtime whose dependencies have drifted.

The gate is fail-closed and checks the fixed 204-entry local material library,
offline Whisper model, Noto CJK font coverage, required FFmpeg encoders and
filters, output access, and the no-charge HeyGen upload preflight. It prints no
credential values. Install the reviewed script and
`deploy/systemd/huangque-content.service.d/digital-human-v2-preflight.conf`
before deploying the digital-human runtime. A failed gate leaves the previous
runtime and service untouched; roll back the script/drop-in to their previously
reviewed bytes if the release platform itself must be reverted.
