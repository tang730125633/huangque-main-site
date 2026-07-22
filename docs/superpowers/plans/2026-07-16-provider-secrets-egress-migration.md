# Provider Secrets and Egress Migration Implementation Plan

> **For Codex:** Use superpowers:executing-plans task-by-task. Never print secret values and stop on any failed security check.

**Goal:** Enable the test server to use the main site's provider credentials, production COS bucket, and shared xray egress without copying production authentication, payment, or database state.

**Architecture:** Export an explicit allowlist from the main server into a temporary mode-600 file, transfer it server-to-server over SCP, and delete the temporary export. Run a dedicated test xray service on loopback port 10809. Load the provider file into content-related systemd units and expose independent image, collection, and download services only through Nginx.

**Tech Stack:** Ubuntu 22.04, systemd, Nginx, Python 3.10, xray, SSH/SCP, SQLite.

---

### Task 1: Add and run the failing deployment acceptance test

**Files:**
- Create: `deploy/test-server/verify-full-environment.sh`

The test checks secret file permissions without reading values, required variable names, xray loopback binding, six systemd services, Nginx syntax, API health flags, loopback-only backend exposure, and clean Git state.

Run it before implementation. Expected RED: missing `/etc/huangque-test/providers.env` and missing egress/independent services.

### Task 2: Add versioned full-environment service configuration

**Files:**
- Modify: `deploy/test-server/nginx.conf`
- Modify: `deploy/test-server/huangque-test-content.service`
- Modify: `deploy/test-server/huangque-test-admin.service`
- Create: `deploy/test-server/huangque-test-egress.service`
- Create: `deploy/test-server/huangque-test-imggen.service`
- Create: `deploy/test-server/huangque-test-leadgen.service`
- Create: `deploy/test-server/huangque-test-dl.service`

Add exact Nginx routes for download (8097), collection/leads (8100), and banana/reverse (8101). Load both test and provider environment files. Start content-related services after the best-effort egress service. Run all business processes as `admin` and bind only to loopback.

Validate with `git diff --check`, `systemd-analyze verify`, and `nginx -t` before restart.

### Task 3: Export and transfer secrets without disclosure

**Files outside Git:**
- Main temporary: `/home/ubuntu/hq-provider-export.env`
- Test permanent: `/etc/huangque-test/providers.env`
- Test xray config: `/etc/huangque-test/egress/xray-client.json`
- Test binary: `/usr/local/bin/xray-egress`

Use a fixed allowlist for provider, endpoint, proxy, and COS variables. Exclude auth/payment/database variables. Transfer with `scp -3`; never pass file contents through command output. Set provider file `root:root 600`, xray config `root:admin 640`, and binary `root:root 755`. Remove the main temporary export immediately after successful transfer.

### Task 4: Start egress and backend services

Install the versioned units and Nginx configuration, reload systemd, enable/start xray, verify `127.0.0.1:10809`, then enable/restart content, admin, image, collection, download, auth, and Nginx services.

Use unauthenticated, no-cost HTTP checks through the proxy to verify TLS connectivity. Do not execute image, text, video, or collection jobs.

### Task 5: Verify GREEN, commit, and push

Run `deploy/test-server/verify-full-environment.sh`; expected exit 0. Confirm main and test temporary files are absent, Git does not track secret paths, and provider values never appeared in output. Commit only versioned units, Nginx, test script, and this plan. Push `main` after verification.

Report shared-cost/shared-COS risks, service operations, and the fact that provider health only proves configuration/connectivity—not paid generation quality.
