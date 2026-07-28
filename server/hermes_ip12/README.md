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
