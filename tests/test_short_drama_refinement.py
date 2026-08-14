import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
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
        self.complete_assembly = mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={
                "available": True,
                "reassembly_required": False,
                "message": "complete preview fixture",
            },
        )
        self.complete_assembly.start()

    def tearDown(self):
        self.complete_assembly.stop()
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
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={
                "available": True,
                "reassembly_required": False,
                "message": "complete preview",
            },
        ):
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

    def test_candidate_adoption_defers_full_film_reassembly(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
                "defer_reassembly": True,
            }, "adopt-shot-02-without-reassembly",
        )
        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

            self.assertEqual("succeeded", job["status"])
            self.assertTrue(job["result"]["candidate_adopted"])
            self.assertTrue(job["result"]["reassembly_required"])
            self.assertEqual(0, self.refinement_renderer_mock.call_count)

            current = short_drama_refinement.workspace(
                self.db, "alice", "alice", self.project["id"]
            )["current_refinement"]
            self.assertEqual(before["url"], current["url"])
            self.assertEqual(before["preview_file_hash"], current["preview_file_hash"])
            self.assertEqual([], current["issues"])
            target = next(
                shot for shot in current["shots"] if shot["shot_key"] == "shot_02"
            )
            self.assertEqual(replacement_id, target["provider_version_id"])
            self.assertEqual("provider_regeneration", target["visual_source"])
            self.assertEqual(
                replacement_id,
                current["media"]["staged_replacements"][0]["provider_version_id"],
            )
            self.assertTrue(current["assembly_status"]["reassembly_required"])
            self.assertEqual(1, current["assembly_status"]["staged_count"])

            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.confirm_refinement(
                    self.db, "alice", "alice", self.acceptance_body(current)
                )
            self.assertEqual(
                "refinement_reassembly_required", blocked.exception.code
            )

            from content_domains import short_drama_autodraft
            reassembly_calls = []
            with mock.patch.object(
                short_drama_autodraft,
                "_render_provider_preview",
                side_effect=self._reassembly_renderer(reassembly_calls),
            ):
                reassembled = short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": current["id"],
                    }, "reassemble-after-candidate-adoption",
                )
            self.assertEqual(1, len(reassembly_calls))
            self.assertNotEqual(current["id"], reassembled["id"])
            self.assertFalse(reassembled["assembly_status"]["reassembly_required"])
            self.assertEqual(0, reassembled["assembly_status"]["staged_count"])
            self.assertNotIn("staged_replacements", reassembled["media"])
            reassembled_target = next(
                shot for shot in reassembled["shots"]
                if shot["shot_key"] == "shot_02"
            )
            self.assertEqual(
                replacement_id, reassembled_target["provider_version_id"]
            )

    def test_candidate_adoption_endpoint_forces_deferred_reassembly(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        body = {
            "project_id": self.project["id"],
            "shot_key": "shot_02",
            "source_version_id": before["id"],
            "replacement_provider_version_id": replacement_id,
            "defer_reassembly": False,
        }
        job = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", body, "candidate-adoption"
        )
        replay = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", body, "candidate-adoption"
        )
        self.assertEqual(job["id"], replay["id"])
        self.assertTrue(job["defer_reassembly"])
        self.assertTrue(replay["defer_reassembly"])

        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

        self.assertEqual("succeeded", job["status"])
        self.assertTrue(job["result"]["candidate_adopted"])
        self.assertEqual(0, self.refinement_renderer_mock.call_count)

    def test_candidate_adoption_http_route_ignores_false_defer_flag(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        handler = Handler(
            "/api/gen/short-drama/refinement/candidates/adopt",
            body={
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
                "defer_reassembly": False,
            },
            key="candidate-adoption-http",
        )
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )

        self.assertTrue(short_drama.dispatch_http(handler, "POST", self.db, verify))
        self.assertEqual(200, handler.response[0])
        self.assertTrue(handler.response[1]["defer_reassembly"])

    def test_candidate_adoption_requires_fixed_problem_shot_and_candidate(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        issue_replacement = self.add_provider_replacement("shot_02")

        for field in ("source_version_id", "replacement_provider_version_id"):
            body = {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": source["id"],
                "replacement_provider_version_id": issue_replacement,
            }
            body.pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(
                    short_drama_refinement.RefinementError
                ) as invalid:
                    short_drama_refinement.adopt_refinement_candidate(
                        self.db, "alice", "alice", body,
                        "candidate-missing-" + field,
                    )
                self.assertEqual("refinement_candidate_invalid", invalid.exception.code)

        normal_replacement = self.add_provider_replacement("shot_01")
        with self.assertRaises(short_drama_refinement.RefinementError) as normal:
            short_drama_refinement.adopt_refinement_candidate(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "shot_key": "shot_01",
                    "source_version_id": source["id"],
                    "replacement_provider_version_id": normal_replacement,
                }, "candidate-normal-shot",
            )
        self.assertEqual("refinement_candidate_not_required", normal.exception.code)

        with self.assertRaises(short_drama_refinement.RefinementError) as stale:
            short_drama_refinement.adopt_refinement_candidate(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "shot_key": "shot_02",
                    "source_version_id": "stale-refinement-version",
                    "replacement_provider_version_id": issue_replacement,
                }, "candidate-stale-source",
            )
        self.assertEqual("refinement_source_stale", stale.exception.code)

        conn = self.db()
        try:
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_jobs"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, jobs)

    def test_staged_candidate_probe_failure_still_requires_reassembly(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
                "defer_reassembly": True,
            }, "adopt-before-probe-failure",
        )
        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"])

        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            side_effect=short_drama_refinement.media_plan.MediaPlanError(
                "media_probe_failed", "candidate cannot be probed"
            ),
        ):
            current = short_drama_refinement.workspace(
                self.db, "alice", "alice", self.project["id"]
            )["current_refinement"]
            self.assertFalse(current["assembly_status"]["available"])
            self.assertTrue(current["assembly_status"]["reassembly_required"])
            self.assertEqual(1, current["assembly_status"]["staged_count"])
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.confirm_refinement(
                    self.db, "alice", "alice", self.acceptance_body(current)
                )
        self.assertEqual("refinement_reassembly_required", blocked.exception.code)

    def test_reassembly_rejects_remaining_issues_before_render(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        shots = [dict(item) for item in source["shots"]]
        extra_issue = {
            "code": "manual_review_required",
            "shot_key": "shot_01",
            "message": "first shot still needs review",
        }
        shots[0]["status"] = "degraded"
        shots[0]["issue"] = dict(extra_issue)
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_refinement_versions SET shots_json=?,issues_json=? "
                "WHERE id=?",
                (
                    json.dumps(shots),
                    json.dumps([extra_issue] + list(source["issues"])),
                    source["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        replacement_id = self.add_provider_replacement("shot_02")
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": source["id"],
                "replacement_provider_version_id": replacement_id,
                "defer_reassembly": True,
            }, "stage-one-of-two-problem-shots",
        )
        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"])
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual(["shot_01"], [
            item["shot_key"] for item in source["issues"]
        ])
        self.assertEqual(1, len(source["media"]["staged_replacements"]))
        from content_domains import short_drama_autodraft

        calls = []
        with mock.patch.object(
            short_drama_autodraft,
            "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "must-not-reassemble-with-open-issues",
                )
        self.assertEqual("refinement_issues_remaining", blocked.exception.code)
        self.assertEqual([], calls)

    def test_candidate_reassembly_rejects_issues_without_side_effects(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        conn = self.db()
        try:
            versions_before = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        from content_domains import short_drama_autodraft

        calls = []
        with mock.patch.object(
            short_drama_autodraft,
            "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "candidate-reassembly-with-issues",
                )
        self.assertEqual("refinement_issues_remaining", blocked.exception.code)
        self.assertEqual([], calls)

        conn = self.db()
        try:
            versions_after = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            operations = conn.execute(
                "SELECT COUNT(*) FROM short_drama_reassembly_operations "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            idempotency = conn.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE idem_key=?",
                ("candidate-reassembly-with-issues",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(versions_before, versions_after)
        self.assertEqual(0, operations)
        self.assertEqual(0, idempotency)

    def test_candidate_reassembly_rejects_legacy_result_with_open_issues(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        from content_domains import short_drama_autodraft

        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft,
            "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            legacy = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                }, "legacy-reassembly-with-issues",
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "candidate-must-not-reuse-legacy-result",
                )
        self.assertNotEqual(source["id"], legacy["id"])
        self.assertEqual("refinement_issues_remaining", blocked.exception.code)
        self.assertEqual(1, len(calls))

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

    def test_visual_refinement_uses_preview_audio_fallback_without_locked_voice(self):
        output_relative = "rendered/real-refinement.mp4"
        output = Path(self.tmp.name) / output_relative
        output.parent.mkdir(parents=True, exist_ok=True)

        def assemble(_project_id, _job_id, assembly):
            self.assertEqual(2, len(assembly["shots"]))
            self.assertEqual(
                "provider_audio", assembly["media_contract"]["media_mode"]
            )
            self.assertEqual(
                "refinement_preview_audio_fallback",
                assembly["media_contract"]["evidence_source"],
            )
            self.assertTrue(assembly["media_contract"]["preview_only"])
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
            "delivery_eligible": False,
            "reason": "locked_voice_timeline_missing",
            "audio_hash": "", "subtitle_hash": "",
            "timeline_hash": "", "subtitle_required": False,
            "audio_tracks": [], "subtitles": [],
        }
        provider_probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        from content_domains import short_drama_autodraft
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()
            manifest = json.loads(row[0])
            manifest["media_contract"] = dict(locked_media)
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET manifest_json=? "
                "WHERE id='draft-v1'",
                (json.dumps(manifest),),
            )
            conn.commit()
        finally:
            conn.close()
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

    def _reassembly_source(self):
        return short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]

    def _reassembly_renderer(self, calls=None, barrier=None, fail=False):
        from content_domains import short_drama_autodraft

        def render(project_id, render_id, assembly):
            if calls is not None:
                calls.append(render_id)
            if barrier is not None:
                barrier.wait(timeout=5)
            target = (
                Path(self.tmp.name) / "short_drama_autodraft" /
                project_id / render_id / "preview-720p.mp4"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"complete-reassembly-preview")
            if fail:
                raise short_drama_autodraft.AutodraftError(
                    "preview_render_failed", "render failed", 409
                )
            return {
                "file": target.relative_to(self.tmp.name).as_posix(),
                "url": "/api/gen/file/" + target.relative_to(
                    self.tmp.name
                ).as_posix(),
                "probe": {
                    "duration_ms": 10000,
                    "video": {"width": 1280, "height": 720},
                    "audio": {"codec": "aac"},
                },
                "duration_ms": 10000,
            }
        return render

    def test_reassembly_is_zero_cost_idempotent_and_preserves_history(self):
        source = self._reassembly_source()
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_refinement_acceptances "
                "(id,project_id,refinement_version_id,checklist_json,"
                "source_hashes_json,snapshot_json,snapshot_hash,accepted_by,"
                "accepted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "accept-before-reassembly", self.project["id"], source["id"],
                    "{}", "{}", "{}", "snapshot-before-reassembly", "alice",
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            first = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "version_id": source["id"],
                }, "reassembly-idempotent",
            )
            replay = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "version_id": source["id"],
                }, "reassembly-idempotent",
            )
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(1, len(calls))
        self.assertEqual(0, first["points_charged"])
        self.assertFalse(first["provider_called"])
        conn = self.db()
        try:
            versions = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            charges = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            invalidation = conn.execute(
                "SELECT invalidation_reason FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (source["id"],),
            ).fetchone()[0]
            historical = conn.execute(
                "SELECT id FROM short_drama_refinement_versions WHERE id=?",
                (source["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(2, versions)
        self.assertEqual(0, charges)
        self.assertEqual("preview_reassembled", invalidation)
        self.assertEqual(source["id"], historical[0])

    def test_reassembly_rejects_missing_key_conflict_stale_and_invalid_media(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "Idempotency-Key"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, ""
            )
        idem_db = short_drama_refinement._idempotency_db_factory(self.db)
        short_drama_refinement.submission_idempotency.begin(
            idem_db, "alice", short_drama_refinement._REASSEMBLY_ENDPOINT,
            "reassembly-conflict", body,
        )
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "不同重新装配请求"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "version_id": "other"},
                "reassembly-conflict",
            )
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "正在处理中"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, "reassembly-conflict"
            )
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "预览版本已变化"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "version_id": "stale"},
                "reassembly-stale",
            )
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media",
            return_value={"duration_ms": 0, "video": None, "audio": None},
        ):
            with self.assertRaisesRegex(short_drama_refinement.RefinementError, "有效视频流"):
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "reassembly-invalid-media"
                )

    def test_reassembly_rejects_missing_and_uncontrolled_provider_files(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        conn = self.db()
        try:
            original = conn.execute(
                "SELECT file FROM short_drama_provider_shot_versions WHERE id=?",
                ("provider-version-shot_01",),
            ).fetchone()[0]
            for index, (provider_file, expected) in enumerate((
                ("provider/missing.mp4", "provider_asset_missing"),
                ("../outside.mp4", "provider_asset_path_invalid"),
            )):
                conn.execute(
                    "UPDATE short_drama_provider_shot_versions SET file=? WHERE id=?",
                    (provider_file, "provider-version-shot_01"),
                )
                conn.commit()
                with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                    short_drama_refinement.reassemble_refinement(
                        self.db, "alice", "alice", body,
                        "reassembly-invalid-file-%d" % index,
                    )
                self.assertEqual(expected, raised.exception.code)
            conn.execute(
                "UPDATE short_drama_provider_shot_versions SET file=? WHERE id=?",
                (original, "provider-version-shot_01"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_reassembly_failure_cleans_output_and_aborts_idempotency(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        from content_domains import short_drama_autodraft
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(fail=True),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "reassembly-failure"
                )
        root = Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"]
        self.assertFalse(any(root.glob("reassembly-*")) if root.exists() else False)
        conn = self.db()
        try:
            idem = conn.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE idem_key=?",
                ("reassembly-failure",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, idem)

    def test_concurrent_reassembly_creates_one_version_and_one_render(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        calls, results, errors = [], [], []
        render_started = threading.Event()
        release_render = threading.Event()
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft

        def worker(key):
            try:
                results.append(short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, key
                ))
            except Exception as error:
                errors.append(error)

        renderer = self._reassembly_renderer(calls)

        def blocking_renderer(project_id, render_id, assembly):
            render_started.set()
            release_render.wait(timeout=5)
            return renderer(project_id, render_id, assembly)

        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=blocking_renderer,
        ):
            first = threading.Thread(
                target=worker, args=("concurrent-reassembly-0",)
            )
            second = threading.Thread(
                target=worker, args=("concurrent-reassembly-1",)
            )
            first.start()
            self.assertTrue(render_started.wait(timeout=5))
            second.start()
            second.join(timeout=5)
            release_render.set()
            threads = [first, second]
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertEqual(
            "refinement_reassembly_in_progress", errors[0].code
        )
        self.assertEqual(1, len(calls))
        conn = self.db()
        try:
            versions = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(2, versions)

    def test_expired_reassembly_lease_is_taken_over_and_orphan_is_cleaned(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        orphan_id = "reassembly-crashed-worker"
        orphan = (
            Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"] /
            orphan_id / "preview-720p.mp4"
        )
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"incomplete-output")
        conn = self.db()
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_reassembly_operations "
                "(id,project_id,source_version_id,status,lease_token,lease_owner,"
                "lease_expires_at,heartbeat_at,render_id,created_at,updated_at) "
                "VALUES (?,?,?,'processing',?,?,?,?,?,?,?)",
                (
                    "crashed-operation", self.project["id"], source["id"],
                    "dead-token", "dead-worker", now - 1, now - 60,
                    orphan_id, now - 60, now - 60,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(),
        ):
            result = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, "expired-lease-takeover"
            )
        self.assertFalse(orphan.parent.exists())
        conn = self.db()
        try:
            operation = conn.execute(
                "SELECT status,lease_token,refinement_version_id FROM "
                "short_drama_reassembly_operations WHERE id='crashed-operation'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("succeeded", operation[0])
        self.assertIsNone(operation[1])
        self.assertEqual(result["id"], operation[2])

    def test_reassembly_heartbeat_prevents_takeover_during_long_render(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        started = threading.Event()
        calls, result, errors = [], [], []
        renderer = self._reassembly_renderer(calls)

        def slow_renderer(project_id, render_id, assembly):
            started.set()
            time.sleep(4.2)
            return renderer(project_id, render_id, assembly)

        def first_worker():
            try:
                result.append(short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "heartbeat-owner"
                ))
            except Exception as error:
                errors.append(error)

        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 3,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=slow_renderer,
        ):
            worker = threading.Thread(target=first_worker)
            worker.start()
            self.assertTrue(started.wait(timeout=5))
            time.sleep(3.4)
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "heartbeat-contender"
                )
            self.assertEqual(
                "refinement_reassembly_in_progress", raised.exception.code
            )
            worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(1, len(result))
        self.assertEqual(1, len(calls))

    def test_reassembly_heartbeat_prevents_takeover_during_slow_preprocessing(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        preprocessing_started = threading.Event()
        release_preprocessing = threading.Event()
        calls, result, errors = [], [], []
        probe_calls = 0
        probe_lock = threading.Lock()
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft

        def slow_probe(_path):
            nonlocal probe_calls
            with probe_lock:
                probe_calls += 1
                first = probe_calls == 1
            if first:
                preprocessing_started.set()
                release_preprocessing.wait(timeout=8)
            return probe

        def first_worker():
            try:
                result.append(short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "slow-preprocess-owner"
                ))
            except Exception as error:
                errors.append(error)

        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 3,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", side_effect=slow_probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            worker = threading.Thread(target=first_worker)
            worker.start()
            self.assertTrue(preprocessing_started.wait(timeout=5))
            time.sleep(3.4)
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "slow-preprocess-contender"
                )
            self.assertEqual(
                "refinement_reassembly_in_progress", raised.exception.code
            )
            release_preprocessing.set()
            worker.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(1, len(result))
        self.assertEqual(1, len(calls))

    def test_reassembly_stops_before_render_after_heartbeat_loss(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        renewals = 0

        def renewal(*_args, **_kwargs):
            nonlocal renewals
            renewals += 1
            return renewals == 1

        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 1,
        ), mock.patch.object(
            short_drama_refinement, "_heartbeat_reassembly_operation",
            side_effect=renewal,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media",
            side_effect=lambda _path: (time.sleep(1.3), probe)[1],
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "heartbeat-loss"
                )
        self.assertEqual("refinement_reassembly_lease_lost", raised.exception.code)
        self.assertEqual([], calls)

    def test_reassembly_cancels_running_renderer_after_heartbeat_loss(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        renderer_started = threading.Event()
        renderer_cancelled = threading.Event()
        successful_calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        renewals = 0

        def renewal(*_args, **_kwargs):
            nonlocal renewals
            renewals += 1
            return renewals == 1

        def cancellable_renderer(project_id, render_id, assembly):
            cancel_event = assembly["_cancel_event"]
            partial = (
                Path(self.tmp.name) / "short_drama_autodraft" / project_id /
                render_id / "preview-720p.mp4"
            )
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            renderer_started.set()
            self.assertTrue(cancel_event.wait(timeout=5))
            renderer_cancelled.set()
            raise short_drama_autodraft.AutodraftError(
                "preview_render_cancelled", "cancelled", 409,
            )

        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 1,
        ), mock.patch.object(
            short_drama_refinement, "_heartbeat_reassembly_operation",
            side_effect=renewal,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=cancellable_renderer,
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "render-heartbeat-loss"
                )
        self.assertTrue(renderer_started.is_set())
        self.assertTrue(renderer_cancelled.is_set())
        self.assertEqual("refinement_reassembly_lease_lost", raised.exception.code)
        self.assertFalse(any(
            (Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"]).glob(
                "reassembly-*"
            )
        ))

        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(successful_calls),
        ):
            result = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, "render-heartbeat-takeover"
            )
        self.assertEqual(1, len(successful_calls))
        conn = self.db()
        try:
            operation = conn.execute(
                "SELECT status,refinement_version_id FROM "
                "short_drama_reassembly_operations WHERE project_id=? "
                "AND source_version_id=?", (self.project["id"], source["id"]),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(("succeeded", result["id"]), tuple(operation))

    def test_reassembly_http_owner_editor_succeed_and_viewer_is_read_only(self):
        source = self._reassembly_source()
        board_id = "shared-refinement-board"
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET board_id=? WHERE id=?",
                (board_id, self.project["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        roles = {"alice": "owner", "bob": "editor", "eve": "viewer"}
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        access = lambda handler: {
            "board_id": board_id, "role": roles[handler._token()],
        }
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(),
        ):
            owner = Handler(
                "/api/gen/short-drama/refinement/reassemble",
                body={"project_id": self.project["id"], "version_id": source["id"]},
                key="owner-http-reassembly", token="alice",
            )
            self.assertTrue(short_drama.dispatch_http(
                owner, "POST", self.db, verify, canvas_access_resolver=access,
            ))
            self.assertEqual(200, owner.response[0])

            editor = Handler(
                "/api/gen/short-drama/refinement/reassemble",
                body={
                    "project_id": self.project["id"],
                    "version_id": owner.response[1]["id"],
                },
                key="editor-http-reassembly", token="bob",
            )
            self.assertTrue(short_drama.dispatch_http(
                editor, "POST", self.db, verify, canvas_access_resolver=access,
            ))
            self.assertEqual(200, editor.response[0])

            viewer = Handler(
                "/api/gen/short-drama/refinement/reassemble",
                body={
                    "project_id": self.project["id"],
                    "version_id": editor.response[1]["id"],
                },
                key="viewer-http-reassembly", token="eve",
            )
            self.assertTrue(short_drama.dispatch_http(
                viewer, "POST", self.db, verify, canvas_access_resolver=access,
            ))
        self.assertEqual(403, viewer.response[0])

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

    def test_provider_audio_preference_reuses_generated_shot_sound(self):
        from content_domains import short_drama_autodraft

        saved = short_drama_refinement.set_media_preference(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "mode": "provider_audio",
            },
        )
        self.assertEqual("provider_audio", saved["mode"])
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            contract = short_drama_autodraft._locked_media_contract(conn, project)
        finally:
            conn.close()
        self.assertTrue(contract["delivery_eligible"])
        self.assertEqual("provider_audio", contract["media_mode"])
        self.assertEqual(
            "explicit_provider_audio_confirmation",
            contract["evidence_source"],
        )
        self.assertFalse(contract["silent_confirmed"])
        self.assertEqual([], contract["audio_tracks"])

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

    def test_incomplete_assembly_blocks_confirmation_in_authoritative_transaction(self):
        version = self.repaired_version("incomplete-assembly-confirmation")
        body = self.acceptance_body(version)
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={
                "available": True,
                "reassembly_required": True,
                "message": "preview is missing shot duration",
            },
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.confirm_refinement(
                    self.db, "alice", "alice", body,
                )
        self.assertEqual("refinement_reassembly_required", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone()[0])
            self.assertEqual("draft", conn.execute(
                "SELECT status FROM short_drama_refinement_versions WHERE id=?",
                (version["id"],),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_unavailable_assembly_blocks_confirmation_without_side_effects(self):
        version = self.repaired_version("unavailable-assembly-confirmation")
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": False, "reassembly_required": False},
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", self.acceptance_body(version),
            )
        self.assertEqual("refinement_assembly_unavailable", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone()[0])
            self.assertEqual("draft", conn.execute(
                "SELECT status FROM short_drama_refinement_versions WHERE id=?",
                (version["id"],),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_incomplete_assembly_blocks_quote_without_creating_one(self):
        version = self.repaired_version("incomplete-assembly-quote")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={"available": False, "reassembly_required": False},
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.create_delivery_quote(
                    self.db, "alice", {
                        "project_id": self.project["id"],
                        "version_id": version["id"],
                    },
                )
        self.assertEqual("refinement_assembly_unavailable", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_quotes"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_reassembly_required_blocks_quote_without_creating_one(self):
        version = self.repaired_version("reassembly-required-quote")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": True},
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_reassembly_required", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_quotes"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_delivery_quote_serializes_with_acceptance_invalidation(self):
        version = self.repaired_version("quote-acceptance-race")
        self.confirm_version(version)
        writer_started = threading.Event()
        writer_done = threading.Event()
        writer_errors = []

        def invalidate_acceptance():
            conn = self.db()
            try:
                writer_started.set()
                conn.execute(
                    "UPDATE short_drama_refinement_acceptances "
                    "SET invalidated_at=?,invalidation_reason='concurrent change' "
                    "WHERE refinement_version_id=?",
                    (int(time.time()), version["id"]),
                )
                conn.commit()
            except Exception as error:
                writer_errors.append(error)
            finally:
                conn.close()
                writer_done.set()

        original = short_drama_refinement._refinement_assembly_status
        worker = None
        def status_during_race(conn, project, source):
            nonlocal worker
            worker = threading.Thread(target=invalidate_acceptance)
            worker.start()
            self.assertTrue(writer_started.wait(2))
            self.assertFalse(writer_done.wait(0.2))
            return original(conn, project, source)

        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            side_effect=status_during_race,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        worker.join(5)
        self.assertTrue(writer_done.is_set())
        self.assertEqual([], writer_errors)
        self.assertTrue(quote["quote_token"])

    def test_incomplete_assembly_blocks_start_before_charge_or_job(self):
        version = self.repaired_version("incomplete-assembly-start")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": False},
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": True},
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    }, "incomplete-assembly-start", deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("refinement_reassembly_required", raised.exception.code)
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_unavailable_assembly_blocks_start_before_charge_or_job(self):
        version = self.repaired_version("unavailable-assembly-start")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": False},
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": False, "reassembly_required": False},
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "unavailable-assembly-start", deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
        self.assertEqual("refinement_assembly_unavailable", raised.exception.code)
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs"
            ).fetchone()[0])
        finally:
            conn.close()

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

    def test_delivery_refund_lease_blocks_concurrent_scanners(self):
        now = int(time.time())
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES('refund-race','alice',?,'refund-race','hash','quote',"
                "25,'refund_pending',?,?)", (self.project["id"], now, now),
            )
            conn.commit()
        finally:
            conn.close()
        started = threading.Event()
        release = threading.Event()
        points_domain = mock.Mock()
        points_domain.refund_points.side_effect = lambda *_args, **_kwargs: (
            started.set(), release.wait(5)
        )
        attempt = {"id": "refund-race", "state": "refund_pending"}
        first = threading.Thread(
            target=short_drama_refinement.reconcile_delivery_refund,
            args=(self.db, points_domain, attempt),
        )
        first.start()
        self.assertTrue(started.wait(5))
        second = short_drama_refinement.reconcile_delivery_refund(
            self.db, points_domain, attempt,
        )
        release.set()
        first.join(5)
        self.assertEqual("refund_pending", second["state"])
        points_domain.refund_points.assert_called_once()

    def test_delivery_refund_scanner_isolates_one_database_failure(self):
        now = int(time.time())
        conn = self.db()
        try:
            for attempt_id in ("refund-bad", "refund-good"):
                conn.execute(
                    "INSERT INTO short_drama_delivery_attempts "
                    "(id,actor_username,project_id,idempotency_key,request_hash,"
                    "quote_token,cost,state,created_at,updated_at) "
                    "VALUES(?, 'alice', ?, ?, 'hash', 'quote', 25,"
                    "'refund_pending',?,?)",
                    (attempt_id, self.project["id"], attempt_id, now, now),
                )
            conn.commit()
        finally:
            conn.close()
        original = short_drama_refinement.reconcile_delivery_refund
        def flaky(db_factory, points_domain, attempt):
            if attempt["id"] == "refund-bad":
                raise sqlite3.OperationalError("temporary database failure")
            return original(db_factory, points_domain, attempt)
        points_domain = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "reconcile_delivery_refund", side_effect=flaky,
        ):
            recovered = short_drama_refinement.retry_delivery_attempt_refunds(
                self.db, points_domain,
            )
        self.assertEqual(1, recovered)
        conn = self.db()
        try:
            states = dict(conn.execute(
                "SELECT id,state FROM short_drama_delivery_attempts "
                "WHERE id IN ('refund-bad','refund-good')"
            ).fetchall())
        finally:
            conn.close()
        self.assertEqual("refund_pending", states["refund-bad"])
        self.assertEqual("refunded", states["refund-good"])

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


class ShortDramaRefinementSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "legacy-content.db")
        self.db = lambda: sqlite3.connect(self.database)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db, "alice", {
                "title": "legacy preference migration",
                "synopsis": "preserve existing media preference rows",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "cinematic", "target_platform": "test",
                "point_budget": 0,
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_media_preference_schema_upgrade_is_idempotent(self):
        conn = self.db()
        try:
            conn.execute("DROP TABLE short_drama_refinement_media_preferences")
            conn.execute(
                "CREATE TABLE short_drama_refinement_media_preferences ("
                "project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) "
                "ON DELETE CASCADE, mode TEXT NOT NULL CHECK(mode IN "
                "('voice_timeline','silent')), confirmed_by TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO short_drama_refinement_media_preferences "
                "(project_id,mode,confirmed_by,updated_at) VALUES(?,?,?,?)",
                (self.project["id"], "voice_timeline", "alice", 123),
            )
            conn.execute(
                "DELETE FROM short_drama_schema_migrations WHERE name=?",
                (short_drama_refinement._MEDIA_PREFERENCE_MIGRATION,),
            )
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_refinement_media_preferences'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT project_id,mode,confirmed_by,updated_at FROM "
                "short_drama_refinement_media_preferences"
            ).fetchone()
            legacy = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_refinement_media_preferences_legacy'"
            ).fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        self.assertIn("provider_audio", sql)
        self.assertEqual((self.project["id"], "voice_timeline", "alice", 123), row)
        self.assertEqual(0, legacy)
        self.assertEqual("ok", integrity)
        self.assertEqual([], foreign_keys)

    def test_legacy_media_preference_upgrade_resumes_after_rename_crash(self):
        conn = self.db()
        try:
            conn.execute("DROP TABLE short_drama_refinement_media_preferences")
            conn.execute(
                "CREATE TABLE short_drama_refinement_media_preferences_legacy ("
                "project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) "
                "ON DELETE CASCADE, mode TEXT NOT NULL CHECK(mode IN "
                "('voice_timeline','silent')), confirmed_by TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO short_drama_refinement_media_preferences_legacy "
                "VALUES(?,?,?,?)",
                (self.project["id"], "voice_timeline", "alice", 123),
            )
            conn.execute(
                "DELETE FROM short_drama_schema_migrations WHERE name=?",
                (short_drama_refinement._MEDIA_PREFERENCE_MIGRATION,),
            )
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT project_id,mode,confirmed_by,updated_at FROM "
                "short_drama_refinement_media_preferences"
            ).fetchone()
            marker = conn.execute(
                "SELECT 1 FROM short_drama_schema_migrations WHERE name=?",
                (short_drama_refinement._MEDIA_PREFERENCE_MIGRATION,),
            ).fetchone()
            legacy = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_refinement_media_preferences_legacy'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((self.project["id"], "voice_timeline", "alice", 123), row)
        self.assertIsNone(legacy)
        self.assertEqual((1,), marker)

    def test_reassembly_operation_schema_is_additive_idempotent_and_constrained(self):
        conn = self.db()
        try:
            conn.execute("DROP TABLE short_drama_reassembly_operations")
            counts_before = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (
                    "short_drama_projects",
                    "short_drama_refinement_jobs",
                    "short_drama_refinement_versions",
                    "short_drama_refinement_acceptances",
                    "short_drama_delivery_jobs",
                    "short_drama_delivery_versions",
                )
            }
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_reassembly_operations'"
            ).fetchone()[0]
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_reassembly_operations)"
                )
            }
            foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(short_drama_reassembly_operations)"
            ).fetchall()
            indexes = conn.execute(
                "PRAGMA index_list(short_drama_reassembly_operations)"
            ).fetchall()
            unique_columns = []
            for index in indexes:
                if index[2]:
                    unique_columns.append(tuple(
                        row[2] for row in conn.execute(
                            "PRAGMA index_info(%s)" % index[1]
                        )
                    ))
            counts_after = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in counts_before
            }
            operation_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_reassembly_operations"
            ).fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        self.assertIn("status IN ('processing','succeeded')", table_sql)
        self.assertTrue({
            "id", "project_id", "source_version_id", "status", "lease_token",
            "lease_owner", "lease_expires_at", "heartbeat_at", "render_id",
            "refinement_version_id", "created_at", "updated_at",
        }.issubset(columns))
        self.assertEqual(3, len(foreign_keys))
        self.assertIn(("project_id", "source_version_id"), unique_columns)
        self.assertEqual(counts_before, counts_after)
        self.assertEqual(0, operation_count)
        self.assertEqual("ok", integrity)
        self.assertEqual([], violations)

    def test_defer_reassembly_column_migrates_legacy_jobs_idempotently(self):
        job_id = "legacy-refinement-job"
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_refinement_jobs "
                "(id,project_id,source_version_id,shot_key,actor_username,"
                "idempotency_key,request_hash,replacement_provider_version_id,"
                "defer_reassembly,status,progress,poll_count,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,'failed',100,1,?,?)",
                (
                    job_id, self.project["id"], "legacy-source-version",
                    "shot_01", "alice", "legacy-schema-job", "legacy-hash",
                    None, 100, 100,
                ),
            )
            conn.execute(
                "ALTER TABLE short_drama_refinement_jobs "
                "DROP COLUMN defer_reassembly"
            )
            counts_before = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (
                    "short_drama_projects",
                    "short_drama_refinement_jobs",
                    "short_drama_refinement_versions",
                    "short_drama_provider_shot_versions",
                )
            }
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            conn.row_factory = sqlite3.Row
            columns = {
                row[1]: row for row in conn.execute(
                    "PRAGMA table_info(short_drama_refinement_jobs)"
                )
            }
            migrated = short_drama_refinement._job(conn.execute(
                "SELECT * FROM short_drama_refinement_jobs WHERE id=?",
                (job_id,),
            ).fetchone())
            counts_after = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in counts_before
            }
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        self.assertIn("defer_reassembly", columns)
        self.assertEqual(1, columns["defer_reassembly"][3])
        self.assertEqual("0", columns["defer_reassembly"][4])
        self.assertFalse(migrated["defer_reassembly"])
        self.assertEqual(counts_before, counts_after)
        self.assertEqual("ok", integrity)
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
