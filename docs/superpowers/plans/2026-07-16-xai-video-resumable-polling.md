# xAI Video Resumable Polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make xAI video polling tolerate transient failures, resume the same paid upstream task after service restarts, and recover job 7 without a second charge or POST.

**Architecture:** Keep the paid create POST non-retriable, persist its `request_id` before the first poll, and classify retryable polling failures separately from terminal failures. Reuse the existing job queue by re-queuing recoverable xAI jobs on startup; the video handler detects the persisted provider ID and calls a poll-only `resume()` path.

**Tech Stack:** Python 3.10, `unittest`, `urllib.request`, SQLite, existing content job worker and systemd service.

## Global Constraints

- Never retry xAI create POST requests.
- Retry only polling GET requests for network errors and HTTP 408, 429, 500, 502, 503, 504.
- Use the existing `XAI_VIDEO_TIMEOUT` as the single total deadline.
- Never log API keys, COS credentials, signed media URLs, or prompts.
- Keep `_refund_once` as the only refund path and preserve its idempotency.
- Do not submit a new paid xAI video during verification.
- Recover job 7 with provider ID `6680de26-4069-91bc-b8ec-01b9667a66e9` without changing `refunded=1`.

---

## File Structure

- Modify `server/content_domains/video_xai.py`: transient error classification, retrying poll loop, immediate request-ID persistence, and `resume()`.
- Modify `server/content_domains/video.py`: query persisted xAI state and choose resume instead of create.
- Modify `server/content_domains/core.py`: re-queue recoverable xAI jobs during startup instead of failing/refunding them.
- Modify `tests/test_video_xai.py`: adapter-level retry, terminal error, persistence, deadline, and resume tests.
- Modify `tests/test_xiaole_video.py`: handler-level proof that a persisted provider ID bypasses create.
- Modify `tests/test_job_refund_cas.py`: startup recovery and refund behavior.
- Create `scripts/recover_xai_video_job.py`: guarded one-job compensation tool.
- Create `tests/test_recover_xai_video_job.py`: dry-run/apply safety tests for the compensation tool.

---

### Task 1: Classify and retry transient xAI polling failures

**Files:**
- Modify: `tests/test_video_xai.py`
- Modify: `server/content_domains/video_xai.py`

**Interfaces:**
- Produces: `TransientXaiError(RuntimeError)` with `status_code`.
- Produces: `resume(request_id, model, duration, job_id=None, heartbeat=None, now=None, sleep=None)`.
- Preserves: `generate(model, prompt, duration, aspect_ratio, resolution, image_url=None, job_id=None, heartbeat=None, now=None, sleep=None)` and `edit(model, prompt, video_url, duration, job_id=None, heartbeat=None, now=None, sleep=None)` return dictionaries with `request_id`, `model`, `source_video_url`, and `duration`.

- [ ] **Step 1: Write failing adapter tests**

Add an HTTP-error helper and tests to `tests/test_video_xai.py`:

