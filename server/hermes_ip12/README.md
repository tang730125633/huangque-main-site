# Hermes IP12 Flask

This directory is the Git source for the existing Hermes Flask application.
Runtime data stays outside Git under `data/`, `media_library/`, and `knowledge/`.
Secrets are supplied through the systemd `EnvironmentFile`; never add them here.

Production keeps the original flat-module layout:

```bash
cd /home/ubuntu/hermes-web
python3 -c 'import server; server.app.run(host="127.0.0.1", port=3102, debug=False)'
```

`/` serves the current v6 workbench. `/classic` preserves the original
report-and-deliverable interface against the same conversations.

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
