# -*- coding: utf-8 -*-
import pathlib
import base64
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core, feature_flags, points, startup_recovery, video, video_openai  # noqa: E402


class SoraPayloadTests(unittest.TestCase):
    REF = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"0" * 24
    ).decode()
    def validate(self, **overrides):
        body = {
            "prompt": "A ceramic perfume bottle rotating under soft studio light",
            "model": "sora-2",
            "seconds": 4,
            "ratio": "9:16",
            "resolution": "720p",
        }
        body.update(overrides)
        with patch.object(video, "SORA_VIDEO_ENABLED", True), \
                patch.object(video_openai, "available", return_value=True):
            return video.validate_sora_video_payload(body)

    def test_beta_requires_explicit_server_switch(self):
        with patch.object(video, "SORA_VIDEO_ENABLED", False):
            with self.assertRaisesRegex(ValueError, "未开启"):
                video.validate_sora_video_payload({"prompt": "demo"})

    def test_missing_admin_flag_is_fail_closed_for_sora_only(self):
        with patch.object(feature_flags, "_cached_rows", return_value={}):
            self.assertFalse(feature_flags.is_enabled("sora_video"))
            self.assertTrue(feature_flags.is_enabled("video"))

    def test_stale_enabled_sora_flag_is_ignored_when_flag_db_fails(self):
        cache = {"loaded_at": 0, "items": {"sora_video": {"enabled": True}}}
        with patch.object(feature_flags, "_CACHE", cache), \
                patch.object(feature_flags, "_load_rows", side_effect=OSError("db down")), \
                patch.object(feature_flags.time, "time", return_value=100):
            self.assertFalse(feature_flags.is_enabled("sora_video"))
            self.assertTrue(feature_flags.is_enabled("video"))

    def test_beta_auto_closes_on_the_official_shutdown_date(self):
        with patch.object(video, "SORA_VIDEO_ENABLED", True):
            self.assertTrue(video.sora_video_is_open("2026-09-23"))
            self.assertFalse(video.sora_video_is_open("2026-09-24"))
            self.assertFalse(video.sora_video_is_open("2026-09-25"))

    def test_sora_2_only_accepts_720p_portrait_or_landscape(self):
        self.assertEqual(self.validate()["size"], "720x1280")
        self.assertEqual(self.validate(ratio="16:9")["size"], "1280x720")
        with self.assertRaisesRegex(ValueError, "不支持"):
            self.validate(resolution="1024p")
        with self.assertRaisesRegex(ValueError, "比例"):
            self.validate(ratio="1:1")

    def test_pro_maps_each_resolution_to_official_size(self):
        expected = {
            ("720p", "9:16"): "720x1280",
            ("720p", "16:9"): "1280x720",
            ("1024p", "9:16"): "1024x1792",
            ("1024p", "16:9"): "1792x1024",
            ("1080p", "9:16"): "1080x1920",
            ("1080p", "16:9"): "1920x1080",
        }
        for (resolution, ratio), size in expected.items():
            with self.subTest(resolution=resolution, ratio=ratio):
                self.assertEqual(
                    self.validate(model="sora-2-pro", resolution=resolution, ratio=ratio)["size"],
                    size,
                )

    def test_only_small_beta_duration_matrix_is_exposed(self):
        for seconds in (4, 8, 12):
            self.assertEqual(self.validate(seconds=seconds)["seconds"], seconds)
        for seconds in (0, 5, 10, 16, 20, "4.0", True):
            with self.subTest(seconds=seconds):
                with self.assertRaisesRegex(ValueError, "仅支持 4、8、12 秒"):
                    self.validate(seconds=seconds)

    def test_model_and_prompt_are_server_whitelisted(self):
        with self.assertRaisesRegex(ValueError, "模型"):
            self.validate(model="sora-3")
        with self.assertRaisesRegex(ValueError, "提示词"):
            self.validate(prompt="  ")

    def test_accepts_one_reference_and_resolves_its_prompt_mention(self):
        result = self.validate(prompt="让 @图片1 缓慢旋转", reference_images=[self.REF])
        self.assertEqual(result["reference_images"], [self.REF])
        self.assertIn("第1张参考图", result["provider_prompt"])
        with self.assertRaisesRegex(ValueError, "最多支持1张"):
            self.validate(reference_images=[self.REF, self.REF])
        with self.assertRaisesRegex(ValueError, "当前只有 0 张"):
            self.validate(prompt="使用 @图片1", reference_images=[])