```python
def _http_error(code, body=b'{}'):
    return urllib.error.HTTPError(
        "https://api.x.ai/v1/videos/rid-1", code, "error", {}, io.BytesIO(body)
    )


def test_poll_retries_503_without_second_create(self):
    opener = Mock()
    opener.open.side_effect = [
        _Response({"request_id": "rid-1"}),
        _http_error(503),
        _Response({"status": "pending"}),
        _Response({"status": "done", "video": {
            "url": "https://vidgen.x.ai/v.mp4", "duration": 5,
        }}),
    ]
    clock = iter([0, 0, 1, 2, 3, 4, 5])
    sleeps = []
    with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
         patch.object(video_xai, "_opener", return_value=opener):
        result = video_xai.generate(
            "grok-imagine-video", "demo", 5, "9:16", "720p",
            now=lambda: next(clock), sleep=sleeps.append,
        )
    self.assertEqual(result["request_id"], "rid-1")
    self.assertEqual(opener.open.call_count, 4)
    self.assertEqual(sleeps[0], 5)
    create_calls = [c for c in opener.open.call_args_list if c.args[0].get_method() == "POST"]
    self.assertEqual(len(create_calls), 1)


def test_resume_polls_existing_id_without_post(self):
    opener = Mock()
    opener.open.return_value = _Response({"status": "done", "video": {
        "url": "https://vidgen.x.ai/resumed.mp4", "duration": 10,
    }})
    clock = iter([0, 0, 1])
    with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
         patch.object(video_xai, "_opener", return_value=opener):
        result = video_xai.resume(
            "rid-existing", "grok-imagine-video", 10,
            now=lambda: next(clock), sleep=lambda _: None,
        )
    self.assertEqual(result["request_id"], "rid-existing")
    req = opener.open.call_args.args[0]
    self.assertEqual(req.get_method(), "GET")
    self.assertTrue(req.full_url.endswith("/videos/rid-existing"))


def test_poll_does_not_retry_terminal_400(self):
    opener = Mock()
    opener.open.side_effect = [_Response({"request_id": "rid-1"}), _http_error(400)]
    with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
         patch.object(video_xai, "_opener", return_value=opener):
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            video_xai.generate("grok-imagine-video", "demo", 5, "9:16", "720p")
    self.assertEqual(opener.open.call_count, 2)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_video_xai.XaiVideoTests.test_poll_retries_503_without_second_create \
  tests.test_video_xai.XaiVideoTests.test_resume_polls_existing_id_without_post \
  tests.test_video_xai.XaiVideoTests.test_poll_does_not_retry_terminal_400 -v
```

Expected: the 503 test raises `RuntimeError`, and the resume test fails because `resume` does not exist.

- [ ] **Step 3: Implement transient classification and poll-only resume**

In `server/content_domains/video_xai.py`, add:

```python
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
TRANSIENT_BACKOFF = (5, 10, 20, 30)


class TransientXaiError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code
```

In `_request_json`, classify transient HTTP failures before terminal mappings:

```python
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code in TRANSIENT_HTTP_CODES:
            raise TransientXaiError(
                "xAI视频临时不可用: HTTP %s %s" % (exc.code, detail),
                status_code=exc.code,
            )
        if exc.code in (401, 403):
            raise RuntimeError("xAI鉴权失败: HTTP %s %s" % (exc.code, detail))
        if exc.code == 402:
            raise RuntimeError("xAI账户余额不足: %s" % detail)
        raise RuntimeError("xAI视频接口失败: HTTP %s %s" % (exc.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientXaiError("xAI视频网络异常: %s" % str(exc)[:300])
```

Replace `_poll` with a single-deadline loop that retries only `TransientXaiError`:

```python
def _poll(opener, request_id, model, duration, job_id=None, heartbeat=None,
          now=None, sleep=None):
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + XAI_VIDEO_TIMEOUT
    last_status = ""
    last_transient = None
    transient_attempt = 0
    while now() < deadline:
        try:
            result = _request_json(
                opener, "GET", "/videos/" + urllib.parse.quote(request_id), timeout=60
            )
            last_transient = None
            transient_attempt = 0
        except TransientXaiError as exc:
            last_transient = exc
            if heartbeat:
                heartbeat(
                    job_id, "xai_retrying", provider_video_id=request_id,
                    model=model, error=str(exc)[:300],
                )
            delay = TRANSIENT_BACKOFF[min(transient_attempt, len(TRANSIENT_BACKOFF) - 1)]
            transient_attempt += 1
            if now() + delay >= deadline:
                break
            sleep(delay)
            continue
        status = str(result.get("status") or "").strip().lower()
        if status != last_status:
            print("[xai-video] request_id=%s model=%s status=%s" %
                  (request_id, model, status), flush=True)
            last_status = status
        if heartbeat:
            heartbeat(
                job_id, "xai_" + (status or "pending"),
                provider_video_id=request_id, model=model, error="",
            )
        if status == "done":
            video = result.get("video") or {}
            url = str(video.get("url") or "").strip() if isinstance(video, dict) else ""
            if not url:
                raise RuntimeError("xAI视频已完成但未返回成片URL")
            return {
                "request_id": request_id,
                "model": str(result.get("model") or model),
                "source_video_url": url,
                "duration": video.get("duration") or duration,
                "respect_moderation": video.get("respect_moderation"),
            }
        if status in {"failed", "expired"}:
            detail = result.get("error") or result.get("message") or status
            raise RuntimeError("xAI视频生成%s: %s" %
                               ("过期" if status == "expired" else "失败", str(detail)[:500]))
        sleep(XAI_VIDEO_POLL_INTERVAL)
    if last_transient:
        raise TimeoutError("xAI视频查询超时: %s" % str(last_transient)[:200])
    raise TimeoutError("xAI视频生成超时")


def resume(request_id, model, duration, job_id=None, heartbeat=None, now=None, sleep=None):
    if not str(request_id or "").strip():
        raise ValueError("恢复xAI视频缺少 request_id")
    return _poll(
        _opener(), str(request_id).strip(), model, duration,
        job_id=job_id, heartbeat=heartbeat, now=now, sleep=sleep,
    )
```

