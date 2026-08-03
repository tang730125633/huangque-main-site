# -*- coding: utf-8 -*-

import json
import pathlib
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core
from content_domains import video_compose
from content_domains import video_compose_analysis as analysis
from content_domains import video_compose_asr as asr
from content_domains import video_compose_media as media
from content_domains import video_compose_render as render
from content_domains import video_compose_store as store
import content_api


class VideoComposeAnalysisTests(unittest.TestCase):
    def test_detects_silence_filler_repetition_and_builds_non_destructive_edl(self):
        result = analysis.detect_candidates(5000, [
            {"text": "大家", "start_ms": 300, "end_ms": 700, "confidence": 0.98},
            {"text": "嗯", "start_ms": 760, "end_ms": 930, "confidence": 0.91},
            {"text": "我们", "start_ms": 1700, "end_ms": 2050, "confidence": 0.96},
            {"text": "我们", "start_ms": 2140, "end_ms": 2490, "confidence": 0.95},
            {"text": "开始", "start_ms": 2700, "end_ms": 3100, "confidence": 0.97},
        ])
        kinds = [item["type"] for item in result["candidates"]]
        self.assertIn("leading_silence", kinds)
        self.assertIn("silence", kinds)
        self.assertIn("filler_word", kinds)
        self.assertIn("repetition", kinds)
        self.assertIn("trailing_silence", kinds)
        decisions = {
            item["id"]: ("remove" if item["type"] in {
                "leading_silence", "silence", "trailing_silence"
            } else "keep")
            for item in result["candidates"]
        }
        edl = analysis.build_edl(result["duration_ms"], result["candidates"], decisions)
        self.assertGreater(edl["source_duration_ms"], edl["output_duration_ms"])
        self.assertEqual(300, edl["keep_ranges"][0]["source_start_ms"])
        self.assertEqual(3100, edl["keep_ranges"][-1]["source_end_ms"])

    def test_enriches_audio_silences_and_requires_confirmation_for_tail_outtake(self):
        words = [
            {"text": "正文", "start_ms": 300, "end_ms": 1200},
            {"text": "那我可以了", "start_ms": 2200, "end_ms": 3000},
        ]
        base = analysis.detect_candidates(3200, words)
        candidates = analysis.enrich_candidates(3200, base["words"], base["candidates"], [
            {"start_ms": 0, "end_ms": 280, "duration_ms": 280},
            {"start_ms": 1200, "end_ms": 2200, "duration_ms": 1000},
            {"start_ms": 3000, "end_ms": 3200, "duration_ms": 200},
        ])
        by_type = {item["type"]: item for item in candidates}
        self.assertTrue(by_type["leading_silence"]["default_selected"])
        self.assertTrue(by_type["silence"]["default_selected"])
        self.assertTrue(by_type["trailing_silence"]["default_selected"])
        self.assertEqual("那我可以了", by_type["suspected_misspeaking"]["text"])
        self.assertFalse(by_type["suspected_misspeaking"]["default_selected"])

    def test_audio_silence_overlapping_asr_speech_is_never_default_deleted(self):
        words = [{"text": "低声说话", "start_ms": 1000, "end_ms": 2200}]
        base = analysis.detect_candidates(3000, words)
        candidates = analysis.enrich_candidates(3000, base["words"], base["candidates"], [
            {"start_ms": 1200, "end_ms": 2000, "duration_ms": 800},
        ])
        candidate = next(item for item in candidates if item["start_ms"] == 1200)
        self.assertFalse(candidate["default_selected"])
        self.assertIn("必须试听确认", candidate["reason"])

    def test_requires_a_decision_for_every_candidate_and_never_deletes_whole_video(self):
        result = analysis.detect_candidates(1000, [
            {"text": "嗯", "start_ms": 300, "end_ms": 700},
        ])
        with self.assertRaisesRegex(ValueError, "确认全部"):
            analysis.build_edl(1000, result["candidates"], {})
        decisions = {item["id"]: "remove" for item in result["candidates"]}
        with self.assertRaisesRegex(ValueError, "不能删除整条视频"):
            analysis.build_edl(1000, result["candidates"], decisions)

    def test_rejects_unordered_or_out_of_range_word_timestamps(self):
        with self.assertRaisesRegex(ValueError, "顺序"):
            analysis.detect_candidates(2000, [
                {"text": "后", "start_ms": 1000, "end_ms": 1200},
                {"text": "前", "start_ms": 800, "end_ms": 900},
            ])
        with self.assertRaisesRegex(ValueError, "范围"):
            analysis.detect_candidates(1000, [
                {"text": "越界", "start_ms": 900, "end_ms": 1100},
            ])