class SoraPointsTests(unittest.TestCase):
    def test_points_preserve_the_existing_margin_by_model_and_resolution(self):
        # 官方裸成本按 $0.10/$0.30/$0.50/$0.70 每秒；1 元=10 点。
        # Beta 售价保持约 4.2x 的现有 Grok 毛利安全垫，不能让 Pro 回落到 30 点/秒亏损。
        cases = {
            ("sora-2", "720p"): 30,
            ("sora-2-pro", "720p"): 90,
            ("sora-2-pro", "1024p"): 150,
            ("sora-2-pro", "1080p"): 210,
        }
        for (model, resolution), rate in cases.items():
            with self.subTest(model=model, resolution=resolution):
                self.assertEqual(
                    points.cost_of("sora_video", {
                        "model": model, "resolution": resolution, "seconds": 4,
                    }),
                    rate * 4,
                )

    def test_twelve_second_points_are_rate_times_twelve(self):
        for (model, resolution), rate in points.SORA_VIDEO_RATE.items():
            with self.subTest(model=model, resolution=resolution):
                self.assertEqual(
                    points.cost_of("sora_video", {
                        "model": model, "resolution": resolution, "seconds": 12,
                    }),
                    rate * 12,
                )

    def test_unknown_sora_price_never_becomes_free(self):
        self.assertGreater(points.cost_of("sora_video", {}), 0)
        self.assertEqual(
            points.cost_of("sora_video", {
                "model": "unknown", "resolution": "unknown", "seconds": 999,
            }),
            max(points.SORA_VIDEO_RATE.values()) * 12,
        )


