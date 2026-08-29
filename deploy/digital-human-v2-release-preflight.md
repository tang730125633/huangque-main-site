# Digital-human v2 release preflight

The reviewed checkout's versioned gate is
`scripts/digital_human_v2_release_preflight.py`. A release transaction must run
it as the `ubuntu` service user **before copying any runtime file**. The same
gate is installed as `huangque-content.service` `ExecStartPre`, so a later
restart cannot expose a runtime whose dependencies have drifted.

The test server `8.148.158.106` path
`/home/ubuntu/material-libraries/huangque-media` is the only material-library
source of truth. Production requests never read that server directly. The
executable release contract is
`scripts/digital_human_material_mirror_release.py`; prose or a manual recursive
copy is not a release contract.

On the test server, a trusted operator builds an immutable release directory:

```sh
python3 scripts/digital_human_material_mirror_release.py build \
  --source-root /home/ubuntu/material-libraries/huangque-media \
  --output-root /home/ubuntu/releases/digital-human-materials
```

The builder validates all 318 indexed files and emits only `manifest.json` and
`bundle.tar.gz`. The manifest locks the source host/root, production mirror
root, entry count, index digest, bundle digest, file count, and payload size.
The release directory is content-addressed by the index digest and cannot be
overwritten. Record the builder's `manifest_sha256` in the reviewed release
ticket, then transfer that exact two-file directory through the approved
private artifact channel; the web application and customer requests have no
credentials or route to the test server.

After the reviewed release has been transferred, drain customer generation
traffic and run the installer as the production `ubuntu` service user:

```sh
python3 scripts/digital_human_material_mirror_release.py verify \
  --release-directory /home/ubuntu/releases/digital-human-materials/huangque-media-<index-prefix> \
  --manifest-sha256 <approved-manifest-sha256>
python3 scripts/digital_human_material_mirror_release.py install \
  --release-directory /home/ubuntu/releases/digital-human-materials/huangque-media-<index-prefix> \
  --manifest-sha256 <approved-manifest-sha256>
```

The installer rejects path traversal, links, special files, digest drift,
unindexed files, a wrong host/root/count, and a target other than
`/home/ubuntu/material-libraries/huangque-media`. It extracts to a private
staging directory, verifies every file, records a transaction journal, moves
the previous complete mirror to a versioned backup, atomically activates the
new mirror, and runs the full production runtime preflight. Any failure before
completion restores the previous mirror. A process interrupted after the old
mirror moves is recovered automatically by the next install, or explicitly:

```sh
python3 scripts/digital_human_material_mirror_release.py recover
```

The installed `.huangque-mirror-source.json` record has exactly
`schema_version`, `source_host`, `source_root`, `mirror_root`, `entry_count`,
and `index_sha256`; the gate recomputes the local `index.jsonl` digest and
rejects a missing, extra, malformed, drifting, or differently sourced record.
Retain the reported backup until post-release acceptance is complete. No
customer request may select a server path.

The gate is fail-closed and checks that locked 318-entry mirror, the production
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
