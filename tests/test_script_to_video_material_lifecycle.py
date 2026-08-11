import base64
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


if os.name == "nt":
    sys.modules.setdefault("fcntl", SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_args: None,
    ))

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core, script_to_video, video  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ScriptToVideoMaterialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.old = {
            "job_db": core.JOB_DB,
            "audio_db": core.AUDIO_DB,
            "core_out": core.OUT_DIR,
            "script_out": script_to_video.OUT_DIR,
            "video_out": video.OUT_DIR,
            "video_video_out": video.VIDEO_OUT_DIR,
            "avatar": script_to_video._get_first_avatar,
            "gen_video": video.gen_video,
        }
        core.JOB_DB = str(self.root / "jobs.db")
        core.AUDIO_DB = str(self.root / "audio.db")
        core.OUT_DIR = self.out
        script_to_video.OUT_DIR = self.out
        video.OUT_DIR = self.out
        video.VIDEO_OUT_DIR = self.out / "video"
        video.VIDEO_OUT_DIR.mkdir()
        script_to_video._get_first_avatar = lambda _username: {"id": 1}
        self._init_databases()

    def tearDown(self):
        core.JOB_DB = self.old["job_db"]
        core.AUDIO_DB = self.old["audio_db"]
        core.OUT_DIR = self.old["core_out"]
        script_to_video.OUT_DIR = self.old["script_out"]
        video.OUT_DIR = self.old["video_out"]
        video.VIDEO_OUT_DIR = self.old["video_video_out"]
        script_to_video._get_first_avatar = self.old["avatar"]
        video.gen_video = self.old["gen_video"]
        self.temp.cleanup()

    def _init_databases(self):
        with sqlite3.connect(core.JOB_DB) as conn:
            conn.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT,
                cost INTEGER DEFAULT 20, status TEXT DEFAULT 'pending',
                payload TEXT, result TEXT, error TEXT, created_at INTEGER DEFAULT 1,
                updated_at INTEGER DEFAULT 1, owner TEXT DEFAULT 'content',
                refunded INTEGER DEFAULT 0)""")
        with sqlite3.connect(core.AUDIO_DB) as conn:
            conn.execute("""CREATE TABLE video_assets(
                job_id INTEGER PRIMARY KEY, phase TEXT, status TEXT, error TEXT,
                updated_at INTEGER, provider_video_id TEXT, image_asset_id TEXT,
                audio_asset_id TEXT, video_file TEXT, source_video_url TEXT)""")

    def _image(self, rel, data=PNG):
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _job(self, payload, username="alice", status="pending", result=None, kind="script_to_video"):
        with sqlite3.connect(core.JOB_DB) as conn:
            cur = conn.execute(
                "INSERT INTO jobs(kind,username,status,payload,result) VALUES(?,?,?,?,?)",
                (kind, username, status, json.dumps(payload, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False) if result is not None else None),
            )
            job_id = cur.lastrowid
        if kind == "script_to_video":
            with sqlite3.connect(core.AUDIO_DB) as conn:
                conn.execute(
                    "INSERT INTO video_assets(job_id,phase,status,updated_at) VALUES(?,?,?,1)",
                    (job_id, "queued", "running"),
                )
        return job_id

    def _owned_asset(self, username="alice", rel="image/source.png"):
        self._image(rel)
        self._job({}, username=username, status="done", kind="image", result={
            "file": rel, "prompt": "产品",
        })
        return rel

    @staticmethod
    def _plan(rel=None, source="asset"):
        return [{
            "scene_index": 0, "prompt": "产品特写", "source": source,
            "file": rel,
        }]

    def test_reused_asset_is_frozen_before_enqueue_and_survives_source_delete(self):
        rel = self._owned_asset()
        job_id = self._job({"material_plan": self._plan(rel)})
        state = script_to_video.freeze_reused_materials_for_job(job_id, "alice")
        frozen = self.out / state["materials"][0]["file"]
        self.assertTrue(frozen.is_file())
        (self.out / rel).unlink()
        with sqlite3.connect(core.JOB_DB) as conn:
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        materials = script_to_video._prepare_frozen_materials(
            job_id, "alice", self._plan(rel),
        )
        self.assertEqual(materials[0]["file"], state["materials"][0]["file"])

    def test_missing_zero_byte_and_cross_user_asset_never_reach_provider(self):
        cases = []
        cases.append(("missing", self._plan("image/missing.png")))
        self._image("image/zero.png", b"")
        cases.append(("zero", self._plan("image/zero.png")))
        foreign = self._owned_asset("bob", "image/bob.png")
        cases.append(("cross-user", self._plan(foreign)))
        for label, plan in cases:
            with self.subTest(label=label):
                job_id = self._job({"material_plan": plan}, status="running")
                with mock.patch.object(video, "gen_video") as provider:
                    with self.assertRaises((PermissionError, RuntimeError)):
                        script_to_video.gen_script_to_video({
                            "_username": "alice", "_job_id": job_id,
                            "scenes": [{"line": "介绍产品"}],
                            "material_plan": plan,
                        })
                provider.assert_not_called()

    def test_unreadable_asset_is_rejected_before_provider(self):
        rel = self._owned_asset()
        job_id = self._job({"material_plan": self._plan(rel)}, status="running")
        original = Path.open

        def denied(path, *args, **kwargs):
            if Path(path) == (self.out / rel):
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied), mock.patch.object(video, "gen_video") as provider:
            with self.assertRaises(PermissionError):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": self._plan(rel),
                })
        provider.assert_not_called()

    def test_generated_material_is_ready_before_provider_create(self):
        generated = self._image("image/generated.png")
        plan = self._plan(None, "generate")
        job_id = self._job({"material_plan": plan}, status="running")

        def fake_provider(payload, provider_lifecycle=None):
            state = script_to_video.get_recovery_state(job_id)
            self.assertEqual(state["phase"], "materials_ready")
            self.assertTrue((self.out / state["materials"][0]["file"]).is_file())
            base = self._image("video/base.mp4", b"video")
            return {"video_file": base.relative_to(self.out).as_posix(), "video_url": "/base"}

        with mock.patch("content_domains.image.gen_image", return_value={
                "file": generated.relative_to(self.out).as_posix()}), \
             mock.patch.object(video, "gen_video", side_effect=fake_provider), \
             mock.patch.object(script_to_video, "_provider_file_exists", return_value=False), \
             mock.patch.object(script_to_video, "_compose_materials", return_value="video/base.mp4"):
            result = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": plan,
                "subtitle": False,
            })
        self.assertEqual(result["pipeline"], "talking_with_materials")

    def test_ten_concurrent_freezes_share_one_server_derived_copy(self):
        rel = self._owned_asset()
        job_id = self._job({"material_plan": self._plan(rel)})
        results = []
        errors = []

        def run():
            try:
                results.append(script_to_video.freeze_reused_materials_for_job(job_id, "alice"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)
        self.assertEqual(len({item["materials"][0]["file"] for item in results}), 1)

    def test_provider_acceptance_is_persisted_and_restart_does_not_post_twice(self):
        state = {}
        creates = []
        self._image("image/a.png")
        audio = self.out / "audio/a.mp3"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"audio")

        def submitted(data):
            state.update(data)

        lifecycle = {"state": {}, "on_submitting": lambda data: state.update(data),
                     "on_submitted": submitted, "on_completed": lambda _data: None}
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "_heygen_create_video", side_effect=lambda *a, **k: creates.append(1) or "vid-1"), \
             mock.patch.object(video, "_heygen_poll_video", side_effect=RuntimeError("restart")):
            with self.assertRaises(video.HeyGenBilledError):
                video.generate_heygen_video_recoverable(
                    "image/a.png", "audio/a.mp3", "720p", "9:16", "medium", lifecycle,
                )
        self.assertEqual(state["provider_video_id"], "vid-1")
        resume = {"state": state, "on_completed": lambda _data: None}
        with mock.patch.object(video, "_heygen_create_video") as create, \
             mock.patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://example/video"}), \
             mock.patch.object(video, "_download_video_file", return_value="video/base.mp4"), \
             mock.patch.object(video, "_extract_first_frame_cover", return_value=None):
            video.generate_heygen_video_recoverable(
                "unused", "unused", "720p", "9:16", "medium", resume,
            )
        create.assert_not_called()
        self.assertEqual(len(creates), 1)

    def test_provider_create_uses_shared_slot_and_only_429_retry_wrapper(self):
        events = []
        self._image("image/a.png")
        audio = self.out / "audio/a.mp3"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"audio")

        class Slot:
            def __enter__(self):
                events.append("slot-enter")

            def __exit__(self, *_args):
                events.append("slot-exit")

        def retry_429(fn, label):
            events.append(("retry-429", label))
            return fn()

        def create(*_args, **_kwargs):
            self.assertIn("slot-enter", events)
            self.assertNotIn("slot-exit", events)
            events.append("create")
            return "vid-1"

        lifecycle = {
            "state": {},
            "on_submitting": lambda _data: events.append("submitting"),
            "on_submitted": lambda _data: events.append("submitted"),
            "on_completed": lambda _data: events.append("completed"),
        }
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "heygen_slot", return_value=Slot()), \
             mock.patch.object(video, "_heygen_retry_429", side_effect=retry_429) as retry, \
             mock.patch.object(video, "_heygen_create_video", side_effect=create) as create_call, \
             mock.patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://example/video"}), \
             mock.patch.object(video, "_download_video_file", return_value="video/base.mp4"), \
             mock.patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.generate_heygen_video_recoverable(
                "image/a.png", "audio/a.mp3", "720p", "9:16", "medium", lifecycle,
            )

        self.assertEqual(result["video_id"], "vid-1")
        create_call.assert_called_once()
        retry.assert_called_once()
        self.assertLess(events.index("slot-enter"), events.index("create"))
        self.assertLess(events.index("create"), events.index("slot-exit"))

    def test_definitive_provider_rejection_clears_ambiguous_phase_without_fallback(self):
        self._image("image/a.png")
        audio = self.out / "audio/a.mp3"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"audio")
        events = []
        lifecycle = {
            "state": {},
            "on_submitting": lambda data: events.append(("submitting", data)),
            "on_rejected": lambda data: events.append(("rejected", data)),
        }
        rejected = RuntimeError("HeyGen接口失败: HTTP 400")
        rejected.__cause__ = urllib.error.HTTPError(
            "https://example.invalid", 400, "bad request", {}, None,
        )
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "_heygen_create_video", side_effect=rejected) as create, \
             mock.patch.object(video, "generate_heygen_video_direct") as fallback:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                video.generate_heygen_video_recoverable(
                    "image/a.png", "audio/a.mp3", "720p", "9:16", "medium", lifecycle,
                )
        create.assert_called_once()
        fallback.assert_not_called()
        self.assertEqual([name for name, _ in events], ["submitting", "rejected"])

    def test_script_pipeline_definitive_rejection_preserves_http_error_and_consistent_phase(self):
        job_id = self._job({}, status="running")

        def reject_provider(_payload, provider_lifecycle=None):
            provider_lifecycle["on_prepared"]({
                "audio_file": "audio/speech.mp3", "image_file": "image/avatar.png",
            })
            provider_lifecycle["on_submitting"]({
                "provider": "heygen_relay", "image_asset_id": "img-1",
                "audio_asset_id": "aud-1",
            })
            provider_lifecycle["on_rejected"]({"provider": "heygen_relay"})
            raise RuntimeError("HeyGen接口失败: HTTP 400")

        with mock.patch.object(video, "gen_video", side_effect=reject_provider), \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=[]), \
             mock.patch.object(script_to_video, "_provider_file_exists", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": [],
                    "subtitle": False,
                })

        state = script_to_video.get_recovery_state(job_id)
        self.assertEqual(state["phase"], "materials_ready")
        self.assertIsNone(state.get("provider_video_id"))
        with sqlite3.connect(core.AUDIO_DB) as conn:
            phase = conn.execute(
                "SELECT phase FROM video_assets WHERE job_id=?", (job_id,),
            ).fetchone()[0]
        self.assertEqual(phase, "materials_ready")

    def test_compose_failure_retries_local_only_and_keeps_base_video(self):
        plan = []
        job_id = self._job({"material_plan": plan}, status="running")
        base = self.out / "video/base.mp4"
        base.write_bytes(b"video")
        provider_calls = []

        def provider(_payload, provider_lifecycle=None):
            provider_calls.append(1)
            provider_lifecycle["on_submitting"]({
                "provider": "heygen_relay", "image_asset_id": "img", "audio_asset_id": "aud",
            })
            provider_lifecycle["on_submitted"]({
                "provider": "heygen_relay", "provider_video_id": "vid-1",
                "image_asset_id": "img", "audio_asset_id": "aud",
            })
            result = {"video_id": "vid-1", "video_file": "video/base.mp4", "video_url": "/base"}
            provider_lifecycle["on_completed"](result)
            return {"provider_video_id": "vid-1", **result}

        video.gen_video = provider
        material = [{"scene_index": 0, "prompt": "产品", "source": "asset", "file": "frozen.png"}]
        with mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=material), \
             mock.patch.object(script_to_video, "_compose_materials", side_effect=RuntimeError("disk full")):
            with self.assertRaises(script_to_video.ScriptToVideoRecoveryRequired):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": material,
                    "subtitle": False,
                })
        with mock.patch.object(video, "gen_video", side_effect=AssertionError("must not create")), \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=material), \
             mock.patch.object(script_to_video, "_compose_materials", return_value="video/final.mp4"), \
             mock.patch.object(video, "public_url", return_value="/final"):
            result = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": material,
                "subtitle": False,
            })
        self.assertEqual(result["video_file"], "video/final.mp4")
        self.assertEqual(provider_calls, [1])

    def test_ambiguous_submitting_is_held_and_known_id_is_requeued(self):
        first = self._job({"_script_to_video_state": {
            "phase": "provider_submitting", "provider": "heygen_relay",
        }}, status="running")
        second = self._job({"_script_to_video_state": {
            "phase": "provider_submitted", "provider": "heygen_relay",
            "provider_video_id": "vid-2",
        }}, status="running")
        requeued = []
        result = script_to_video.reclaim_orphaned_jobs(
            lambda job_id: requeued.append(job_id) or True,
            logger=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result["held"], {first})
        self.assertEqual(requeued, [second])

    def test_done_handler_result_is_replayed_after_refresh_without_provider_or_compose(self):
        final = {"type": "script_to_video", "pipeline": "talking_with_materials",
                 "video_file": "video/final.mp4", "video_url": "/final"}
        (self.out / "video/final.mp4").write_bytes(b"video")
        job_id = self._job({"_script_to_video_state": {
            "phase": "done", "provider_video_id": "vid-1", "final_result": final,
        }}, status="running")
        with mock.patch.object(video, "gen_video") as provider, \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=[]), \
             mock.patch.object(script_to_video, "_compose_materials") as compose:
            replay = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": [],
            })
        self.assertEqual(replay, final)
        provider.assert_not_called()
        compose.assert_not_called()

    def test_pre_provider_failure_refunds_at_most_once_via_job_terminal_cas(self):
        job_id = self._job({}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")
        core.HANDLERS["script_to_video"] = lambda _payload: (_ for _ in ()).throw(
            RuntimeError("material missing")
        )
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"):
                core.run_job(job_id)
                # A second execution cannot claim the same task in production;
                # emulate the terminal CAS outcome for the repeated callback.
                with sqlite3.connect(core.JOB_DB) as conn:
                    conn.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original
        self.assertEqual(refunds, [job_id])

    def test_explicit_provider_failed_terminal_refunds_once_without_requeue(self):
        job_id = self._job({"_script_to_video_state": {
            "phase": "provider_submitted",
            "provider": "heygen_relay",
            "provider_video_id": "vid-failed",
        }}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")
        core.HANDLERS["script_to_video"] = lambda _payload: (_ for _ in ()).throw(
            video.HeyGenProviderFailed("HeyGen视频生成失败: provider rejected")
        )
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(core, "_requeue_running_job") as requeue, \
                 mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"):
                core.run_job(job_id)
                with sqlite3.connect(core.JOB_DB) as conn:
                    conn.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original

        self.assertEqual(refunds, [job_id])
        requeue.assert_not_called()

    def test_running_error_holds_when_recovery_state_database_is_unavailable(self):
        job_id = self._job({}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")
        core.HANDLERS["script_to_video"] = lambda _payload: (_ for _ in ()).throw(
            RuntimeError("worker failed")
        )
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(script_to_video, "get_recovery_state", side_effect=sqlite3.OperationalError("database is locked")), \
                 mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"), \
                 mock.patch.object(video, "gen_video") as provider:
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original

        self.assertEqual(refunds, [])
        provider.assert_not_called()
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "running")

    def test_reaper_holds_when_recovery_state_database_is_unavailable(self):
        job_id = self._job({}, status="running")
        refunds = []
        points_domain = SimpleNamespace(retry_breakdown_refunds=lambda _limit: None)
        video_domain = SimpleNamespace(retry_pending_seedance_cleanups=lambda **_kwargs: None)
        with mock.patch.object(core, "_domains", return_value=(None, points_domain, video_domain)), \
             mock.patch.object(script_to_video, "get_recovery_state", side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
             mock.patch.object(core.time, "sleep", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                core.reaper()

        self.assertEqual(refunds, [])
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "running")

    def test_startup_holds_all_when_script_recovery_query_is_unavailable(self):
        job_id = self._job({"_script_to_video_state": {
            "phase": "provider_submitting", "provider": "heygen_relay",
        }}, status="running")
        with mock.patch.object(script_to_video, "jdb", side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(core.startup_recovery, "reclaim_orphaned_running") as generic_reclaimer, \
             mock.patch.object(core, "_fail_job_and_schedule_refund") as refund:
            self.assertEqual(core.reclaim_orphaned_running(), 0)

        generic_reclaimer.assert_not_called()
        refund.assert_not_called()
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "running")


if __name__ == "__main__":
    unittest.main()
