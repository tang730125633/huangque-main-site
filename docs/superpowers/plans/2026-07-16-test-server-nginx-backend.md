# Test Server Nginx and Backend Implementation Plan

> **For Codex:** Use superpowers:executing-plans to implement this plan task by task, stopping on any failed verification.

**Goal:** Serve the Huangque test site at `http://8.138.143.64` with isolated authentication, content-health, and admin backend services.

**Architecture:** Nginx serves `/opt/huangque-test-server/site` and proxies three loopback-only Python services managed by systemd. A root-owned test-only environment file supplies an independently generated internal token and disables Secure cookies while the test endpoint is HTTP.

**Tech Stack:** Ubuntu 22.04, Nginx, systemd, Python 3.10 standard-library HTTP services, SQLite, curl.

---

### Task 1: Add versioned test deployment configuration

**Files:**
- Create: `deploy/test-server/nginx.conf`
- Create: `deploy/test-server/huangque-test-auth.service`
- Create: `deploy/test-server/huangque-test-content.service`
- Create: `deploy/test-server/huangque-test-admin.service`

**Step 1: Write Nginx configuration**

Configure port 80, static root, clean URLs, `/api/auth/` to 8095, `/api/gen/` to 8096, and `/api/admin/` to 8098. Preserve proxy headers and reasonable request/time limits.

**Step 2: Write systemd units**

Run all services as `admin`, load `/etc/huangque-test/test.env`, use `/opt/huangque-test-server/server` as the working directory, and restart on failure.

**Step 3: Validate repository artifacts**

Run: `git diff --check`
Expected: exit 0 with no whitespace errors.

### Task 2: Install and stage Nginx

**Files:**
- Install: `/etc/nginx/sites-available/huangque-test`
- Link: `/etc/nginx/sites-enabled/huangque-test`

**Step 1: Install package**

Run: `apt-get update` and `DEBIAN_FRONTEND=noninteractive apt-get install -y nginx`
Expected: package installation exits 0.

**Step 2: Copy site configuration**

Remove only the default enabled-site symlink if present, install the repository configuration, and create the enabled-site symlink.

**Step 3: Validate Nginx**

Run: `nginx -t`
Expected: syntax and configuration tests are successful.

### Task 3: Configure isolated backend services

**Files:**
- Create: `/etc/huangque-test/test.env`
- Install: `/etc/systemd/system/huangque-test-auth.service`
- Install: `/etc/systemd/system/huangque-test-content.service`
- Install: `/etc/systemd/system/huangque-test-admin.service`

**Step 1: Verify runtime data is ignored**

Run `git check-ignore` for `server/users.db`, `server/content_jobs.db`, `server/admin_config.db`, and `server/content_out/`. If any path is not ignored, add a narrow ignore rule before starting services.

**Step 2: Create test-only environment**

Generate a random `HQ_INTERNAL_TOKEN`; set `HQ_AUTH_COOKIE_SECURE=0`, loopback `AUTH_BASE`, port values, test database paths, and disable COS collection. Set file mode 600. Do not add third-party or production secrets.

**Step 3: Install units and reload systemd**

Copy the versioned units to `/etc/systemd/system/`, run `systemctl daemon-reload`, then enable and start all three services.

**Step 4: Verify loopback services**

Run:
- `curl --fail http://127.0.0.1:8095/api/auth/health`
- `curl --fail http://127.0.0.1:8096/api/gen/health`
- `systemctl --no-pager --full status` for all three units

Expected: both health calls succeed and all units are active.

### Task 4: Start Nginx and verify end-to-end behavior

**Step 1: Enable and restart Nginx**

Run: `systemctl enable nginx` and `systemctl restart nginx`.

**Step 2: Verify local proxy and static routes**

Run:
- `curl -I http://127.0.0.1/`
- `curl --fail http://127.0.0.1/api/auth/health`
- `curl --fail http://127.0.0.1/api/gen/health`
- `curl --fail http://127.0.0.1/workbench/inspiration`

Expected: root redirects, health routes succeed, and the workbench returns HTML.

**Step 3: Verify registration and login flow**

Create a unique temporary test username, register through Nginx, log in with a cookie jar, and call `/api/auth/me`. Do not print or persist generated internal secrets. Leave the test user in the isolated test database as verification evidence.

**Step 4: Verify public endpoint**

Run equivalent `curl` checks against `http://8.138.143.64`. If local checks pass but public checks fail, report the Alibaba Cloud security-group requirement instead of changing unrelated firewall settings.

### Task 5: Commit, push, and final audit

**Step 1: Audit service exposure and Git state**

Run:
- `ss -ltnp`
- `systemctl is-enabled` for Nginx and the three services
- `git status --short`

Expected: only ports 22 and 80 are public; backend ports bind to 127.0.0.1; no databases, logs, or secrets appear in Git status.

**Step 2: Commit versioned deployment artifacts**

Commit the plan and `deploy/test-server/` files with an intentional message after all verification succeeds.

**Step 3: Push to the user's repository**

Push the resulting `main` commit to the configured `origin` only after the deployment is verified.

**Step 4: Report operational commands**

Provide the public HTTP URL and concise commands for status, restart, and logs. Explicitly state that HTTPS remains unavailable until a test domain is assigned.
