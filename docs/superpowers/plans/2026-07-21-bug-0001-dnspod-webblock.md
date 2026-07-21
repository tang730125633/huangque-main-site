# BUG-0001 DNSPod Webblock Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic command-line check that detects DNSPod HTTP interception for `huangquechuanmei.com`, verifies the site-owned HTTPS path, and documents the Tencent Cloud filing recovery procedure.

**Architecture:** Keep HTTP transport separate from pure response classification. The checker performs one non-following HTTP request and one following HTTPS request, converts observations into a small result object, and emits JSON plus a process exit code. Unit tests inject observations and fake transport results, so CI never depends on live DNS, Tencent Cloud, or a mainland network.

**Tech Stack:** Python 3 standard library (`argparse`, `dataclasses`, `json`, `urllib`), `unittest`, Markdown.

## Global Constraints

- Target only the Tencent Cloud production server path for `huangquechuanmei.com`; do not reference or migrate to another server.
- Do not modify production Nginx, DNS, services, databases, or cloud resources.
- Do not add third-party Python dependencies.
- Treat GitHub Actions as unit-test coverage only; mainland-network regression remains an explicit operational step.
- Never emit cookies, authorization headers, passwords, or other credentials.

---

## File Map

- `scripts/domain_access_check.py`: command-line transport, response classification, JSON reporting, and exit status.
- `tests/test_domain_access_check.py`: deterministic tests for healthy redirects, DNSPod interception, cross-domain redirects, bad statuses, and transport failures.
- `deploy/生产环境清单与还原手册.md`: Tencent Cloud filing recovery and mainland regression procedure.

### Task 1: Define response classification with failing tests

**Files:**
- Create: `tests/test_domain_access_check.py`
- Create: `scripts/domain_access_check.py`

**Interfaces:**
- Produces: `Observation(status: int, url: str, location: str | None, elapsed_ms: int)`.
- Produces: `CheckResult(ok: bool, code: str, message: str, http: Observation | None, https: Observation | None)`.
- Produces: `classify(domain: str, http: Observation, https: Observation | None) -> CheckResult`.

- [ ] **Step 1: Add tests for the healthy path and DNSPod interception**

Create `tests/test_domain_access_check.py` with imports and the first two tests:

```python
import unittest

from scripts.domain_access_check import CheckResult, Observation, classify


class DomainAccessClassificationTest(unittest.TestCase):
    def test_accepts_site_owned_http_to_https_redirect(self):
        http = Observation(301, "http://huangquechuanmei.com/",
                           "https://huangquechuanmei.com/", 12)
        https = Observation(200,
                            "https://huangquechuanmei.com/workbench/inspiration",
                            None, 25)

        result = classify("huangquechuanmei.com", http, https)

        self.assertEqual(result, CheckResult(True, "OK", "site reachable", http, https))

    def test_detects_dnspod_webblock_redirect(self):
        http = Observation(
            302,
            "http://huangquechuanmei.com/",
            "https://dnspod.qcloud.com/static/webblock.html?d=huangquechuanmei.com",
            10,
        )

        result = classify("huangquechuanmei.com", http, None)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "DNSPOD_WEBBLOCK")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Add only the interface stubs needed for test collection**

Create `scripts/domain_access_check.py`:

```python
#!/usr/bin/env python3
from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    status: int
    url: str
    location: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str
    message: str
    http: Observation | None = None
    https: Observation | None = None


