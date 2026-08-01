import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_refinement


class Handler:
    def __init__(self, path, body=None, key="refinement-test-key", token="alice"):
        self.path = path
        self.body = body
        self.token = token
        self.headers = {"Idempotency-Key": key}
        self.response = None

    def _token(self):
        return self.token

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaRefinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        self.free = mock.patch.dict(
            os.environ, {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "1",
                "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "demo",
                "CONTENT_OUT": self.tmp.name,
            }
        )
        self.free.start()
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db, "alice", {
                "title": "精修与交付测试",
                "synopsis": "朋友在公园发现一封来自未来的信。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "target_platform": "抖音",
                "point_budget": 0,
            },
        )
        now = int(time.time())
        shots = [
            {
                "shot_key": "shot_01", "sort_order": 1, "status": "ready",
                "start_ms": 0, "end_ms": 5000, "issue": None,
            },
            {
                "shot_key": "shot_02", "sort_order": 2, "status": "degraded",
                "start_ms": 5000, "end_ms": 10000,
                "issue": {"code": "safe_visual_fallback", "shot_key": "shot_02"},
            },
        ]
        issues = [{
            "code": "safe_visual_fallback", "shot_key": "shot_02",
            "message": "使用安全替代画面",
        }]
        manifest = {
            "resolution": "720p", "duration_ms": 10000,
            "shots": shots, "issues": issues,
            "media_contract": {
                "contract_version": "short-drama-locked-media-v1",
                "delivery_eligible": True,
                "audio_hash": "audio-hash",
                "subtitle_hash": "subtitle-hash",
                "timeline_hash": "timeline-hash",
                "material_hash": "material-hash",
                "subtitle_required": True,
            },
        }
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_autodraft_versions "
                "(id,project_id,job_id,version,plan_id,status,url,manifest_json,"
                "input_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "draft-v1", self.project["id"], "draft-job", 1, "plan-v1",
                    "degraded", "/assets/meiye_video.mp4",
                    json.dumps(manifest), "draft-hash", now,
                ),
            )
            for index, shot_key in enumerate(("shot_01", "shot_02"), 1):
                relative = "provider/%s-v2.mp4" % shot_key
                path = Path(self.tmp.name) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(("provider-media-%s" % shot_key).encode())
                job_id = "provider-job-%s" % shot_key
                version_id = "provider-version-%s" % shot_key
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_jobs "
                    "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                    "character_key,avatar_id,provider,provider_job_id,status,progress,"
                    "poll_count,input_hash,request_json,cost,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?, 'succeeded',100,1,?,'{}',0,?,?)",
                    (
                        job_id, self.project["id"], "alice", "alice", "plan-v1",
                        shot_key, "lead", "avatar-1", "test_provider",
                        "external-%s" % shot_key, "input-%s" % shot_key, now, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions "
                    "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                    "status,file,url,input_hash,created_at) "
                    "VALUES (?,?,?,?,2,'test_provider',?,'ready',?,?,?,?)",
                    (
                        version_id, self.project["id"], job_id, shot_key,
                        "external-%s" % shot_key, relative,
                        "/api/gen/file/" + relative, "input-%s" % shot_key, now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        self.real_refinement_renderer = (
            short_drama_refinement._render_refinement_preview
        )
        self.refinement_renderer = mock.patch.object(
            short_drama_refinement, "_render_refinement_preview",
            side_effect=self._fake_refinement_preview,
        )
        self.refinement_renderer_mock = self.refinement_renderer.start()

    def tearDown(self):
        self.refinement_renderer.stop()
        self.free.stop()
        self.tmp.cleanup()

    def _fake_refinement_preview(self, conn, job, source):
        shots, assets = [], []
        for original in source["shots"]:
            shot = dict(original)
            shot_key = str(shot["shot_key"])
            requested = (
                job["replacement_provider_version_id"]
                if shot_key == job["shot_key"]
                else str(shot.get("provider_version_id") or "")
            )
            asset = short_drama_refinement._provider_asset(
                conn, job["project_id"], shot_key, requested or None,
            )
            file_hash = short_drama_refinement._file_hash(
                Path(self.tmp.name) / asset["file"]
            )
            shot.update({
                "status": "ready", "issue": None,
                "visual_source": (
                    "provider_regeneration"
                    if shot_key == job["shot_key"] else "provider"
                ),
                "provider": asset["provider"],
                "provider_version_id": asset["id"],
                "provider_version": int(asset["version"]),
                "provider_job_id": asset["provider_job_id"],
                "file": asset["file"], "url": asset["url"],
                "file_hash": file_hash, "input_hash": asset["input_hash"],
            })
            shots.append(shot)
            assets.append({
                "id": asset["id"], "shot_key": shot_key,
                "input_hash": asset["input_hash"], "file_hash": file_hash,
            })
        relative = "refinement/%s/preview-720p.mp4" % job["id"]
        output = Path(self.tmp.name) / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        source_url = str(source.get("url") or "")
        source_file = None
        if source_url.startswith("/api/gen/file/"):
            source_file = Path(self.tmp.name) / source_url.removeprefix(
                "/api/gen/file/"
            )
        if source_file and source_file.is_file():
            shutil.copyfile(source_file, output)
            with output.open("ab") as handle:
                handle.write(("\nrefinement:" + job["id"]).encode())
        else:
            output.write_bytes(("preview:" + job["id"]).encode())
        file_hash = short_drama_refinement._file_hash(output)
        manifest = json.loads(conn.execute(
            "SELECT manifest_json FROM short_drama_autodraft_versions WHERE id=?",
            (source["source_draft_version_id"],),
        ).fetchone()[0])
        media = dict(manifest.get("media_contract") or {})
        media["material_hash"] = short_drama_refinement._hash(assets)
        return {
            "url": "/api/gen/file/" + relative,
            "file": relative, "file_hash": file_hash,
            "probe": {"video": {"width": 1280, "height": 720}, "audio": {}},
            "media_contract": media, "shots": shots,
        }

    def add_provider_replacement(self, shot_key):
        conn = self.db()
        try:
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM "
                "short_drama_provider_shot_versions WHERE project_id=? AND shot_key=?",
                (self.project["id"], shot_key),
            ).fetchone()[0])
            now = int(time.time())
            job_id = "provider-job-%s-%d" % (shot_key, version)
            version_id = "provider-version-%s-%d" % (shot_key, version)
            relative = "provider/%s-v%d.mp4" % (shot_key, version)
            path = Path(self.tmp.name) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("provider-media-%s-v%d" % (shot_key, version)).encode())
            conn.execute(
                "INSERT INTO short_drama_provider_shot_jobs "
                "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                "character_key,avatar_id,provider,provider_job_id,status,progress,"
                "poll_count,input_hash,request_json,cost,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'succeeded',100,1,?,'{}',0,?,?)",
                (
                    job_id, self.project["id"], "alice", "alice", "plan-v1",
                    shot_key, "lead", "avatar-1", "test_provider",
                    "external-%s-%d" % (shot_key, version),
                    "input-%s-%d" % (shot_key, version), now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_provider_shot_versions "
                "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                "status,file,url,input_hash,created_at) "
                "VALUES (?,?,?,?,?,'test_provider',?,'ready',?,?,?,?)",
                (
                    version_id, self.project["id"], job_id, shot_key, version,
                    "external-%s-%d" % (shot_key, version), relative,
                    "/api/gen/file/" + relative,
                    "input-%s-%d" % (shot_key, version), now,
                ),
            )
            conn.commit()
            return version_id
        finally:
            conn.close()

    def repaired_version(self, key):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            key,
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        return version

    def confirmed_version(self, key):
        version = self.repaired_version(key)
        self.confirm_version(version)
        return version

    def acceptance_body(self, version):
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        return {
            "project_id": self.project["id"],
            "version_id": version["id"],
            "checklist": {
                key: True for key in short_drama_refinement.ACCEPTANCE_CHECKS
            },
            "source_hashes": workspace["acceptance_requirements"]["source_hashes"],
        }

    def confirm_version(self, version):
        return short_drama_refinement.confirm_refinement(
            self.db, "alice", "alice", self.acceptance_body(version)
        )

    def test_workspace_seeds_refinement_from_playable_draft(self):
        result = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("refining", result["state"])
        self.assertEqual(1, result["current_refinement"]["version"])
        self.assertEqual(1, len(result["current_refinement"]["issues"]))
        self.assertEqual("development_free", result["billing"]["mode"])
        self.assertTrue(result["billing"]["delivery_enabled"])
        self.assertFalse(result["billing"]["deliverable"])

    def test_local_ffmpeg_capability_enables_real_1080p_delivery(self):
        process = mock.Mock(
            returncode=0, stdout=" V..... libx264 A..... aac ", stderr=""
        )
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "CONTENT_OUT": self.tmp.name,
        }), mock.patch.object(
            short_drama_refinement.subprocess, "run", return_value=process
        ):
            capability = short_drama_refinement._delivery_capability()
        self.assertTrue(capability["delivery_enabled"])
        self.assertTrue(capability["deliverable"])
        self.assertEqual("local_ffmpeg", capability["adapter"])
        self.assertEqual("local_1080p_renderer", capability["reason"])

    def _assert_real_formal_delivery(self, ratio, preview_size, expected_size):
        ffmpeg = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
        ffprobe = shutil.which(os.environ.get("FFPROBE_BIN", "ffprobe"))
        if not ffmpeg or not ffprobe:
            if os.environ.get("CI"):
                self.fail("CI must install FFmpeg and FFprobe for media contract tests")
            self.skipTest("real FFmpeg and FFprobe are not installed")
        # Keep every provider asset, refinement preview, and formal delivery
        # under the same controlled CONTENT_OUT root. Acceptance evidence binds
        # physical paths and hashes, so changing the root after confirmation
        # must (correctly) invalidate it.
        root = Path(self.tmp.name)
        source = root / "drafts" / "preview.mp4"
        source.parent.mkdir(parents=True)
        subtitle_file = root / "locked.srt"
        subtitle_file.write_text(
            "1\n00:00:00,000 --> 00:00:29,900\nlocked subtitle\n",
            encoding="utf-8",
        )
        generated = subprocess.run([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            "color=c=blue:size=%s:rate=25:duration=30" % preview_size,
            "-f", "lavfi", "-i", "sine=frequency=660:duration=30",
            "-f", "srt", "-i", str(subtitle_file),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-c:s", "mov_text", "-t", "30", str(source),
        ], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, generated.returncode, generated.stderr)
        conn = self.db()
        try:
            manifest = json.loads(conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()[0])
            manifest["duration_ms"] = 30000
            manifest["shots"][0].update({"start_ms": 0, "end_ms": 15000})
            manifest["shots"][1].update({"start_ms": 15000, "end_ms": 30000})
            conn.execute(
                "UPDATE short_drama_projects SET ratio=? WHERE id=?",
                (ratio, self.project["id"]),
            )
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET url=?,manifest_json=? "
                "WHERE id='draft-v1'",
                (
                    "/api/gen/file/drafts/preview.mp4",
                    json.dumps(manifest),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        version = self.repaired_version("real-delivery-" + ratio)
        self.confirm_version(version)
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "CONTENT_OUT": str(root), "FFMPEG_BIN": ffmpeg,
            "FFPROBE_BIN": ffprobe,
        }):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "real-delivery-job-" + ratio,
            )
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"], job.get("error"))
        output = root / job["result"]["url"].removeprefix("/api/gen/file/")
        probe = short_drama_refinement.media_plan.probe_media(output)
        self.assertEqual(expected_size, (
            int(probe["video"]["width"]), int(probe["video"]["height"]),
        ))
        self.assertIsNotNone(probe["audio"])
        self.assertLessEqual(abs(int(probe["duration_ms"]) - 30000), 300)
        subtitle = subprocess.run([
            ffprobe, "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(output),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, subtitle.returncode, subtitle.stderr)
        self.assertTrue(subtitle.stdout.strip())

    def test_real_ffmpeg_horizontal_formal_delivery_contract(self):
        self._assert_real_formal_delivery("16:9", "1280x720", (1920, 1080))

    def test_real_ffmpeg_vertical_formal_delivery_contract(self):
        self._assert_real_formal_delivery("9:16", "720x1280", (1080, 1920))

    def test_single_shot_job_creates_new_issue_free_version(self):
        preview = short_drama_refinement.preview_change(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
        )
        self.assertEqual(["shot_02"], preview["affected_shots"])
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "redo-shot-02",
        )
        replay = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "redo-shot-02",
        )
        self.assertEqual(job["id"], replay["id"])
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("succeeded", job["status"])
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual([], workspace["current_refinement"]["issues"])

    def test_redo_publishes_new_preview_url_and_physical_hash(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        version = self.repaired_version("physical-redo")
        self.assertNotEqual(before["url"], version["url"])
        self.assertTrue(version["preview_file_hash"])
        target = next(
            shot for shot in version["shots"] if shot["shot_key"] == "shot_02"
        )
        self.assertEqual("provider_regeneration", target["visual_source"])
        self.assertTrue(target["file_hash"])
        self.assertEqual("provider-version-shot_02", target["provider_version_id"])
        self.assertGreaterEqual(self.refinement_renderer_mock.call_count, 1)
        output = Path(self.tmp.name) / version["media"]["preview_file"]
        self.assertTrue(output.is_file())
        self.assertEqual(
            version["preview_file_hash"],
            short_drama_refinement._file_hash(output),
        )

    def test_redo_failure_preserves_issue_and_is_not_reexecuted(self):
        failed_renderer = mock.Mock(side_effect=RuntimeError("renderer failed"))
        with mock.patch.object(
            short_drama_refinement, "_render_refinement_preview", failed_renderer,
        ):
            job = short_drama_refinement.start_refinement_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "shot_key": "shot_02",
                    "replacement_provider_version_id": "provider-version-shot_02",
                }, "failed-physical-redo",
            )
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
            repeated = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["error"]["issue_preserved"])
        self.assertEqual("failed", repeated["status"])
        self.assertEqual(1, failed_renderer.call_count)
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual("shot_02", current["issues"][0]["shot_key"])

    def test_issue_revision_change_while_job_runs_fails_closed(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_02",
                "source_version_id": source["id"],
                "replacement_provider_version_id": "provider-version-shot_02",
            }, "issue-revision-race",
        )
        marked = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "version_id": source["id"],
                "shot_key": "shot_02", "issue_code": "newer_review_issue",
                "message": "A newer issue revision supersedes the queued redo",
            },
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertEqual("refinement_source_stale", job["error"]["code"])
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual(marked["id"], current["id"])
        self.assertEqual("newer_review_issue", current["issues"][0]["code"])

    def test_real_refinement_path_validates_provider_files_and_calls_assembler(self):
        output_relative = "rendered/real-refinement.mp4"
        output = Path(self.tmp.name) / output_relative
        output.parent.mkdir(parents=True, exist_ok=True)

        def assemble(_project_id, _job_id, assembly):
            self.assertEqual(2, len(assembly["shots"]))
            output.write_bytes(b"new-immutable-refinement-preview")
            return {
                "file": output_relative,
                "url": "/api/gen/file/" + output_relative,
                "probe": {
                    "duration_ms": 30000,
                    "video": {"width": 1280, "height": 720},
                    "audio": {"codec": "aac"},
                },
            }

        assembler = mock.Mock(side_effect=assemble)
        locked_media = {
            "contract_version": "short-drama-locked-media-v1",
            "delivery_eligible": True,
            "evidence_source": "locked_voice_tables",
            "audio_hash": "audio-hash", "subtitle_hash": "subtitle-hash",
            "timeline_hash": "timeline-hash", "subtitle_required": True,
            "audio_tracks": [], "subtitles": [],
        }
        provider_probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement, "_render_refinement_preview",
            side_effect=self.real_refinement_renderer,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media",
            return_value=provider_probe,
        ), mock.patch.object(
            short_drama_autodraft, "_locked_media_contract",
            return_value=locked_media,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview", assembler,
        ):
            job = short_drama_refinement.start_refinement_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "shot_key": "shot_02",
                    "replacement_provider_version_id": "provider-version-shot_02",
                }, "real-refinement-path",
            )
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"], job.get("error"))
        self.assertEqual(1, assembler.call_count)
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual("/api/gen/file/" + output_relative, version["url"])
        self.assertEqual(
            short_drama_refinement._file_hash(output),
            version["preview_file_hash"],
        )

    def test_confirm_quote_and_formal_delivery_snapshot(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "fix-for-delivery",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        version = workspace["current_refinement"]
        confirmed = self.confirm_version(version)
        self.assertEqual("confirmed", confirmed["status"])
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice",
            {"project_id": self.project["id"], "version_id": version["id"]},
        )
        self.assertEqual(0, quote["cost"])
        delivery = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "formal-delivery",
        )
        for _ in range(6):
            delivery = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], delivery["id"]
            )
        self.assertEqual("succeeded", delivery["status"])
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("demo_ready", workspace["state"])
        self.assertEqual("source", workspace["current_delivery"]["snapshot"]["resolution"])
        self.assertEqual(
            "demo_preview",
            workspace["current_delivery"]["snapshot"]["output_kind"],
        )
        self.assertFalse(workspace["current_delivery"]["snapshot"]["deliverable"])
        self.assertTrue(workspace["current_delivery"]["snapshot"]["immutable"])

    def test_production_delivery_is_closed_without_real_executor(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "close-paid-delivery",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        deduct = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "0",
                "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "production",
                "HQ_SHORT_DRAMA_FORMAL_COST": "80",
            },
            clear=False,
        ):
            workspace = short_drama_refinement.workspace(
                self.db, "alice", "alice", self.project["id"]
            )
            self.assertEqual("disabled", workspace["billing"]["mode"])
            self.assertFalse(workspace["billing"]["delivery_enabled"])
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.create_delivery_quote(
                    self.db, "alice",
                    {"project_id": self.project["id"], "version_id": version["id"]},
                )
            with self.assertRaises(short_drama_refinement.RefinementError) as start_error:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": "must-not-create-anything",
                    },
                    "disabled-production-delivery",
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("formal_delivery_unavailable", raised.exception.code)
        self.assertEqual(
            "formal_delivery_unavailable", start_error.exception.code
        )
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_quotes"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_attempts"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_jobs"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_unresolved_issues_block_confirmation(self):
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": workspace["current_refinement"]["id"],
                },
            )
        self.assertEqual("refinement_issues_remaining", raised.exception.code)

    def test_issue_free_confirmation_requires_complete_checklist_and_current_hashes(self):
        version = self.repaired_version("acceptance-contract")
        with self.assertRaises(short_drama_refinement.RefinementError) as missing:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_acceptance_incomplete", missing.exception.code)
        stale = self.acceptance_body(version)
        stale["source_hashes"] = dict(stale["source_hashes"], audio="changed")
        with self.assertRaises(short_drama_refinement.RefinementError) as rejected:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", stale,
            )
        self.assertEqual("refinement_acceptance_stale", rejected.exception.code)
        confirmed = self.confirm_version(version)
        self.assertTrue(confirmed["acceptance"]["valid"])
        self.assertEqual("alice", confirmed["acceptance"]["accepted_by"])

    def test_mark_issue_invalidates_acceptance_until_redo_and_reacceptance(self):
        version = self.confirmed_version("accept-before-issue")
        marked = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
                "shot_key": "shot_01", "issue_code": "continuity_error",
                "message": "人物动作不连续",
            },
        )
        self.assertEqual("draft", marked["status"])
        self.assertEqual("shot_01", marked["issues"][0]["shot_key"])
        conn = self.db()
        try:
            acceptance = conn.execute(
                "SELECT invalidation_reason FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("issue_reported", acceptance[0])
        replacement_id = self.add_provider_replacement("shot_01")
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_01",
                "source_version_id": marked["id"],
                "replacement_provider_version_id": replacement_id,
            }, "redo-reported-shot",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual([], current["current_refinement"]["issues"])
        self.assertIsNone(current["acceptance"])

    def test_marked_v3_rejects_historical_v2_until_new_v4_exists(self):
        current = self.confirmed_version("provider-monotonic-v2")
        marked_v2 = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "version_id": current["id"],
                "shot_key": "shot_02", "issue_code": "visual_regression",
                "message": "V2 needs a new Provider render",
            },
        )
        v3_id = self.add_provider_replacement("shot_02")
        v3_job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_02",
                "source_version_id": marked_v2["id"],
                "replacement_provider_version_id": v3_id,
            }, "provider-monotonic-v3",
        )
        for _ in range(4):
            v3_job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], v3_job["id"]
            )
        self.assertEqual("succeeded", v3_job["status"])
        current_v3 = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        target_v3 = next(
            item for item in current_v3["shots"]
            if item["shot_key"] == "shot_02"
        )
        self.assertEqual(3, target_v3["provider_version"])

        marked_v3 = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "version_id": current_v3["id"], "shot_key": "shot_02",
                "issue_code": "visual_regression",
                "message": "V3 has a newly reported visual problem",
            },
        )
        issue = marked_v3["issues"][0]
        self.assertEqual(3, issue["provider_version_floor"])
        self.assertEqual(v3_id, issue["source_provider_version_id"])
        with self.assertRaises(short_drama_refinement.RefinementError) as old:
            short_drama_refinement.start_refinement_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "shot_key": "shot_02",
                    "source_version_id": marked_v3["id"],
                    "replacement_provider_version_id": "provider-version-shot_02",
                }, "provider-history-v2-must-fail",
            )
        self.assertEqual("refinement_new_provider_asset_required", old.exception.code)
        unchanged = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual(marked_v3["id"], unchanged["id"])
        self.assertEqual("shot_02", unchanged["issues"][0]["shot_key"])

        v4_id = self.add_provider_replacement("shot_02")
        v4_job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_02",
                "source_version_id": marked_v3["id"],
                "replacement_provider_version_id": v4_id,
            }, "provider-monotonic-v4",
        )
        for _ in range(4):
            v4_job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], v4_job["id"]
            )
        self.assertEqual("succeeded", v4_job["status"])
        self.assertEqual(issue["issue_id"], v4_job["result"]["issue_revision"])
        self.assertEqual(
            v3_id, v4_job["result"]["source_provider_version_id"]
        )
        self.assertEqual(
            "provider-job-shot_02-4",
            v4_job["result"]["replacement_provider_job_id"],
        )
        repaired_v4 = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual([], repaired_v4["issues"])
        target_v4 = next(
            item for item in repaired_v4["shots"]
            if item["shot_key"] == "shot_02"
        )
        self.assertEqual(4, target_v4["provider_version"])

    def test_locked_audio_change_invalidates_previous_acceptance(self):
        version = self.confirmed_version("accept-before-audio-change")
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()
            manifest = json.loads(row[0])
            manifest["media_contract"]["audio_hash"] = "new-audio-hash"
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET manifest_json=? WHERE id='draft-v1'",
                (json.dumps(manifest),),
            )
            conn.commit()
        finally:
            conn.close()
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertFalse(workspace["acceptance"]["valid"])
        self.assertEqual(
            "source_changed", workspace["acceptance"]["invalidation_reason"]
        )
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_acceptance_required", raised.exception.code)

    def test_delivery_render_exception_is_persisted_as_terminal_failure(self):
        version = self.confirmed_version("terminal-delivery-failure")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        job = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "quote_token": quote["quote_token"],
            }, "terminal-render-job",
        )
        for _ in range(3):
            job = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        with mock.patch.object(
            short_drama_refinement, "_complete_delivery",
            side_effect=OSError("renderer unavailable"),
        ) as render:
            job = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertEqual("formal_render_failed", job["error"]["code"])
        replay = short_drama_refinement.get_delivery_job(
            self.db, "alice", self.project["id"], job["id"]
        )
        self.assertEqual("failed", replay["status"])
        self.assertEqual(job["poll_count"], replay["poll_count"])
        render.assert_called_once()

    def test_local_ffmpeg_capability_reports_missing_tools(self):
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "FFMPEG_BIN": "missing-ffmpeg", "FFPROBE_BIN": "missing-ffprobe",
        }), mock.patch.object(
            short_drama_refinement.subprocess, "run",
            side_effect=FileNotFoundError("missing"),
        ):
            capability = short_drama_refinement._delivery_capability()
        self.assertFalse(capability["delivery_enabled"])
        self.assertFalse(capability["deliverable"])
        self.assertEqual("missing_ffmpeg", capability["reason"])

    def test_local_ffmpeg_capability_rejects_each_required_dependency(self):
        def run_result(command, encoders=" V..... libx264 A..... aac "):
            if command[-1] == "-encoders":
                return mock.Mock(returncode=0, stdout=encoders, stderr="")
            return mock.Mock(returncode=0, stdout="available", stderr="")

        cases = (
            ("ffprobe", "missing_ffprobe", None),
            ("libx264", "missing_libx264", " A..... aac "),
            ("aac", "missing_aac", " V..... libx264 "),
            ("output_writable", "missing_output_writable", None),
        )
        for dependency, expected_reason, encoders in cases:
            with self.subTest(dependency=dependency), mock.patch.dict(
                os.environ,
                {"HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg"},
            ), mock.patch.object(
                short_drama_refinement.subprocess, "run",
                side_effect=lambda command, **kwargs: run_result(
                    command, encoders if encoders is not None
                    else " V..... libx264 A..... aac "
                ),
            ) as run, mock.patch.object(
                short_drama_refinement.tempfile, "NamedTemporaryFile",
                side_effect=(OSError("read only")
                             if dependency == "output_writable" else None),
                wraps=(None if dependency == "output_writable"
                       else tempfile.NamedTemporaryFile),
            ):
                if dependency == "ffprobe":
                    run.side_effect = lambda command, **kwargs: (
                        mock.Mock(returncode=1, stdout="", stderr="missing")
                        if command[0] == os.environ.get("FFPROBE_BIN", "ffprobe")
                        else run_result(command)
                    )
                capability = short_drama_refinement._delivery_capability()
            self.assertFalse(capability["delivery_enabled"])
            self.assertFalse(capability["deliverable"])
            self.assertFalse(capability["checks"][dependency])
            self.assertEqual(expected_reason, capability["reason"])

    def test_delivery_quote_is_single_use_across_new_idempotency_keys(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"}, "repair",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice",
            {"project_id": self.project["id"], "version_id": version["id"]},
        )
        short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "delivery-1",
        )
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
                "delivery-2",
            )
        self.assertEqual("delivery_quote_consumed", raised.exception.code)

    def test_delivery_same_idempotency_key_replays_without_duplicate_charge(self):
        version = self.confirmed_version("repair-for-replay")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice",
            {"project_id": self.project["id"], "version_id": version["id"]},
        )
        deduct = mock.Mock()
        first = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "delivery-replay",
            deduct_points=deduct,
            project_usage=short_drama._project_point_usage,
        )
        replay = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "delivery-replay",
            deduct_points=deduct,
            project_usage=short_drama._project_point_usage,
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        deduct.assert_not_called()

    def test_charge_then_job_link_failure_refunds_and_closes_attempt(self):
        version = self.confirmed_version("repair-for-refund")
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        refund = mock.Mock()
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice",
                {"project_id": self.project["id"], "version_id": version["id"]},
            )

            def deduct_then_consume(*_args):
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_delivery_quotes "
                        "SET consumed_job_id='raced-job' WHERE token=?",
                        (quote["quote_token"],),
                    )
                    conn.commit()
                finally:
                    conn.close()

            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "delivery-refund",
                    deduct_points=deduct_then_consume,
                    refund_points=refund,
                    project_usage=short_drama._project_point_usage,
                )
        refund.assert_called_once()
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-refund'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)

    def test_refund_failure_is_persisted_for_recovery(self):
        version = self.confirmed_version("repair-for-refund-pending")
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice",
                {"project_id": self.project["id"], "version_id": version["id"]},
            )

            def deduct_then_consume(*_args):
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_delivery_quotes "
                        "SET consumed_job_id='raced-job' WHERE token=?",
                        (quote["quote_token"],),
                    )
                    conn.commit()
                finally:
                    conn.close()

            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "delivery-refund-pending",
                    deduct_points=deduct_then_consume,
                    refund_points=mock.Mock(
                        side_effect=RuntimeError("refund unavailable")
                    ),
                    project_usage=short_drama._project_point_usage,
                )
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-refund-pending'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refund_pending", state)
        points_domain = mock.Mock()
        recovered = short_drama_refinement.retry_delivery_attempt_refunds(
            self.db, points_domain
        )
        self.assertEqual(1, recovered)
        points_domain.refund_points.assert_called_once()
        self.assertEqual(
            "short-drama-delivery-refund:",
            points_domain.refund_points.call_args.kwargs[
                "transaction_key"
            ][:28],
        )
        conn = self.db()
        try:
            recovered_state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-refund-pending'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", recovered_state)
        self.assertEqual(
            0,
            short_drama_refinement.retry_delivery_attempt_refunds(
                self.db, points_domain
            ),
        )
        points_domain.refund_points.assert_called_once()

    def test_formal_delivery_rechecks_project_budget_before_reserving_points(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "repair-for-budget",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=10 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice",
                {"project_id": self.project["id"], "version_id": version["id"]},
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "budget-blocked-delivery",
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("point_budget_exceeded", raised.exception.code)

    def test_http_delivery_route_counts_real_spend_and_reservations_before_charge(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "repair-for-http-budget",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        now = int(time.time())
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=100 WHERE id=?",
                (self.project["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES('charged-old','alice',?,'charged-old','hash-charged',"
                "'quote-charged',50,'charged',?,?)",
                (self.project["id"], now, now),
            )
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES('reserved-old','alice',?,'reserved-old','hash-old',"
                "'quote-old',10,'accepted',?,?)",
                (self.project["id"], now, now),
            )
            conn.commit()
        finally:
            conn.close()

        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote_handler = Handler(
                "/api/gen/short-drama/delivery/quote",
                body={
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
                key="http-budget-quote",
            )
            self.assertTrue(short_drama.dispatch_http(
                quote_handler, "POST", self.db, verify
            ))
            self.assertEqual(200, quote_handler.response[0])
            job_handler = Handler(
                "/api/gen/short-drama/delivery/jobs",
                body={
                    "project_id": self.project["id"],
                    "quote_token": quote_handler.response[1]["quote_token"],
                },
                key="http-budget-job",
            )
            self.assertTrue(short_drama.dispatch_http(
                job_handler, "POST", self.db, verify, deduct_points=deduct
            ))
        self.assertEqual(409, job_handler.response[0])
        self.assertEqual("point_budget_exceeded", job_handler.response[1]["code"])
        deduct.assert_not_called()
        conn = self.db()
        try:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts "
                "WHERE id NOT IN ('charged-old','reserved-old')"
            ).fetchone()[0]
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, attempts)
        self.assertEqual(0, jobs)

    def test_http_routes_expose_workspace_refinement_and_delivery_contracts(self):
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        workspace = Handler(
            "/api/gen/short-drama/refinement?project_id=" + self.project["id"]
        )
        self.assertTrue(short_drama.dispatch_http(workspace, "GET", self.db, verify))
        self.assertEqual(200, workspace.response[0])
        self.assertEqual("refining", workspace.response[1]["state"])

        preview = Handler(
            "/api/gen/short-drama/refinement/changes/preview",
            body={"project_id": self.project["id"], "shot_key": "shot_02"},
        )
        self.assertTrue(short_drama.dispatch_http(preview, "POST", self.db, verify))
        self.assertEqual(200, preview.response[0])
        self.assertEqual(["shot_02"], preview.response[1]["affected_shots"])

        rejected = Handler(
            "/api/gen/short-drama/refinement/confirm",
            body={
                "project_id": self.project["id"],
                "version_id": workspace.response[1]["current_refinement"]["id"],
            },
        )
        self.assertTrue(short_drama.dispatch_http(rejected, "POST", self.db, verify))
        self.assertEqual(409, rejected.response[0])
        self.assertEqual("refinement_issues_remaining", rejected.response[1]["code"])

        issue = Handler(
            "/api/gen/short-drama/refinement/issues",
            body={
                "project_id": self.project["id"],
                "version_id": workspace.response[1]["current_refinement"]["id"],
                "shot_key": "shot_01", "issue_code": "continuity_error",
                "message": "动作不连续",
            },
        )
        self.assertTrue(short_drama.dispatch_http(issue, "POST", self.db, verify))
        self.assertEqual(200, issue.response[0])
        self.assertEqual("shot_01", issue.response[1]["issues"][-1]["shot_key"])


if __name__ == "__main__":
    unittest.main()