- [ ] **Step 4: Run adapter tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_video_xai -v
```

Expected: all xAI adapter tests pass; existing create-network test still proves POST is attempted once.

- [ ] **Step 5: Commit Task 1**

```bash
git add server/content_domains/video_xai.py tests/test_video_xai.py
git commit -m "fix: retry transient xai video polling failures"
```

---

### Task 2: Persist provider IDs before polling and resume in the video handler

**Files:**
- Modify: `tests/test_video_xai.py`
- Modify: `tests/test_xiaole_video.py`
- Modify: `server/content_domains/video_xai.py`
- Modify: `server/content_domains/video.py`

**Interfaces:**
- Produces: `get_resumable_xai_request(job_id) -> dict | None` in `video.py`.
- Consumes: `video_xai.resume(request_id, model, duration, job_id=None, heartbeat=None, now=None, sleep=None)` from Task 1.
- Preserves: `gen_xiaole_video(payload)` response schema.

- [ ] **Step 1: Write failing persistence and handler-resume tests**

Add to `tests/test_video_xai.py`:

```python
def test_request_id_is_persisted_before_first_poll(self):
    opener = Mock()
    opener.open.side_effect = [
        _Response({"request_id": "rid-early"}),
        _Response({"status": "done", "video": {"url": "https://vidgen.x.ai/v.mp4"}}),
    ]
    heartbeat = Mock()
    clock = iter([0, 0, 1])
    with patch.object(video_xai, "XAI_API_KEY", "test-key"), \
         patch.object(video_xai, "_opener", return_value=opener):
        video_xai.generate(
            "grok-imagine-video", "demo", 5, "9:16", "720p",
            job_id=7, heartbeat=heartbeat,
            now=lambda: next(clock), sleep=lambda _: None,
        )
    first = heartbeat.call_args_list[0]
    self.assertEqual(first.args[:2], (7, "xai_pending"))
    self.assertEqual(first.kwargs["provider_video_id"], "rid-early")
```

Add to `tests/test_xiaole_video.py`, using the file's existing patches for download and cover creation:

```python
def test_existing_xai_provider_id_resumes_without_generate(self):
    resumed = {
        "request_id": "rid-existing", "model": "grok-imagine-video",
        "source_video_url": "https://vidgen.x.ai/existing.mp4", "duration": 10,
    }
    payload = {
        "channel": "grok", "prompt": "demo", "model": "grok-imagine-video",
        "ratio": "9:16", "duration": 10, "resolution": "720p",
        "_job_id": 7, "_username": "qilin",
    }
    with patch("content_domains.video.get_resumable_xai_request", return_value={
             "request_id": "rid-existing", "model": "grok-imagine-video",
         }), \
         patch("content_domains.video_xai.resume", return_value=resumed) as resume, \
         patch("content_domains.video_xai.generate") as generate, \
         patch("content_domains.video._download_xiaole_video", return_value="video/out.mp4"), \
         patch("content_domains.video._extract_first_frame_cover", return_value=None), \
         patch("content_domains.video.update_video_asset_phase"):
        result = video.gen_xiaole_video(payload)
    generate.assert_not_called()
    resume.assert_called_once()
    self.assertEqual(result["provider_video_id"], "rid-existing")
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_video_xai.XaiVideoTests.test_request_id_is_persisted_before_first_poll \
  tests.test_xiaole_video.XiaoleVideoTests.test_existing_xai_provider_id_resumes_without_generate -v
