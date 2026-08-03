import os
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import (
    short_drama,
    short_drama_autodraft,
    short_drama_conversation,
    short_drama_preflight,
)


class Handler:
    def __init__(self, path, body=None, key="autodraft-route-key"):
        self.path = path
        self.body = body
        self.headers = {"Idempotency-Key": key}
        self.response = None

    def _token(self):
        return "alice"

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaAutodraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        self.free = mock.patch.dict(
            os.environ, {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "1",
                "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "1",
                "CONTENT_OUT": self.tmp.name,
            }
        )
        self.free.start()
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db,
            "alice",
            {
                "title": "自动草稿测试",
                "synopsis": "两个朋友在公园找到一封来自未来的信，并做出不同选择。",
                "ratio": "16:9",
                "target_duration": 30,
                "shot_count": 6,
                "visual_style": "电影感写实",
                "target_platform": "抖音",
                "point_budget": 0,
            },
        )
        selected = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "方案一 · 情感治愈",
            },
            "autodraft-select-direction",
        )
        confirmed = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": selected["conversation"]["revision"],
                "message": "确认这个方向",
            },
            "autodraft-confirm-direction",
        )
        generated = short_drama_conversation.generate_script(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "温暖反转",
            },
            "generate-key",
        )
        locked = short_drama_conversation.lock_script(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "lock-key",
        )
        prepared = short_drama_preflight.generate_plan(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": locked["conversation"]["revision"],
                "quality_route": "quick_draft",
            },
            "prepare-key",
        )
        current = prepared["current_plan"]
        short_drama_preflight.confirm_plan(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "plan_id": current["id"],
                "plan_version": current["version"],
                "accepted_issue_keys": current["plan"]["required_acceptance"],
            },
            "confirm-key",
        )
        self.plan_id = current["id"]

    def tearDown(self):
        self.free.stop()
        self.tmp.cleanup()

    def _start(self, key="autodraft-key"):
        return short_drama_autodraft.start_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "plan_id": self.plan_id},
            key,
        )

    def _provider_avatar(self):
        return {
            "id": "avatar-local-1",
            "username": "alice",
            "name": "记者林夏",
            "status": "ready",
            "provider_avatar_id": "heygen-avatar-1",
        }

    def _provider_quote(self):
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        shot = workspace["provider_poc"]["shots"][0]
        avatar = self._provider_avatar()
        return short_drama_autodraft.create_provider_quote(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "plan_id": self.plan_id,
                "shot_key": shot["shot_key"],
                "avatar_id": avatar["id"],
            },
            avatar_lookup=lambda _username, _avatar_id: avatar,
        )

    def test_confirmed_plan_starts_free_local_pollable_job(self):
        job = self._start()
        self.assertEqual("queued", job["status"])
        self.assertEqual(0, job["cost"])
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("producing", workspace["state"])
        self.assertEqual("development_free", workspace["billing"]["mode"])
        self.assertEqual("demo", workspace["production"]["mode"])

    def test_completion_exception_is_terminal_and_not_rendered_again(self):
        job = self._start("terminal-preview-failure")
        temp = (
            Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"] /
            (".%s.tmp" % job["id"])
        )
        temp.mkdir(parents=True)
        (temp / "partial.mp4").write_bytes(b"partial")
        renderer = mock.Mock(side_effect=RuntimeError("ffmpeg timed out"))
        with mock.patch.object(short_drama_autodraft, "_complete", renderer):
            for _ in range(6):
                job = short_drama_autodraft.get_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
            repeated = short_drama_autodraft.get_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertEqual("failed", job["phase"])
        self.assertEqual(100, job["progress"])
        self.assertTrue(job["error"]["retryable"])
        self.assertTrue(job["error"]["temporary_output_cleaned"])
        self.assertFalse(temp.exists())
        self.assertEqual("failed", repeated["status"])
        self.assertEqual(1, renderer.call_count)
        conn = self.db()
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM short_drama_autodraft_jobs WHERE project_id=? "
                "AND status IN ('queued','running')", (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, active)

    def test_http_start_route_accepts_project_usage_contract(self):
        handler = Handler(
            "/api/gen/short-drama/autodraft/jobs",
            body={"project_id": self.project["id"], "plan_id": self.plan_id},
        )
        verify = lambda token: {
            "username": token,
            "must_change": False,
        } if token else None
        self.assertTrue(
            short_drama.dispatch_http(handler, "POST", self.db, verify)
        )
        self.assertEqual(200, handler.response[0])
        self.assertEqual("queued", handler.response[1]["status"])

    def test_polling_finishes_with_playable_degraded_draft(self):
        job = self._start()
        result = job
        for _ in range(8):
            result = short_drama_autodraft.get_job(
                self.db, "alice", self.project["id"], job["id"]
            )
            if result["status"] not in short_drama_autodraft.ACTIVE:
                break
        self.assertEqual("degraded", result["status"])
        self.assertEqual(100, result["progress"])
        self.assertEqual("/assets/meiye_video.mp4", result["result"]["url"])
        self.assertEqual(6, len(result["result"]["shot_cards"]))
        self.assertEqual(1, len(result["result"]["issues"]))

        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("draft_ready", workspace["state"])
        self.assertEqual("degraded", workspace["current_version"]["status"])
        self.assertTrue(workspace["current_version"]["is_demo"])

    def test_real_start_is_blocked_before_attempt_when_provider_is_unavailable(self):
        with mock.patch.dict(
            os.environ, {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "0",
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "",
            }
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                self._start("provider-unavailable-key")
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"]
            )
        self.assertEqual("autodraft_provider_unavailable", raised.exception.code)
        self.assertEqual(503, raised.exception.status)
        self.assertFalse(workspace["production"]["ready"])
        self.assertEqual(
            "provider_not_selected",
            workspace["production"]["provider"]["code"],
        )
        conn = self.db()
        try:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM short_drama_autodraft_attempts "
                "WHERE idempotency_key='provider-unavailable-key'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, attempts)

    def test_provider_preflight_compiles_one_shot_without_external_submission(self):
        avatar = {
            "id": "avatar-local-1",
            "username": "alice",
            "name": "记者林夏",
            "status": "ready",
            "provider_avatar_id": "heygen-avatar-1",
        }
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "0",
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-preflight-only",
            },
        ):
            workspace = short_drama_autodraft.workspace(
                self.db,
                "alice",
                "alice",
                self.project["id"],
                avatar_list=lambda _username, _limit: [avatar],
            )
            shot = workspace["provider_poc"]["shots"][0]
            with mock.patch(
                "providers.short_drama_visual.heygen_cinematic."
                "HeyGenCinematicShotProvider.create_job"
            ) as create_job:
                result = short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "avatar_id": avatar["id"],
                    },
                    avatar_lookup=lambda _username, _avatar_id: avatar,
                )
        self.assertTrue(result["ready"])
        self.assertFalse(result["billable"])
        self.assertFalse(result["external_submission"])
        self.assertEqual("[已绑定]", result["request"]["provider_avatar"])
        self.assertNotIn("heygen-avatar-1", json.dumps(result, ensure_ascii=False))
        conversation = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        source_shot = conversation["current_script"]["script"]["shots"][0]
        self.assertTrue(
            result["request"]["prompt"].startswith(
                source_shot["provider_prompt"].rstrip("。；; ")
            )
        )
        self.assertIn(
            "禁止项：%s" % source_shot["negative_prompt"].rstrip("。；; "),
            result["request"]["prompt"],
        )
        self.assertEqual(64, len(result["request_hash"]))
        create_job.assert_not_called()

    def test_provider_preflight_uses_locked_provider_prompt_as_source_of_truth(self):
        conn = self.db()
        try:
            raw = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0]
            plan = json.loads(raw)
            shot = plan["material_plan"][0]
            shot["provider_prompt"] = "唯一真实提示词：雨夜车站，女儿回头看向母亲"
            shot["negative_prompt"] = "字幕，水印，额外人物"
            shot["visual_prompt"] = "这段旧画面描述不能进入真实请求"
            shot["input_hash"] = short_drama_autodraft._hash(shot)
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        avatar = self._provider_avatar()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-preflight-only",
            },
        ):
            result = short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": avatar["id"],
                },
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )

        self.assertEqual(
            "唯一真实提示词：雨夜车站，女儿回头看向母亲 "
            "禁止项：字幕，水印，额外人物。",
            result["request"]["prompt"],
        )
        self.assertNotIn("旧画面描述", result["request"]["prompt"])

    def test_legacy_plan_recovers_prompt_from_its_locked_script_snapshot(self):
        conversation = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        source_shot = conversation["current_script"]["script"]["shots"][0]
        conn = self.db()
        try:
            raw = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0]
            plan = json.loads(raw)
            shot = plan["material_plan"][0]
            shot.pop("provider_prompt", None)
            shot.pop("negative_prompt", None)
            shot["visual_prompt"] = "旧计划中的简略画面说明"
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        avatar = self._provider_avatar()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-preflight-only",
            },
        ):
            result = short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": avatar["id"],
                },
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )

        self.assertTrue(
            result["request"]["prompt"].startswith(
                source_shot["provider_prompt"].rstrip("。；; ")
            )
        )
        self.assertIn(
            source_shot["negative_prompt"].rstrip("。；; "),
            result["request"]["prompt"],
        )
        self.assertNotIn("旧计划中的简略画面说明", result["request"]["prompt"])

    def test_provider_preflight_rejects_unowned_avatar(self):
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        shot = workspace["provider_poc"]["shots"][0]
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": "avatar-bob",
                },
                avatar_lookup=lambda _username, _avatar_id: {
                    "id": "avatar-bob",
                    "username": "bob",
                    "status": "ready",
                    "provider_avatar_id": "provider-bob",
                },
            )
        self.assertEqual("provider_avatar_forbidden", raised.exception.code)
        self.assertEqual(403, raised.exception.status)

    def test_provider_preflight_http_route_is_non_billable(self):
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        handler = Handler(
            "/api/gen/short-drama/autodraft/provider-preflight",
            body={
                "project_id": self.project["id"],
                "plan_id": self.plan_id,
                "shot_key": workspace["provider_poc"]["shots"][0]["shot_key"],
                "avatar_id": "avatar-local-1",
            },
        )
        verify = lambda token: {
            "username": token,
            "must_change": False,
        } if token else None
        avatar = {
            "id": "avatar-local-1",
            "username": "alice",
            "name": "记者林夏",
            "status": "ready",
            "provider_avatar_id": "heygen-avatar-1",
        }
        self.assertTrue(short_drama.dispatch_http(
            handler,
            "POST",
            self.db,
            verify,
            avatar_lookup=lambda _username, _avatar_id: avatar,
        ))
        self.assertEqual(200, handler.response[0])
        self.assertFalse(handler.response[1]["external_submission"])

    def test_start_is_idempotent_and_rejects_changed_request(self):
        first = self._start("same-key")
        replay = self._start("same-key")
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.start_job(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "plan_id": "different"},
                "same-key",
            )
        self.assertIn(
            raised.exception.code,
            {"confirmed_plan_required", "idempotency_conflict"},
        )

    def test_only_one_active_job_per_project(self):
        self._start("first-key")
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            self._start("second-key")
        self.assertEqual("active_autodraft_job", raised.exception.code)

    def test_provider_assembly_requires_all_shots_and_pins_latest_versions(self):
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot_keys = [item["shot_key"] for item in plan["material_plan"]]
            now = 1700000000
            for index, shot_key in enumerate(shot_keys[:-1]):
                for version in (1, 2) if index == 0 else (1,):
                    job_id = "job-%s-%s" % (shot_key, version)
                    conn.execute(
                        "INSERT INTO short_drama_provider_shot_jobs "
                        "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                        "character_key,avatar_id,provider,provider_job_id,status,progress,"
                        "poll_count,input_hash,request_json,result_json,error_json,cost,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,100,1,?,?,?,?,0,?,?)",
                        (
                            job_id, self.project["id"], "alice", "alice",
                            self.plan_id, shot_key, "character_1", "avatar_1",
                            "heygen_cinematic", "provider-" + job_id, "succeeded",
                            "hash-%s-%s" % (shot_key, version), "{}", "{}", None,
                            now + version, now + version,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO short_drama_provider_shot_versions "
                        "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                        "status,file,url,input_hash,created_at) "
                        "VALUES (?,?,?,?,?,'heygen_cinematic',?,'ready',?,?,?,?)",
                        (
                            "version-%s-%s" % (shot_key, version), self.project["id"],
                            job_id, shot_key, version, "provider-" + job_id,
                            "video/%s-v%s.mp4" % (shot_key, version),
                            "/api/gen/file/video/%s-v%s.mp4" % (shot_key, version),
                            "hash-%s-%s" % (shot_key, version), now + version,
                        ),
                    )
            partial = short_drama_autodraft._provider_assembly_snapshot(
                conn, self.project["id"], plan,
            )
            self.assertFalse(partial["all_ready"])
            self.assertEqual([shot_keys[-1]], partial["missing_shot_keys"])
            self.assertEqual(
                "version-%s-2" % shot_keys[0], partial["shots"][0]["id"]
            )
        finally:
            conn.close()

    def test_vertical_provider_preview_keeps_audio_and_uses_vertical_dimensions(self):
        root = Path(self.tmp.name) / "content-out"
        source = root / "provider" / "shot.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"provider-video")
        commands = []

        def render(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"rendered-preview")
            return mock.Mock(returncode=0, stdout="", stderr="")

        probe = {
            "duration_ms": 5000,
            "video": {
                "codec": "h264", "width": 720, "height": 1280,
                "fps": 25, "pix_fmt": "yuv420p", "sar": "1:1", "rotation": 0,
            },
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
        }
        with mock.patch.dict(os.environ, {"CONTENT_OUT": str(root)}), \
                mock.patch.object(
                    short_drama_autodraft.subprocess, "run", side_effect=render,
                ), mock.patch.object(
                    short_drama_autodraft.media_plan, "probe_media", return_value=probe,
                ):
            result = short_drama_autodraft._render_provider_preview(
                "project", "job", {
                    "shots": [{"file": "provider/shot.mp4"}],
                    "ratio": "9:16", "duration_ms": 5000,
                    "media_contract": {"audio_tracks": [], "subtitles": []},
                },
            )
        command = commands[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=720:1280", filters)
        self.assertNotIn("-an", command)
        self.assertIn("[outa]", command)
        self.assertEqual(
            "/api/gen/file/short_drama_autodraft/project/job/preview-720p.mp4",
            result["url"],
        )

    def test_real_ffmpeg_preview_preserves_ratio_audio_duration_and_subtitles(self):
        ffmpeg = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
        ffprobe = shutil.which(os.environ.get("FFPROBE_BIN", "ffprobe"))
        if not ffmpeg or not ffprobe:
            if os.environ.get("CI"):
                self.fail("CI must install FFmpeg and FFprobe for media contract tests")
            self.skipTest("real FFmpeg and FFprobe are not installed")
        root = Path(self.tmp.name) / "real-media"
        source_dir = root / "provider"
        source_dir.mkdir(parents=True)
        for ratio, source_size, expected in (
            ("16:9", "320x180", (1280, 720)),
            ("9:16", "180x320", (720, 1280)),
        ):
            source = source_dir / ("shot-" + ratio.replace(":", "-") + ".mp4")
            generated = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "testsrc2=size=%s:rate=25:duration=1" % source_size,
                "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, generated.returncode, generated.stderr)
            with mock.patch.dict(os.environ, {
                "CONTENT_OUT": str(root), "FFMPEG_BIN": ffmpeg,
                "FFPROBE_BIN": ffprobe,
            }):
                result = short_drama_autodraft._render_provider_preview(
                    "project-" + ratio.replace(":", "-"),
                    "job-" + ratio.replace(":", "-"),
                    {
                        "shots": [{"file": source.relative_to(root).as_posix()}],
                        "ratio": ratio, "duration_ms": 1000,
                        "media_contract": {
                            "audio_tracks": [],
                            "subtitles": [{
                                "start_ms": 0, "end_ms": 900,
                                "text": "real subtitle",
                            }],
                        },
                    },
                )
            probe = result["probe"]
            self.assertEqual(expected, (
                int(probe["video"]["width"]), int(probe["video"]["height"]),
            ))
            self.assertIsNotNone(probe["audio"])
            self.assertLessEqual(abs(int(probe["duration_ms"]) - 1000), 200)
            output = root / result["file"]
            subtitle = subprocess.run([
                ffprobe, "-v", "error", "-select_streams", "s",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(output),
            ], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, subtitle.returncode, subtitle.stderr)
            self.assertTrue(subtitle.stdout.strip())

    def test_unconfirmed_project_cannot_start(self):
        other = short_drama.create_project(
            self.db, "alice",
            {
                "title": "未准备项目", "synopsis": "一个尚未完成准备的短剧项目。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "写实", "target_platform": "抖音",
                "point_budget": 0,
            },
        )
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.start_job(
                self.db, "alice", "alice",
                {"project_id": other["id"], "plan_id": ""},
                "unconfirmed-key",
            )
        self.assertEqual("confirmed_plan_required", raised.exception.code)

    def test_cancel_then_retry_preserves_same_task_identity(self):
        job = self._start()
        canceled = short_drama_autodraft.cancel_job(
            self.db, "alice",
            {"project_id": self.project["id"], "job_id": job["id"]},
        )
        self.assertEqual("canceled", canceled["status"])
        retried = short_drama_autodraft.retry_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "job_id": job["id"]},
        )
        self.assertEqual(job["id"], retried["id"])
        self.assertEqual("queued", retried["status"])

    def test_lost_charge_response_is_recovered_from_ledger(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "0"}
        ):
            conn = self.db()
            try:
                raw = conn.execute(
                    "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                    (self.plan_id,),
                ).fetchone()[0]
                plan = json.loads(raw)
                plan.setdefault("estimate", {})["points"] = 7
                conn.execute(
                    "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                    (json.dumps(plan), self.plan_id),
                )
                conn.commit()
            finally:
                conn.close()

            def lost_response(*_args):
                raise TimeoutError("charge response lost")

            job = short_drama_autodraft.start_job(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "plan_id": self.plan_id},
                "recovered-charge-key",
                deduct_points=lost_response,
                charge_lookup=lambda _key: {
                    "username": "alice", "delta": -7,
                },
            )
        self.assertEqual("queued", job["status"])
        self.assertEqual(7, job["cost"])
        conn = self.db()
        try:
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        finally:
            conn.close()
        self.assertEqual(7, usage["spent_points"])
        self.assertEqual(0, usage["reserved_points"])

    def test_start_rechecks_unified_project_budget_before_charging(self):
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=5 WHERE id=?",
                (self.project["id"],),
            )
            raw = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0]
            plan = json.loads(raw)
            plan.setdefault("estimate", {})["points"] = 7
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        deduct = mock.Mock()
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "0"}
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_job(
                    self.db,
                    "alice",
                    "alice",
                    {"project_id": self.project["id"], "plan_id": self.plan_id},
                    "budget-blocked-key",
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("point_budget_exceeded", raised.exception.code)
        deduct.assert_not_called()

    def test_single_shot_quote_charge_submit_poll_and_archive(self):
        class FakeProvider:
            name = "heygen_cinematic"
            configured = True

            def create_job(self, request):
                self.request = request
                return {"provider_job_id": "provider-job-1"}

            def get_job(self, provider_job_id):
                return {
                    "status": "completed",
                    "result_url": "https://provider.example/result.mp4",
                }

            def fetch_result(self, provider_job_id, result_url):
                return {
                    "provider_job_id": provider_job_id,
                    "file": "video/provider-job-1.mp4",
                    "url": "/api/files/video/provider-job-1.mp4",
                }

        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
                "HQ_SHORT_DRAMA_PROVIDER_SHOT_POINTS_PER_SECOND": "10",
            },
        ):
            quote = self._provider_quote()
            charged = []
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]},
                "provider-shot-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda user, cost, reason, key: charged.append(
                    (user, cost, reason, key)
                ),
                project_usage=short_drama._project_point_usage,
            )
            provider = FakeProvider()
            with mock.patch(
                "content_domains.short_drama_autodraft.load_from_environment",
                return_value=provider,
            ):
                completed = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual(quote["cost"], charged[0][1])
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual("/api/files/video/provider-job-1.mp4", completed["result"]["url"])
        self.assertEqual("heygen-avatar-1", provider.request["provider_avatar_id"])
        conn = self.db()
        try:
            version = conn.execute(
                "SELECT status,url FROM short_drama_provider_shot_versions "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()
            attempt = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(("ready", "/api/files/video/provider-job-1.mp4"), version)
        self.assertEqual("done", attempt[0])

    def test_single_shot_idempotency_replay_does_not_charge_twice(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            deduct = mock.Mock()
            first = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, "same-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
            replay = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, "same-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, deduct.call_count)

    def test_single_shot_start_rejects_quote_after_avatar_binding_changes(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            changed_avatar = dict(self._provider_avatar())
            changed_avatar["provider_avatar_id"] = "heygen-avatar-replaced"
            deduct = mock.Mock()
            with self.assertRaises(
                short_drama_autodraft.AutodraftError
            ) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "stale-avatar-provider-key",
                    avatar_lookup=lambda *_args: changed_avatar,
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("provider_quote_stale", raised.exception.code)
        deduct.assert_not_called()

    def test_single_shot_billing_recovery_requires_two_empty_ledger_checks(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]},
                "billing-recovery-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda *_args: None,
                project_usage=short_drama._project_point_usage,
            )
            conn = self.db()
            try:
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs "
                    "SET status='billing',created_at=100,error_json=NULL "
                    "WHERE id=?",
                    (job["id"],),
                )
                conn.execute(
                    "UPDATE short_drama_provider_shot_attempts "
                    "SET state='accepted' WHERE job_id=?",
                    (job["id"],),
                )
                conn.commit()
            finally:
                conn.close()
            with mock.patch(
                "content_domains.short_drama_autodraft.time.time",
                return_value=1000,
            ):
                first = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    charge_lookup=lambda _key: None,
                )
            with mock.patch(
                "content_domains.short_drama_autodraft.time.time",
                return_value=1061,
            ):
                second = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    charge_lookup=lambda _key: None,
                )
        self.assertEqual("billing", first["status"])
        self.assertEqual("failed", second["status"])
        self.assertEqual("billing_not_committed", second["error"]["code"])

    def test_single_shot_provider_rejection_refunds_once(self):
        class RejectingProvider:
            name = "heygen_cinematic"
            configured = True

            def create_job(self, request):
                raise RuntimeError("provider rejected request")

        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, "refund-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda *_args: None,
                project_usage=short_drama._project_point_usage,
            )
            refunds = []
            with mock.patch(
                "content_domains.short_drama_autodraft.load_from_environment",
                return_value=RejectingProvider(),
            ):
                failed = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=lambda user, cost, reason, key: refunds.append(
                        (user, cost, reason, key)
                    ),
                )
        self.assertEqual("failed", failed["status"])
        self.assertEqual(1, len(refunds))
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)


if __name__ == "__main__":
    unittest.main()
