import json
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
                job_id INTEGER,cost INTEGER DEFAULT 0,points_before INTEGER,points_after INTEGER,
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

    def test_fixture_reader_rejects_paths_outside_private_directory(self):
        with self.assertRaisesRegex(ValueError, "名称无效"):
            self.admin._fixture_data_url("@fixture/../admin_api.py")

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