```

Expected: persistence assertion fails and `get_resumable_xai_request` is missing.

- [ ] **Step 3: Persist immediately and add the resumable asset lookup**

In both `generate()` and `edit()` in `video_xai.py`, after validating `request_id` and before `_poll`, add:

```python
    if heartbeat:
        heartbeat(
            job_id, "xai_pending", provider_video_id=request_id,
            model=model, error="",
        )
```

In `server/content_domains/video.py`, add near the other video asset helpers:

```python
def get_resumable_xai_request(job_id):
    if not job_id:
        return None
    with closing(adb()) as c:
        row = c.execute(
            """SELECT provider_video_id, model, phase, status
               FROM video_assets WHERE job_id=?""",
            (job_id,),
        ).fetchone()
    if not row or not row["provider_video_id"]:
        return None
    phase = str(row["phase"] or "")
    if not (phase.startswith("xai_") or phase == "downloading"):
        return None
    return {
        "request_id": row["provider_video_id"],
        "model": row["model"] or "grok-imagine-video",
        "phase": phase,
        "status": row["status"],
    }
```

- [ ] **Step 4: Route existing IDs to `resume()`**

At the start of the `use_xai` branch in `gen_xiaole_video`, select the operation without creating a second task:

```python
        from . import video_xai
        operation = payload.get("operation") or "generate"
        reference_video_file = reference_video_url = None
        existing = get_resumable_xai_request(job_id)
        if existing:
            xres = video_xai.resume(
                existing["request_id"], existing.get("model") or model,
                payload.get("duration") or 10,
                job_id=job_id, heartbeat=update_video_asset_phase,
            )
        elif operation == "edit":
            reference_video_file = _save_data_file(
                payload.get("reference_video_data"), "grok_edit_source", [".mp4"]
            )
            if not reference_video_file:
                raise RuntimeError("参考视频保存失败")
            source_public_url = public_url(reference_video_file, "video/mp4")
            if not str(source_public_url).startswith(("http://", "https://")):
                raise RuntimeError("xAI官方视频编辑需要可公网访问的参考视频，COS转存失败")
            reference_video_url = _file_url(reference_video_file)
            xres = video_xai.edit(
                model="grok-imagine-video", prompt=prompt,
                video_url=source_public_url,
                duration=payload.get("source_duration"), job_id=job_id,
                heartbeat=update_video_asset_phase,
            )
        else:
            image_url = ref_images[0] if ref_images else None
            if image_url and not str(image_url).startswith(("http://", "https://")):
                raise RuntimeError("xAI官方图生视频需要可公网访问的参考图，COS转存失败")
            xres = video_xai.generate(
                model=model, prompt=prompt, image_url=image_url,
                duration=payload.get("duration") or 10,
                aspect_ratio=ratio,
                resolution=payload.get("resolution") or "720p",
                job_id=job_id, heartbeat=update_video_asset_phase,
            )
```

- [ ] **Step 5: Run handler and adapter tests**

Run:

```bash
python3 -m unittest tests.test_video_xai tests.test_xiaole_video -v
```

Expected: all tests pass and the resume test proves `generate` was not called.

- [ ] **Step 6: Commit Task 2**

```bash
git add server/content_domains/video_xai.py server/content_domains/video.py \
  tests/test_video_xai.py tests/test_xiaole_video.py