class VideoComposeAsrTests(unittest.TestCase):
    def test_parses_word_timestamps_and_segments(self):
        result = asr.parse_verbose_response({
            "text": "你好 AI",
            "language": "zh",
            "words": [
                {"word": "你好", "start": 0.12, "end": 0.62},
                {"word": "AI", "start": 0.72, "end": 1.02},
            ],
            "segments": [{"text": "你好 AI", "start": 0.1, "end": 1.1}],
        })
        self.assertEqual("你好 AI", result["text"])
        self.assertEqual(120, result["words"][0]["start_ms"])
        self.assertEqual(1020, result["words"][1]["end_ms"])
        self.assertEqual([{"text": "你好 AI", "start_ms": 100, "end_ms": 1100}], result["segments"])

    def test_interpolates_segments_when_provider_omits_words(self):
        result = asr.parse_verbose_response({
            "text": "你好", "segments": [{"text": "你好", "start": 1, "end": 2}],
        })
        self.assertEqual([
            {"text": "你", "start_ms": 1000, "end_ms": 1500, "confidence": None},
            {"text": "好", "start_ms": 1500, "end_ms": 2000, "confidence": None},
        ], result["words"])

    def test_rejects_empty_asr_response(self):
        with self.assertRaisesRegex(ValueError, "没有识别"):
            asr.parse_verbose_response({"text": ""})


class VideoComposeMediaTests(unittest.TestCase):
    def test_remaps_source_cues_through_multiple_keep_ranges(self):
        edl = {"keep_ranges": [
            {"source_start_ms": 100, "source_end_ms": 500},
            {"source_start_ms": 900, "source_end_ms": 1500},
        ]}
        self.assertEqual(0, media.source_to_output_ms(100, edl))
        self.assertEqual(400, media.source_to_output_ms(900, edl))
        self.assertEqual(1000, media.source_to_output_ms(1500, edl))
        self.assertIsNone(media.source_to_output_ms(700, edl))
        self.assertEqual([
            {"text": "保留", "start_ms": 50, "end_ms": 300},
        ], media.remap_cues([
            {"text": "保留", "start_ms": 150, "end_ms": 400},
            {"text": "删除", "start_ms": 600, "end_ms": 800},
        ], edl))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_builds_decodable_clean_master_from_edl(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.mp4"
            output = pathlib.Path(directory) / "clean.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=320x568:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
            ], check=True, timeout=30)
            result = media.build_clean_master(source, {"keep_ranges": [
                {"source_start_ms": 0, "source_end_ms": 500},
                {"source_start_ms": 1000, "source_end_ms": 2000},
            ]}, output, timeout=60)
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(1500, result["duration_ms"], delta=180)
            self.assertEqual("h264", result["video_codec"])
            self.assertEqual("aac", result["audio_codec"])


