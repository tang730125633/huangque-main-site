import json
import http.server
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
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import core, points, timeline_compose
import hq_cli_api


class TimelineComposeTests(unittest.TestCase):
    def payload(self):
        return {
            "mode": "timeline",
            "segments": [
                {"type": "image", "asset_id": 11, "duration": 2, "transition": "fade"},
                {"type": "video", "asset_id": 22, "trim_start": 1, "trim_end": 4,
                 "transition": "none"},
                {"type": "text_card", "text": "报错救星，专治小白卡壳", "duration": 2,
                 "transition": "none"},
            ],
            "ratio": "9:16", "preserve_source_audio": True, "bgm": False,
        }

    def test_validation_binds_owned_assets_duration_and_price_breakdown(self):
        with mock.patch.object(
            timeline_compose, "_owned_file",
            side_effect=[("/tmp/image.png", "i"), ("/tmp/video.mp4", "v")],
        ) as owned, mock.patch.object(
            timeline_compose, "_probe",
            return_value={"duration": 5.0, "has_audio": True, "width": 720, "height": 1280},
        ), mock.patch.object(
            timeline_compose, "_probe_image",
        ), mock.patch.object(
            timeline_compose, "FONT_PATH", __file__,
        ), mock.patch.object(
            timeline_compose.pricing if hasattr(timeline_compose, "pricing") else __import__(
                "content_domains.pricing", fromlist=["pricing"]
            ), "get_price",
            side_effect=lambda key: {
                "video.timeline_compose.base": 5,
                "video.timeline_compose.segment": 1,
                "video.timeline_compose.30s": 2,
            }[key],
        ):
            value = timeline_compose.validate_payload(self.payload(), "alice")
        self.assertEqual(6.5, value["duration"])
        self.assertEqual({
            "base": 5, "segments": 3, "duration_blocks": 1,
            "duration": 2, "total": 10,
        }, value["cost_breakdown"])
        self.assertEqual(10, points.cost_of("matrix_template_video", value))
        self.assertEqual([
            mock.call("alice", "image", 11, 0),
            mock.call("alice", "video", 22),
        ], owned.call_args_list)
        self.assertTrue(value["_timeline_validated"])

    def test_invalid_timeline_and_bgm_fail_before_render(self):
        cases = [
            dict(self.payload(), segments=self.payload()["segments"][:-1] + [
                {"type": "text_card", "text": "结尾", "transition": "fade"},
            ]),
            dict(self.payload(), bgm=True),
            dict(self.payload(), segments=[{"type": "image", "asset_id": 11}]),
        ]
        with mock.patch.object(timeline_compose, "_owned_file", return_value=("/tmp/a", "x")), \
             mock.patch.object(timeline_compose, "_probe_image"), \
             mock.patch.object(timeline_compose, "_probe", return_value={
                 "duration": 5.0, "has_audio": False, "width": 720, "height": 1280,
             }):
            for value in cases:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    timeline_compose.validate_payload(value, "alice")

    def test_fractional_asset_id_is_rejected(self):
        value = self.payload()
        value["segments"][0]["asset_id"] = 11.5
        with self.assertRaisesRegex(ValueError, "asset_id 无效"):
            timeline_compose.validate_payload(value, "alice")

    def test_unreadable_image_is_rejected_before_quote(self):
        with mock.patch.object(
            timeline_compose, "_owned_file", return_value=("/tmp/broken.png", "x"),
        ), mock.patch.object(
            timeline_compose, "_probe_image", side_effect=ValueError("图片素材无法读取"),
        ):
            with self.assertRaisesRegex(ValueError, "图片素材无法读取"):
                timeline_compose.validate_payload(self.payload(), "alice")

    def test_cli_plan_is_typed_quoted_and_reuses_matrix_submission_path(self):
        value = dict(self.payload())
        value.pop("mode")
        plan = hq_cli_api.action_plan("video-timeline-compose", value)
        self.assertEqual("generation", plan["kind"])
        self.assertEqual("generation:quote", plan["scope"])
        self.assertEqual("matrix_template_video", plan["generation_kind"])
        self.assertEqual("/api/gen/matrix-template", plan["endpoint"])
        self.assertEqual("timeline", plan["payload"]["mode"])
        self.assertEqual(("cost_breakdown",), plan["quote_result_fields"])
        self.assertIn("video-timeline-compose", {
            item["action"] for item in hq_cli_api.action_catalog({
                "matrix_template_video": True,
            })["actions"]
        })

    def test_invalid_owned_asset_is_rejected_before_charge(self):
        from content_domains import audio, video

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.db"
            original = core.JOB_DB
            core.JOB_DB = str(database)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(core, "verify", return_value={
                    "username": "alice", "points": 100, "must_change": False,
                }), mock.patch.object(
                    core, "_domains", return_value=(audio, points, video),
                ), mock.patch.object(
                    timeline_compose, "_owned_file",
                    side_effect=ValueError("资产不存在或不属于当前账号"),
                ), mock.patch.object(
                    points, "deduct_points",
                    side_effect=AssertionError("invalid asset must not charge"),
                ) as deduct:
                    request = urllib.request.Request(
                        "http://127.0.0.1:%d/api/gen/matrix-template" % server.server_port,
                        data=json.dumps(self.payload(), ensure_ascii=False).encode(),
                        headers={
                            "Authorization": "Bearer account-token",
                            "Content-Type": "application/json",
                            "Idempotency-Key": "timeline-invalid-asset",
                        }, method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.build_opener(
                            urllib.request.ProxyHandler({})
                        ).open(request, timeout=10)
                    self.assertEqual(400, raised.exception.code)
                    self.assertIn("不属于当前账号", json.loads(raised.exception.read())["detail"])
                    deduct.assert_not_called()
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                core.JOB_DB = original

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_local_renderer_outputs_two_images_two_videos_text_fade_and_bgm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = [root / "image-1.png", root / "image-2.png"]
            videos = [root / "video-1.mp4", root / "video-2.mp4"]
            for path, color in zip(images, ("0x2563eb", "0xdc2626")):
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=%s:s=480x640:d=0.1" % color,
                    "-frames:v", "1", str(path),
                ], check=True)
            for path, color, frequency in zip(
                videos, ("0x16a34a", "0x7c3aed"), (440, 550),
            ):
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=%s:s=640x480:r=30:d=1.2" % color,
                    "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=1.2" % frequency,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
                ], check=True)
            bgm = root / "bgm.wav"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=6",
                str(bgm),
            ], check=True)
            raw = {
                "mode": "timeline", "ratio": "9:16", "preserve_source_audio": True,
                "bgm": True, "bgm_asset_id": 5, "bgm_volume": 0.15,
                "segments": [
                    {"type": "image", "asset_id": 1, "duration": 1.2,
                     "transition": "fade"},
                    {"type": "video", "asset_id": 2, "trim_start": 0,
                     "trim_end": 1.2, "transition": "fade"},
                    {"type": "image", "asset_id": 3, "duration": 1.2,
                     "transition": "fade"},
                    {"type": "video", "asset_id": 4, "trim_start": 0,
                     "trim_end": 1.2, "transition": "fade"},
                    {"type": "text_card", "text": "时间轴成片", "duration": 1.2,
                     "transition": "none"},
                ],
            }
            with mock.patch.object(
                timeline_compose, "_owned_file",
                side_effect=[
                    (str(images[0]), timeline_compose._snapshot(images[0])),
                    (str(videos[0]), timeline_compose._snapshot(videos[0])),
                    (str(images[1]), timeline_compose._snapshot(images[1])),
                    (str(videos[1]), timeline_compose._snapshot(videos[1])),
                    (str(bgm), timeline_compose._snapshot(bgm)),
                ],
            ), mock.patch.object(
                timeline_compose, "FONT_PATH", "/System/Library/Fonts/STHeiti Medium.ttc",
            ), mock.patch.object(core, "OUT_DIR", root), mock.patch.object(
                core, "public_url", return_value="https://example.com/timeline.mp4",
            ), mock.patch(
                "content_domains.matrix_template_video._persist_runtime",
                return_value=True,
            ), mock.patch("content_domains.pricing.get_price", side_effect=lambda key: {
                "video.timeline_compose.base": 5,
                "video.timeline_compose.segment": 1,
                "video.timeline_compose.30s": 2,
            }[key]):
                validated = timeline_compose.validate_payload(raw, "alice")
                result = timeline_compose.generate({
                    **validated, "_username": "alice", "_job_id": 9001,
                })
            self.assertEqual("timeline_compose", result["mode"])
            self.assertEqual((1080, 1920), (result["width"], result["height"]))
            self.assertEqual(5, result["segment_count"])
            output = root / result["video_file"]
            self.assertTrue(output.is_file())
            probe = timeline_compose._probe(output)
            self.assertAlmostEqual(result["duration"], probe["duration"], delta=.35)


if __name__ == "__main__":
    unittest.main()