git commit -m "feat: resume persisted xai video requests"
```

---

### Task 3: Re-queue recoverable xAI jobs after restart

**Files:**
- Modify: `tests/test_job_refund_cas.py`
- Modify: `server/content_domains/core.py`

**Interfaces:**
- Consumes: `video.get_resumable_xai_request(job_id)` from Task 2.
- Produces: `_requeue_running_job(job_id) -> bool`.
- Preserves: current failure/refund behavior for all non-resumable orphaned jobs.

- [ ] **Step 1: Write a failing startup recovery test**

Extend the job fixture so `_insert` accepts `kind`, then add:

```python
def test_reclaim_requeues_resumable_xai_without_refund(self):
    jid = self._insert(300, kind="xiaole_video")

    class _FakeVideo:
        @staticmethod
        def get_resumable_xai_request(job_id):
            return {"request_id": "rid-existing"} if job_id == jid else None

    self.core._domains = lambda: (None, type("P", (), {
        "safe_refund_points": staticmethod(lambda *args: None)
    }), _FakeVideo)
    n = self.core.reclaim_orphaned_running()
    self.assertEqual(n, 1)
    self.assertEqual(self._row(jid)["status"], "pending")
    self.assertEqual(self._row(jid)["refunded"], 0)
    self.assertEqual(self.refunds, [])
```

Update `_insert` exactly as follows:

```python
def _insert(self, cost=20, kind="video"):
    now = int(time.time())
    with closing(self.core.jdb()) as c:
        cur = c.execute(
            "INSERT INTO jobs(kind,username,cost,status,created_at,updated_at) "
            "VALUES(?,?,?,'running',?,?)",
            (kind, "u", cost, now, now),
        )
        c.commit()
        return cur.lastrowid
```

- [ ] **Step 2: Run the startup test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_job_refund_cas.JobRefundCasTests.test_reclaim_requeues_resumable_xai_without_refund -v
```

Expected: job status is `error` and refund count is 1 under the current implementation.

- [ ] **Step 3: Add an atomic running-to-pending transition**

In `server/content_domains/core.py`, add:

```python
def _requeue_running_job(job_id):
    now = int(time.time())
    with closing(jdb()) as c:
        cur = c.execute(
            "UPDATE jobs SET status='pending', error=NULL, updated_at=? "
            "WHERE id=? AND status='running'",
            (now, job_id),
        )
        c.commit()
        return cur.rowcount == 1
```

In `reclaim_orphaned_running()`, before terminal failure handling, add:

```python
        resumable = None
        if r["kind"] == "xiaole_video":
            try:
                resumable = _domains()[2].get_resumable_xai_request(r["id"])
            except Exception:
                resumable = None
        if resumable and _requeue_running_job(r["id"]):
            print("[startup] 恢复xAI视频任务 job=%s request_id=%s" %
                  (r["id"], resumable["request_id"]), flush=True)
            n += 1
            continue
```

Update the startup summary so it distinguishes re-queued jobs from failed/refunded jobs and does not claim every handled job was refunded.

- [ ] **Step 4: Run lifecycle tests**

Run:

```bash
python3 -m unittest \
  tests.test_job_refund_cas \
  tests.test_job_owner_and_parallel \
  tests.test_video_failed_asset_sync \
  tests.test_graceful_drain -v
```

Expected: all tests pass; existing non-xAI orphan tests still end in `error` with one refund each.

- [ ] **Step 5: Commit Task 3**

```bash
git add server/content_domains/core.py tests/test_job_refund_cas.py
git commit -m "fix: resume persisted xai jobs after restart"
```

---

### Task 4: Add a guarded compensation tool for already-refunded jobs

**Files:**
- Create: `scripts/recover_xai_video_job.py`
- Create: `tests/test_recover_xai_video_job.py`

