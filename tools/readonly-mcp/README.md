# Huangque read-only MCP

The existing production server is now tracked here. Baseline SHA-256 before
the journal fix: `832f1fc4fdf85864b201386dd433412254675cf2f64f886b9668549443dcaa0f`.

Production: `dapeng-server`, service `huangque-readonly-mcp`, deployed file
`/opt/huangque-readonly-mcp/server.py`. Credentials remain in protected remote
environment files; never copy them into this repository.

Journal readers use the existing `ubuntu` account's `adm` membership directly.
Keep `NoNewPrivileges=yes`; sudo cannot work inside that service sandbox.
Command failures and incomplete journal access return MCP `isError=true`.
The error summary selects journal priority `err` and above, not all textual
application errors. Use per-service recent logs for application diagnostics.

Check: `python3 tests/test_readonly_mcp_logs.py`.

Deploy only this file after CI and merge, checking the live file has not changed
since inspection. Back it up first, install the merged file, restart only
`huangque-readonly-mcp`, compare SHA-256, and invoke both log tools through the
connected MCP. On failure restore the backup and restart the same service.

This fix covers all journal-reading paths. Other existing sudo-based operations
(storage-authority verification and backup-directory listing) are outside the
log-reading repair and still require separate permission work; this is not an
acceptance of all 23 tools or their security boundaries.