class SoraHandlerTests(unittest.TestCase):
    def run_sora_failure(self, error):
        class Connection:
            def execute(self, *_args):
                return self

            def fetchone(self):
                return {
                    "id": 7, "kind": "sora_video", "username": "u", "cost": 120,
                    "payload": "{}", "status": "pending",
                }

            def close(self):
                pass

        recover = Mock(return_value=True)
        terminal = Mock(return_value=True)
        refund = Mock()
        with patch.object(video, "recover_sora_paid_job", recover), \
             patch.object(core, "jdb", return_value=Connection()), \
             patch.object(core.jobs_store, "claim_running", return_value=True), \
             patch.object(core, "_start_job_heartbeat", return_value=Mock()), \
             patch.object(core, "HANDLERS", {"sora_video": Mock(side_effect=error)}), \
             patch.object(core, "_domains", return_value=(None, None, video)), \
             patch.object(core, "_set_terminal", terminal), \
             patch.object(core, "_refund_once", refund), \
             patch.object(core, "_mark_video_asset_failed"):
            core.run_job(7)
        return recover, terminal, refund

    def test_confirmed_create_rejection_reaches_terminal_refund(self):
        recover, terminal, refund = self.run_sora_failure(
            video_openai.CreateRejected("HTTP 402")
        )
        recover.assert_not_called()
        terminal.assert_called_once()
        refund.assert_called_once_with(7, "u", 120)

    def test_unknown_create_outcome_never_reaches_terminal_refund(self):
        recover, terminal, refund = self.run_sora_failure(
            video_openai.CreateOutcomeUnknown("connection reset")
        )
        recover.assert_called_once()
        terminal.assert_not_called()
        refund.assert_not_called()

    def test_sora_requires_a_valid_client_idempotency_key(self):
        source = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn('if kind == "sora_video" and not idem_key:', source)

    def test_provider_id_strict_write_requires_an_asset_row(self):
        class Connection:
            def __init__(self, rowcount):
                self.rowcount = rowcount

            def execute(self, *_args):
                return self

            def commit(self):
                pass

            def close(self):
                pass

        with patch.object(video, "adb", return_value=Connection(0)):
            with self.assertRaisesRegex(RuntimeError, "资产行"):
                video.update_video_asset_phase(
                    7, "sora_queued", strict=True, provider_video_id="video_paid"
                )
        with patch.object(video, "adb", return_value=Connection(1)), \
             patch.object(video, "jdb", return_value=Connection(1)):
            self.assertTrue(video.update_video_asset_phase(
                7, "sora_queued", strict=True, provider_video_id="video_paid"
            ))

    def test_unknown_paid_submission_is_held_for_reconciliation(self):
        with patch.object(video, "get_resumable_sora_request", return_value={
            "video_id": None, "submission_unknown": True,
        }), patch.object(video, "update_video_asset_phase") as update:
            self.assertTrue(video.recover_sora_paid_job(7, "response lost"))
        update.assert_called_once_with(7, "sora_recovery_required", error="response lost")

    def test_persisted_provider_job_is_held_when_local_processing_fails(self):
        with patch.object(video, "get_resumable_sora_request", return_value={
            "video_id": "video_paid", "submission_unknown": False,
        }), patch.object(video, "update_video_asset_phase") as update:
            self.assertTrue(video.recover_sora_paid_job(7, "invalid download"))
        update.assert_called_once_with(7, "sora_recovery_required", error="invalid download")

    def test_lost_requeue_cas_never_falls_through_to_refund(self):
        requeue = Mock(return_value=False)
        with patch.object(video, "get_resumable_sora_request", return_value={
            "video_id": "video_paid", "submission_unknown": False,
        }), patch.object(video, "update_video_asset_phase") as update:
            self.assertTrue(video.recover_sora_paid_job(7, "worker lost", requeue))
        requeue.assert_called_once_with(7)
        update.assert_not_called()

    def test_manual_recovery_phase_is_not_automatically_requeued(self):
        requeue = Mock(return_value=True)
        with patch.object(video, "get_resumable_sora_request", return_value={
            "video_id": "video_paid", "phase": "sora_recovery_required",
        }), patch.object(video, "update_video_asset_phase") as update:
            self.assertTrue(video.recover_sora_paid_job(7, "reaper", requeue))
        requeue.assert_not_called()
        update.assert_not_called()

    def test_unknown_create_result_never_posts_again(self):
        with patch.object(video, "get_resumable_sora_request", return_value={
            "video_id": None, "submission_unknown": True, "phase": "sora_submitting",
        }), patch.object(video_openai, "generate") as generate:
            with self.assertRaises(video.SoraSubmissionUnknown):
                video.gen_sora_video({"_job_id": 7, "prompt": "product shot"})
        generate.assert_not_called()

    def test_submitting_marker_failure_stops_before_post(self):
        with patch.object(video, "get_resumable_sora_request", return_value=None), \
             patch.object(video, "update_video_asset_phase", side_effect=RuntimeError("asset db down")), \
             patch.object(video_openai, "generate") as generate:
            with self.assertRaisesRegex(RuntimeError, "asset db down"):
                video.gen_sora_video({"_job_id": 7, "prompt": "product shot"})
        generate.assert_not_called()

    def test_created_video_id_is_persisted_and_result_enters_video_assets_contract(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            def out_path(rel):
                fp = root / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                return fp

            def download(video_id, destination, max_bytes=None, **_kwargs):
                calls.append(("download", video_id, pathlib.Path(destination).name))
                pathlib.Path(destination).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
                return pathlib.Path(destination)

            def heartbeat(job_id, phase, **fields):
                calls.append(("heartbeat", job_id, phase, fields))

            with patch.object(video, "_out_path", side_effect=out_path), \
                    patch.object(video, "_faststart_video_file", side_effect=lambda rel: rel), \
                    patch.object(video, "_extract_first_frame_cover", return_value=None), \
                    patch.object(video, "_file_url", side_effect=lambda rel: "/api/gen/file/" + rel), \
                    patch.object(video, "update_video_asset_phase", side_effect=heartbeat), \
                    patch.object(video, "get_resumable_sora_request", return_value=None), \
                    patch.object(video.provider_keys, "claim_candidate", return_value={
                        "id": "key_test", "secret": "secret_test"
                    }), \
                    patch.object(video.provider_keys, "set_health"), \
                    patch.object(video_openai, "generate", return_value={
                        "video_id": "video_123", "model": "sora-2-pro",
                        "status": "completed", "seconds": "4", "size": "1024x1792",
                    }) as generate, \
                    patch.object(video_openai, "download_content", side_effect=download):
                result = video.gen_sora_video({
                    "_job_id": 19,
                    "prompt": "product shot",
                    "model": "sora-2-pro",
                    "seconds": 4,
                    "ratio": "9:16",
                    "resolution": "1024p",
                    "size": "1024x1792",
                })

        generate.assert_called_once()
        self.assertEqual(result["type"], "video")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["mode"], "sora")
        self.assertEqual(result["provider_video_id"], "video_123")
        self.assertEqual(result["model"], "sora-2-pro")
        self.assertTrue(result["video_file"].startswith("video/sora_"))
        self.assertTrue(any(c[0] == "heartbeat" and c[2] == "sora_completed" for c in calls))
        self.assertTrue(any(c[0] == "download" for c in calls))

    def test_restart_resumes_existing_provider_job_without_second_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            def out_path(rel):
                fp = root / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                return fp

            def download(_video_id, destination, max_bytes=None, **_kwargs):
                pathlib.Path(destination).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
                return pathlib.Path(destination)

            with patch.object(video, "_out_path", side_effect=out_path), \
                    patch.object(video, "_faststart_video_file", side_effect=lambda rel: rel), \
                    patch.object(video, "_extract_first_frame_cover", return_value=None), \
                    patch.object(video, "_file_url", side_effect=lambda rel: "/api/gen/file/" + rel), \
                    patch.object(video, "update_video_asset_phase"), \
                    patch.object(video, "get_resumable_sora_request", return_value={
                        "video_id": "video_existing", "model": "sora-2", "seconds": 4,
                        "size": "720x1280", "provider_key_id": "key_existing",
                    }), \
                    patch.object(video.provider_keys, "candidates", return_value=[
                        {"id": "key_existing", "secret": "secret_existing"}
                    ]), \
                    patch.object(video_openai, "generate") as generate, \
                    patch.object(video_openai, "resume", return_value={
                        "video_id": "video_existing", "model": "sora-2",
                        "status": "completed", "seconds": "4", "size": "720x1280",
                    }) as resume, \
                    patch.object(video_openai, "download_content", side_effect=download):
                result = video.gen_sora_video({
                    "_job_id": 20, "prompt": "landscape", "model": "sora-2",
                    "seconds": 4, "ratio": "9:16", "resolution": "720p", "size": "720x1280",
                })

        generate.assert_not_called()
        resume.assert_called_once()
        self.assertEqual(result["provider_video_id"], "video_existing")


