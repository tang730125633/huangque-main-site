# HQ CLI V0.1

`hq` is a zero-dependency Python 3.9+ discovery and navigation CLI. It is not
the Huangque product API: it cannot accept a base URL, credentials, methods,
task/project/board IDs, or submit a product action. It only returns official
extensionless deep links, plus fixed anonymous health checks in `doctor`.

An API exposes one or more system operations. HQ CLI is the Agent-facing
program that packages Huangque's approved APIs and workflows behind one stable
command contract. V0.1 starts with discovery and safe navigation; later
versions can add authenticated actions without teaching every Agent the
underlying endpoints.

## Install a wheel

```sh
python -m pip install huangque_hq_cli-0.1.0-py3-none-any.whl
```

## Recommended four steps for a no-context agent

```sh
hq capabilities --json
hq describe image --json
hq run image --environment main --input @input.json --json
hq doctor --environment main --json
```

`--input` is optional and defaults to `{}`. Its JSON schema contains only
business fields; `--environment` is a fixed `main` or `zelong` CLI option.
`run` URL-encodes only schema-approved prefill fields and opens no browser
unless `--open-browser` is explicit.

Every success and error is one JSON envelope with `schema` and `cli_version`.
Exit codes: `0` success, `2` usage, `3` unknown capability, `4` input schema,
`5` doctor health failure, `6` planned/auth-required capability, `7` browser launch failure.

## V0.1 boundary

IP12, IP12 report, and image/video/audio generation remain visible as
`planned_auth` capabilities but `hq run` rejects them. This release does not
log in, pay, generate, modify assets, create IP12 projects, open a canvas
collaboration, or call any account-bound API.
