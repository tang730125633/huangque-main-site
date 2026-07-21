# BUG-0003 Security Headers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden both tracked Nginx configurations with consistent security headers and precise-version suppression.

**Architecture:** Keep the deployment self-contained in the existing site configuration. Repeat the header set at each Nginx location that overrides `add_header` inheritance, and enforce the invariant with source-level unit tests.

**Tech Stack:** Nginx 1.18 configuration, Python `unittest`.

## Global Constraints

- Target only `129.204.166.13` configuration; no migration or new-server references.
- Do not require third-party Nginx modules.
- Do not deploy or reload production services in this PR workflow.

### Task 1: Add failing configuration tests

**Files:** `tests/test_nginx_csp.py`

- [ ] Assert both tracked configs contain two `server_tokens off;` directives.
- [ ] Assert the HTTPS server and each location with its own `add_header` contain all four security headers.
- [ ] Assert every CSP policy contains `frame-ancestors 'none'`.
- [ ] Run `python -m unittest tests.test_nginx_csp -v` and confirm RED.

### Task 2: Harden both Nginx configs

**Files:** `deploy/nginx-huangquechuanmei.conf`, `server/nginx-huangquechuanmei.conf`

- [ ] Add `server_tokens off` to HTTPS and HTTP servers.
- [ ] Add the four headers at server scope and repeat them in `location = /`, `location /`, and the static-resource location.
- [ ] Add `frame-ancestors 'none'` to every CSP copy and make the server copy available in both configs.
- [ ] Run the focused test and confirm GREEN.

### Task 3: Verify and publish

- [ ] Run `python -m unittest tests.test_nginx_csp -v`.
- [ ] Run `python scripts/ci_validate.py` when the repository snapshot supports it.
- [ ] Verify the remote diff contains only the two configs, test, design, and plan.
- [ ] Create a draft PR against `main` and wait for “代码与安全门禁”.
