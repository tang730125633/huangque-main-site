import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class AdminE2ERunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server.admin_api as admin_api
        cls.admin = admin_api

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old = self.admin.ADMIN_DB, self.admin.JOB_DB, self.admin.ASSET_DB, self.admin.CONTENT_OUT
        self.admin.ADMIN_DB = root / "admin.db"
        self.admin.JOB_DB = root / "jobs.db"
        self.admin.ASSET_DB = root / "assets.db"
        self.admin.CONTENT_OUT = root / "content_out"
        self.admin.CONTENT_OUT.mkdir()
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute("""CREATE TABLE admin_e2e_runs(
                run_id TEXT PRIMARY KEY,batch_id TEXT DEFAULT '',operation_id TEXT,
                username TEXT DEFAULT '',status TEXT,
                job_id INTEGER,acceptance_id TEXT DEFAULT '',evidence_json TEXT DEFAULT '{}',
                cost INTEGER DEFAULT 0,points_before INTEGER,points_after INTEGER,
                transaction_key TEXT DEFAULT '',error TEXT DEFAULT '',created_by TEXT,
                created_at INTEGER,updated_at INTEGER)""")
            connection.execute("""CREATE TABLE admin_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,target TEXT,
                detail TEXT,created_at INTEGER)""")
            connection.commit()

    def tearDown(self):
        self.admin.ADMIN_DB, self.admin.JOB_DB, self.admin.ASSET_DB, self.admin.CONTENT_OUT = self.old
        self.tmp.cleanup()

    def test_server_runs_once_without_exposing_fixture_or_account_token(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 30)), \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 77, "cost": 30, "points_left": 470,
             }) as submit:
            run = self.admin.start_e2e_run(
                "root", "admin-token", "video.digital_ip.text.single"
            )
            self.assertEqual(run["username"], "qa-dedicated")
            self.assertEqual(run["job_id"], 77)
            payload = submit.call_args.args[2]
            self.assertTrue(payload["image_data"].startswith("data:image/"))
            self.assertNotIn("short-lived-secret", json.dumps(run))
            self.assertNotIn("image_data", json.dumps(run))
            with self.assertRaisesRegex(ValueError, "已有一条生产链测试"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "video.digital_ip.audio"
                )

    def test_uncertain_submit_is_persisted_and_blocks_a_new_idempotency_key(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 10)), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": [
                 {"id": 1, "scope": "public", "voice_key": "S_public"},
             ]}), \
             patch.object(self.admin, "_content_e2e_request",
                          side_effect=self.admin.E2ESubmitUncertain("response timeout")):
            with self.assertRaisesRegex(RuntimeError, "提交结果未知"):
                self.admin.start_e2e_run("root", "admin-token", "audio.tts.public")
            with self.assertRaisesRegex(ValueError, "已有一条生产链测试"):
                self.admin.start_e2e_run("root", "admin-token", "audio.tts.public")
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            row = connection.execute("SELECT status,error FROM admin_e2e_runs").fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertIn("禁止自动重试", row[1])

    def test_response_read_timeout_is_an_uncertain_submit(self):
        with patch.object(self.admin.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.side_effect = TimeoutError("late")
            with self.assertRaises(self.admin.E2ESubmitUncertain):
                self.admin._content_e2e_request(
                    "/api/gen/audio", "account-token", {"text": "qa"}, "e2e:one", 10
                )

    def test_private_binary_runner_uploads_raw_fixture_with_idempotency(self):
        payload = self.admin._e2e_payload(
            "script.breakdown.local_image",
            self.admin.function_registry.e2e_runner("script.breakdown.local_image"),
        )
        payload["qa_run_id"] = "a" * 32
        response = MagicMock()
        response.read.return_value = b'{"job_id":23,"cost":20,"points_left":480}'
        with patch.object(self.admin.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            result = self.admin._content_e2e_upload(
                "/api/gen/breakdown/local-upload?media_type=image",
                "qa-token", payload, "e2e:binary", 20,
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(result["job_id"], 23)
        self.assertTrue(request.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(request.headers["Content-type"], "image/png")
        self.assertEqual(request.headers["Idempotency-key"], "e2e:binary")
        self.assertEqual(request.headers["X-hq-qa-run-id"], "a" * 32)
        self.assertNotIn("file_data", json.dumps(result))

    def test_local_reverse_run_uses_binary_submitter_once(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(
                 cost_of=lambda kind, payload: 20,
             )), \
             patch.object(self.admin, "_content_e2e_upload", return_value={
                 "job_id": 24, "cost": 20, "points_left": 480,
             }) as upload, \
             patch.object(self.admin, "_content_e2e_request") as json_submit:
            run = self.admin.start_e2e_run(
                "root", "admin-token", "script.breakdown.local_image"
            )
        self.assertEqual(run["job_id"], 24)
        endpoint, token, payload, idem, cost = upload.call_args.args
        self.assertEqual(endpoint, "/api/gen/breakdown/local-upload?media_type=image")
        self.assertEqual((token, cost), ("short-lived-secret", 20))
        self.assertEqual((payload["source_page"], payload["source_type"]), ("script", "image"))
        self.assertEqual(payload["qa_run_id"], run["run_id"])
        self.assertEqual(idem, "e2e:" + run["run_id"])
        json_submit.assert_not_called()

    def test_fixture_reader_rejects_paths_outside_private_directory(self):
        with self.assertRaisesRegex(ValueError, "名称无效"):
            self.admin._fixture_data_url("@fixture/../admin_api.py")

    def test_collect_fixture_stays_server_side_and_submits_to_leadgen(self):
        private_url = "https://www.douyin.com/video/1234567890"
        runner = self.admin.function_registry.e2e_runner("collect.content.comments")
        with patch.dict(os.environ, {"HQ_E2E_COLLECT_URL": private_url}):
            payload = self.admin._e2e_payload("collect.content.comments", runner)
        self.assertEqual(payload["url"], private_url)
        self.assertNotIn(private_url, json.dumps(
            self.admin._e2e_parameters("collect.content.comments", payload),
            ensure_ascii=False,
        ))
        response = MagicMock()
        response.read.return_value = b'{"job_id":12,"cost":3,"points_left":97}'
        with patch.object(self.admin.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            result = self.admin._content_e2e_request(
                "/api/gen/collect", "qa-token", payload, "e2e:collect", 3
            )
        self.assertEqual(result["job_id"], 12)
        self.assertEqual(urlopen.call_args.args[0].full_url, self.admin.LEADGEN_BASE + "/api/gen/collect")

    def test_missing_collect_fixture_blocks_before_paid_submission(self):
        runner = self.admin.function_registry.e2e_runner("collect.content.video")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "HQ_E2E_COLLECT_URL"):
                self.admin._e2e_payload("collect.content.video", runner)

    def test_leads_runner_uses_one_public_result_candidate(self):
        runner = self.admin.function_registry.e2e_runner("leads.keyword.search")
        payload = self.admin._e2e_payload("leads.keyword.search", runner)
        self.assertEqual((payload["platforms"], payload["count"], payload["pages"]),
                         (["douyin"], 1, 1))
        self.assertEqual((payload["provider"], payload["source_page"]), ("tikhub", "leads"))
        self.assertEqual(self.admin._e2e_kind("/api/gen/leads"), "leads")

    def test_script_and_canvas_runners_use_customer_page_payloads(self):
        script = self.admin.function_registry.e2e_runner("script.write.spoken")
        script_payload = self.admin._e2e_payload("script.write.spoken", script)
        self.assertEqual(
            (script_payload["source_page"], script_payload["format"], script_payload["style"]),
            ("script", "script", "口播"),
        )
        with patch.dict(os.environ, {"HQ_E2E_COLLECT_URL": "https://www.douyin.com/video/1234567890"}):
            breakdown = self.admin._e2e_payload(
                "script.breakdown.reverse",
                self.admin.function_registry.e2e_runner("script.breakdown.reverse"),
            )
        self.assertEqual(
            (breakdown["source_page"], breakdown["mode"]),
            ("script", "reverse_prompt"),
        )
        local_image = self.admin._e2e_payload(
            "script.breakdown.local_image",
            self.admin.function_registry.e2e_runner("script.breakdown.local_image"),
        )
        self.assertTrue(local_image["file_data"].startswith("data:image/png;base64,"))
        self.assertEqual(
            (local_image["source_page"], local_image["source_type"], local_image["media_type"]),
            ("script", "image", "image"),
        )
        canvas = self.admin._e2e_payload(
            "canvas.agent.plan",
            self.admin.function_registry.e2e_runner("canvas.agent.plan"),
        )
        self.assertEqual(
            self.admin.function_registry.e2e_runner("canvas.agent.plan")["endpoint"]["path"],
            "/api/gen/canvas_agent",
        )
        self.assertEqual((canvas["source_page"], canvas["scope"]), ("canvas", "local"))
        self.assertEqual(canvas["selected_node_ids"], ["qa_product"])
        self.assertEqual(canvas["quoted_cost"], 3)
        with patch.object(self.admin.feature_flags, "is_enabled", return_value=True), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 3)):
            prepared = self.admin._e2e_prepare_operation({"token": "qa", "account": {}}, "canvas.agent.plan")
        self.assertEqual((prepared["endpoint"], prepared["kind"]),
                         ("/api/gen/canvas_agent", "canvas_agent"))
        self.assertEqual(self.admin._e2e_kind("/api/gen/copy"), "copy")
        self.assertEqual(self.admin._e2e_kind("/api/gen/breakdown"), "breakdown")
        self.assertEqual(
            self.admin._e2e_kind("/api/gen/breakdown/local-upload?media_type=image"),
            "breakdown",
        )
        self.assertEqual(self.admin._e2e_kind("/api/gen/canvas_agent"), "canvas_agent")

        for operation, channel in (("canvas.video.grok", "grok"),
                                   ("canvas.video.micro", "micro")):
            video = self.admin._e2e_payload(
                operation, self.admin.function_registry.e2e_runner(operation)
            )
            self.assertEqual((video["source_page"], video["channel"]), ("canvas", channel))
            self.assertEqual(video["resolution"], "480p")

    def test_canvas_image_nodes_reuse_private_image_fixture_on_canvas_routes(self):
        expected = {
            "canvas.image.banana.nb2": ("/api/gen/banana", "banana", "nb2"),
            "canvas.image.banana.pro": ("/api/gen/banana", "banana", "pro"),
            "canvas.image.openai": ("/api/gen/image", "openai", None),
            "canvas.image.zelong": ("/api/gen/image", "zelong", None),
        }
        for operation, (endpoint, provider, model) in expected.items():
            runner = self.admin.function_registry.e2e_runner(operation)
            payload = self.admin._e2e_payload(operation, runner)
            self.assertTrue(runner["supported"])
            self.assertEqual((runner["endpoint"]["path"], payload["source_page"]),
                             (endpoint, "canvas"))
            self.assertEqual(payload["provider"], provider)
            self.assertEqual(payload.get("model"), model)
            self.assertEqual(self.admin._e2e_kind(endpoint), "image")
            self.assertNotIn("qa-serum", json.dumps(
                self.admin.function_registry.list_pages(), ensure_ascii=False
            ))

    def test_paid_short_drama_modes_stay_blocked_before_auth_or_charge(self):
        operations = [
            "short_drama.live_action.shot_video",
            "short_drama.live_action.preview",
            "short_drama.live_action.delivery",
        ]
        with patch.object(self.admin, "auth_admin_request") as auth:
            for operation in operations:
                result = self.admin.e2e_preflight("admin-token", operation)
                self.assertFalse(result["ready"])
                self.assertTrue(result["blocker"])
            auth.assert_not_called()

    def test_short_drama_character_reference_runs_customer_endpoints_then_locks_and_cleans(self):
        session = {"token": "qa-token", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        character = {
            "character_key": "character_1", "name": "林夏",
            "reference_file": "", "reference_url": "",
            "reference_version": 0, "reference_locked": False,
        }
        calls = []

        def submit(path, _token, payload, idem, cost, require_job_id=True, method="POST"):
            calls.append((path, payload, idem, cost, require_job_id, method))
            if path.endswith("/projects/import"):
                return {"id": "project-character", "revision": 1,
                        "characters": [character]}
            if path.startswith("/api/gen/short-drama/project?"):
                return {"id": "project-character", "revision": 2,
                        "characters": [character]}
            if path.endswith("/generate-character-reference"):
                return {"job_id": 77, "cost": 35, "points_left": 465}
            self.fail("unexpected request " + path)

        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(
                 cost_of=lambda _kind, _payload: 35,
             )), \
             patch.object(self.admin, "_content_e2e_request", side_effect=submit):
            run = self.admin.start_e2e_run(
                "root", "admin-token", "short_drama.live_action.character_reference"
            )
        self.assertEqual((run["status"], run["job_id"], run["cost"]),
                         ("queued", 77, 35))
        self.assertEqual([item[5] for item in calls], ["POST", "PUT", "POST"])
        self.assertEqual(calls[1][1]["characters"], [character])
        self.assertEqual(calls[2][1], {
            "project_id": "project-character", "revision": 2,
            "character_key": "character_1",
        })
        ready_character = dict(
            character, reference_file="image/character.png",
            reference_url="/api/gen/file/image/character.png",
            reference_version=1,
        )
        locked_character = dict(ready_character, reference_locked=True)
        final_calls = []

        def finish(path, _token, _payload, _run_id, _step):
            final_calls.append(path)
            if path.endswith("/confirm-character-reference"):
                return {"id": "project-character", "revision": 4,
                        "characters": [locked_character]}
            return {"deleted": True}

        job_evidence = {
            "status": "done", "provider_task_id": None,
            "route_provider": "banana", "completed": True,
            "delivery_verified": True, "billing_state": "charged",
            "artifact_check": "decodable", "error": "",
        }
        with patch.object(self.admin, "_e2e_job_evidence", return_value=job_evidence), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={
                 "id": "project-character", "revision": 3,
                 "characters": [ready_character],
             }), \
             patch.object(self.admin, "_short_drama_e2e_request", side_effect=finish):
            self.assertTrue(self.admin._finalize_short_drama_character_e2e(
                run["run_id"], "admin-token"
            ))
        self.assertEqual(final_calls, [
            "/api/gen/short-drama/confirm-character-reference",
            "/api/gen/short-drama/project/delete",
        ])
        with patch.object(self.admin, "_e2e_job_evidence", return_value=job_evidence), \
             patch.object(self.admin, "points_domain", SimpleNamespace(
                 get_points_transaction=lambda _key: {
                     "username": "qa-dedicated", "delta": -35, "after_points": 465,
                 }
             )):
            finished = self.admin.list_e2e_runs(1)[0]
        self.assertEqual(finished["status"], "completed")
        self.assertTrue(all(stage["state"] == "passed" for stage in finished["stages"]))
        self.assertTrue(finished["evidence"]["reference_locked"])
        self.assertTrue(finished["evidence"]["project_cleaned"])

    def test_short_drama_script_planning_runs_real_project_journey_and_cleans_up(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        responses = {
            "/api/gen/short-drama/projects/import": {"id": "project-qa", "revision": 1},
            "/api/gen/short-drama/conversation/messages": {
                "conversation": {"revision": 2, "understanding": {"direction_confirmed": True}},
            },
            "/api/gen/short-drama/conversation/script/generate": {
                "conversation": {"revision": 3}, "current_script": {"id": "script-qa"},
            },
            "/api/gen/short-drama/conversation/script/lock": {
                "conversation": {"revision": 4, "state": "script_locked"},
                "current_script": {"id": "script-qa", "status": "locked"},
            },
            "/api/gen/short-drama/preflight/generate": {
                "state": "ready_for_confirmation", "current_plan": {
                    "id": "plan-qa", "version": 1,
                    "plan": {"ready": True, "required_acceptance": ["recommended_assets"]},
                },
            },
            "/api/gen/short-drama/preflight/confirm": {
                "state": "confirmed", "current_plan": {"status": "confirmed"},
            },
            "/api/gen/short-drama/projects/live-action/abandon": {"deleted": True},
        }
        calls = []

        def request(path, _token, _payload, idem, cost, require_job_id=True):
            calls.append((path, idem, cost, require_job_id))
            return responses[path]

        with patch.object(self.admin, "auth_admin_request", side_effect=[session, session]) as auth, \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request", side_effect=request):
            run = self.admin.start_e2e_run(
                "root", "admin-token", "short_drama.live_action.script_planning"
            )
        self.assertEqual((run["status"], run["acceptance_id"], run["cost"]),
                         ("completed", "project-qa", 0))
        self.assertTrue(all(stage["state"] == "passed" for stage in run["stages"]))
        self.assertEqual([item[0] for item in calls], list(responses))
        self.assertTrue(all(item[2:] == (0, False) for item in calls))
        self.assertEqual(len({item[1] for item in calls}), len(calls))
        self.assertEqual(auth.call_count, 2)
        self.assertNotIn("short-lived-secret", json.dumps(run))
        html = (Path(__file__).resolve().parents[1] / "site/admin/index.html").read_text(encoding="utf-8")
        self.assertIn("确认使用专用测试账号；本次 0 点，只运行一次", html)

    def test_short_drama_project_is_cleaned_when_a_middle_step_fails(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        paths = []

        def request(path, _token, _payload, _idem, _cost, require_job_id=True):
            paths.append(path)
            if path.endswith("/projects/import"):
                return {"id": "project-failed", "revision": 1}
            if path.endswith("/conversation/messages"):
                raise self.admin.E2ESubmitRejected("方向确认失败")
            if path.endswith("/projects/live-action/abandon"):
                raise self.admin.E2ESubmitRejected("临时项目清理失败")
            self.fail("unexpected request " + path)

        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request", side_effect=request):
            with self.assertRaisesRegex(Exception, "方向确认失败.*临时项目清理失败"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "short_drama.live_action.script_planning"
                )
        self.assertEqual(paths[-1], "/api/gen/short-drama/projects/live-action/abandon")
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            row = connection.execute(
                "SELECT status,acceptance_id,evidence_json,error FROM admin_e2e_runs"
            ).fetchone()
        status, acceptance_id, evidence_json, error = row
        self.assertEqual(status, "failed")
        self.assertEqual(acceptance_id, "project-failed")
        self.assertIn("临时项目清理失败", json.loads(evidence_json)["cleanup_error"])
        self.assertIn("临时项目清理失败", error)

    def test_short_drama_zero_point_journey_rechecks_balance_before_passing(self):
        before = {"token": "qa-token", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        after = {"token": "qa-token-2", "account": {
            "username": "qa-dedicated", "points": 499, "membership_active": True,
        }}
        responses = iter([
            {"id": "project-billing", "revision": 1},
            {"conversation": {"revision": 2, "understanding": {"direction_confirmed": True}}},
            {"conversation": {"revision": 3}, "current_script": {"id": "script-billing"}},
            {"conversation": {"revision": 4, "state": "script_locked"}},
            {"state": "ready_for_confirmation", "current_plan": {
                "id": "plan-billing", "version": 1,
                "plan": {"ready": True, "required_acceptance": []},
            }},
            {"state": "confirmed", "current_plan": {"status": "confirmed"}},
            {"deleted": True},
        ])
        with patch.object(self.admin, "auth_admin_request", side_effect=[before, after]), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request",
                          side_effect=lambda *_args, **_kwargs: next(responses)):
            with self.assertRaisesRegex(ValueError, "点数变化"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "short_drama.live_action.script_planning"
                )
        run = self.admin.list_e2e_runs(1)[0]
        self.assertEqual((run["status"], run["points_before"], run["points_after"]),
                         ("failed", 500, 499))
        self.assertEqual(next(stage for stage in run["stages"] if stage["key"] == "billing")["state"],
                         "failed")

    def test_cleaned_uncertain_short_drama_step_becomes_failed_not_global_unknown(self):
        session = {"token": "qa-token", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        message_attempts = 0

        def request(path, _token, _payload, _idem, _cost, require_job_id=True):
            nonlocal message_attempts
            if path.endswith("/projects/import"):
                return {"id": "project-uncertain", "revision": 1}
            if path.endswith("/conversation/messages"):
                message_attempts += 1
                raise self.admin.E2ESubmitUncertain("response lost")
            if path.endswith("/projects/live-action/abandon"):
                return {"deleted": True}
            self.fail("unexpected request " + path)

        with patch.object(self.admin, "auth_admin_request", side_effect=[session, session]), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request", side_effect=request):
            with self.assertRaises(self.admin.E2ESubmitRejected):
                self.admin.start_e2e_run(
                    "root", "admin-token", "short_drama.live_action.script_planning"
                )
        self.assertEqual(message_attempts, 2)
        run = self.admin.list_e2e_runs(1)[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["acceptance_id"], "project-uncertain")
        self.assertEqual(run["evidence"]["delivery_verified"], False)

    def test_uncertain_import_stays_recoverable_when_balance_refresh_also_fails(self):
        session = {"token": "qa-token", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request",
                          side_effect=[session, RuntimeError("auth down")]), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request",
                          side_effect=self.admin.E2ESubmitUncertain("import response lost")):
            with self.assertRaisesRegex(RuntimeError, "结果未知"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "short_drama.live_action.script_planning"
                )
        self.assertEqual(self.admin.list_e2e_runs(1)[0]["status"], "unknown")

    def test_uncertain_cleanup_stays_recoverable_after_definite_step_failure(self):
        session = {"token": "qa-token", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}

        def request(path, _token, _payload, _idem, _cost, require_job_id=True):
            if path.endswith("/projects/import"):
                return {"id": "project-cleanup-unknown", "revision": 1}
            if path.endswith("/conversation/messages"):
                raise self.admin.E2ESubmitRejected("direction rejected")
            if path.endswith("/projects/live-action/abandon"):
                raise self.admin.E2ESubmitUncertain("cleanup response lost")
            self.fail("unexpected request " + path)

        with patch.object(self.admin, "auth_admin_request", side_effect=[session, session]), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request", side_effect=request):
            with self.assertRaisesRegex(RuntimeError, "结果未知"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "short_drama.live_action.script_planning"
                )
        run = self.admin.list_e2e_runs(1)[0]
        self.assertEqual((run["status"], run["acceptance_id"]),
                         ("unknown", "project-cleanup-unknown"))

    def test_admin_restart_marks_running_short_drama_for_idempotent_recovery(self):
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute(
                """INSERT INTO admin_e2e_runs(
                       run_id,operation_id,username,status,acceptance_id,evidence_json,
                       created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("restart-run", "short_drama.live_action.script_planning", "qa-dedicated",
                 "running", "project-restart", '{"imported":true}', "root", 1, 2),
            )
            connection.commit()
        with patch.object(self.admin, "feature_flags", None), \
             patch.object(self.admin, "provider_keys", None), \
             patch.object(self.admin.pricing, "init_db"), \
             patch.object(self.admin.inspiration_cases, "init_db"), \
             patch.object(self.admin, "short_drama_lipsync_rollout", None), \
             patch.object(self.admin, "short_drama_lipsync_observability", None):
            self.admin.init_db()
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            status, error = connection.execute(
                "SELECT status,error FROM admin_e2e_runs WHERE run_id='restart-run'"
            ).fetchone()
        self.assertEqual(status, "unknown")
        self.assertIn("按原幂等键恢复并清理", error)

    def test_admin_restart_recovers_unknown_import_by_original_idempotency_key(self):
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute(
                """INSERT INTO admin_e2e_runs(
                       run_id,operation_id,username,status,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("restart-unknown", "short_drama.live_action.script_planning",
                 "qa-dedicated", "running", "root", 1, 2),
            )
            connection.commit()
        with patch.object(self.admin, "feature_flags", None), \
             patch.object(self.admin, "provider_keys", None), \
             patch.object(self.admin.pricing, "init_db"), \
             patch.object(self.admin.inspiration_cases, "init_db"), \
             patch.object(self.admin, "short_drama_lipsync_rollout", None), \
             patch.object(self.admin, "short_drama_lipsync_observability", None):
            self.admin.init_db()
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM admin_e2e_runs WHERE run_id='restart-unknown'"
                ).fetchone()[0],
                "unknown",
            )
        session = {"token": "qa-token", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        calls = []

        responses = {
            "/api/gen/short-drama/projects/import": {"id": "project-recovered", "revision": 1},
            "/api/gen/short-drama/conversation/messages": {
                "conversation": {"revision": 2, "understanding": {"direction_confirmed": True}},
            },
            "/api/gen/short-drama/conversation/script/generate": {
                "conversation": {"revision": 3}, "current_script": {"id": "script-recovered"},
            },
            "/api/gen/short-drama/conversation/script/lock": {
                "conversation": {"revision": 4, "state": "script_locked"},
            },
            "/api/gen/short-drama/preflight/generate": {
                "state": "ready_for_confirmation", "current_plan": {
                    "id": "plan-recovered", "version": 1,
                    "plan": {"ready": True, "required_acceptance": []},
                },
            },
            "/api/gen/short-drama/preflight/confirm": {
                "state": "confirmed", "current_plan": {"status": "confirmed"},
            },
            "/api/gen/short-drama/projects/live-action/abandon": {"deleted": True},
        }

        def request(path, _token, _payload, idem, _cost, require_job_id=True):
            calls.append((path, idem))
            return responses[path]

        with patch.object(self.admin, "auth_admin_request",
                          side_effect=[session, session, session]), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_content_e2e_request", side_effect=request):
            preflight = self.admin.e2e_preflight(
                "admin-token", "short_drama.live_action.script_planning"
            )
        self.assertTrue(preflight["ready"])
        self.assertEqual([path for path, _key in calls], list(responses))
        self.assertTrue(all(key.startswith("e2e:restart-unknown:") for _path, key in calls))
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            status, acceptance_id = connection.execute(
                "SELECT status,acceptance_id FROM admin_e2e_runs WHERE run_id='restart-unknown'"
            ).fetchone()
        self.assertEqual((status, acceptance_id), ("completed", "project-recovered"))

    def test_copy_and_canvas_structured_delivery_are_verified(self):
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute(
                """CREATE TABLE assets(
                       id INTEGER PRIMARY KEY,job_id INTEGER,kind TEXT,stage TEXT,
                       deleted INTEGER DEFAULT 0)"""
            )
            connection.execute(
                "INSERT INTO assets(job_id,kind,stage,deleted) VALUES(12,'copy','work',0)"
            )
            connection.commit()
        copy = self.admin._structured_asset_evidence({
            "id": 12, "kind": "copy", "collect_mode": "",
            "result_json": json.dumps({"type": "copy", "scenes": [{"scene": "产品特写"}]}),
        })
        self.assertTrue(copy["delivery_verified"])
        self.assertEqual(copy["artifact_check"], "structured_asset")
        canvas = self.admin._structured_asset_evidence({
            "id": 13, "kind": "canvas_agent", "collect_mode": "",
            "result_json": json.dumps({
                "type": "canvas_agent", "content": "计划已生成",
                "plan": {"content": "计划已生成", "requires_confirmation": True, "actions": [
                    {"type": "create_generation_draft", "mode": "text"},
                    {"type": "create_generation_draft", "mode": "image"},
                ]},
            }),
        })
        self.assertTrue(canvas["delivery_verified"])
        self.assertEqual(canvas["artifact_check"], "structured_result")
        incomplete = self.admin._structured_asset_evidence({
            "id": 14, "kind": "canvas_agent", "collect_mode": "",
            "result_json": json.dumps({
                "type": "canvas_agent", "content": "只返回了文案草稿",
                "plan": {"content": "只返回了文案草稿", "requires_confirmation": True,
                         "actions": [{"type": "create_generation_draft", "mode": "text"}]},
            }),
        })
        self.assertFalse(incomplete["delivery_verified"])
        for result in (
            {"type": "copy", "scenes": []},
            {"type": "copy", "scenes": "不是分镜数组"},
        ):
            invalid_copy = self.admin._structured_asset_evidence({
                "id": 12, "kind": "copy", "collect_mode": "",
                "result_json": json.dumps(result),
            })
            self.assertFalse(invalid_copy["delivery_verified"])
        missing_asset = self.admin._structured_asset_evidence({
            "id": 99, "kind": "copy", "collect_mode": "",
            "result_json": json.dumps({"type": "copy", "scenes": [{"scene": "产品特写"}]}),
        })
        self.assertFalse(missing_asset["delivery_verified"])
        for plan in (
            {"content": "计划", "requires_confirmation": False, "actions": [
                {"type": "create_generation_draft", "mode": "text"},
                {"type": "create_generation_draft", "mode": "image"},
            ]},
            {"content": "", "requires_confirmation": True, "actions": [
                {"type": "create_generation_draft", "mode": "text"},
                {"type": "create_generation_draft", "mode": "image"},
            ]},
            {"content": "计划", "requires_confirmation": True, "actions": [
                {"type": "run_generation", "mode": "text"},
                {"type": "run_generation", "mode": "image"},
            ]},
            {"content": "计划", "requires_confirmation": True, "actions": ["text", "image"]},
        ):
            invalid_canvas = self.admin._structured_asset_evidence({
                "id": 13, "kind": "canvas_agent", "collect_mode": "",
                "result_json": json.dumps({"type": "canvas_agent", "content": plan["content"], "plan": plan}),
            })
            self.assertFalse(invalid_canvas["delivery_verified"])

    def test_breakdown_delivery_must_match_the_requested_mode(self):
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute(
                """CREATE TABLE assets(
                       id INTEGER PRIMARY KEY,job_id INTEGER,kind TEXT,stage TEXT,
                       deleted INTEGER DEFAULT 0)"""
            )
            connection.execute(
                "INSERT INTO assets(job_id,kind,stage,deleted) VALUES(21,'breakdown','work',0)"
            )
            connection.commit()
        wrong = self.admin._structured_asset_evidence({
            "id": 21, "kind": "breakdown", "collect_mode": "", "request_mode": "reverse_prompt",
            "result_json": json.dumps({"type": "breakdown", "scenes": [{"scene": "错误模式"}]}),
        })
        self.assertFalse(wrong["delivery_verified"])
        right = self.admin._structured_asset_evidence({
            "id": 21, "kind": "breakdown", "collect_mode": "", "request_mode": "reverse_prompt",
            "result_json": json.dumps({"type": "breakdown_reverse", "prompt": "镜头缓慢推进"}),
        })
        self.assertTrue(right["delivery_verified"])
        scenes = self.admin._structured_asset_evidence({
            "id": 21, "kind": "breakdown", "collect_mode": "", "request_mode": "scenes",
            "result_json": json.dumps({"type": "breakdown", "scenes": [{"scene": "开场产品特写"}]}),
        })
        self.assertTrue(scenes["delivery_verified"])
        for request_mode, result in (
            ("reverse_prompt", {"type": "breakdown_reverse", "prompt": {"bad": True}}),
            ("scenes", {"type": "breakdown", "scenes": "不是分镜数组"}),
        ):
            invalid = self.admin._structured_asset_evidence({
                "id": 21, "kind": "breakdown", "collect_mode": "",
                "request_mode": request_mode, "result_json": json.dumps(result),
            })
            self.assertFalse(invalid["delivery_verified"])

    def test_script_and_canvas_jobs_complete_the_same_eight_stage_contract(self):
        jobs = [
            (91, "copy", 4, {"provider": "copy_model", "source_page": "script"},
             {"type": "copy", "scenes": [{"scene": "产品特写"}]}, "script.write.spoken", True),
            (92, "breakdown", 20, {"provider": "tikhub+zhipu", "source_page": "script", "mode": "scenes"},
             {"type": "breakdown", "scenes": [{"scene": "三秒开场"}]}, "script.breakdown.scenes", True),
            (93, "canvas_agent", 3, {"provider": "openai_responses", "source_page": "canvas"},
             {"type": "canvas_agent", "content": "计划已生成", "plan": {
                 "content": "计划已生成", "requires_confirmation": True, "actions": [
                     {"type": "create_generation_draft", "mode": "text"},
                     {"type": "create_generation_draft", "mode": "image"},
                 ]}}, "canvas.agent.plan", True),
            (97, "breakdown", 20, {"provider": "local+zhipu", "source_page": "script",
                                    "source_type": "image", "mode": "reverse_prompt"},
             {"type": "breakdown_reverse", "prompt": "暖色产品静物特写"},
             "script.breakdown.local_image", True),
            (94, "copy", 4, {"provider": "copy_model", "source_page": "script"},
             {"type": "copy", "scenes": [{"scene": "未入资产库"}]}, "script.write.spoken", False),
            (95, "breakdown", 20, {"provider": "tikhub+zhipu", "source_page": "script", "mode": "scenes"},
             {"type": "breakdown_reverse", "prompt": "结果类型错误"}, "script.breakdown.scenes", False),
            (96, "canvas_agent", 3, {"provider": "openai_responses", "source_page": "canvas"},
             {"type": "canvas_agent", "content": "未等待确认", "plan": {
                 "content": "未等待确认", "requires_confirmation": False, "actions": [
                     {"type": "create_generation_draft", "mode": "text"},
                     {"type": "create_generation_draft", "mode": "image"},
                 ]}}, "canvas.agent.plan", False),
        ]
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,payload TEXT,result TEXT)""")
            connection.executemany(
                "INSERT INTO jobs VALUES(?,?, 'done', ?,0,'',1,2,?,?)",
                [(job_id, kind, cost, json.dumps(payload), json.dumps(result))
                 for job_id, kind, cost, payload, result, _operation, _valid in jobs],
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute("""CREATE TABLE assets(
                id INTEGER PRIMARY KEY,job_id INTEGER,kind TEXT,stage TEXT,deleted INTEGER DEFAULT 0)""")
            connection.executemany(
                "INSERT INTO assets VALUES(?,?,?,'work',0)",
                [(1, 91, "copy"), (2, 92, "breakdown"), (3, 97, "breakdown")],
            )
            connection.commit()
        ledgers = {}
        rows = []
        for job_id, _kind, cost, _payload, _result, operation, _valid in jobs:
            key = "ledger-%s" % job_id
            ledgers[key] = {"username": "qa-dedicated", "delta": -cost, "after_points": 100 - cost}
            rows.append({
                "run_id": "run-%s" % job_id, "operation_id": operation,
                "username": "qa-dedicated", "status": "completed", "job_id": job_id,
                "cost": cost, "points_before": 100, "points_after": 100 - cost,
                "transaction_key": key, "error": "", "created_by": "root",
                "created_at": 1, "updated_at": 2,
            })
        with patch.object(self.admin, "points_domain", SimpleNamespace(
                get_points_transaction=lambda key: ledgers.get(key))):
            runs = [self.admin._public_e2e_run(row) for row in rows]
        for run, job in zip(runs, jobs):
            expected_delivery = job[-1]
            delivery = next(stage for stage in run["stages"] if stage["key"] == "delivery")
            self.assertEqual(delivery["state"], "passed" if expected_delivery else "failed", run)
            self.assertTrue(all(
                stage["state"] == "passed" for stage in run["stages"] if stage["key"] != "delivery"
            ), run)
            self.assertEqual(run["evidence"]["delivery_verified"], expected_delivery)
            self.assertTrue(run["evidence"]["route_provider"])

    def test_cinematic_open_uses_one_ready_qa_avatar_and_four_second_quote(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": [
                 {"id": 41, "status": "ready"}, {"id": 42, "status": "ready"},
             ]}), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 120)), \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 88, "cost": 120, "points_left": 380,
             }) as submit:
            run = self.admin.start_e2e_run(
                "root", "admin-token", "video.cinematic.open"
            )
        payload = submit.call_args.args[2]
        self.assertEqual(submit.call_args.args[:2], ("/api/gen/cinematic", "short-lived-secret"))
        self.assertEqual(payload["avatar_ids"], [41])
        self.assertEqual(payload["cine_mode"], "open")
        self.assertEqual(payload["duration"], 4)
        self.assertEqual(run["job_id"], 88)
        self.assertNotIn("short-lived-secret", json.dumps(run))

    def test_cinematic_open_fails_before_submission_without_ready_avatar(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": []}), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 120)), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            with self.assertRaisesRegex(ValueError, "尚未登记已就绪"):
                self.admin.start_e2e_run(
                    "root", "admin-token", "video.cinematic.open"
                )
        submit.assert_not_called()

    def test_preflight_quotes_fixture_without_creating_paid_task_or_exposing_bytes(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 30)), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            result = self.admin.e2e_preflight(
                "admin-token", "video.digital_ip.text.single"
            )
        self.assertTrue(result["ready"])
        self.assertEqual((result["cost"], result["points"]), (30, 500))
        self.assertIn("清晰度：720p", result["parameters"])
        self.assertNotIn("short-lived-secret", json.dumps(result))
        self.assertNotIn("data:image", json.dumps(result))
        submit.assert_not_called()

    def test_preflight_blocks_every_mode_while_another_journey_is_running(self):
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute(
                """INSERT INTO admin_e2e_runs(
                       run_id,operation_id,username,status,job_id,cost,points_before,
                       points_after,transaction_key,error,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("active-run", "video.omni.text", "qa-dedicated", "running", 77,
                 30, 500, 470, "ledger-77", "", "root", 1, 2),
            )
            connection.commit()
        with patch.object(self.admin, "_e2e_job_evidence", return_value={
                "status": "running", "provider_task_id": "provider-77",
                "completed": False, "delivery_verified": False,
                "billing_state": "in_flight", "artifact_check": "not_recorded",
                "error": "",
        }):
            result = self.admin.e2e_preflight("admin-token", "video.grok.text")
        self.assertFalse(result["ready"])
        self.assertIn("另一条生产链测试", result["blocker"])

    def test_grok_image_uses_matching_product_fixture_and_supported_resolution(self):
        runner = self.admin.function_registry.e2e_runner("video.grok.image")
        payload = self.admin._e2e_payload("video.grok.image", runner)
        self.assertEqual(payload["resolution"], "720p")
        self.assertEqual(len(payload["reference_images"]), 1)
        self.assertTrue(payload["reference_images"][0].startswith("data:image/png;base64,"))
        self.assertIn("精华瓶", payload["prompt"])

    def test_image_page_exposes_exactly_thirteen_low_cost_private_runners(self):
        page = next(item for item in self.admin.function_registry.list_pages()
                    if item["key"] == "banana")
        modes = [mode for feature in page["functions"] for mode in feature["modes"]]
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        def image_cost(_kind, payload):
            provider = payload.get("provider") or "openai"
            quality = "hd" if payload.get("quality") == "hd" else "std"
            if provider == "banana":
                key = "image.banana.%s.%s" % (payload["model"], quality)
            elif provider == "seedream":
                key = "image.seedream.%s.%s" % (payload["variant"], quality)
            else:
                key = "image.%s.%s" % (provider, quality)
            return self.admin.pricing.get_price(key)
        with patch.object(self.admin, "list_e2e_runs", return_value=[]), \
             patch.object(self.admin, "_e2e_page_modes", return_value=modes), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin.feature_flags, "is_enabled", return_value=True), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=image_cost)), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            result = self.admin.e2e_batch_preflight("admin-token", "banana")
        self.assertTrue(result["ready"])
        self.assertEqual((result["target_count"], result["ready_count"]), (13, 13))
        self.assertEqual(result["total_cost"], 236)
        self.assertEqual(len(page["auxiliary_actions"]), 2)
        submit.assert_not_called()

        payloads = {
            mode["key"]: self.admin._e2e_payload(
                mode["key"], self.admin.function_registry.e2e_runner(mode["key"])
            ) for mode in modes
        }
        self.assertTrue(all(item["source_page"] == "banana" for item in payloads.values()))
        self.assertTrue(all(item["provider"] == operation.split(".")[1]
                            for operation, item in payloads.items()))
        self.assertTrue(all(self.admin._e2e_kind(
            self.admin.function_registry.e2e_runner(operation)["endpoint"]["path"]
        ) == "image" for operation in payloads))
        self.assertEqual(payloads["image.banana.pro.text"]["model"], "pro")
        self.assertEqual(payloads["image.seedream.pro.reference"]["variant"], "pro")
        self.assertEqual(len(payloads["image.xiaole.reference"]["reference_images"]), 1)
        for operation, item in payloads.items():
            if operation.endswith(".reference"):
                self.assertEqual(len(item["reference_images"]), 1)
            elif operation != "image.openai.inpaint":
                self.assertNotIn("reference_images", item)
        self.assertIn("mask", payloads["image.openai.inpaint"])
        self.assertIn("image", payloads["image.openai.inpaint"])

    def test_disabled_image_channel_is_excluded_from_one_click_batch(self):
        page = {"key": "banana", "inventory_status": "verified", "functions": [
            {"key": "openai", "runtime_visible": True, "modes": [
                {"key": "image.openai.text", "validation": {"supported": True}},
            ]},
            {"key": "xiaole", "runtime_visible": True,
             "flag_keys": ["image_xiaole"], "modes": [
                {"key": "image.xiaole.text", "validation": {"supported": True}},
                {"key": "image.xiaole.reference", "validation": {"supported": True}},
             ]},
        ]}
        with patch.object(self.admin, "load_function_registry", return_value=[page]), \
             patch.object(self.admin, "service_status", return_value=[]), \
             patch.object(self.admin.feature_flags, "is_enabled",
                          side_effect=lambda key: key != "image_xiaole"):
            modes = self.admin._e2e_page_modes("banana")
        self.assertEqual([mode["key"] for mode in modes], ["image.openai.text"])

    def test_disabled_mode_cannot_be_submitted_individually(self):
        runner = {
            "supported": True, "flag_keys": ["image_xiaole"],
            "endpoint": {"method": "POST", "path": "/api/gen/image"},
        }
        with patch.object(self.admin.function_registry, "e2e_runner", return_value=runner), \
             patch.object(self.admin.feature_flags, "is_enabled", return_value=False), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            with self.assertRaisesRegex(ValueError, "暂停接单"):
                self.admin._e2e_prepare_operation(
                    {"token": "short-lived-secret", "account": {}},
                    "image.xiaole.text",
                )
        submit.assert_not_called()

    def test_image_batch_is_all_or_nothing(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        modes = [
            {"key": "image.openai.text", "validation": {"supported": True}},
            {"key": "image.openai.reference", "validation": {"supported": True}},
        ]
        with patch.object(self.admin, "list_e2e_runs", return_value=[]), \
             patch.object(self.admin, "_e2e_page_modes", return_value=modes), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_e2e_prepare_operation", side_effect=[
                 {"cost": 20}, ValueError("测试素材未部署"),
             ]):
            result = self.admin.e2e_batch_preflight("admin-token", "banana")
        self.assertFalse(result["ready"])
        self.assertIn("图片完整旅程", result["blocker"])
        self.assertEqual((result["ready_count"], result["blocked_count"]), (1, 1))

    def test_banana_submit_uses_dedicated_imggen_service(self):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "job_id": 7, "cost": 18, "points_left": 482,
        }).encode()
        response.__enter__.return_value = response
        with patch.object(self.admin.urllib.request, "urlopen", return_value=response) as urlopen, \
             patch.object(self.admin, "IMGGEN_BASE", "http://imggen.test"), \
             patch.object(self.admin, "CONTENT_BASE", "http://content.test"):
            self.admin._content_e2e_request(
                "/api/gen/banana", "account-token", {"prompt": "qa"}, "e2e:image", 18
            )
            self.admin._content_e2e_request(
                "/api/gen/image", "account-token", {"prompt": "qa"}, "e2e:image2", 20
            )
        banana_request, image_request = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(banana_request.full_url, "http://imggen.test/api/gen/banana")
        self.assertEqual(image_request.full_url, "http://content.test/api/gen/image")
        self.assertEqual(banana_request.headers["Idempotency-key"], "e2e:image")
        self.assertEqual(banana_request.headers["X-hq-expected-cost"], "18")

    def test_cinematic_motion_uses_private_video_and_server_quote(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value={"items": [
                 {"id": 41, "status": "ready"},
             ]}), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 90)), \
             patch.object(self.admin, "_content_e2e_post", return_value={"cost": 90}) as quote, \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 89, "cost": 90, "points_left": 410,
             }) as submit:
            ready = self.admin.e2e_preflight("admin-token", "video.cinematic.motion")
            run = self.admin.start_e2e_run("root", "admin-token", "video.cinematic.motion")
        self.assertTrue(ready["ready"])
        self.assertIn("时长：随参考视频", ready["parameters"])
        self.assertEqual(quote.call_args.args[0], "/api/gen/cinematic/quote")
        payload = submit.call_args.args[2]
        self.assertEqual(payload["cine_mode"], "motion")
        self.assertEqual(payload["avatar_ids"], [41])
        self.assertTrue(payload["reference_video_data"].startswith("data:video/"))
        self.assertEqual(run["cost"], 90)

    def test_audio_public_runner_resolves_voice_server_side_and_quotes_ten_points(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        voices = {"items": [{
            "id": 1, "scope": "public", "voice_key": "S_public",
            "provider_voice": "longwan",
        }]}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", return_value=voices), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 10)), \
             patch.object(self.admin, "_content_e2e_request", return_value={
                 "job_id": 101, "cost": 10, "points_left": 490,
             }) as submit:
            ready = self.admin.e2e_preflight("admin-token", "audio.tts.public")
            run = self.admin.start_e2e_run("root", "admin-token", "audio.tts.public")
        payload = submit.call_args.args[2]
        self.assertEqual(submit.call_args.args[:2], ("/api/gen/audio", "short-lived-secret"))
        self.assertEqual(payload["voice"], "S_public")
        self.assertEqual(payload["source_page"], "audio")
        self.assertEqual((payload["speed"], payload["pitch"], payload["volume"]), (1.0, 0, 0))
        self.assertEqual((ready["cost"], run["cost"]), (10, 10))
        self.assertNotIn("S_public", json.dumps(ready))
        self.assertNotIn("S_public", json.dumps(run))

    def test_audio_personal_runner_requires_ready_owned_cosyvoice(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_content_e2e_get", side_effect=[
                 {"items": []}, {"items": [{"status": "active", "voice_id": None}]},
             ]), \
             patch.object(self.admin, "points_domain", SimpleNamespace(cost_of=lambda kind, payload: 10)), \
             patch.object(self.admin, "_content_e2e_request") as submit:
            with self.assertRaisesRegex(ValueError, "个人测试音色尚未准备"):
                self.admin.start_e2e_run("root", "admin-token", "audio.tts.personal")
        submit.assert_not_called()

    def test_audio_private_fixture_bootstrap_is_idempotent_and_sanitized(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        with patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "_ready_audio_voice_key", side_effect=ValueError("missing")), \
             patch.object(self.admin, "_content_e2e_get", side_effect=[
                 {"items": [{"slot_id": "private-slot", "status": "active"}]},
                 {"items": [{"slot_id": "private-slot", "status": "training"}]},
             ]), \
             patch.object(self.admin, "_fixture_data_url", return_value="data:audio/mpeg;base64,PRIVATE"), \
             patch.object(self.admin, "_content_e2e_post", return_value={"ok": True}) as clone:
            result = self.admin.prepare_audio_e2e_personal_fixture("admin-token")
            repeated = self.admin.prepare_audio_e2e_personal_fixture("admin-token")
        self.assertEqual(result["state"], "training")
        self.assertEqual(repeated["state"], "training")
        self.assertNotIn("private-slot", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))
        clone_payload = clone.call_args.args[2]
        self.assertEqual(clone.call_args.args[:2], ("/api/gen/audio/clone-vip", "short-lived-secret"))
        self.assertEqual(clone_payload["slot_id"], "private-slot")
        self.assertTrue(clone_payload["audio"].startswith("data:audio/"))
        clone.assert_called_once()

    def test_audio_e2e_uses_sync_route_asset_decode_and_ledger_evidence(self):
        (self.admin.CONTENT_OUT / "result.mp3").write_bytes(b"audio")
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,payload TEXT,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(101,'audio','done',10,0,'',1,2,?,?)",
                (json.dumps({"provider": "cosyvoice", "voice_scope": "public"}),
                 json.dumps({"file": "result.mp3", "url": "/api/gen/file/audio/result.mp3"})),
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute("""CREATE TABLE audio_assets(
                id INTEGER PRIMARY KEY,job_id INTEGER,file TEXT,url TEXT,
                asset_kind TEXT,metadata_json TEXT,created_at INTEGER)""")
            connection.execute(
                "INSERT INTO audio_assets VALUES(1,101,'result.mp3','/api/gen/file/audio/result.mp3',"
                "'voice','{}',2)"
            )
            connection.commit()
        row = {
            "run_id": "audio-101", "operation_id": "audio.tts.public",
            "username": "qa-dedicated", "status": "completed", "job_id": 101,
            "cost": 10, "points_before": 500, "points_after": 490,
            "transaction_key": "ledger-audio-101", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -10, "after_points": 490}
        with patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"mp3\n")), \
             patch.object(self.admin, "points_domain", SimpleNamespace(get_points_transaction=lambda key: ledger)):
            run = self.admin._public_e2e_run(row)
        stages = {stage["key"]: stage for stage in run["stages"]}
        self.assertTrue(all(stage["state"] == "passed" for stage in run["stages"]))
        self.assertEqual(stages["route"]["detail"], "任务已选择 CosyVoice 音频线路")
        self.assertEqual(stages["provider"]["name"], "同步生产协议")
        self.assertIn("不适用", stages["provider"]["detail"])
        self.assertEqual(stages["delivery"]["detail"], "文件存在且可解码")
        self.assertEqual(stages["billing"]["detail"], "扣点流水一致")

    def test_image_e2e_joins_provider_output_decode_and_ledger_evidence(self):
        image_bytes = (Path(__file__).resolve().parents[1] / "server/qa_fixtures/qa-serum.png").read_bytes()
        (self.admin.CONTENT_OUT / "result.png").write_bytes(image_bytes)
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,payload TEXT,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(201,'image','done',12,0,'',1,2,?,?)",
                (json.dumps({"source_page": "banana", "provider": "xiaole"}),
                 json.dumps({"files": ["result.png"], "urls": ["/api/gen/file/result.png"],
                             "provider_task_id": "xiaole-201"})),
            )
            connection.commit()
        row = {
            "run_id": "image-201", "operation_id": "image.xiaole.text",
            "username": "qa-dedicated", "status": "completed", "job_id": 201,
            "cost": 12, "points_before": 500, "points_after": 488,
            "transaction_key": "ledger-image-201", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -12, "after_points": 488}
        with patch.object(self.admin, "points_domain", SimpleNamespace(get_points_transaction=lambda key: ledger)):
            run = self.admin._public_e2e_run(row)
        stages = {stage["key"]: stage for stage in run["stages"]}
        self.assertTrue(all(stage["state"] == "passed" for stage in run["stages"]))
        self.assertIn("xiaole-201", stages["provider"]["detail"])
        self.assertEqual(stages["delivery"]["detail"], "文件存在且可解码")

    def test_image_placeholder_provider_id_cannot_pass_provider_stage(self):
        image_bytes = (Path(__file__).resolve().parents[1] / "server/qa_fixtures/qa-serum.png").read_bytes()
        (self.admin.CONTENT_OUT / "result.png").write_bytes(image_bytes)
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,payload TEXT,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(202,'image','done',12,0,'',1,2,?,?)",
                (json.dumps({"source_page": "banana", "provider": "xiaole"}),
                 json.dumps({"files": ["result.png"], "urls": ["/api/gen/file/result.png"],
                             "provider_task_id": "None"})),
            )
            connection.commit()
        row = {
            "run_id": "image-202", "operation_id": "image.xiaole.text",
            "username": "qa-dedicated", "status": "completed", "job_id": 202,
            "cost": 12, "points_before": 500, "points_after": 488,
            "transaction_key": "ledger-image-202", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -12, "after_points": 488}
        with patch.object(self.admin, "points_domain", SimpleNamespace(get_points_transaction=lambda key: ledger)):
            run = self.admin._public_e2e_run(row)
        stages = {stage["key"]: stage for stage in run["stages"]}
        self.assertNotEqual(stages["provider"]["state"], "passed")
        self.assertEqual(stages["provider"]["detail"], "尚无供应商任务编号")

    def test_corrupt_image_cannot_pass_delivery_stage(self):
        (self.admin.CONTENT_OUT / "broken.png").write_bytes(b"not-an-image")
        evidence = self.admin._verify_local_artifact({
            "result_file": "broken.png", "result_url": "/api/gen/file/broken.png",
            "delivery_verified": False, "artifact_check": "not_recorded",
        })
        self.assertFalse(evidence["delivery_verified"])
        self.assertEqual(evidence["artifact_check"], "decode_failed")

    def test_task_card_and_e2e_share_delivery_and_ledger_evidence(self):
        (self.admin.CONTENT_OUT / "result.mp4").write_bytes(b"video")
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(77,'done',30,0,'',1,2,?)",
                (json.dumps({"video_file": "result.mp4", "video_url": "https://cdn.example/result.mp4",
                             "provider_video_id": "provider-77"}),),
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute("""CREATE TABLE video_assets(
                job_id INTEGER,phase TEXT,provider_video_id TEXT,video_file TEXT,
                video_url TEXT,status TEXT,error TEXT,updated_at INTEGER)""")
            connection.execute(
                "INSERT INTO video_assets VALUES(77,'completed','provider-77','result.mp4',"
                "'https://cdn.example/result.mp4','done','',2)"
            )
            connection.commit()
        row = {
            "run_id": "run-77", "operation_id": "video.digital_ip.text.single",
            "username": "qa-dedicated", "status": "completed", "job_id": 77,
            "cost": 30, "points_before": 500, "points_after": 470,
            "transaction_key": "ledger-77", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -30, "after_points": 470}
        with patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"h264\n")), \
             patch.object(self.admin, "points_domain", SimpleNamespace(get_points_transaction=lambda key: ledger)):
            evidence = self.admin._e2e_job_evidence(77)
            run = self.admin._public_e2e_run(row)
        self.assertTrue(evidence["delivery_verified"])
        self.assertEqual(evidence["artifact_check"], "decodable")
        self.assertEqual(next(stage for stage in run["stages"] if stage["key"] == "delivery")["state"], "passed")
        self.assertEqual(next(stage for stage in run["stages"] if stage["key"] == "billing")["detail"], "扣点流水一致")
        self.assertNotIn("transaction_key", run)

    def test_collect_structured_result_and_asset_complete_all_eight_stages(self):
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,payload TEXT,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(81,'collect','done',3,0,'',1,2,?,?)",
                (json.dumps({"provider": "tikhub", "want": ["comments"]}),
                 json.dumps({"type": "collect", "video": {"title": "测试内容"},
                             "copy": {}, "comments": [{"text": "测试评论"}]})),
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute("""CREATE TABLE assets(
                id INTEGER PRIMARY KEY,job_id INTEGER,kind TEXT,stage TEXT,deleted INTEGER DEFAULT 0)""")
            connection.execute("INSERT INTO assets VALUES(9,81,'collect','material',0)")
            connection.commit()
        row = {
            "run_id": "run-81", "operation_id": "collect.content.comments",
            "username": "qa-dedicated", "status": "completed", "job_id": 81,
            "cost": 3, "points_before": 100, "points_after": 97,
            "transaction_key": "ledger-81", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -3, "after_points": 97}
        with patch.object(self.admin, "points_domain", SimpleNamespace(
                get_points_transaction=lambda key: ledger)):
            run = self.admin._public_e2e_run(row)
        self.assertEqual(run["evidence"]["artifact_check"], "structured_asset")
        self.assertEqual(run["evidence"]["route_provider"], "tikhub")
        self.assertTrue(all(stage["state"] == "passed" for stage in run["stages"]))
        self.assertEqual(
            next(stage for stage in run["stages"] if stage["key"] == "generation")["name"],
            "数据采集",
        )

    def test_collect_empty_comments_are_not_delivery_evidence(self):
        evidence = self.admin._structured_asset_evidence({
            "id": 82,
            "kind": "collect",
            "collect_mode": "comments",
            "result_json": json.dumps({
                "type": "collect", "video": {"title": "测试内容"},
                "copy": {}, "comments": [],
            }),
        })
        self.assertFalse(evidence["delivery_verified"])
        self.assertEqual(evidence["artifact_check"], "invalid_structured")

    def test_collect_video_checks_download_proxy_once_and_caches_result(self):
        response = MagicMock()
        response.headers = {"Content-Type": "video/mp4"}
        response.read.side_effect = [b"complete-video-payload", b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.object(self.admin, "AUTH_INTERNAL_TOKEN", "internal-test-token"), \
             patch.object(self.admin.urllib.request, "urlopen", return_value=response) as open_url, \
             patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=b"h264\n", stderr=b"")) as probe:
            first = self.admin._download_proxy_evidence(83, {
                "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
                "title": "测试视频",
            })
            second = self.admin._download_proxy_evidence(83, {
                "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
                "title": "测试视频",
            })
        self.assertEqual(first, second)
        self.assertTrue(first[0])
        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(probe.call_count, 1)
        request = open_url.call_args.args[0]
        self.assertIn("/api/gen/dl?", request.full_url)
        self.assertEqual(request.get_header("X-hq-internal-token"), "internal-test-token")

    def test_collect_video_concurrent_check_is_waiting_not_failed(self):
        entered = threading.Event()
        release = threading.Event()
        response = MagicMock()
        response.headers = {"Content-Type": "video/mp4"}

        def read(_size):
            if not entered.is_set():
                entered.set()
                release.wait(2)
                return b"complete-video-payload"
            return b""

        response.read.side_effect = read
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        first = []
        with patch.object(self.admin, "AUTH_INTERNAL_TOKEN", "internal-test-token"), \
             patch.object(self.admin.urllib.request, "urlopen", return_value=response) as open_url, \
             patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=b"h264\n", stderr=b"")):
            worker = threading.Thread(target=lambda: first.append(
                self.admin._download_proxy_evidence(87, {
                    "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
                })
            ))
            worker.start()
            self.assertTrue(entered.wait(2))
            pending = self.admin._download_proxy_evidence(87, {
                "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
            })
            release.set()
            worker.join(2)

        self.assertIsNone(pending[0])
        self.assertIn("正在", pending[1])
        self.assertTrue(first[0][0])
        self.assertEqual(open_url.call_count, 1)

        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute(
                "INSERT INTO admin_e2e_delivery_checks VALUES(88,'checking','正在验收',?)",
                (int(time.time()),),
            )
            connection.commit()
        evidence = self.admin._structured_asset_evidence({
            "id": 88,
            "kind": "collect",
            "collect_mode": "video",
            "result_json": json.dumps({"video": {"play_url": "https://example.com/video.mp4"}}),
        })
        self.assertEqual(evidence["artifact_check"], "checking")
        self.assertTrue(evidence["output_reference_present"])

        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,kind TEXT,status TEXT,cost INTEGER,refunded INTEGER,error TEXT,
                created_at INTEGER,updated_at INTEGER,payload TEXT,result TEXT)""")
            connection.execute(
                "INSERT INTO jobs VALUES(88,'collect','done',3,0,'',1,2,?,?)",
                (json.dumps({"provider": "tikhub", "want": ["video"]}),
                 json.dumps({"video": {"play_url": "https://example.com/video.mp4"}})),
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute("""CREATE TABLE assets(
                id INTEGER PRIMARY KEY,job_id INTEGER,kind TEXT,stage TEXT,deleted INTEGER DEFAULT 0)""")
            connection.execute("INSERT INTO assets VALUES(10,88,'collect','material',0)")
            connection.commit()
        row = {
            "run_id": "run-88", "operation_id": "collect.content.video",
            "username": "qa-dedicated", "status": "completed", "job_id": 88,
            "cost": 3, "points_before": 100, "points_after": 97,
            "transaction_key": "ledger-88", "error": "", "created_by": "root",
            "created_at": 1, "updated_at": 2,
        }
        ledger = {"username": "qa-dedicated", "delta": -3, "after_points": 97}
        with patch.object(self.admin, "points_domain", SimpleNamespace(
                get_points_transaction=lambda key: ledger)):
            run = self.admin._public_e2e_run(row)
        delivery = next(stage for stage in run["stages"] if stage["key"] == "delivery")
        self.assertEqual(delivery["state"], "waiting")

    def test_collect_video_retries_a_stale_failed_download_check(self):
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            connection.execute("""CREATE TABLE admin_e2e_delivery_checks(
                job_id INTEGER PRIMARY KEY,status TEXT,detail TEXT,updated_at INTEGER)""")
            connection.execute(
                "INSERT INTO admin_e2e_delivery_checks VALUES(84,'failed','temporary',?)",
                (int(time.time()) - 61,),
            )
            connection.commit()
        response = MagicMock()
        response.headers = {"Content-Type": "video/mp4"}
        response.read.side_effect = [b"complete-video-payload", b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.object(self.admin, "AUTH_INTERNAL_TOKEN", "internal-test-token"), \
             patch.object(self.admin.urllib.request, "urlopen", return_value=response) as open_url, \
             patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=b"h264\n", stderr=b"")):
            passed, _detail = self.admin._download_proxy_evidence(84, {
                "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
            })
        self.assertTrue(passed)
        self.assertEqual(open_url.call_count, 1)

    def test_collect_video_rejects_early_eof_even_when_probe_would_pass(self):
        response = MagicMock()
        response.headers = {"Content-Type": "video/mp4", "Content-Length": "100"}
        response.read.side_effect = [b"short", b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.object(self.admin, "AUTH_INTERNAL_TOKEN", "internal-test-token"), \
             patch.object(self.admin.urllib.request, "urlopen", return_value=response), \
             patch.object(self.admin.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=b"h264\n", stderr=b"")) as probe:
            passed, detail = self.admin._download_proxy_evidence(85, {
                "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
            })
        self.assertFalse(passed)
        self.assertIn("不完整", detail)
        probe.assert_not_called()

    def test_collect_video_stale_claim_cannot_overwrite_newer_check(self):
        response = MagicMock()
        response.headers = {"Content-Type": "video/mp4"}
        response.read.side_effect = [b"complete-video-payload", b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        def supersede_claim(*_args, **_kwargs):
            with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
                connection.execute(
                    "UPDATE admin_e2e_delivery_checks SET detail='new claim',updated_at=updated_at+1 WHERE job_id=86"
                )
                connection.commit()
            return SimpleNamespace(returncode=0, stdout=b"h264\n", stderr=b"")

        with patch.object(self.admin, "AUTH_INTERNAL_TOKEN", "internal-test-token"), \
             patch.object(self.admin.urllib.request, "urlopen", return_value=response), \
             patch.object(self.admin.subprocess, "run", side_effect=supersede_claim):
            passed, detail = self.admin._download_proxy_evidence(86, {
                "play_url": "https://video.huangquechuanmei.com/collect/test.mp4",
            })
        self.assertIsNone(passed)
        self.assertEqual(detail, "new claim")

    def test_batch_preflight_quotes_all_stale_prepared_modes_without_submitting(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        modes = [
            {"key": "video.grok.text", "validation": {"supported": True}},
            {"key": "video.grok.image", "validation": {"supported": True}},
            {"key": "video.sora.text", "validation": {"supported": True}},
            {"key": "video.unprepared", "validation": {"supported": False}},
        ]
        fresh = {"operation_id": "video.sora.text", "status": "completed",
                 "updated_at": int(time.time()), "stages": [{"state": "passed"}]}
        with patch.object(self.admin, "list_e2e_runs", return_value=[fresh]), \
             patch.object(self.admin, "_e2e_page_modes", return_value=modes), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_e2e_prepare_operation", side_effect=[
                 {"cost": 30}, {"cost": 40},
             ]) as prepare, \
             patch.object(self.admin, "_content_e2e_request") as submit:
            result = self.admin.e2e_batch_preflight("admin-token", "video")
        self.assertTrue(result["ready"])
        self.assertEqual((result["ready_count"], result["fresh_count"], result["unprepared_count"]), (2, 1, 1))
        self.assertEqual((result["total_cost"], result["points"]), (70, 500))
        self.assertEqual(prepare.call_count, 2)
        submit.assert_not_called()

    def test_batch_preflight_can_quote_fresh_modes_for_manual_rerun(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        modes = [{"key": "video.sora.text", "validation": {"supported": True}}]
        fresh = {"operation_id": "video.sora.text", "status": "completed",
                 "updated_at": int(time.time()), "stages": [{"state": "passed"}]}
        with patch.object(self.admin, "list_e2e_runs", return_value=[fresh]), \
             patch.object(self.admin, "_e2e_page_modes", return_value=modes), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_e2e_prepare_operation", return_value={"cost": 120}):
            result = self.admin.e2e_batch_preflight(
                "admin-token", "video", include_fresh=True
            )
        self.assertTrue(result["ready"])
        self.assertTrue(result["include_fresh"])
        self.assertEqual((result["ready_count"], result["fresh_count"], result["total_cost"]), (1, 1, 120))
        html = (Path(__file__).resolve().parents[1] / "site/admin/index.html").read_text(encoding="utf-8")
        self.assertIn("重新验收全部 ", html)
        self.assertIn("RERUN_BATCH", html)

    def test_audio_batch_is_all_or_nothing_and_totals_twenty_points(self):
        session = {"token": "short-lived-secret", "account": {
            "username": "qa-dedicated", "points": 500, "membership_active": True,
        }}
        modes = [
            {"key": "audio.tts.public", "validation": {"supported": True}},
            {"key": "audio.tts.personal", "validation": {"supported": True}},
        ]
        with patch.object(self.admin, "list_e2e_runs", return_value=[]), \
             patch.object(self.admin, "_e2e_page_modes", return_value=modes), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_e2e_prepare_operation", side_effect=[
                 {"cost": 10}, ValueError("个人测试音色尚未准备"),
             ]):
            blocked = self.admin.e2e_batch_preflight("admin-token", "audio")
        self.assertFalse(blocked["ready"])
        self.assertEqual((blocked["ready_count"], blocked["blocked_count"], blocked["total_cost"]), (1, 1, 10))
        self.assertTrue(blocked["audio_fixture_required"])
        with patch.object(self.admin, "list_e2e_runs", return_value=[]), \
             patch.object(self.admin, "_e2e_page_modes", return_value=modes), \
             patch.object(self.admin, "auth_admin_request", return_value=session), \
             patch.object(self.admin, "points_domain", SimpleNamespace()), \
             patch.object(self.admin, "_e2e_prepare_operation", side_effect=[
                 {"cost": 10}, {"cost": 10},
             ]):
            ready = self.admin.e2e_batch_preflight("admin-token", "audio")
        self.assertTrue(ready["ready"])
        self.assertEqual((ready["ready_count"], ready["blocked_count"], ready["total_cost"]), (2, 0, 20))
        self.assertFalse(ready["audio_fixture_required"])

    def test_start_batch_creates_one_group_and_starts_one_scheduler(self):
        preflight = {
            "ready": True, "account": "qa-dedicated", "target_count": 2,
            "ready_count": 2, "total_cost": 70,
            "items": [
                {"operation_id": "video.grok.text", "ready": True, "cost": 30, "blocker": ""},
                {"operation_id": "video.grok.image", "ready": True, "cost": 40, "blocker": ""},
            ],
        }
        with patch.object(self.admin, "e2e_batch_preflight", return_value=preflight), \
             patch.object(self.admin.threading, "Thread") as thread:
            batch = self.admin.start_e2e_batch("root", "admin-token", "video")
        self.assertEqual((batch["total"], batch["waiting"], batch["status"]), (2, 2, "running"))
        self.assertTrue(batch["batch_id"])
        thread.return_value.start.assert_called_once_with()
        with closing(sqlite3.connect(self.admin.ADMIN_DB)) as connection:
            rows = connection.execute(
                "SELECT DISTINCT batch_id,status FROM admin_e2e_runs ORDER BY status"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "planned")

    def test_batch_scheduler_respects_global_and_task_kind_caps(self):
        counts = {"total": 2, "by_kind": {"sora_video": 1, "xiaole_video": 1}}
        caps = {"total": 5, "sora_video": 1, "xiaole_video": 2}
        with patch.object(self.admin.function_registry, "e2e_runner", side_effect=lambda operation: {
            "endpoint": {"path": "/api/gen/sora_video" if operation == "sora" else "/api/gen/xiaole_video"}
        }):
            self.assertFalse(self.admin._e2e_batch_can_submit("sora", counts, caps))
            self.assertTrue(self.admin._e2e_batch_can_submit("xiaole", counts, caps))
            counts["total"] = 5
            self.assertFalse(self.admin._e2e_batch_can_submit("xiaole", counts, caps))

    def test_release_moves_fixtures_to_private_runtime_and_removes_public_copy(self):
        root = Path(__file__).resolve().parents[1]
        ship = (root / "ship").read_text(encoding="utf-8")
        drift = (root / "scripts/drift_sentinel.py").read_text(encoding="utf-8")
        self.assertIn("server/qa_fixtures/*", ship)
        self.assertIn("workbench/assets/qa/$(basename", ship)
        self.assertIn("QA_FIXTURES_RUNTIME", drift)


class AuthE2ESessionTests(unittest.TestCase):
    def setUp(self):
        import server.auth_server as auth_server
        self.auth = auth_server
        self.tmp = tempfile.TemporaryDirectory()
        self.old = self.auth.DB, self.auth.INTERNAL_TOKEN, self.auth.E2E_TEST_USERNAME
        self.auth.DB = str(Path(self.tmp.name) / "users.db")
        self.auth.INTERNAL_TOKEN = "internal-test-token"
        self.auth.E2E_TEST_USERNAME = "qa-dedicated"
        self.auth.init_db()
        self.auth.create_user("root", "secret1", 0, "admin")
        self.auth.create_user("qa-dedicated", "secret2", 500, "member")
        with closing(self.auth.db()) as connection:
            connection.execute("UPDATE users SET must_change=0 WHERE username IN ('root','qa-dedicated')")
            connection.commit()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.auth.DB, self.auth.INTERNAL_TOKEN, self.auth.E2E_TEST_USERNAME = self.old
        self.tmp.cleanup()

    def test_only_admin_internal_call_can_issue_dedicated_account_session(self):
        admin_token = self.auth.issue_token("root")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/auth/admin/e2e/session" % self.server.server_address[1],
            data=b"{}", method="POST", headers={
                "Authorization": "Bearer " + admin_token,
                "X-HQ-Internal-Token": self.auth.INTERNAL_TOKEN,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read())
        self.assertEqual(data["account"]["username"], "qa-dedicated")
        with closing(self.auth.db()) as connection:
            row = connection.execute(
                "SELECT username,scope FROM tokens WHERE token=?", (data["token"],)
            ).fetchone()
        self.assertEqual((row["username"], row["scope"]), ("qa-dedicated", "account"))
        blocked = urllib.request.Request(
            request.full_url, data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + admin_token},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(blocked, timeout=3)
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
