# Local Test-Backend Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve local workbench files and proxy same-origin `/api/*` requests to the configured test server without copying secrets or databases locally.

**Architecture:** A standard-library Python HTTP server serves `site/` and forwards API requests to one fixed, validated upstream. The proxy rewrites only `Set-Cookie` attributes that are incompatible with loopback HTTP, filters hop-by-hop headers, sanitizes logs, and exposes a CLI used by the local launcher.

**Tech Stack:** Python 3 standard library (`http.server`, `http.client`, `urllib.parse`, `argparse`), `unittest`, Bash launcher.

## Global Constraints

- Bind only to `127.0.0.1`.
- Never copy or log server secrets, databases, Cookie, Authorization, query strings, or request bodies.
- The upstream is fixed at startup and must use `http://` or `https://` without credentials or a path prefix.
- Preserve `HttpOnly`, `SameSite`, `Path`, expiry and other Cookie attributes; remove `Domain` and remove `Secure` only for local HTTP.
- Do not bypass authentication, authorization, point deduction, rate limits, or safety checks.
- Automated and integration verification must not trigger a paid image or video generation.

---

### Task 1: Proxy primitives and safety tests

**Files:**
- Create: `scripts/dev_proxy.py`
- Create: `tests/test_dev_proxy.py`

**Interfaces:**
- Produces: `normalize_upstream(value: str) -> urllib.parse.SplitResult`
- Produces: `rewrite_set_cookie(value: str, local_http: bool = True) -> str`
- Produces: `safe_request_path(value: str) -> str`
- Produces: `is_hop_by_hop(name: str) -> bool`

- [ ] **Step 1: Write failing unit tests for upstream validation, Cookie rewriting, safe paths, and hop-by-hop headers**

```python
class ProxyPrimitiveTests(unittest.TestCase):
    def test_normalize_upstream_accepts_fixed_http_origins(self):
        parsed = dev_proxy.normalize_upstream("http://8.138.143.64")
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("http", "8.138.143.64", ""))

    def test_normalize_upstream_rejects_credentials_paths_and_bad_schemes(self):
        for value in ("file:///tmp/site", "http://user:pass@example.com",
                      "https://example.com/prefix", "example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dev_proxy.normalize_upstream(value)

    def test_rewrite_set_cookie_for_loopback_http(self):
        value = ("hq_session=secret; Domain=example.com; Path=/; Secure; "
                 "HttpOnly; SameSite=Strict; Max-Age=3600")
        rewritten = dev_proxy.rewrite_set_cookie(value, local_http=True)
        self.assertNotIn("Domain=", rewritten)
        self.assertNotIn("Secure", rewritten)
        self.assertIn("Path=/", rewritten)
        self.assertIn("HttpOnly", rewritten)
        self.assertIn("SameSite=Strict", rewritten)
        self.assertIn("Max-Age=3600", rewritten)

    def test_safe_request_path_removes_query_and_fragment(self):
        self.assertEqual(dev_proxy.safe_request_path("/api/auth/login?token=secret#x"),
                         "/api/auth/login")

    def test_hop_by_hop_header_names_are_recognized_case_insensitively(self):
        self.assertTrue(dev_proxy.is_hop_by_hop("Connection"))
        self.assertTrue(dev_proxy.is_hop_by_hop("transfer-encoding"))
        self.assertFalse(dev_proxy.is_hop_by_hop("Content-Type"))
```

- [ ] **Step 2: Run the primitive tests and verify RED**

Run: `python -m unittest tests.test_dev_proxy.ProxyPrimitiveTests -v`

Expected: import failure because `scripts/dev_proxy.py` does not exist.

- [ ] **Step 3: Implement the primitive functions and constants**

```python
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

def normalize_upstream(value):
    parsed = urllib.parse.urlsplit((value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("upstream must be an http(s) origin")
    if parsed.username or parsed.password:
        raise ValueError("upstream credentials are not allowed")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("upstream must not contain a path, query, or fragment")
    return parsed._replace(path="")

def rewrite_set_cookie(value, local_http=True):
    pieces = [piece.strip() for piece in value.split(";")]
    kept = []
    for index, piece in enumerate(pieces):
        lowered = piece.lower()
        if index > 0 and lowered.startswith("domain="):
            continue
        if index > 0 and local_http and lowered == "secure":
            continue
        kept.append(piece)
    return "; ".join(kept)

def safe_request_path(value):
    return urllib.parse.urlsplit(value).path

def is_hop_by_hop(name):
    return name.lower() in HOP_BY_HOP
```