class VideoComposeRenderTests(unittest.TestCase):
    def test_prepares_frozen_template_from_validated_data(self):
        with tempfile.TemporaryDirectory() as directory:
            clean = pathlib.Path(directory) / "clean.mp4"
            clean.write_bytes(b"fixture")
            workspace = pathlib.Path(directory) / "project"
            data = render.prepare_workspace(clean, {
                "duration_ms": 5000,
                "cues": [
                    {"text": "你好 AI", "start_ms": 0, "end_ms": 2400, "keywords": ["AI"]},
                    {"text": "你也可以", "start_ms": 2400, "end_ms": 5000},
                ],
                "hook": {"line_1": "你好", "line_2": "开始创作"},
                "headlines": [{"text": "AI", "start_ms": 2400, "end_ms": 3200}],
                "brand": {"name": "黄雀 AI", "cta": "开始创作"},
                "cut_points_ms": [2400],
            }, workspace)
            markup = (workspace / "index.html").read_text(encoding="utf-8")
            self.assertEqual(5000, data["duration_ms"])
            self.assertIn('data-duration="5.000"', markup)
            self.assertIn('<span class="key">AI</span>', markup)
            self.assertIn('id="headline-1"', markup)
            self.assertIn('window.__timelines.main', markup)
            self.assertNotIn("__DURATION__", markup)
            self.assertTrue((workspace / "clean-master.mp4").is_file())

    def test_rejects_overlapping_caption_contract(self):
        with self.assertRaisesRegex(ValueError, "时间重叠"):
            render.normalize_input({
                "duration_ms": 3000,
                "cues": [
                    {"text": "一", "start_ms": 0, "end_ms": 2000},
                    {"text": "二", "start_ms": 1000, "end_ms": 3000},
                ],
            })


class VideoComposeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = store.DB_PATH
        store.DB_PATH = str(pathlib.Path(self.temp.name) / "compose.db")

    def tearDown(self):
        store.DB_PATH = self.original_db
        self.temp.cleanup()

    def test_project_analysis_confirmation_and_revision_conflict(self):
        project = store.create_project("fang", 7, "source-sha", {"asset_id": 7})
        detected = analysis.detect_candidates(3000, [
            {"text": "开始", "start_ms": 400, "end_ms": 800},
            {"text": "结束", "start_ms": 1800, "end_ms": 2300},
        ])
        ready = store.save_analysis(
            "fang", project["id"], 1, detected,
            analysis.transcript_hash(detected["duration_ms"], detected["words"]),
        )
        self.assertEqual("review_required", ready["status"])
        self.assertEqual(2, ready["revision"])
        with self.assertRaises(store.RevisionConflict):
            store.save_analysis("fang", project["id"], 1, detected, "stale")
        decisions = {item["id"]: "keep" for item in ready["candidates"]}
        edl = analysis.build_edl(ready["duration_ms"], ready["candidates"], decisions)
        confirmed = store.confirm_edit_decisions("fang", project["id"], 2, decisions, edl)
        self.assertEqual("review_confirmed", confirmed["status"])
        self.assertEqual(1, confirmed["edit_decision_version"])
        completed = store.save_render_result(
            "fang", project["id"], 3,
            {"template_id": "viral-talking-head-v1", "template_version": "1.0.0"},
            "video-compose/fang/clean.mp4", "video-compose/fang/output.mp4", 77,
            {"output": {"duration_ms": 3000}},
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual("viral-talking-head-v1", completed["template_id"])
        self.assertEqual("video-compose/fang/output.mp4", completed["output_file"])
        self.assertEqual(77, completed["output_asset_id"])
        with self.assertRaises(store.ProjectNotFound):
            store.get_project("other-user", project["id"])


class VideoComposeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.assets_db = root / "assets.db"
        self.originals = {
            "store_db": store.DB_PATH,
            "verify": core.verify,
            "domains": core._domains,
            "adb": core.adb,
            "out_dir": core.OUT_DIR,
            "resolver": core._resolve_out_file,
            "queue": core._job_queue,
            "ids": core._queued_job_ids,
        }
        store.DB_PATH = str(root / "compose.db")
        with closing(sqlite3.connect(self.assets_db)) as connection:
            connection.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY,job_id INTEGER,username TEXT,mode TEXT,
                video_file TEXT,video_url TEXT,text TEXT,resolution TEXT,ratio TEXT,
                motion TEXT,phase TEXT,model TEXT,status TEXT,error TEXT,created_at INTEGER,updated_at INTEGER
            )""")
            connection.execute(
                "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (7, 70, "fang", "text", "video/source.mp4", None, "口播原稿",
                 "1080p", "9:16", None, "completed", "heygen", "done", None, 1234, 1234),
            )
            connection.execute(
                "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (8, 80, "other", "text", "video/private.mp4", None, "别人的视频",
                 "1080p", "9:16", None, "completed", "heygen", "done", None, 1234, 1234),
            )
            connection.commit()

        def asset_db():
            connection = sqlite3.connect(self.assets_db)
            connection.row_factory = sqlite3.Row
            return connection

        core.verify = lambda token: ({"username": "fang", "must_change": False} if token == "test" else None)
        core._domains = lambda: (None, None, None)
        core.adb = asset_db
        core.OUT_DIR = root / "out"
        core.OUT_DIR.mkdir()
        source = core.OUT_DIR / "video" / "source.mp4"
        source.parent.mkdir()
        source.write_bytes(b"source")
        core._resolve_out_file = lambda rel: source if rel == "video/source.mp4" else None
        core._job_queue = queue.Queue(maxsize=4)
        core._queued_job_ids = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), content_api.H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        store.DB_PATH = self.originals["store_db"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["domains"]
        core.adb = self.originals["adb"]
        core.OUT_DIR = self.originals["out_dir"]
        core._resolve_out_file = self.originals["resolver"]
        core._job_queue = self.originals["queue"]
        core._queued_job_ids = self.originals["ids"]
        self.temp.cleanup()

    def request(self, method, path, body=None, token="test"):
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with self.opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_owned_asset_analysis_review_and_reload(self):
        status, created = self.request("POST", "/api/gen/video-compose/projects", {
            "source_asset_id": 7,
        })
        self.assertEqual(201, status)
        project = created["project"]
        self.assertEqual("created", project["status"])
        self.assertNotIn("video_url", project["source"])

        status, analyzed = self.request(
            "POST", "/api/gen/video-compose/projects/%s/analysis" % project["id"], {
                "expected_revision": project["revision"],
                "duration_ms": 3000,
                "words": [
                    {"text": "开始", "start_ms": 300, "end_ms": 800, "confidence": 0.98},
                    {"text": "结束", "start_ms": 1800, "end_ms": 2400, "confidence": 0.97},
                ],
            },
        )
        self.assertEqual(200, status)
        ready = analyzed["project"]
        self.assertEqual("review_required", ready["status"])
        decisions = {
            item["id"]: ("remove" if item["default_selected"] else "keep")
            for item in ready["candidates"]
        }
        status, reviewed = self.request(
            "POST", "/api/gen/video-compose/projects/%s/edit-decisions" % project["id"], {
                "expected_revision": ready["revision"], "decisions": decisions,
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("review_confirmed", reviewed["project"]["status"])
        _, loaded = self.request("GET", "/api/gen/video-compose/projects/%s" % project["id"])
        self.assertEqual(reviewed["project"]["edl"], loaded["project"]["edl"])

    def test_automatic_analysis_render_and_authenticated_output(self):
        _, created = self.request("POST", "/api/gen/video-compose/projects", {"source_asset_id": 7})
        project = created["project"]
        fake_words = [
            {"text": "你好", "start_ms": 200, "end_ms": 900, "confidence": 0.98},
            {"text": "AI", "start_ms": 1600, "end_ms": 2200, "confidence": 0.97},
        ]
        def build_clean(_source, _edl, output):
            pathlib.Path(output).write_bytes(b"clean")
            return {"duration_ms": 2400, "video_codec": "h264", "audio_codec": "aac", "has_audio": True}
        def render_video(_clean, _payload, output):
            pathlib.Path(output).write_bytes(b"finished-video")
            return {"template_id": "viral-talking-head-v1", "template_version": "1.0.0",
                    "output": {"duration_ms": 2400, "video_codec": "h264", "audio_codec": "aac", "has_audio": True}}
        with mock.patch.object(video_compose.asr, "transcribe", return_value={
                "text": "你好 AI", "words": fake_words, "duration_ms": 2500}), \
             mock.patch.object(video_compose.media, "probe_media", return_value={"duration_ms": 2500}), \
             mock.patch.object(video_compose.media, "detect_silence_ranges", return_value=[
                 {"start_ms": 900, "end_ms": 1600, "duration_ms": 700}
             ]), \
             mock.patch.object(video_compose.media, "build_clean_master", side_effect=build_clean), \
             mock.patch.object(video_compose.renderer, "render", side_effect=render_video) as render_mock:
            _, analyzed = self.request(
                "POST", "/api/gen/video-compose/projects/%s/analyze-source" % project["id"],
                {"expected_revision": project["revision"]},
            )
            ready = analyzed["project"]
            decisions = {item["id"]: "keep" for item in ready["candidates"]}
            _, reviewed = self.request(
                "POST", "/api/gen/video-compose/projects/%s/edit-decisions" % project["id"],
                {"expected_revision": ready["revision"], "decisions": decisions},
            )
            confirmed = reviewed["project"]
            _, rendered = self.request(
                "POST", "/api/gen/video-compose/projects/%s/render" % project["id"],
                {"expected_revision": confirmed["revision"], "hook": {}, "headlines": [], "brand": {}},
            )
            _, replayed = self.request(
                "POST", "/api/gen/video-compose/projects/%s/render" % project["id"],
                {"expected_revision": confirmed["revision"], "hook": {}, "headlines": [], "brand": {}},
            )
        self.assertEqual("completed", rendered["project"]["status"])
        self.assertEqual(rendered["project"]["output_asset_id"],
                         replayed["project"]["output_asset_id"])
        self.assertEqual(1, render_mock.call_count)
        request = urllib.request.Request(
            self.base + rendered["output_url"], method="GET",
            headers={"Authorization": "Bearer test"},
        )
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual("video/mp4", response.headers.get_content_type())
            self.assertEqual(b"finished-video", response.read())

    def test_rejects_cross_user_asset_and_arbitrary_source_fields(self):
        for body in ({"source_asset_id": 8}, {"source_asset_id": 7, "url": "https://example.com/video.mp4"}):
            request = urllib.request.Request(
                self.base + "/api/gen/video-compose/projects",
                data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.opener.open(request, timeout=5)
            self.assertIn(rejected.exception.code, {400, 404})

    def test_requires_authentication(self):
        request = urllib.request.Request(
            self.base + "/api/gen/video-compose/projects", method="GET",
            headers={"Authorization": "Bearer invalid"},
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.opener.open(request, timeout=5)
        self.assertEqual(401, rejected.exception.code)


class VideoComposeDeploymentTests(unittest.TestCase):
    def test_runtime_bootstrap_uses_cpu_and_existing_system_browser(self):
        script = (ROOT / "deploy/zelong/bootstrap-video-compose-runtime.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("ONNXRUNTIME_NODE_INSTALL_CUDA=skip", script)
        self.assertIn("HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium-browser", script)
        self.assertIn("hyperframes@0.7.90", script)
        renderer_source = (ROOT / "server/content_domains/video_compose_render.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'environment["HYPERFRAMES_BROWSER_PATH"] = "/usr/bin/chromium-browser"',
            renderer_source,
        )
        self.assertIn("hyperframes@0.7.90", renderer_source)
        for package_path in (
            ROOT / "site/assets/one-click/templates/viral-talking-head-v1/package.json",
            ROOT / "tools/hyperframes/viral-talking-head-v1/package.json",
        ):
            self.assertIn("hyperframes@0.7.90", package_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