class SoraStartupRecoveryTests(unittest.TestCase):
    def test_startup_does_not_requeue_manual_recovery_job(self):
        class Rows:
            def execute(self, *_args):
                return self

            def fetchall(self):
                return [{"id": 9, "username": "u", "cost": 120, "kind": "sora_video"}]

            def close(self):
                pass

        domain = type("Domain", (), {
            "get_resumable_sora_request": staticmethod(lambda _job_id: {
                "video_id": "video_paid", "phase": "sora_recovery_required",
            })
        })
        terminal, refund, requeue = Mock(), Mock(), Mock()
        startup_recovery.reclaim_orphaned_running(
            jdb=lambda: Rows(), service_owner="content",
            domains=lambda: (None, None, domain), set_terminal=terminal,
            refund_once=refund, mark_video_asset_failed=Mock(), requeue_job=requeue,
            logger=lambda *_args, **_kwargs: None,
        )
        terminal.assert_not_called()
        refund.assert_not_called()
        requeue.assert_not_called()

    def test_reaper_never_refunds_an_unknown_paid_submission(self):
        class StopLoop(Exception):
            pass

        class Rows:
            def execute(self, *_args):
                return self

            def fetchall(self):
                return [{"id": 9, "username": "u", "cost": 120, "kind": "sora_video",
                         "payload": "{}", "updated_at": 1}]

            def close(self):
                pass

        recover = Mock(return_value=True)
        terminal = Mock()
        with patch.object(video, "recover_sora_paid_job", recover), \
             patch.object(core, "jdb", return_value=Rows()), \
             patch.object(core, "_domains", return_value=(None, None, video)), \
             patch.object(core, "_set_terminal", terminal), \
             patch.object(core.time, "time", return_value=5000), \
             patch.object(core.time, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                core.reaper()
        terminal.assert_not_called()
        recover.assert_called_once()

    def test_reaper_expires_stale_recovery_hold_with_known_provider_id(self):
        class StopLoop(Exception):
            pass

        class Rows:
            def execute(self, *_args):
                return self

            def fetchall(self):
                return [{"id": 10, "username": "u", "cost": 120,
                         "kind": "sora_video", "payload": "{}", "updated_at": 1}]

            def close(self):
                pass

        recover = Mock(return_value=True)
        recovery = Mock(return_value={
            "video_id": "video-paid", "submission_unknown": False,
            "phase": "sora_recovery_required",
        })
        fail = Mock(return_value=True)
        with patch.object(video, "recover_paid_video_error", recover), \
             patch.object(video, "get_resumable_sora_request", recovery), \
             patch.object(core, "jdb", return_value=Rows()), \
             patch.object(core, "_domains", return_value=(None, None, video)), \
             patch.object(core, "_fail_job_and_schedule_refund", fail), \
             patch.object(core, "_mark_video_asset_failed"), \
             patch.object(core.time, "time", return_value=5000), \
             patch.object(core.time, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                core.reaper()
        recover.assert_called_once()
        recovery.assert_called_once_with(10)
        fail.assert_called_once()
        self.assertIn("恢复超时", fail.call_args.args[1])

    def test_persisted_video_id_requeues_without_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            jobs_path = root / "jobs.db"
            assets_path = root / "assets.db"

            with sqlite3.connect(str(jobs_path)) as conn:
                conn.execute(
                    """CREATE TABLE jobs(
                           id INTEGER PRIMARY KEY, username TEXT, cost INTEGER,
                           kind TEXT, status TEXT, owner TEXT, error TEXT,
                           updated_at INTEGER)"""
                )
                conn.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                    (41, "tester", 120, "sora_video", "running", "content", None, 1),
                )
            with sqlite3.connect(str(assets_path)) as conn:
                conn.execute(
                    """CREATE TABLE video_assets(
                           job_id INTEGER PRIMARY KEY, provider_video_id TEXT,
                           provider_key_id TEXT, model TEXT, phase TEXT, status TEXT, resolution TEXT,
                           ratio TEXT, error TEXT, updated_at INTEGER)"""
                )
                conn.execute(
                    "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        41, "video_persisted", "env", "sora-2", "sora_in_progress",
                        "running", "720p", "9:16", None, 1,
                    ),
                )

            def job_db():
                conn = sqlite3.connect(str(jobs_path))
                conn.row_factory = sqlite3.Row
                return conn

            def asset_db():
                conn = sqlite3.connect(str(assets_path))
                conn.row_factory = sqlite3.Row
                return conn

            terminal = Mock()
            refund = Mock()
            failed_asset = Mock()
            logs = []
            with patch.object(video, "adb", side_effect=asset_db):
                handled = startup_recovery.reclaim_orphaned_running(
                    jdb=job_db,
                    service_owner="content",
                    domains=lambda: (None, None, video),
                    set_terminal=terminal,
                    refund_once=refund,
                    mark_video_asset_failed=failed_asset,
                    requeue_job=lambda job_id: startup_recovery.requeue_running_job(
                        job_db, job_id
                    ),
                    logger=lambda message, flush=False: logs.append(message),
                )

            self.assertEqual(handled, 1)
            with sqlite3.connect(str(jobs_path)) as conn:
                status, error = conn.execute(
                    "SELECT status,error FROM jobs WHERE id=41"
                ).fetchone()
            self.assertEqual((status, error), ("pending", None))
            terminal.assert_not_called()
            refund.assert_not_called()
            failed_asset.assert_not_called()
            self.assertTrue(any("Sora" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
