import base64
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import (
    audio, breakdown, cli_gateway, cli_uploads, core, matrix_template_video,
    submission_idempotency, upstream_guard, video,
    video_minimax_h3, video_openai,
)


class _Points:
    class AuthPointsError(Exception):
        pass

    def __init__(self):
        self.deductions = []

    def cost_of(self, kind, payload):
        return 24

    def get_points(self, username):
        return 100

    def deduct_points(self, *_args, **_kwargs):
        return 76

    def refund_points(self, *_args, **_kwargs):
        return None


class _DispatchNothing:
    class RevisionConflict(Exception):
        pass

    def dispatch_http(self, *args, **kwargs):
        return False

    @property
    def short_drama_production(self):
        return self

    def fail_linked_character_reference_job(self, *args, **kwargs):
        return None

    def fail_linked_job(self, *args, **kwargs):
        return None

    def _http_error(self, handler, error, **kwargs):
        handler._send(400, {"detail": str(error)})


class HQCLIContentTests(unittest.TestCase):
    def setUp(self):
        self.points = _Points()
        self.originals = {
            "internal": core.AUTH_INTERNAL_TOKEN,
            "verify": core.verify,
            "domains": core._domains,
            "short_drama": core._short_drama_domain,
            "digital_ip": core._digital_ip_domain,
            "require_enabled": core.feature_flags.require_enabled,
            "security": core.miniprogram_security.check_payload,
            "shutting_down": core.is_shutting_down,
            "upstream": upstream_guard.exhausted_reason,
            "handlers": core.HANDLERS,
        }
        core.AUTH_INTERNAL_TOKEN = "test-cli-secret"
        core.verify = lambda token: {"username": "alice", "must_change": False}
        core._domains = lambda: (audio, self.points, object())
        core._short_drama_domain = lambda: _DispatchNothing()
        core._digital_ip_domain = lambda: _DispatchNothing()
        core.feature_flags.require_enabled = lambda kind: None
        core.miniprogram_security.check_payload = lambda payload: None
        core.is_shutting_down = lambda: False
        upstream_guard.exhausted_reason = lambda kind, payload: None
        core.HANDLERS = {"image": lambda payload: payload}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        core.AUTH_INTERNAL_TOKEN = self.originals["internal"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["domains"]
        core._short_drama_domain = self.originals["short_drama"]
        core._digital_ip_domain = self.originals["digital_ip"]
        core.feature_flags.require_enabled = self.originals["require_enabled"]
        core.miniprogram_security.check_payload = self.originals["security"]
        core.is_shutting_down = self.originals["shutting_down"]
        upstream_guard.exhausted_reason = self.originals["upstream"]
        core.HANDLERS = self.originals["handlers"]

    def _post(self, path, payload, internal=True, expected=None, idempotency_key=""):
        headers = {"Authorization": "Bearer bridge-token", "Content-Type": "application/json"}
        if internal:
            headers["X-HQ-Internal-Token"] = "test-cli-secret"
        if expected is not None:
            headers["X-HQ-Expected-Cost"] = str(expected)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.getcode(), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_server_quote_and_expected_cost_gate_precede_any_deduction(self):
        generation = {"prompt": "a yellow bird", "provider": "openai", "ratio": "1:1", "quality": "hd", "count": 1}
        status, quote = self._post("/api/gen/cli/quote", {"kind": "image", "payload": generation})
        self.assertEqual((200, 24, 100), (status, quote["cost"], quote["points"]))
        self.assertEqual(403, self._post("/api/gen/cli/quote", {"kind": "image", "payload": generation}, internal=False)[0])
        status, result = self._post("/api/gen/image", generation, expected=25)
        self.assertEqual(409, status)
        self.assertEqual("quote_cost_changed", result["code"])
        self.assertEqual([], self.points.deductions)

    def test_breakdown_quote_rejects_canonical_duplicates_before_pricing(self):
        query_variants = {
            "kind": "breakdown",
            "payload": {"urls": [
                "https://www.douyin.com/video/1234567890123456789",
                "https://www.douyin.com/video/1234567890123456789?share=1#reply",
            ]},
        }
        short_and_direct = {
            "kind": "breakdown",
            "payload": {"urls": [
                "https://v.douyin.com/AbCdEf/",
                "https://www.douyin.com/video/1234567890123456789",
            ]},
        }
        with mock.patch.object(
            self.points, "cost_of", wraps=self.points.cost_of,
        ) as cost_of, mock.patch.object(
            breakdown,
            "_resolved_link",
            side_effect=lambda url: {
                "url": url,
                "platform": "douyin",
                "id": "1234567890123456789",
                "note_type": "video",
            },
        ):
            for request in (query_variants, short_and_direct):
                with self.subTest(urls=request["payload"]["urls"]):
                    status, result = self._post("/api/gen/cli/quote", request)
                    self.assertEqual(400, status)
                    self.assertIn("同一作品", result["detail"])
        cost_of.assert_not_called()

    def test_matrix_replay_and_conflict_precede_remote_preflight(self):
        body = {
            "top_text": "有效标题", "bottom_text": "有效行动文案",
            "template_id": "native-bold", "bgm": True,
        }
        key = "matrix-response-loss-001"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")), \
             mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
             mock.patch.object(core, "HANDLERS", {"matrix_template_video": lambda payload: payload}), \
             mock.patch.object(
                 matrix_template_video, "validate_payload",
                 side_effect=AssertionError("replay must precede preflight"),
             ):
            state, _ = submission_idempotency.begin(
                core.jdb, "alice", "/api/gen/matrix-template", key, body)
            self.assertEqual("new", state)
            submission_idempotency.complete(
                core.jdb, "alice", "/api/gen/matrix-template", key,
                {"job_id": 92, "cost": 5, "points_left": 95},
            )
            replay_status, replay = self._post(
                "/api/gen/matrix-template", body, expected=5,
                idempotency_key=key,
            )
            changed_status, changed = self._post(
                "/api/gen/matrix-template", dict(body, bottom_text="不同文案"),
                expected=5, idempotency_key=key,
            )
        self.assertEqual((200, 92), (replay_status, replay["job_id"]))
        self.assertEqual((409, "idempotency_conflict"), (
            changed_status, changed["code"]))

    def test_internal_submission_reconcile_is_read_only_and_fail_closed(self):
        body = {
            "top_text": "有效标题", "bottom_text": "关注查看更多",
            "template_id": "native-bold", "bgm": True,
        }
        key = "matrix-reconcile-read-only-001"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")):
            submission_idempotency.begin(
                core.jdb, "alice", "/api/gen/matrix-template", key, body,
            )
            submission_idempotency.complete(
                core.jdb, "alice", "/api/gen/matrix-template", key,
                {"job_id": 93, "cost": 5, "accepted": True},
            )
            with closing(core.jdb()) as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS jobs(
                    id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,
                    status TEXT,payload TEXT,created_at INTEGER,updated_at INTEGER,
                    owner TEXT,refunded INTEGER DEFAULT 0
                )""")
                connection.execute(
                    "INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at,owner) "
                    "VALUES(93,'matrix_template_video','alice',5,'running','{}',1,1,'content')"
                )
                connection.commit()
            replay_status, replay = self._post(
                "/api/gen/internal/submission-reconcile", {
                    "endpoint": "/api/gen/matrix-template",
                    "idempotency_key": key, "input": body,
                },
            )
            missing_status, missing = self._post(
                "/api/gen/internal/submission-reconcile", {
                    "endpoint": "/api/gen/matrix-template",
                    "idempotency_key": "matrix-reconcile-missing-001",
                    "input": body,
                },
            )
            conflict_status, conflict = self._post(
                "/api/gen/internal/submission-reconcile", {
                    "endpoint": "/api/gen/matrix-template",
                    "idempotency_key": key,
                    "input": dict(body, bottom_text="不同文案"),
                },
            )
            denied_status, _ = self._post(
                "/api/gen/internal/submission-reconcile", {
                    "endpoint": "/api/gen/matrix-template",
                    "idempotency_key": key, "input": body,
                }, internal=False,
            )
        self.assertEqual((replay_status, replay["job_id"]), (200, 93))
        self.assertTrue(replay["reconciled"])
        self.assertEqual((missing_status, missing["code"]), (404, "idempotency_not_found"))
        self.assertEqual((conflict_status, conflict["code"]), (409, "idempotency_conflict"))
        self.assertEqual(denied_status, 403)
        self.assertEqual(self.points.deductions, [])

    def test_submission_reconcile_health_requires_internal_token(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")):
            self.assertEqual(
                self._post("/api/gen/internal/submission-reconcile/health", {})[0], 200,
            )
            self.assertEqual(
                self._post(
                    "/api/gen/internal/submission-reconcile/health", {}, internal=False,
                )[0], 403,
            )

    def test_stale_processing_without_attempt_is_confirmed_uncharged(self):
        body = {
            "top_text": "有效标题", "bottom_text": "关注查看更多",
            "template_id": "native-bold", "bgm": True,
        }
        key = "matrix-before-deduct-crash-001"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")):
            submission_idempotency.begin(
                core.jdb, "alice", "/api/gen/matrix-template", key, body,
            )
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE submission_idempotency SET created_at=0,updated_at=0 "
                    "WHERE username='alice' AND endpoint='/api/gen/matrix-template' "
                    "AND idem_key=?",
                    (key,),
                )
                connection.commit()
            status, result = self._post(
                "/api/gen/internal/submission-reconcile", {
                    "endpoint": "/api/gen/matrix-template",
                    "idempotency_key": key, "input": body,
                },
            )
            state, _ = submission_idempotency.replay_existing(
                core.jdb, "alice", "/api/gen/matrix-template", key, [body],
            )
        self.assertEqual((status, result["code"]), (404, "idempotency_not_found"))
        self.assertEqual(state, "missing")
        self.assertEqual(self.points.deductions, [])

    def test_matrix_new_request_preflight_unavailable_is_structured_and_free(self):
        body = {
            "top_text": "有效标题", "bottom_text": "有效行动文案",
            "template_id": "native-bold", "bgm": True,
        }
        key = "matrix-preflight-down-001"
        cost = mock.Mock(return_value=5)
        self.points.cost_of = cost
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")), \
             mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
             mock.patch.object(core, "HANDLERS", {"matrix_template_video": lambda payload: payload}), \
             mock.patch.object(
                 matrix_template_video, "validate_payload",
                 side_effect=core.feature_flags.FeatureDisabled("模板成片服务暂不可用"),
             ), mock.patch.object(core.jobs_store, "create_paid_job") as create:
            status, result = self._post(
                "/api/gen/matrix-template", body, expected=5,
                idempotency_key=key,
            )
            state, _ = submission_idempotency.begin(
                core.jdb, "alice", "/api/gen/matrix-template", key, body)
        self.assertEqual((503, "feature_disabled"), (status, result["code"]))
        self.assertEqual("new", state)
        self.assertEqual([], self.points.deductions)
        cost.assert_not_called()
        create.assert_not_called()

    def test_matrix_normal_submit_uses_durable_attempt_and_replays_once(self):
        body = {
            "top_text": "有效标题", "bottom_text": "关注查看更多",
            "template_id": "native-bold", "bgm": True,
        }
        key = "matrix-durable-normal-001"
        self.points.cost_of = mock.Mock(return_value=5)
        self.points.get_points_transaction = mock.Mock(return_value=None)
        self.points.deduct_points = mock.Mock(return_value=95)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")), \
             mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
             mock.patch.object(core, "HANDLERS", {"matrix_template_video": lambda payload: payload}), \
             mock.patch.object(matrix_template_video, "validate_payload", return_value=body), \
             mock.patch.object(core, "enqueue_job", return_value=True):
            with closing(core.jdb()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                    status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                    created_at INTEGER,updated_at INTEGER,owner TEXT,refunded INTEGER DEFAULT 0
                    ,deleted INTEGER DEFAULT 0
                )""")
                submission_idempotency.ensure_table(connection)
                core.matrix_template_submission.ensure_table(connection)
                connection.commit()
            first_status, first = self._post(
                "/api/gen/matrix-template", body, expected=5, idempotency_key=key,
            )
            second_status, second = self._post(
                "/api/gen/matrix-template", body, expected=5, idempotency_key=key,
            )
            attempt = core.matrix_template_submission.get(
                core.jdb, "alice", "/api/gen/matrix-template", key,
            )
            with closing(core.jdb()) as connection:
                job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(attempt["state"], "linked")
        self.assertEqual(attempt["job_id"], first["job_id"])
        self.assertEqual(job_count, 1)
        self.points.deduct_points.assert_called_once()

    def test_matrix_core_freezes_semantics_and_worker_reuses_after_cache_clear(self):
        top = "我在广州，组了一个健康赛道创业者的圈子"
        bottom = "评论区扣888"
        raw = {
            "top_text": top, "bottom_text": bottom,
            "template_id": "ref-04-fixture-04", "bgm": False,
        }
        semantic = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": matrix_template_video.matrix_template_semantics._source_sha256(
                top, bottom,
            ),
            "top1_end": top.index("，"),
            "top_break_after": [top.index("，")],
            "bottom_break_after": [],
        }
        execution = dict(raw, duration=11.0, semantic_layout=semantic)
        key = "matrix-frozen-semantic-001"
        self.points.cost_of = mock.Mock(return_value=5)
        self.points.get_points_transaction = mock.Mock(return_value=None)
        self.points.deduct_points = mock.Mock(return_value=95)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
             mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")), \
             mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
             mock.patch.object(core, "HANDLERS", {"matrix_template_video": lambda payload: payload}), \
             mock.patch.object(core, "enqueue_job", return_value=True):
            with closing(core.jdb()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                    status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                    created_at INTEGER,updated_at INTEGER,owner TEXT,refunded INTEGER DEFAULT 0,
                    deleted INTEGER DEFAULT 0
                )""")
                submission_idempotency.ensure_table(connection)
                core.matrix_template_submission.ensure_table(connection)
                connection.commit()

            with mock.patch.object(
                matrix_template_video, "validate_payload", return_value=execution,
            ):
                status, accepted = self._post(
                    "/api/gen/matrix-template", raw, expected=5,
                    idempotency_key=key,
                )

            attempt = core.matrix_template_submission.get(
                core.jdb, "alice", "/api/gen/matrix-template", key,
            )
            with closing(core.jdb()) as connection:
                job_payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=?", (accepted["job_id"],),
                ).fetchone()[0])

            self.assertEqual(200, status)
            self.assertEqual(raw, attempt["input"])
            self.assertEqual(execution, attempt["execution"])
            self.assertEqual(execution, job_payload)

            contract = {
                "version": 1, "max_width_px": 996,
                "layers": {
                    layer: {
                        "font_size_px": values[0],
                        "font_weight": values[1],
                        "max_width_px": values[2],
                        "max_lines": values[3],
                    }
                    for layer, values in matrix_template_video._SEMANTIC_CONTRACTS[
                        "v04"
                    ].items()
                },
            }
            template = {
                "id": raw["template_id"], "name": "fixture",
                "engine": "hyperframes", "variant": "v04",
                "font_mode": "template_locked", "font_selectable": False,
                "semantic_layout": contract,
            }
            remote_id = "f" * 32
            remote_calls = []

            def remote_request(method, path, body=None, **kwargs):
                remote_calls.append((method, path, body, kwargs))
                if (method, path) == ("POST", "/v1/preflight"):
                    return {"payload": dict(body, duration=11.0)}
                if (method, path) == ("POST", "/v1/jobs"):
                    return {"job_id": remote_id, "status": "pending"}
                if (method, path) == ("GET", "/v1/jobs/" + remote_id):
                    return {"status": "completed", "result": {
                        "file_url": "/v1/files/" + remote_id + ".mp4",
                        "duration": 11.0, "width": 1080, "height": 1920,
                        "template_id": raw["template_id"],
                        "material_manifest": [],
                    }}
                raise AssertionError((method, path))

            matrix_template_video.matrix_template_semantics._CACHE.clear()
            with mock.patch.object(
                     core, "HANDLERS",
                     {"matrix_template_video": matrix_template_video.generate},
                 ), mock.patch.object(
                     core, "_start_job_heartbeat", return_value=lambda: None,
                 ), mock.patch.object(core.assets_store, "record_asset"), \
                 mock.patch.object(video, "record_video_asset"), \
                 mock.patch.object(matrix_template_video, "require_available"), \
                 mock.patch.object(
                     matrix_template_video, "public_templates", return_value=[template],
                 ), mock.patch.object(
                     matrix_template_video.matrix_template_semantics, "resolve",
                     side_effect=AssertionError("worker must not call AI again"),
                 ) as resolve, mock.patch.object(
                     matrix_template_video, "_request", side_effect=remote_request,
                 ), mock.patch.object(
                     matrix_template_video, "_persist_runtime", return_value=True,
                 ), mock.patch.object(
                     matrix_template_video, "_download",
                     return_value=("video/frozen-semantic.mp4", 4096),
                 ), mock.patch.object(
                     matrix_template_video, "public_url",
                     return_value="/api/gen/file/frozen-semantic",
                 ), mock.patch.object(matrix_template_video.time, "sleep"):
                core.run_job(accepted["job_id"])

            with closing(core.jdb()) as connection:
                completed = connection.execute(
                    "SELECT status,result FROM jobs WHERE id=?",
                    (accepted["job_id"],),
                ).fetchone()
            result = json.loads(completed["result"])

            resolve.assert_not_called()
            self.assertEqual("done", completed["status"])
            self.assertEqual("video/frozen-semantic.mp4", result["video_file"])
            self.assertEqual(
                ["/v1/preflight", "/v1/jobs", "/v1/jobs/" + remote_id],
                [item[1] for item in remote_calls],
            )
            self.assertTrue(all(
                call[2].get("semantic_layout") == semantic
                for call in remote_calls[:2]
            ))

    def test_matrix_queue_full_returns_queryable_refund_state(self):
        body = {
            "top_text": "有效标题", "bottom_text": "关注查看更多",
            "template_id": "native-bold", "bgm": True,
        }
        for pending in (False, True):
            with self.subTest(pending=pending), \
                 tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, \
                 mock.patch.object(core, "JOB_DB", str(Path(folder) / "jobs.db")), \
                 mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                 mock.patch.object(core, "HANDLERS", {"matrix_template_video": lambda payload: payload}), \
                 mock.patch.object(matrix_template_video, "validate_payload", return_value=body), \
                 mock.patch.object(core, "enqueue_job", return_value=False):
                self.points.cost_of = mock.Mock(return_value=5)
                self.points.get_points_transaction = mock.Mock(return_value=None)
                self.points.deduct_points = mock.Mock(return_value=95)
                self.points.refund_points = mock.Mock(
                    side_effect=RuntimeError("auth unavailable") if pending else None
                )
                with closing(core.jdb()) as connection:
                    connection.execute("""CREATE TABLE jobs(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                        created_at INTEGER,updated_at INTEGER,owner TEXT,refunded INTEGER DEFAULT 0,
                        deleted INTEGER DEFAULT 0
                    )""")
                    submission_idempotency.ensure_table(connection)
                    core.matrix_template_submission.ensure_table(connection)
                    connection.commit()
                key = "matrix-queue-full-%s" % ("pending" if pending else "refunded")
                status, result = self._post(
                    "/api/gen/matrix-template", body, expected=5,
                    idempotency_key=key,
                )
                replay_status, replay = self._post(
                    "/api/gen/matrix-template", body, expected=5,
                    idempotency_key=key,
                )
                with closing(core.jdb()) as connection:
                    row = connection.execute(
                        "SELECT status,refunded FROM jobs WHERE id=?", (result["job_id"],)
                    ).fetchone()
            self.assertEqual((202, 202), (status, replay_status))
            self.assertEqual(result["job_id"], replay["job_id"])
            expected = "pending" if pending else "refunded"
            self.assertEqual(expected, result["refund_state"])
            self.assertEqual(("error", 2 if pending else 1), tuple(row))

    def test_audio_validation_rejects_bad_knobs_before_generation(self):
        with self.assertRaisesRegex(ValueError, "pitch"):
            audio.validate_audio_payload({"text": "hello", "pitch": 99})
        clean = audio.validate_audio_payload({"text": " hello ", "speed": 1.2, "pitch": 0, "volume": 0})
        self.assertEqual(("hello", 1.2), (clean["text"], clean["speed"]))
        original = audio.resolve_audio_provider_voice
        audio.resolve_audio_provider_voice = lambda username, voice_key: voice_key
        try:
            public = audio.validate_audio_payload(
                {"text": "hello", "voice": "S_d21F8OR62", "voice_scope": "personal", "provider": "openai"},
                "qa",
            )
            personal = audio.validate_audio_payload(
                {"text": "hello", "voice": "vip_slot_1", "voice_scope": "public", "provider": "openai"},
                "qa",
            )
        finally:
            audio.resolve_audio_provider_voice = original
        self.assertEqual((public["voice_scope"], public["provider"]), ("public", "cosyvoice"))
        self.assertEqual((personal["voice_scope"], personal["provider"]), ("personal", "cosyvoice"))

    def test_voice_clone_expands_private_upload_and_replays_same_request(self):
        voice = {"voice_key": "vip_slot_12345678", "status": "training"}
        replay_response = {"ok": True, "voice": dict(voice, replayed=True)}
        with mock.patch.object(
            cli_uploads, "expand_voice_clone_payload",
            return_value={"slot_id": "slot_12345678", "name": "我的声音",
                          "audio": "data:audio/wav;base64,AA==", "audio_format": "wav"},
        ) as expand, mock.patch.object(
            audio, "validate_clone_vip_payload", side_effect=lambda _user, value: value,
        ), mock.patch.object(
            submission_idempotency, "begin",
            side_effect=[("new", None), ("replay", replay_response), ("conflict", None)],
        ), mock.patch.object(
            submission_idempotency, "complete",
        ) as complete, mock.patch.object(
            audio, "clone_request_replay", return_value=None,
        ), mock.patch.object(
            audio, "mark_clone_training", return_value=voice,
        ) as mark, mock.patch.object(audio, "clone_vip_voice_background") as background:
            first = self._post("/api/gen/cli/voice-clone", {
                "slot_id": "slot_12345678", "name": "我的声音",
                "audio_upload_id": "aud_" + "a" * 32,
            }, idempotency_key="clone-request-0001")
            second = self._post("/api/gen/cli/voice-clone", {
                "slot_id": "slot_12345678", "name": "我的声音",
                "audio_upload_id": "aud_" + "a" * 32,
            }, idempotency_key="clone-request-0001")
            conflict = self._post("/api/gen/cli/voice-clone", {
                "slot_id": "slot_87654321", "name": "另一个声音",
                "audio_upload_id": "aud_" + "b" * 32,
            }, idempotency_key="clone-request-0001")
        self.assertEqual((200, 200, 409), (first[0], second[0], conflict[0]))
        self.assertEqual("idempotency_conflict", conflict[1]["code"])
        self.assertEqual("training", second[1]["voice"]["status"])
        complete.assert_called_once()
        expand.assert_called_once()
        mark_args = mark.call_args.args
        self.assertEqual(
            ("alice", "slot_12345678", "我的声音", "clone-request-0001"),
            mark_args[:4],
        )
        self.assertRegex(mark_args[4], r"^[0-9a-f]{64}$")
        background.assert_called_once_with("alice", {
            "slot_id": "slot_12345678", "name": "我的声音",
            "audio": "data:audio/wav;base64,AA==", "audio_format": "wav",
            "_request_id": "clone-request-0001",
        })

    def test_voice_clone_rejects_auth_feature_and_idempotency_guards_before_upload(self):
        payload = {
            "slot_id": "slot_12345678", "name": "我的声音",
            "audio_upload_id": "aud_" + "a" * 32,
        }
        self.assertEqual(403, self._post(
            "/api/gen/cli/voice-clone", payload, internal=False,
            idempotency_key="clone-request-0001",
        )[0])
        self.assertEqual(400, self._post("/api/gen/cli/voice-clone", payload)[0])
        with mock.patch.object(core, "verify", return_value=None):
            self.assertEqual(401, self._post(
                "/api/gen/cli/voice-clone", payload,
                idempotency_key="clone-request-0001",
            )[0])
        with mock.patch.object(
            core, "verify", return_value={"username": "alice", "must_change": True},
        ):
            self.assertEqual(403, self._post(
                "/api/gen/cli/voice-clone", payload,
                idempotency_key="clone-request-0001",
            )[0])
        with mock.patch.object(
            core.feature_flags, "require_enabled",
            side_effect=core.feature_flags.FeatureDisabled("维护中"),
        ), mock.patch.object(
            cli_uploads, "expand_voice_clone_payload",
            side_effect=AssertionError("disabled feature must fail before upload lookup"),
        ):
            status, result = self._post(
                "/api/gen/cli/voice-clone", payload,
                idempotency_key="clone-request-0001",
            )
        self.assertEqual((503, "feature_disabled"), (status, result["code"]))

    def test_video_lipsync_uses_owned_assets_and_real_duration_pricing(self):
        with mock.patch.object(video, "get_video_asset", return_value={
                "id": 21, "video_file": "video/source.mp4",
                "ratio": "9:16", "resolution": "768p", "status": "done",
        }), mock.patch.object(video, "get_audio_asset", return_value={
                "id": 34, "file": "audio/speech.mp3",
        }), mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: Path("/") / value), \
                mock.patch.object(video, "_user_owns_output_file", return_value=True), \
                mock.patch.object(video, "_normalize_audio_file_ref", return_value="audio/speech.mp3"), \
                mock.patch.object(video, "_probe_video_duration", return_value=15.1), \
                mock.patch.object(video.pricing, "get_price", side_effect=lambda key: {
                    "video.lipsync.speed": 3, "video.lipsync.precision": 6,
                }[key]):
            clean = video.validate_video_payload({
                "mode": "lipsync", "video_asset_id": 21,
                "audio_asset_id": 34, "lipsync_mode": "speed",
                "dynamic_duration": False,
            }, "alice")
            self.assertEqual(("video/source.mp4", "audio/speech.mp3"), (
                clean["reference_video_file"], clean["audio_file"]))
            self.assertEqual(46, video.video_cost(clean))

    def test_video_lipsync_quote_does_not_require_a_photo_avatar(self):
        clean = {
            "mode": "lipsync", "video_asset_id": 21,
            "audio_asset_id": 34, "reference_video_file": "video/source.mp4",
            "audio_file": "audio/speech.mp3", "_lipsync_duration": 15.0,
            "lipsync_mode": "speed", "dynamic_duration": False,
        }
        with mock.patch.object(
                core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(
                    video, "validate_video_payload", return_value=clean) as validate:
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "video", "payload": {
                    "mode": "lipsync", "video_asset_id": 21,
                    "audio_asset_id": 34, "lipsync_mode": "speed",
                    "dynamic_duration": False,
                },
            })
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once()

    def test_heygen_lipsync_preserves_source_and_uses_idempotency(self):
        captured = {}

        def request(method, path, body=None, headers=None, **_kwargs):
            if method == "POST":
                captured.update(
                    path=path, body=json.loads(body), headers=dict(headers or {}))
                return {"data": {"lipsync_id": "ls_123"}}
            self.assertEqual(("GET", "/lipsyncs/ls_123"), (method, path))
            return {"data": {
                "status": "completed", "duration": 15,
                "video_url": "https://files.heygen.ai/result.mp4",
            }}

        with mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: Path("/") / value), \
                mock.patch.object(video, "_heygen_mcp_enabled", return_value=False), \
                mock.patch.object(video, "_heygen_upload_asset", side_effect=["vid_asset", "aud_asset"]), \
                mock.patch.object(video, "_heygen_request_json", side_effect=request), \
                mock.patch.object(video, "_download_video_file_direct", return_value="video/lipsync.mp4"), \
                mock.patch.object(video, "_extract_first_frame_cover", return_value="video/lipsync.jpg"), \
                mock.patch.object(video, "update_video_asset_phase"):
            result = video.generate_heygen_lipsync(
                "video/source.mp4", "audio/speech.mp3", "speed", False,
                job_id=77,
            )
        self.assertEqual("/lipsyncs", captured["path"])
        self.assertEqual("huangque-lipsync-job-77", captured["headers"]["Idempotency-Key"])
        self.assertEqual({"type": "asset_id", "asset_id": "vid_asset"}, captured["body"]["video"])
        self.assertFalse(captured["body"]["enable_dynamic_duration"])
        self.assertTrue(captured["body"]["keep_the_same_format"])
        self.assertEqual(("ls_123", "video/lipsync.mp4"), (
            result["video_id"], result["video_file"]))

    def test_heygen_lipsync_prefers_configured_mcp_subscription(self):
        calls = []

        def mcp(tool, arguments, **_kwargs):
            calls.append((tool, arguments))
            if tool == "create_lipsync":
                return {"lipsync_id": "ls_mcp"}
            return {
                "status": "completed", "duration": 15,
                "video_url": "https://files.heygen.ai/mcp-result.mp4",
            }

        with mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: Path("/") / value), \
                mock.patch.object(video, "_heygen_mcp_enabled", return_value=True), \
                mock.patch.object(video, "_mux_seedance_upscale_audio", return_value="video/detection.mp4"), \
                mock.patch.object(video, "public_url", side_effect=lambda value, *_args, **_kwargs: "https://media.test/" + value), \
                mock.patch.object(video, "_heygen_mcp_call", side_effect=mcp), \
                mock.patch.object(video, "_heygen_upload_asset", side_effect=AssertionError("MCP must use signed URLs")), \
                mock.patch.object(video, "_download_video_file_direct", return_value="video/lipsync-mcp.mp4"), \
                mock.patch.object(video, "_extract_first_frame_cover", return_value="video/lipsync-mcp.jpg"), \
                mock.patch.object(video, "update_video_asset_phase"):
            result = video.generate_heygen_lipsync(
                "video/source.mp4", "audio/speech.mp3", "speed", False,
                job_id=78,
            )
        self.assertEqual(["create_lipsync", "get_lipsync"], [item[0] for item in calls])
        create = calls[0][1]
        self.assertEqual("https://media.test/video/detection.mp4", create["video"]["url"])
        self.assertEqual("https://media.test/audio/speech.mp3", create["audio"]["url"])
        self.assertFalse(create["enableDynamicDuration"])
        self.assertEqual("video/lipsync-mcp.mp4", result["video_file"])

    def test_sora_submit_expands_cli_reference_before_validation_and_queue(self):
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
        )
        captured = {}

        def create_job(*args, **_kwargs):
            captured.update(args[6])
            return 42, 76

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "HANDLERS", {"sora_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "SORA_VIDEO_ENABLED", True), \
                mock.patch.object(video_openai, "available", return_value=True), \
                mock.patch.object(core, "_idempotency_begin", return_value=("new", None)), \
                mock.patch.object(core, "_idempotency_complete"), \
                mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                mock.patch.object(core, "_user_active_job_count", return_value=0), \
                mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                mock.patch.object(video, "record_video_pending_asset"), \
                mock.patch.object(core, "enqueue_job", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post(
                "/api/gen/sora_video", {
                    "prompt": "让 @图片1 缓慢旋转", "channel": "sora", "model": "sora-2",
                    "seconds": 4, "ratio": "9:16", "resolution": "720p",
                    "reference_upload_ids": [uploaded["upload_id"]],
                }, expected=24, idempotency_key="sora-ref-test-001",
            )
        self.assertEqual((200, 42), (status, result["job_id"]))
        self.assertNotIn("reference_upload_ids", captured)
        self.assertEqual(1, len(captured["reference_images"]))
        self.assertTrue(captured["reference_images"][0].startswith("data:image/png;base64,"))

    def test_minimax_quote_expands_private_reference_as_typed_data_uri(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (40, 80, 120)).save(image, "PNG")
        raw = image.getvalue()
        captured = {}
        original_validate = video.validate_xiaole_video_payload

        def validate(payload, username=None):
            cleaned = original_validate(payload, username)
            captured.update(cleaned)
            return cleaned

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True), \
                mock.patch.object(video, "validate_xiaole_video_payload", side_effect=validate):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "让 @图片1 向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                },
            })

        self.assertEqual(200, status, result)
        self.assertEqual((24, 100), (result["cost"], result["points"]))
        self.assertNotIn("reference_upload_ids", captured)
        self.assertEqual(1, len(captured["reference_images"]))
        self.assertTrue(captured["reference_images"][0].startswith("data:image/png;base64,"))

    def test_minimax_quote_accepts_verified_text_only_2k_contract(self):
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "史诗级太空歌剧院线预告", "channel": "minimax",
                    "duration": 5, "ratio": "16:9", "resolution": "2k",
                },
            })

        self.assertEqual(200, status, result)
        self.assertEqual((24, 100), (result["cost"], result["points"]))

    def test_minimax_legacy_768p_same_key_replays_before_new_task_validation(self):
        legacy = {
            "prompt": "legacy paid request", "channel": "minimax",
            "model": "MiniMax-H3", "duration": 5,
            "ratio": "9:16", "resolution": "768p",
        }
        with mock.patch.object(
            core, "_domains", return_value=(audio, self.points, video),
        ), mock.patch.object(
            core, "HANDLERS", {"xiaole_video": lambda payload: payload},
        ), mock.patch.object(
            core.submission_idempotency, "replay_existing",
            return_value=("replay", {"job_id": 73, "cost": 24}),
        ), mock.patch.object(
            video, "validate_xiaole_video_payload",
            side_effect=AssertionError("replay must precede new-task validation"),
        ):
            status, result = self._post(
                "/api/gen/xiaole_video", legacy,
                expected=24, idempotency_key="minimax-legacy-replay-001",
            )
        self.assertEqual((200, 73), (status, result["job_id"]))

        with mock.patch.object(
            core, "_domains", return_value=(audio, self.points, video),
        ), mock.patch.object(
            core, "HANDLERS", {"xiaole_video": lambda payload: payload},
        ), mock.patch.object(
            core.submission_idempotency, "replay_existing",
            return_value=("missing", None),
        ), mock.patch.object(
            video_minimax_h3, "available", return_value=True,
        ), mock.patch("content_domains.feature_flags.is_enabled", return_value=True):
            status, result = self._post(
                "/api/gen/xiaole_video", legacy,
                expected=24, idempotency_key="minimax-new-768-rejected-001",
            )
        self.assertEqual(400, status, result)
        self.assertIn("仅支持 2K", result["detail"])
        self.assertEqual([], self.points.deductions)

    def test_minimax_historical_upload_hash_conflict_fails_closed(self):
        request = {
            "prompt": "让 @图片1 挥手", "channel": "minimax",
            "duration": 5, "ratio": "9:16",
            "reference_upload_ids": ["img_" + "a" * 32],
        }
        expanded = dict(request)
        expanded.pop("reference_upload_ids")
        expanded["reference_images"] = ["https://example.com/reference.png"]
        with mock.patch.object(
            core, "_domains", return_value=(audio, self.points, video),
        ), mock.patch.object(
            core, "HANDLERS", {"xiaole_video": lambda payload: payload},
        ), mock.patch.object(
            core.submission_idempotency, "replay_existing",
            return_value=("replay", {"job_id": 91, "cost": 24}),
        ), mock.patch.object(
            video, "minimax_idempotency_claim_body",
            side_effect=AssertionError("historical replay must precede strict claim validation"),
        ):
            status, result = self._post(
                "/api/gen/xiaole_video", expanded, expected=24,
                idempotency_key="minimax-legacy-url-replay-001",
            )
        self.assertEqual((200, 91), (status, result["job_id"]))

        with self.assertRaisesRegex(ValueError, "参考图"):
            video.minimax_idempotency_claim_body(expanded)
        old_body = video.minimax_idempotency_replay_bodies(expanded)[0]
        self.assertEqual(expanded["reference_images"], old_body["reference_images"])

        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "jobs.db"

            def database_factory():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            connection = database_factory()
            connection.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT, username TEXT, payload TEXT)"
            )
            submission_idempotency.ensure_table(connection)
            connection.execute(
                "INSERT INTO jobs(id,kind,username,payload) VALUES(?,?,?,?)",
                (91, "xiaole_video", "alice", json.dumps(old_body)),
            )
            connection.commit()
            connection.close()
            state, _ = submission_idempotency.begin(
                database_factory, "alice", "/api/gen/xiaole_video",
                "minimax-expired-replay-001", old_body,
            )
            self.assertEqual("new", state)
            submission_idempotency.complete(
                database_factory, "alice", "/api/gen/xiaole_video",
                "minimax-expired-replay-001", {"job_id": 91, "cost": 24},
            )
            state, _ = submission_idempotency.replay_existing(
                database_factory, "alice", "/api/gen/xiaole_video",
                "minimax-expired-replay-001",
                video.minimax_idempotency_replay_bodies(request),
            )
            self.assertEqual("conflict", state)

            with mock.patch.object(
                core, "_domains", return_value=(audio, self.points, video),
            ), mock.patch.object(
                core, "HANDLERS", {"xiaole_video": lambda payload: payload},
            ), mock.patch.object(
                core, "jdb", side_effect=database_factory,
            ), mock.patch.object(
                cli_uploads, "expand_image_payload",
                side_effect=AssertionError("hash conflicts must precede upload lookup"),
            ) as expand:
                status, result = self._post(
                    "/api/gen/xiaole_video", request,
                    expected=24, idempotency_key="minimax-expired-replay-001",
                )
                different_image = dict(
                    request, reference_upload_ids=["img_" + "b" * 32],
                )
                image_status, image_result = self._post(
                    "/api/gen/xiaole_video", different_image,
                    expected=24, idempotency_key="minimax-expired-replay-001",
                )
                mismatched = dict(request, prompt="让 @图片1 跳舞")
                mismatch_status, mismatch_result = self._post(
                    "/api/gen/xiaole_video", mismatched,
                    expected=24, idempotency_key="minimax-expired-replay-001",
                )

        self.assertEqual((409, "idempotency_conflict"), (status, result["code"]))
        self.assertEqual(
            (409, "idempotency_conflict"), (image_status, image_result["code"]),
        )
        self.assertEqual(
            (409, "idempotency_conflict"),
            (mismatch_status, mismatch_result["code"]),
        )
        expand.assert_not_called()
        self.assertEqual([], self.points.deductions)

    def test_minimax_submit_expands_private_reference_before_charge_and_queue(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (40, 80, 120)).save(image, "PNG")
        raw = image.getvalue()
        captured = {}
        idempotency_bodies = []

        def create_job(*args, **_kwargs):
            captured.update(args[6])
            return 42, 76

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(cli_uploads, "expand_image_payload", wraps=cli_uploads.expand_image_payload) as expand, \
                mock.patch.object(core, "HANDLERS", {"xiaole_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True), \
                mock.patch.object(
                    core, "_idempotency_begin",
                    side_effect=lambda _username, _endpoint, _key, body: (
                        idempotency_bodies.append(dict(body)) or ("new", None)
                    ),
                ), \
                mock.patch.object(core, "_idempotency_complete"), \
                mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                mock.patch.object(core, "_user_active_job_count", return_value=0), \
                mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                mock.patch.object(video, "record_video_pending_asset"), \
                mock.patch.object(core, "enqueue_job", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post(
                "/api/gen/xiaole_video", {
                    "prompt": "让 @图片1 向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                }, expected=24, idempotency_key="minimax-ref-test-001",
            )

        self.assertEqual((200, 42), (status, result["job_id"]))
        self.assertNotIn("reference_upload_ids", captured)
        self.assertEqual(1, len(captured["reference_images"]))
        self.assertTrue(captured["reference_images"][0].startswith("data:image/png;base64,"))
        self.assertEqual(
            video_minimax_h3.ORIGIN_METASO, captured["_minimax_origin"]
        )
        self.assertNotIn("_minimax_api_base", captured)
        self.assertEqual(1, len(idempotency_bodies))
        self.assertNotIn("_minimax_origin", idempotency_bodies[0])
        self.assertNotIn("_minimax_api_base", idempotency_bodies[0])
        self.assertEqual([uploaded["upload_id"]], idempotency_bodies[0]["reference_upload_ids"])
        self.assertEqual([], idempotency_bodies[0]["reference_images"])
        expand.assert_called_once()

    def test_minimax_submit_rejects_foreign_and_expired_references_before_charge(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (120, 80, 40)).save(image, "PNG")
        raw = image.getvalue()
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "HANDLERS", {"xiaole_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            foreign = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "bob", "image/png",
                hashlib.sha256(raw).hexdigest(), now=100,
            )
            own = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(), now=100,
            )
            base_payload = {
                "prompt": "a person waves to camera", "channel": "minimax",
                "duration": 4, "ratio": "9:16", "resolution": "2k",
            }
            with mock.patch.object(cli_uploads.time, "time", return_value=100):
                foreign_status, _ = self._post(
                    "/api/gen/xiaole_video",
                    dict(base_payload, reference_upload_ids=[foreign["upload_id"]]),
                    expected=24, idempotency_key="foreign-ref-test-001",
                )
            with mock.patch.object(cli_uploads.time, "time", return_value=100 + cli_uploads.TTL + 1):
                expired_status, _ = self._post(
                    "/api/gen/xiaole_video",
                    dict(base_payload, reference_upload_ids=[own["upload_id"]]),
                    expected=24, idempotency_key="expired-ref-test-001",
                )

        self.assertEqual((400, 400), (foreign_status, expired_status))
        self.assertEqual([], self.points.deductions)

    def test_minimax_quote_rejects_foreign_private_reference(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (120, 80, 40)).save(image, "PNG")
        raw = image.getvalue()

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "bob", "image/png",
                hashlib.sha256(raw).hexdigest(),
            )
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "人物向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                },
            })

        self.assertEqual(400, status, result)
        self.assertIn("不存在或已失效", result["detail"])

    def test_minimax_quote_rejects_expired_private_reference(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (256, 256), (80, 120, 40)).save(image, "PNG")
        raw = image.getvalue()
        expired_at = 100 + cli_uploads.TTL + 1

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(folder)), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(cli_uploads.time, "time", return_value=expired_at), \
                mock.patch.object(core.feature_flags, "is_enabled", return_value=True), \
                mock.patch.object(video_minimax_h3, "available", return_value=True):
            uploaded = cli_uploads.store_image(
                io.BytesIO(raw), len(raw), "alice", "image/png",
                hashlib.sha256(raw).hexdigest(), now=100,
            )
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "xiaole_video", "payload": {
                    "prompt": "人物向镜头挥手", "channel": "minimax",
                    "duration": 4, "ratio": "9:16", "resolution": "2k",
                    "reference_upload_ids": [uploaded["upload_id"]],
                },
            })

        self.assertEqual(400, status, result)
        self.assertIn("已过期", result["detail"])

    def test_banana_quote_checks_the_banana_flag_not_the_image_flag(self):
        checked = []
        core.feature_flags.require_enabled = checked.append
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "image", "payload": {
                "prompt": "海报", "provider": "banana", "model": "nb2",
                "ratio": "1:1", "quality": "std", "count": 1,
            },
        })
        self.assertEqual((200, 24), (status, result["cost"]))
        self.assertEqual(["banana"], checked)

    def test_collect_search_and_leads_quotes_validate_without_external_calls(self):
        checked = []
        core.feature_flags.require_enabled = checked.append
        cases = (
            ("collect", {"url": "https://v.douyin.com/abc123/", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://weixin.qq.com/sph/Abc123", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://weixin.qq.com:443/sph/Abc123", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://b23.tv/keSUqLz", "want": ["video"]}, 24, "collect"),
            ("collect", {"url": "https://x.com/CrazyKaomei/status/2093502767776366755", "want": ["comments"]}, 24, "collect"),
            ("collect_search", {"platform": "xhs", "keyword": "轻食创业", "page": 2}, 1, "collect"),
            ("leads", {
                "keyword": "美容院拓客", "platforms": ["douyin"],
                "count": 20, "pages": 1, "channels_targets": [],
            }, 24, "leads"),
        )
        with mock.patch.object(cli_gateway.pricing, "get_price", return_value=1):
            for kind, payload, cost, _flag in cases:
                with self.subTest(kind=kind):
                    status, result = self._post(
                        "/api/gen/cli/quote", {"kind": kind, "payload": payload})
                    self.assertEqual((200, cost, 100), (status, result["cost"], result["points"]))
        self.assertEqual([item[3] for item in cases], checked)

        for want in (["video"], ["transcript"]):
            status, _result = self._post("/api/gen/cli/quote", {
                "kind": "collect", "payload": {
                    "url": "https://x.com/CrazyKaomei/status/2093502767776366755", "want": want,
                },
            })
            self.assertEqual(400, status)

        status, result = self._post("/api/gen/cli/quote", {
            "kind": "collect", "payload": {
                "url": "https://douyin.com.evil.example/video/1", "want": ["video"],
            },
        })
        self.assertEqual(400, status)
        self.assertIn("仅支持抖音、小红书、视频号、B 站或 X 单帖", result["detail"])
        for invalid_channels in (
                "http://weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com/sph/",
                "https://weixin.qq.com/sph//Abc123",
                "https://weixin.qq.com/sph/../Abc123",
                "https://weixin.qq.com/sphx/Abc123",
                "https://weixin.qq.com/not-sph/Abc123",
                "https://evil.weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com:80/sph/Abc123",
                "https://weixin.qq.com:444/sph/Abc123",
                "https://%75:%70@weixin.qq.com/sph/Abc123",
                "https://weixin.qq.com.evil.example/sph/Abc123"):
            status, result = self._post("/api/gen/cli/quote", {
                "kind": "collect", "payload": {"url": invalid_channels, "want": ["video"]},
            })
            self.assertEqual(400, status)
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "collect", "payload": {
                "url": "https://mp.weixin.qq.com/s/Abc123", "want": ["video"],
            },
        })
        self.assertEqual(400, status)
        self.assertIn("仅支持抖音、小红书、视频号、B 站或 X 单帖", result["detail"])
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "leads", "payload": {
                "keyword": "", "platforms": ["channels"], "count": 20,
                "pages": 1, "channels_targets": [],
            },
        })
        self.assertEqual(400, status)
        self.assertIn("channels_targets", result["detail"])
        status, result = self._post("/api/gen/cli/quote", {
            "kind": "collect_search", "payload": {
                "platform": "douyin", "keyword": "越界页码", "page": 51,
            },
        })
        self.assertEqual(400, status)
        self.assertIn("1-50", result["detail"])

    def test_digital_ip_text_quote_requires_ready_owned_avatar_and_voice(self):
        request = {
            "mode": "text", "avatar_id": 7, "text": "欢迎来到黄雀",
            "voice": "owned-voice", "resolution": "1080p", "ratio": "9:16",
        }
        cleaned = dict(request, motion="medium", bgm_data="", bgm_volume=0.18)
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value={
                    "id": 7, "status": "ready", "image_file": "avatars/alice.jpg",
                }) as get_avatar, \
                mock.patch.object(audio, "resolve_audio_provider_voice",
                                  return_value="provider-voice") as resolve_voice:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once_with(request, "alice")
        get_avatar.assert_called_once_with("alice", 7)
        resolve_voice.assert_called_once_with("alice", "owned-voice")

        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload", return_value=cleaned), \
                mock.patch.object(video, "get_video_avatar", return_value={
                    "id": 7, "status": "pending", "image_file": "avatars/alice.jpg",
                }), \
                mock.patch.object(audio, "resolve_audio_provider_voice") as resolve_voice:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": request})
        self.assertEqual(400, status)
        self.assertIn("尚未就绪", result["detail"])
        resolve_voice.assert_not_called()

        injected = dict(request, image_data="data:image/png;base64,AAAA")
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload") as validate:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": injected})
        self.assertEqual(400, status)
        self.assertIn("私密上传照片", result["detail"])
        validate.assert_not_called()

    def test_digital_ip_audio_quote_accepts_only_owned_asset_reference(self):
        request = {
            "mode": "audio", "avatar_id": 9, "audio_file": "audio/alice.wav",
            "resolution": "1080p", "ratio": "9:16",
        }
        cleaned = dict(
            request, motion="medium", audio_data="", bgm_data="", bgm_volume=0.18)
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value={
                    "id": 9, "status": "ready", "image_file": "avatars/alice.jpg",
                }), \
                mock.patch.object(audio, "resolve_audio_provider_voice") as resolve_voice:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once_with(request, "alice")
        resolve_voice.assert_not_called()

        injected = dict(request, audio_data="data:audio/wav;base64,AAAA")
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_payload") as validate:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": injected})
        self.assertEqual(400, status)
        self.assertIn("本人资产音频", result["detail"])
        validate.assert_not_called()

        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video", "payload": []})
        self.assertEqual(400, status)
        self.assertIn("合法 JSON", result["detail"])

    def test_cinematic_quote_requires_ready_provider_avatar_and_expands_private_references(self):
        request = {
            "cine_mode": "open", "avatar_ids": [3, 4], "prompt": "在工作室交流",
            "resolution": "720p", "ratio": "16:9", "duration": 10,
        }
        cleaned = dict(
            request, reference_video_files=[], reference_image_files=[], enhance_prompt=False)
        ready = {
            "status": "ready", "image_file": "avatars/alice.jpg",
            "provider_avatar_id": "look-id",
        }
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_cinematic_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value=ready) as get_avatar:
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "cinematic", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))
        validate.assert_called_once_with(request, "alice", [])
        self.assertEqual([mock.call("alice", 3), mock.call("alice", 4)],
                         get_avatar.call_args_list)

        not_provider_ready = dict(ready, provider_avatar_id="")
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_cinematic_payload", return_value=cleaned), \
                mock.patch.object(video, "get_video_avatar", return_value=not_provider_ready):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "cinematic", "payload": request})
        self.assertEqual(400, status)
        self.assertIn("电影化身尚未就绪", result["detail"])

        with_reference = dict(request, reference_video_upload_ids=["vid_" + "a" * 32])
        expanded = dict(request, reference_videos=["data:video/mp4;base64,AAAA"])
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(cli_uploads, "expand_role_media_payload", return_value=expanded) as expand, \
                mock.patch.object(video, "validate_cinematic_payload", return_value=cleaned) as validate, \
                mock.patch.object(video, "get_video_avatar", return_value=ready):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "cinematic", "payload": with_reference})
        self.assertEqual((200, 24), (status, result["cost"]))
        expand.assert_called_once_with(with_reference, "alice")
        validate.assert_called_once_with(expanded, "alice", [])

    def test_cinematic_submit_replays_before_upload_expansion_and_cleans_queue_failure(self):
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l1sAAAAASUVORK5CYII="
        )
        claims = {}
        created = []

        def begin(_username, _endpoint, key, body):
            digest = json.dumps(body, sort_keys=True, separators=(",", ":"))
            if key not in claims:
                claims[key] = {"digest": digest, "response": None}
                return "new", None
            if claims[key]["digest"] != digest:
                return "conflict", None
            response = claims[key]["response"]
            return ("replay", response) if response else ("processing", None)

        def complete(_username, _endpoint, key, response):
            claims[key]["response"] = response

        def abort(_username, _endpoint, key):
            if key in claims and claims[key]["response"] is None:
                claims.pop(key)

        def create_job(*args, **_kwargs):
            created.append(args[6])
            return 40 + len(created), 76

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            upload_root, output_root = root / "uploads", root / "output"

            def out_path(relative):
                path = output_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                return path

            with mock.patch.object(cli_uploads, "UPLOAD_ROOT", upload_root):
                uploaded = cli_uploads.store_image(
                    io.BytesIO(raw), len(raw), "alice", "image/png",
                    hashlib.sha256(raw).hexdigest(),
                )
            request = {
                "cine_mode": "open", "avatar_ids": [3], "prompt": "在工作室交流",
                "resolution": "720p", "ratio": "9:16", "duration": 8,
                "reference_image_upload_ids": [uploaded["upload_id"]],
            }

            with mock.patch.object(cli_uploads, "UPLOAD_ROOT", upload_root), \
                    mock.patch.object(core, "HANDLERS", {"cinematic": lambda payload: payload}), \
                    mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                    mock.patch.object(core, "_idempotency_begin", side_effect=begin), \
                    mock.patch.object(core, "_idempotency_complete", side_effect=complete), \
                    mock.patch.object(core, "_idempotency_abort", side_effect=abort), \
                    mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                    mock.patch.object(core, "_user_active_job_count", return_value=0), \
                    mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                    mock.patch.object(core, "_reject_pending_job"), \
                    mock.patch.object(video, "get_video_avatar", return_value={
                        "id": 3, "status": "ready", "provider_avatar_id": "look-3",
                    }), \
                    mock.patch.object(video, "_out_path", side_effect=out_path), \
                    mock.patch.object(video, "record_video_pending_asset"), \
                    mock.patch.object(video, "update_video_asset_phase"), \
                    mock.patch.object(core, "enqueue_job", side_effect=(True, False)), \
                    mock.patch.object(cli_uploads, "expand_role_media_payload",
                                      wraps=cli_uploads.expand_role_media_payload) as expand:
                status, first = self._post(
                    "/api/gen/cinematic", request, expected=24,
                    idempotency_key="hqcli-cinematic-retry-001",
                )
                self.assertEqual((200, 41), (status, first["job_id"]))
                kept = {path for path in output_root.rglob("*") if path.is_file()}
                self.assertEqual(1, len(kept))

                # Replay must not touch the short-lived upload again.
                for path in upload_root.iterdir():
                    path.unlink()
                status, replay = self._post(
                    "/api/gen/cinematic", request, expected=24,
                    idempotency_key="hqcli-cinematic-retry-001",
                )
                self.assertEqual((200, first), (status, replay))
                self.assertEqual((1, 1), (len(created), expand.call_count))
                self.assertEqual(kept, {
                    path for path in output_root.rglob("*") if path.is_file()
                })

                # A fresh request that cannot enter the queue must remove its files.
                with mock.patch.object(cli_uploads, "_load_image", return_value=(
                    base64.b64encode(raw).decode("ascii"), {"mime": "image/png"},
                )):
                    status, failed = self._post(
                        "/api/gen/cinematic", dict(request, prompt="队列失败"), expected=24,
                        idempotency_key="hqcli-cinematic-retry-002",
                    )
                self.assertEqual((429, "queue_full"), (status, failed["code"]))
                self.assertEqual(2, len(created))
                self.assertEqual(kept, {
                    path for path in output_root.rglob("*") if path.is_file()
                })

    def test_script_to_video_replays_before_mutable_plan_validation(self):
        from content_domains import pixelle_video, script_to_video

        claims = {}
        created = []

        def begin(_username, _endpoint, key, body):
            digest = json.dumps(body, sort_keys=True, separators=(",", ":"))
            if key not in claims:
                claims[key] = {"digest": digest, "response": None}
                return "new", None
            self.assertEqual(claims[key]["digest"], digest)
            return "replay", claims[key]["response"]

        def complete(_username, _endpoint, key, response):
            claims[key]["response"] = response

        prepared = {
            "pipeline": "pixelle", "text": "AI 培训", "mode": "generate",
            "template": "1080x1920/image_default.html", "n_scenes": 1,
            "scenes": [{"line": "AI 培训"}],
        }
        quote_token, _ = pixelle_video.issue_quote(
            prepared, "alice", 24, core.AUTH_INTERNAL_TOKEN)
        prepared_with_quote = dict(prepared, _quote_token=quote_token)

        def create_job(*args, **_kwargs):
            created.append(args[6])
            return 51, 76

        with mock.patch.object(core, "HANDLERS", {"script_to_video": lambda payload: payload}), \
                mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core, "_idempotency_begin", side_effect=begin), \
                mock.patch.object(core, "_idempotency_complete", side_effect=complete), \
                mock.patch.object(core, "_user_video_submit_limit", return_value=None), \
                mock.patch.object(core, "_user_active_job_count", return_value=0), \
                mock.patch.object(core.jobs_store, "create_paid_job", side_effect=create_job), \
                mock.patch.object(video, "record_video_pending_asset"), \
                mock.patch.object(core, "enqueue_job", return_value=True), \
                mock.patch.object(pixelle_video, "paid_plan_association", return_value=None), \
                mock.patch.object(
                    script_to_video, "prepare_script_to_video_payload",
                    side_effect=[prepared_with_quote, AssertionError("replay reached mutable plan validation")],
                ) as prepare:
            request = {
                "pipeline": "pixelle", "text": "AI 培训",
                "quote_token": quote_token,
            }
            status, first = self._post(
                "/api/gen/script_to_video", request, expected=24,
                idempotency_key="script-video-lost-response-001",
            )
            status_replay, replay = self._post(
                "/api/gen/script_to_video", request, expected=24,
                idempotency_key="script-video-lost-response-001",
            )

        self.assertEqual((200, 51), (status, first["job_id"]))
        self.assertEqual((200, first), (status_replay, replay))
        self.assertEqual(1, prepare.call_count)
        self.assertEqual(1, len(created))

    def test_batch_and_tryon_quotes_use_server_validation_and_total_cost(self):
        batch = {
            "mode": "text", "text": "欢迎到店", "voice": "owned-voice",
            "avatars": [{"avatar_id": 1}, {"avatar_id": 2}],
        }
        items = [
            {"mode": "text", "avatar_id": "1", "voice": "owned-voice"},
            {"mode": "text", "avatar_id": "2", "voice": "owned-voice"},
        ]
        ready = {"status": "ready", "image_file": "avatars/alice.jpg"}
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(video, "validate_video_batch_payload", return_value=items), \
                mock.patch.object(video, "get_video_avatar", return_value=ready), \
                mock.patch.object(audio, "resolve_audio_provider_voice", return_value="provider"):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "video_batch", "payload": batch})
        self.assertEqual((200, 48), (status, result["cost"]))

        request = {
            "line": "2", "person_image_upload_id": "img_" + "a" * 32,
            "clothes_upload_id": "img_" + "b" * 32,
        }
        expanded = {"line": "2", "person_image_data": "data:image/png;base64,AAAA",
                    "clothes_data": "data:image/png;base64,BBBB"}
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(cli_uploads, "expand_role_media_payload", return_value=expanded), \
                mock.patch.object(video, "validate_tryon_payload", return_value=expanded):
            status, result = self._post(
                "/api/gen/cli/quote", {"kind": "tryon", "payload": request})
        self.assertEqual((200, 24), (status, result["cost"]))

    def test_tryon_submit_expands_private_roles_before_security_and_cost_gate(self):
        request = {
            "line": "2", "person_image_upload_id": "img_" + "a" * 32,
            "clothes_upload_id": "img_" + "b" * 32,
        }
        expanded = {"line": "2", "person_image_data": "data:image/png;base64,AAAA",
                    "clothes_data": "data:image/png;base64,BBBB", "seconds": 6}
        checked = []
        with mock.patch.object(core, "_domains", return_value=(audio, self.points, video)), \
                mock.patch.object(core, "HANDLERS", {"tryon": lambda payload: payload}), \
                mock.patch.object(cli_uploads, "expand_role_media_payload", return_value=expanded) as expand, \
                mock.patch.object(video, "validate_tryon_payload", return_value=expanded), \
                mock.patch.object(core.miniprogram_security, "check_payload", side_effect=checked.append):
            status, result = self._post("/api/gen/tryon", request, expected=25)
        self.assertEqual((409, "quote_cost_changed"), (status, result["code"]))
        expand.assert_called_once_with(request, "alice")
        self.assertEqual([expanded], checked)
        self.assertEqual([], self.points.deductions)


if __name__ == "__main__":
    unittest.main()