- [ ] **Step 4: Run the primitive tests and verify GREEN**

Run: `python -m unittest tests.test_dev_proxy.ProxyPrimitiveTests -v`

Expected: 5 tests pass.

- [ ] **Step 5: Commit the proxy primitives**

```bash
git add scripts/dev_proxy.py tests/test_dev_proxy.py
git commit -m "test: define local proxy safety rules"
```

### Task 2: Same-origin static and API server

**Files:**
- Modify: `scripts/dev_proxy.py`
- Modify: `tests/test_dev_proxy.py`

**Interfaces:**
- Consumes: Task 1 primitive functions.
- Produces: `DevProxyHandler`, `create_server(site_root, upstream, port=8097) -> ThreadingHTTPServer`
- Produces: CLI `python scripts/dev_proxy.py --upstream URL --port 8097 --site-root site`

- [ ] **Step 1: Write failing integration tests with a local fake upstream**

Tests must start a fake `ThreadingHTTPServer` on an ephemeral port and a proxy on another ephemeral port. Assert:

```python
def test_serves_static_files_from_configured_site_root(self):
    status, headers, body = self.request("GET", "/workbench/inspiration.html")
    self.assertEqual(status, 200)
    self.assertIn(b"LOCAL-INSPIRATION", body)

def test_forwards_api_method_query_body_and_rewrites_cookie(self):
    status, headers, body = self.request(
        "POST", "/api/auth/login?source=local", b'{"username":"qilin"}',
        {"Content-Type": "application/json"},
    )
    self.assertEqual(status, 200)
    payload = json.loads(body)
    self.assertEqual(payload["method"], "POST")
    self.assertEqual(payload["path"], "/api/auth/login?source=local")
    self.assertEqual(payload["body"], '{"username":"qilin"}')
    cookie = dict(headers)["Set-Cookie"]
    self.assertNotIn("Domain=", cookie)
    self.assertNotIn("Secure", cookie)
    self.assertIn("HttpOnly", cookie)

def test_upstream_failure_returns_sanitized_502_json(self):
    self.stop_upstream()
    status, _, body = self.request("GET", "/api/auth/health?secret=hidden")
    self.assertEqual(status, 502)
    self.assertEqual(json.loads(body)["detail"], "test backend unavailable")
    self.assertNotIn("secret", self.proxy_log.getvalue())
```

- [ ] **Step 2: Run the integration tests and verify RED**

Run: `python -m unittest tests.test_dev_proxy.ProxyIntegrationTests -v`

Expected: failures because `create_server` and proxy routing are not implemented.

- [ ] **Step 3: Implement the minimal proxy handler and server factory**

Implementation requirements:

```python
class DevProxyHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _dispatch(self):
        if safe_request_path(self.path).startswith("/api/"):
            return self._proxy_api()
        if self.command not in ("GET", "HEAD"):
            return self._json_error(405, "method not allowed")
        return super().do_GET() if self.command == "GET" else super().do_HEAD()

    do_GET = _dispatch
    do_HEAD = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch

    def log_message(self, fmt, *args):
        sys.stderr.write("[dev-proxy] %s %s\n" %
                         (self.command, safe_request_path(self.path)))
```

`_proxy_api()` must use `HTTPConnection` or `HTTPSConnection`, set the fixed upstream `Host`, filter hop-by-hop and sensitive forwarding metadata, stream the response in 64 KiB chunks, rewrite every `Set-Cookie`, and return `502` for connection errors or `504` for timeouts. `_json_error()` must emit UTF-8 JSON with an explicit `Content-Length` and `Connection: close`.

`create_server()` must resolve `site_root`, reject a missing/non-directory root, call `normalize_upstream`, use `functools.partial(DevProxyHandler, directory=str(root))`, attach the parsed upstream to the server, and bind exactly `("127.0.0.1", port)`.

- [ ] **Step 4: Add an argument parser and guarded `main()`**

```python
def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--site-root", default=str(ROOT / "site"))
    parser.add_argument("--port", type=int, default=8097)
    args = parser.parse_args(argv)
    server = create_server(args.site_root, args.upstream, args.port)
    print("local site: http://127.0.0.1:%d/workbench/" % args.port)
    print("test backend: %s" % normalize_upstream(args.upstream).geturl())
    print("WARNING: API calls use real test-server accounts, points and provider quota")
    server.serve_forever()

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run focused and repository static checks**

Run:

```bash
python -m unittest tests.test_dev_proxy -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