def classify(domain: str, http: Observation, https: Observation | None) -> CheckResult:
    raise NotImplementedError
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_domain_access_check -v`

Expected: both tests run and fail with `NotImplementedError` from `classify`.

- [ ] **Step 4: Implement the minimal classification logic**

Add `from urllib.parse import urlparse` and replace `classify` with:

```python
def _is_webblock(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return (parsed.hostname == "dnspod.qcloud.com"
            and parsed.path == "/static/webblock.html")


def classify(domain: str, http: Observation, https: Observation | None) -> CheckResult:
    if _is_webblock(http.location) or (https and _is_webblock(https.url)):
        return CheckResult(False, "DNSPOD_WEBBLOCK",
                           "request intercepted by DNSPod webblock", http, https)

    redirect = urlparse(http.location or "")
    if http.status not in (301, 302):
        return CheckResult(False, "HTTP_STATUS",
                           f"expected HTTP redirect, got {http.status}", http, https)
    if redirect.scheme != "https" or redirect.hostname != domain:
        return CheckResult(False, "UNEXPECTED_REDIRECT",
                           "HTTP redirect did not target site-owned HTTPS", http, https)
    if https is None:
        return CheckResult(False, "HTTPS_MISSING",
                           "HTTPS observation is missing", http, https)

    final = urlparse(https.url)
    if final.hostname != domain:
        return CheckResult(False, "HTTPS_CROSS_DOMAIN",
                           "HTTPS finished on another domain", http, https)
    if not 200 <= https.status < 300:
        return CheckResult(False, "HTTPS_STATUS",
                           f"expected HTTPS 2xx, got {https.status}", http, https)
    return CheckResult(True, "OK", "site reachable", http, https)
```

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run: `python -m unittest tests.test_domain_access_check -v`

Expected: `Ran 2 tests` and `OK`.

- [ ] **Step 6: Commit the classification slice**

```bash
git add scripts/domain_access_check.py tests/test_domain_access_check.py
git commit -m "test: define domain interception checks"
```

### Task 2: Cover all failure branches and implement CLI transport

**Files:**
- Modify: `tests/test_domain_access_check.py`
- Modify: `scripts/domain_access_check.py`

**Interfaces:**
- Consumes: `Observation`, `CheckResult`, and `classify` from Task 1.
- Produces: `fetch(url: str, timeout: float, follow_redirects: bool) -> Observation`.
- Produces: `run_check(domain: str, timeout: float, fetcher=fetch) -> CheckResult`.
- Produces: `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Add failing classification edge-case tests**

Append these methods to `DomainAccessClassificationTest`:

```python
    def test_rejects_cross_domain_http_redirect(self):
        http = Observation(302, "http://huangquechuanmei.com/",
                           "https://example.com/", 10)
        result = classify("huangquechuanmei.com", http, None)
        self.assertEqual(result.code, "UNEXPECTED_REDIRECT")

    def test_rejects_non_redirecting_http_status(self):
        http = Observation(200, "http://huangquechuanmei.com/", None, 10)
        result = classify("huangquechuanmei.com", http, None)
        self.assertEqual(result.code, "HTTP_STATUS")

    def test_rejects_cross_domain_https_result(self):
        http = Observation(301, "http://huangquechuanmei.com/",
                           "https://huangquechuanmei.com/", 10)
        https = Observation(200, "https://example.com/", None, 20)
        result = classify("huangquechuanmei.com", http, https)
        self.assertEqual(result.code, "HTTPS_CROSS_DOMAIN")

    def test_rejects_non_success_https_status(self):
        http = Observation(301, "http://huangquechuanmei.com/",
                           "https://huangquechuanmei.com/", 10)
        https = Observation(503, "https://huangquechuanmei.com/", None, 20)
        result = classify("huangquechuanmei.com", http, https)
        self.assertEqual(result.code, "HTTPS_STATUS")
```

These tests document the branches already supported by Task 1 and should pass immediately; they do not replace the RED step below.

- [ ] **Step 2: Add failing orchestration tests**

Update the test-file imports to the following, then add:

```python
import unittest

from scripts.domain_access_check import (
    CheckResult,
    Observation,
    classify,
    run_check,
)
```

Add these tests:

```python
class DomainAccessRunTest(unittest.TestCase):
    def test_calls_http_without_following_then_https_with_following(self):
        calls = []

        def fake_fetch(url, timeout, follow_redirects):
            calls.append((url, timeout, follow_redirects))
            if url.startswith("http://"):
                return Observation(301, url, "https://huangquechuanmei.com/", 10)
            return Observation(200, url, None, 20)

        result = run_check("huangquechuanmei.com", 4.0, fake_fetch)

        self.assertTrue(result.ok)
        self.assertEqual(calls, [
            ("http://huangquechuanmei.com/", 4.0, False),
            ("https://huangquechuanmei.com/", 4.0, True),
        ])

    def test_stops_before_https_when_http_is_webblocked(self):
        calls = []

        def fake_fetch(url, timeout, follow_redirects):
            calls.append(url)
            return Observation(302, url,
                "https://dnspod.qcloud.com/static/webblock.html?d=huangquechuanmei.com", 10)

        result = run_check("huangquechuanmei.com", 4.0, fake_fetch)

        self.assertEqual(result.code, "DNSPOD_WEBBLOCK")
        self.assertEqual(calls, ["http://huangquechuanmei.com/"])

    def test_converts_transport_exception_to_result(self):
        def failing_fetch(url, timeout, follow_redirects):
            raise TimeoutError("timed out")

        result = run_check("huangquechuanmei.com", 4.0, failing_fetch)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "NETWORK_ERROR")
        self.assertIn("timed out", result.message)

```

- [ ] **Step 3: Run orchestration tests and verify RED**

Run: `python -m unittest tests.test_domain_access_check.DomainAccessRunTest -v`

Expected: import failure because `run_check` does not exist.

- [ ] **Step 4: Implement transport and orchestration**

Add imports:

```python
import time
import urllib.error
import urllib.request
```

Add the redirect handler and functions:

```python
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, timeout: float, follow_redirects: bool) -> Observation:
    opener = (urllib.request.build_opener()
              if follow_redirects
              else urllib.request.build_opener(_NoRedirect))
    request = urllib.request.Request(url, headers={"User-Agent": "huangque-domain-check/1"})
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return Observation(
        status=response.getcode(),
        url=response.geturl(),
        location=response.headers.get("Location"),
        elapsed_ms=elapsed_ms,
    )


def run_check(domain: str, timeout: float, fetcher=fetch) -> CheckResult:
    try:
        http = fetcher(f"http://{domain}/", timeout, False)
        early = classify(domain, http, None)
        if early.code in {"DNSPOD_WEBBLOCK", "HTTP_STATUS", "UNEXPECTED_REDIRECT"}:
            return early
        https = fetcher(http.location, timeout, True)
        return classify(domain, http, https)
    except Exception as error:
        return CheckResult(False, "NETWORK_ERROR", str(error))


```

- [ ] **Step 5: Run all checker tests and verify GREEN**

Run: `python -m unittest tests.test_domain_access_check -v`

Expected: `Ran 9 tests` and `OK`.

- [ ] **Step 6: Add a failing CLI output and exit-status test**

Add `import io`, `import json`, `from unittest.mock import patch`, and `main` to the test-file imports. Append this method to `DomainAccessRunTest`:

```python
    def test_main_prints_json_and_returns_nonzero_for_failure(self):
        failure = CheckResult(False, "NETWORK_ERROR", "timed out")
        output = io.StringIO()
        with patch("scripts.domain_access_check.run_check", return_value=failure):
            with patch("sys.stdout", output):
                exit_code = main(["--domain", "huangquechuanmei.com"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "NETWORK_ERROR")
```

- [ ] **Step 7: Run the CLI test and verify RED**

Run: `python -m unittest tests.test_domain_access_check.DomainAccessRunTest.test_main_prints_json_and_returns_nonzero_for_failure -v`

Expected: import failure because `main` does not exist.

- [ ] **Step 8: Implement the CLI entry point**

Add `import argparse`, `import json`, and `from dataclasses import asdict` to `scripts/domain_access_check.py`, then append:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check public HTTP/HTTPS domain access")
    parser.add_argument("--domain", default="huangquechuanmei.com")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    result = run_check(args.domain, args.timeout)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 9: Run all checker tests and verify GREEN**

Run: `python -m unittest tests.test_domain_access_check -v`

Expected: `Ran 10 tests` and `OK`.

- [ ] **Step 10: Run the live checker once as diagnostic evidence**

Run: `python scripts/domain_access_check.py --domain huangquechuanmei.com --timeout 10`

Expected in the currently affected environment: JSON with `"ok": false`, `"code": "DNSPOD_WEBBLOCK"`, and process exit code `1`. A different network failure is acceptable evidence only if its JSON code is `NETWORK_ERROR`; it must not be called a passing live check.

- [ ] **Step 11: Commit the executable checker**

```bash
git add scripts/domain_access_check.py tests/test_domain_access_check.py
git commit -m "feat: detect DNSPod domain interception"
```

### Task 3: Document recovery and run complete verification

**Files:**
- Modify: `deploy/生产环境清单与还原手册.md`
- Test: `tests/test_domain_access_check.py`

**Interfaces:**
- Consumes: `python scripts/domain_access_check.py --domain <domain> --timeout <seconds>` from Task 2.
- Produces: an operator procedure for Tencent Cloud filing recovery and 100-run mainland regression.

- [ ] **Step 1: Add the BUG-0001 recovery section to the runbook**

Append this section after the known-issues section:

```markdown
## BUG-0001：DNSPod HTTP 拦截排查与恢复

现象：访问 `http://huangquechuanmei.com/` 时，接入层可能在请求到达 Nginx 前返回 302，并指向 `https://dnspod.qcloud.com/static/webblock.html?d=huangquechuanmei.com`。HTTPS 正常不能证明 HTTP 首访正常。

处理步骤：

1. 登录腾讯云备案控制台，核对 `huangquechuanmei.com` 的备案状态，以及备案是否仍接入当前腾讯云主服务器 `129.204.166.13`。
2. 接入关系失效时，按腾讯云流程办理接入备案或变更接入；不要用修改 Nginx、hosts 或仅推广 HTTPS 代替备案处理。
3. 向腾讯云提交工单，附上复现时间、来源运营商/地区、HTTP 响应状态、`Location` 响应头和完整 webblock URL。
4. 腾讯确认解除拦截后，从无 hosts 覆盖的中国大陆网络运行：

   ```bash
   python3 scripts/domain_access_check.py --domain huangquechuanmei.com --timeout 10
   ```

5. 连续执行 100 次，每次必须返回退出码 0；任何一次 `DNSPOD_WEBBLOCK`、跨域跳转或网络异常都视为回归失败。

注意：仅测试 HEAD、仅测试 HTTPS、使用 hosts 强制解析，或从境外 GitHub Actions 测试，均不能作为该问题已解决的充分证据。
```

- [ ] **Step 2: Run focused and existing related tests**

Run:

```bash
python -m unittest tests.test_domain_access_check -v
python -m unittest tests.test_health_check tests.test_nginx_csp -v
```

Expected: all tests pass with `OK`.

- [ ] **Step 3: Run the repository test suite**

Run: `python -m unittest discover -s tests -v`

Expected: exit code `0`. If unrelated pre-existing tests fail, record the exact failures in the PR and rerun the focused tests to show this change remains green; do not alter unrelated code.

- [ ] **Step 4: Inspect the final diff for scope and secrets**

Run:

```bash
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- scripts/domain_access_check.py tests/test_domain_access_check.py deploy/生产环境清单与还原手册.md docs/superpowers
```

Expected: only the checker, its tests, the runbook, design, and implementation plan appear; no credentials, databases, generated content, new-server references, or production mutations appear.

- [ ] **Step 5: Commit documentation**

```bash
git add deploy/生产环境清单与还原手册.md
git commit -m "docs: add DNSPod webblock recovery runbook"
```

- [ ] **Step 6: Push and create the draft PR**

```bash
git push -u origin codex/bug-0001-dnspod-webblock-monitor
python -c "from pathlib import Path; Path(r'$env:TEMP\bug-0001-pr-body.md').write_text('''## Summary\n\n- detect DNSPod webblock redirects before HTTPS\n- add deterministic unit coverage\n- document Tencent Cloud filing recovery and mainland regression\n\n## Root cause\n\nThe HTTP request is intercepted by the Tencent Cloud/DNSPod access layer before it reaches Nginx. Restoring the domain filing access relationship is the external remediation; this PR adds detection and operational guidance.\n\n## Validation\n\n- `python -m unittest tests.test_domain_access_check -v`\n- `python -m unittest tests.test_health_check tests.test_nginx_csp -v`\n- `python -m unittest discover -s tests -v`\n\nNo production deployment or service restart was performed.\n''', encoding='utf-8')"
gh pr create --draft --base main --head codex/bug-0001-dnspod-webblock-monitor \
  --title "fix: detect DNSPod domain interception" \
  --body-file "$env:TEMP\bug-0001-pr-body.md"
```

The PR body must state the upstream Tencent Cloud filing root cause, list changed files, include focused and full-suite results, record the live diagnostic result without claiming the external block is fixed, and state that nothing was deployed or restarted.