**Interfaces:**
- Produces: `recover_job(job_id, apply=False, job_db=DEFAULT_JOB_DB, asset_db=DEFAULT_ASSET_DB) -> dict`.
- The tool changes only `jobs.status/error/updated_at` and `video_assets.status/phase/error/updated_at`.
- The tool never changes `cost` or `refunded` and never calls an external API.

- [ ] **Step 1: Write failing compensation-tool tests**

Create `tests/test_recover_xai_video_job.py` with this complete temporary-database fixture:

```python
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recover_xai_video_job.py"
spec = importlib.util.spec_from_file_location("recover_xai_video_job", SCRIPT)
recover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recover)


class RecoverXaiVideoJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.job_db = str(root / "jobs.db")
        self.asset_db = str(root / "assets.db")
        with sqlite3.connect(self.job_db) as db:
            db.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, kind TEXT, status TEXT, refunded INTEGER,
                error TEXT, updated_at INTEGER)""")
            db.execute(
                "INSERT INTO jobs VALUES(7,'xiaole_video','error',1,'HTTP 503',0)"
            )
            db.execute(
                "INSERT INTO jobs VALUES(8,'xiaole_video','error',0,'HTTP 503',0)"
            )
        with sqlite3.connect(self.asset_db) as db:
            db.execute("""CREATE TABLE video_assets(
                job_id INTEGER PRIMARY KEY, provider_video_id TEXT, model TEXT,
                status TEXT, phase TEXT, error TEXT, updated_at INTEGER)""")
            db.execute("""INSERT INTO video_assets VALUES(
                7,'rid-7','grok-imagine-video','failed','failed','HTTP 503',0)""")

    def tearDown(self):
        self.tmp.cleanup()

    def job_status(self, job_id):
        with sqlite3.connect(self.job_db) as db:
            return db.execute(
                "SELECT status,refunded FROM jobs WHERE id=?", (job_id,)
            ).fetchone()

    def asset_status(self, job_id):
        with sqlite3.connect(self.asset_db) as db:
            return db.execute(
                "SELECT status,phase FROM video_assets WHERE job_id=?", (job_id,)
            ).fetchone()

    def test_dry_run_does_not_change_databases(self):
        result = recover.recover_job(
            7, apply=False, job_db=self.job_db, asset_db=self.asset_db
        )
        self.assertEqual(result["request_id"], "rid-7")
        self.assertEqual(self.job_status(7), ("error", 1))

    def test_apply_requeues_without_changing_refunded(self):
        recover.recover_job(7, apply=True, job_db=self.job_db, asset_db=self.asset_db)
        self.assertEqual(self.job_status(7), ("pending", 1))
        self.assertEqual(self.asset_status(7), ("running", "xai_pending"))

    def test_rejects_job_without_refund_or_provider_id(self):
        with self.assertRaisesRegex(ValueError, "不满足补偿恢复条件"):
            recover.recover_job(8, apply=True, job_db=self.job_db, asset_db=self.asset_db)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tool tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_recover_xai_video_job -v
```

Expected: import fails because `scripts/recover_xai_video_job.py` does not exist.

- [ ] **Step 3: Implement the guarded database transition**

Create `scripts/recover_xai_video_job.py` with:

```python
#!/usr/bin/env python3
import argparse
import sqlite3
import time

DEFAULT_JOB_DB = "/opt/huangque-test-server/server/content_jobs.db"
DEFAULT_ASSET_DB = "/opt/huangque-test-server/server/audio_assets.db"


def recover_job(job_id, apply=False, job_db=DEFAULT_JOB_DB, asset_db=DEFAULT_ASSET_DB):
    db = sqlite3.connect(job_db, isolation_level=None)
    try:
        db.execute("ATTACH DATABASE ? AS assets", (asset_db,))
        job = db.execute(
            "SELECT id,kind,status,refunded FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        asset = db.execute(
            "SELECT provider_video_id,model FROM assets.video_assets WHERE job_id=?",
            (job_id,),
        ).fetchone()
        valid = (
            job and job[1] == "xiaole_video" and job[2] == "error" and job[3] == 1
            and asset and asset[0]
        )
        if not valid:
            raise ValueError("任务不满足补偿恢复条件")
        result = {"job_id": job_id, "request_id": asset[0], "model": asset[1], "apply": apply}
        if not apply:
            return result
        now = int(time.time())
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            "UPDATE jobs SET status='pending',error=NULL,updated_at=? "
            "WHERE id=? AND status='error' AND refunded=1",
            (now, job_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("任务状态已变化，未执行恢复")
        asset_cur = db.execute(
            "UPDATE assets.video_assets "
            "SET status='running',phase='xai_pending',error=NULL,updated_at=? "
            "WHERE job_id=? AND provider_video_id IS NOT NULL",
            (now, job_id),
        )
        if asset_cur.rowcount != 1:
            raise RuntimeError("视频资产状态已变化，未执行恢复")
        db.execute("COMMIT")
        return result
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="恢复已退点但仍有xAI request_id的视频任务")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(recover_job(args.job_id, apply=args.apply))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tool tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_recover_xai_video_job -v
```

Expected: all three tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add scripts/recover_xai_video_job.py tests/test_recover_xai_video_job.py
git commit -m "ops: add guarded xai video job recovery"
```

---

### Task 5: Full verification, deploy, and recover job 7

**Files:**
- Verify all files changed in Tasks 1-4.
- No additional source files.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: a running test-server deployment and a resumed job 7.

- [ ] **Step 1: Run targeted regression tests**

```bash
python3 -m unittest \
  tests.test_video_xai \
  tests.test_xiaole_video \
  tests.test_job_refund_cas \
  tests.test_job_owner_and_parallel \
  tests.test_video_failed_asset_sync \
  tests.test_graceful_drain \
  tests.test_recover_xai_video_job -v
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Run the complete repository test suite**

```bash
python3 -m unittest discover -s tests -v
```

Expected: zero failures and zero errors. Record the test count in the deployment note.

- [ ] **Step 3: Inspect the final branch diff**

```bash
git status --short
git diff --check origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: clean worktree, no whitespace errors, and only the design, plan, implementation, tests, and recovery script commits.

- [ ] **Step 4: Restart only the content service**

```bash
sudo systemctl restart huangque-test-content.service
systemctl is-active huangque-test-content.service
curl -fsS http://127.0.0.1:8096/api/gen/health
```

Expected: service is `active`; health JSON contains `"ok": true` and `xiaole_video` in `caps`.

- [ ] **Step 5: Dry-run compensation recovery**

```bash
python3 scripts/recover_xai_video_job.py --job-id 7
```

Expected: output contains job 7, the existing request ID, and `'apply': False`; database remains unchanged.

- [ ] **Step 6: Apply compensation recovery once**

```bash
python3 scripts/recover_xai_video_job.py --job-id 7 --apply
```

Expected: output contains `'apply': True`; job 7 becomes `pending`, retains `refunded=1`, and its asset becomes `running/xai_pending`. The existing 30-second pending scanner then queues it.

- [ ] **Step 7: Verify the same provider ID is resumed**

```bash
journalctl -u huangque-test-content.service --since "2 minutes ago" --no-pager
```

Expected: log shows polling for `6680de26-4069-91bc-b8ec-01b9667a66e9`; no second create request is made. Query both SQLite databases read-only to confirm `refunded=1` remains unchanged.

- [ ] **Step 8: Monitor to terminal state without submitting a new task**

Poll job 7 and its `video_assets` row until one of these terminal outcomes:

- `done`: video file and URL are present, asset status is `done`, and `refunded=1` remains unchanged.
- upstream `failed/expired`: job returns to `error`, asset is `failed`, and no additional refund occurs.

Do not click Generate and do not call `/videos/generations` during verification.

- [ ] **Step 9: Push the repair branch**

```bash
git push -u origin codex/xai-video-resume
```

Expected: GitHub branch `codex/xai-video-resume` is updated successfully.