Expected: all proxy tests pass; static checks exit 0.

- [ ] **Step 6: Commit the working proxy**

```bash
git add scripts/dev_proxy.py tests/test_dev_proxy.py
git commit -m "feat: add local test-backend proxy"
```

### Task 3: Launcher, documentation, and no-charge smoke verification

**Files:**
- Modify: `scripts/dev_local.sh`
- Modify: `docs/本地开发与反漂移工作流.md`
- Modify: `tests/test_dev_proxy.py`

**Interfaces:**
- Consumes: Task 2 CLI.
- Produces: `HQ_DEV_UPSTREAM=http://8.138.143.64 bash scripts/dev_local.sh`

- [ ] **Step 1: Add a failing launcher contract test**

```python
def test_launcher_uses_proxy_when_test_upstream_is_configured(self):
    launcher = (ROOT / "scripts" / "dev_local.sh").read_text(encoding="utf-8")
    self.assertIn('if [ -n "${HQ_DEV_UPSTREAM:-}" ]', launcher)
    self.assertIn('dev_proxy.py --upstream "$HQ_DEV_UPSTREAM"', launcher)
    self.assertIn("真实测试服务器点数", launcher)
```

- [ ] **Step 2: Run the launcher test and verify RED**

Run: `python -m unittest tests.test_dev_proxy.LauncherContractTests -v`

Expected: FAIL because the launcher has no upstream mode.

- [ ] **Step 3: Add the upstream mode before the existing local-only mode**

```bash
if [ -n "${HQ_DEV_UPSTREAM:-}" ]; then
  echo "▸ 本地页面 + 测试服务器 API → http://127.0.0.1:$WEB_PORT/workbench/"
  echo "⚠ 所有 API 操作使用真实测试服务器账号、点数和第三方额度"
  exec python3 "$ROOT/scripts/dev_proxy.py" \
    --upstream "$HQ_DEV_UPSTREAM" \
    --site-root "$ROOT/site" \
    --port "$WEB_PORT"
fi
```

Keep the existing local content/static behavior unchanged when `HQ_DEV_UPSTREAM` is empty.

- [ ] **Step 4: Document exact startup and safety behavior**

Add to `docs/本地开发与反漂移工作流.md`:

```markdown
### 本地页面连接测试服务器后端

HQ_DEV_UPSTREAM=http://8.138.143.64 bash scripts/dev_local.sh

- 页面读取本地 `site/`；同源 `/api/*` 转发到测试服务器。
- 登录、点数、任务和资产均为测试服务器真实数据。
- 图片/视频生成会扣真实点数并消耗第三方额度。
- 代理仅监听 `127.0.0.1`，不复制服务器密钥或数据库。
```

- [ ] **Step 5: Run focused tests, static checks, and shell syntax**

Run:

```bash
python -m unittest tests.test_dev_proxy -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
bash -n scripts/dev_local.sh
```

Expected: all commands exit 0.

- [ ] **Step 6: Start the proxy against the configured test server and verify without login or generation**

Run the proxy on `127.0.0.1:8097`, then request:

```text
GET /workbench/inspiration.html -> 200, body comes from local checkout
GET /api/auth/health           -> 200, service=huangque-auth
GET /api/gen/health            -> 200, service=huangque-content
```

Do not automate credential submission and do not call any generation endpoint. The user performs login in the browser; after login, verify `/api/auth/me` shows their real test-server points.

- [ ] **Step 7: Commit launcher and documentation**

```bash
git add scripts/dev_local.sh docs/本地开发与反漂移工作流.md tests/test_dev_proxy.py
git commit -m "docs: add test-backend local workflow"
```

### Task 4: Final branch verification

**Files:** No new files.

**Interfaces:** Verifies all earlier tasks.

- [ ] **Step 1: Review the complete branch diff for secret and database exclusions**

Run:

```bash
git diff origin/main...HEAD --check
git diff origin/main...HEAD --name-status
git status --short --branch
```

Expected: only proxy source, proxy tests, launcher, documentation, design and plan files; no `*.env`, `*.db`, credentials, cookies or generated output.

- [ ] **Step 2: Re-run the focused verification suite**

Run:

```bash
python -m unittest tests.test_dev_proxy -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
bash -n scripts/dev_local.sh
```

Expected: all commands exit 0.

- [ ] **Step 3: Leave the verified local proxy running**

Keep `127.0.0.1:8097` available for the user's browser. Report that login and subsequent generation use real test-server data and quota. Do not push or create a PR until the user requests it.
