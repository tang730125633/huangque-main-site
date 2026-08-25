# -*- coding: utf-8 -*-
import base64
import hashlib
import io
import json
import os
import socket
import ssl
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import ANY, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import (  # noqa: E402
    points,
    short_drama_native_audio,
    submission_idempotency,
    video,
    video_minimax_h3,
)


class _DownloadResponse:
    def __init__(self, body, headers, *, status=200):
        self.body = body
        self.headers = headers
        self.status = status
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        end = len(self.body) if size < 0 else min(
            len(self.body), self.offset + size
        )
        chunk = self.body[self.offset:end]
        self.offset = end
        return chunk


class MiniMaxH3VideoTests(unittest.TestCase):
    @staticmethod
    def _image(fmt="PNG", size=(256, 256)):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", size, (40, 80, 120)).save(output, fmt)
        mime = "jpeg" if fmt == "JPEG" else fmt.lower()
        return "data:image/%s;base64,%s" % (
            mime, base64.b64encode(output.getvalue()).decode("ascii")
        )

    @staticmethod
    def _minimax_result():
        return {
            "request_id": "h3-native-task",
            "source_video_url": "https://cdn.example/native.mp4",
            "model": "MiniMax-H3",
            "duration": 5,
            "ratio": "16:9",
            "resolution": "2k",
            "provider": "minimax_h3_cn",
        }

    def test_faststart_derivative_preserves_supplier_raw_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw_relative = "video/minimax_h3_raw_test.mp4"
            raw_path = root / raw_relative
            raw_path.parent.mkdir(parents=True)
            raw_bytes = b"immutable-provider-video"
            raw_path.write_bytes(raw_bytes)

            def run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"faststart-derived-video")
                return None

            with patch.object(video, "_out_path", side_effect=lambda rel: root / rel), \
                    patch.object(video.subprocess, "run", side_effect=run):
                derived_relative = video._faststart_video_derivative(raw_relative)

            self.assertNotEqual(raw_relative, derived_relative)
            self.assertEqual(raw_bytes, raw_path.read_bytes())
            self.assertEqual(
                b"faststart-derived-video", (root / derived_relative).read_bytes()
            )

    def test_bound_short_drama_returns_raw_and_derived_lineage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw_relative = "video/minimax_h3_raw_test.mp4"
            derived_relative = "video/minimax_h3_derived_test.mp4"
            raw_bytes = b"immutable-provider-video"
            derived_bytes = b"faststart-derived-video"

            def download(*_args, **_kwargs):
                path = root / raw_relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw_bytes)
                return raw_relative

            def derive(_relative):
                (root / derived_relative).write_bytes(derived_bytes)
                return derived_relative

            evidence = {
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "size_bytes": len(raw_bytes),
                "resolution": {"width": 2560, "height": 1440},
                "audio": {"audible": True, "codec": "aac"},
                "inspected_at": 1,
            }
            with patch.object(video, "get_resumable_grok_request", return_value=None), \
                    patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm", "secret": "secret"}), \
                    patch.object(video.provider_keys, "set_health"), \
                    patch.object(video, "update_video_asset_phase"), \
                    patch.object(video_minimax_h3, "generate", return_value=self._minimax_result()) as generate, \
                    patch.object(video, "_download_video_file_direct", side_effect=download), \
                    patch.object(video, "_faststart_video_derivative", create=True, side_effect=derive), \
                    patch.object(video, "_resolve_out_file", side_effect=lambda rel: root / rel), \
                    patch.object(video, "_extract_first_frame_cover", return_value=None), \
                    patch.object(video, "public_url", return_value="https://cos.example/native.mp4"), \
                    patch.object(short_drama_native_audio, "inspect_native_media", create=True, return_value=evidence), \
                    patch.object(short_drama_native_audio, "inspect_native_resolution", return_value=evidence["resolution"]), \
                    patch.object(short_drama_native_audio, "inspect_native_audio", return_value=evidence["audio"]):
                result = video.gen_xiaole_video({
                    "_job_id": 91,
                    "channel": "minimax",
                    "prompt": "人物走进旧城区",
                    "model": "MiniMax-H3",
                    "duration": 5,
                    "ratio": "16:9",
                    "resolution": "2k",
                    "reference_images": [],
                    "_short_drama_native_audio_required": True,
                })

            generate.assert_called_once()
            self.assertEqual(raw_relative, result["raw_video_file"])
            self.assertEqual(derived_relative, result["video_file"])
            self.assertEqual(
                evidence["sha256"], result["native_media"]["raw"]["sha256"]
            )
            self.assertEqual(
                evidence["sha256"],
                result["native_media"]["derived"]["derived_from_sha256"],
            )
            self.assertEqual(raw_bytes, (root / raw_relative).read_bytes())

    def test_native_validation_failure_removes_owned_raw_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw_relative = "video/minimax_h3_raw_failed.mp4"

            def download(*_args, **_kwargs):
                path = root / raw_relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"silent-provider-video")
                return raw_relative

            error = short_drama_native_audio.NativeAudioError(
                "provider_audio_silent", "声音不可听"
            )
            with patch.object(video, "get_resumable_grok_request", return_value=None), \
                    patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm", "secret": "secret"}), \
                    patch.object(video.provider_keys, "set_health"), \
                    patch.object(video, "update_video_asset_phase"), \
                    patch.object(video_minimax_h3, "generate", return_value=self._minimax_result()) as generate, \
                    patch.object(video, "_download_video_file_direct", side_effect=download), \
                    patch.object(video, "_resolve_out_file", side_effect=lambda rel: root / rel), \
                    patch.object(short_drama_native_audio, "inspect_native_media", create=True, side_effect=error), \
                    patch.object(short_drama_native_audio, "inspect_native_resolution", side_effect=error):
                with self.assertRaises(video_minimax_h3.MiniMaxProviderFailed):
                    video.gen_xiaole_video({
                        "_job_id": 92,
                        "channel": "minimax",
                        "prompt": "人物走进旧城区",
                        "model": "MiniMax-H3",
                        "duration": 5,
                        "ratio": "16:9",
                        "resolution": "2k",
                        "reference_images": [],
                        "_short_drama_native_audio_required": True,
                    })

            generate.assert_called_once()
            self.assertFalse((root / raw_relative).exists())

    def test_native_orphan_reaper_deletes_only_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video_root = root / "video"
            video_root.mkdir()
            job_db = root / "jobs.db"
            asset_db = root / "assets.db"
            referenced_job = "video/minimax_h3_raw_referenced.mp4"
            referenced_provider_job = "video/minimax_h3_raw_provider_job.mp4"
            referenced_asset = "video/minimax_h3_derived_referenced.mp4"
            orphan_raw = "video/minimax_h3_raw_orphan.mp4"
            orphan_derived = "video/minimax_h3_derived_orphan.mp4"
            orphan_part = "video/minimax_h3_raw_crashed.mp4.part-deadbeef"
            for relative in (
                referenced_job, referenced_provider_job, referenced_asset, orphan_raw,
                orphan_derived, orphan_part,
            ):
                path = root / relative
                path.write_bytes(relative.encode("utf-8"))
                old = time.time() - 8 * 3600
                os.utime(path, (old, old))
            with closing(sqlite3.connect(job_db)) as connection:
                connection.execute(
                    "CREATE TABLE jobs (id INTEGER, status TEXT, result TEXT)"
                )
                connection.execute(
                    "INSERT INTO jobs VALUES (1,'done',?)",
                    (json.dumps({
                        "raw_video_file": referenced_job,
                        "video_file": "video/minimax_h3_derived_job.mp4",
                    }),),
                )
                connection.execute(
                    "CREATE TABLE short_drama_provider_shot_jobs "
                    "(result_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO short_drama_provider_shot_jobs VALUES (?)",
                    (json.dumps({
                        "native_media": {
                            "raw": {"file": referenced_provider_job},
                        },
                    }),),
                )
                connection.commit()
            with closing(sqlite3.connect(asset_db)) as connection:
                connection.execute(
                    "CREATE TABLE video_assets (video_file TEXT, image_file TEXT)"
                )
                connection.execute(
                    "INSERT INTO video_assets VALUES (?,NULL)",
                    (referenced_asset,),
                )
                connection.commit()
            with patch.object(video, "_out_path", side_effect=lambda rel: root / rel), \
                    patch.object(video, "jdb", side_effect=lambda: sqlite3.connect(job_db)), \
                    patch.object(video, "adb", side_effect=lambda: sqlite3.connect(asset_db)):
                result = video.reap_short_drama_native_orphans(
                    now=int(time.time()), grace_seconds=6 * 3600
                )

            self.assertTrue((root / referenced_job).is_file())
            self.assertTrue((root / referenced_provider_job).is_file())
            self.assertTrue((root / referenced_asset).is_file())
            self.assertFalse((root / orphan_raw).exists())
            self.assertFalse((root / orphan_derived).exists())
            self.assertFalse((root / orphan_part).exists())
            self.assertEqual(
                {orphan_raw, orphan_derived, orphan_part}, set(result["deleted"])
            )

    def test_native_orphan_reaper_preserves_recent_and_database_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video_root = root / "video"
            video_root.mkdir()
            job_db = root / "jobs.db"
            asset_db = root / "assets.db"
            nested_reference = "video/minimax_h3_raw_active.mp4"
            recent_orphan = "video/minimax_h3_derived_recent.mp4"
            (root / nested_reference).write_bytes(b"active")
            (root / recent_orphan).write_bytes(b"recent")
            old = time.time() - 8 * 3600
            os.utime(root / nested_reference, (old, old))
            with closing(sqlite3.connect(job_db)) as connection:
                connection.execute(
                    "CREATE TABLE jobs (id INTEGER, status TEXT, result TEXT)"
                )
                connection.execute(
                    "INSERT INTO jobs VALUES (2,'running',?)",
                    (json.dumps({
                        "native_media": {"raw": {"file": nested_reference}},
                    }),),
                )
                connection.commit()
            with closing(sqlite3.connect(asset_db)) as connection:
                connection.execute(
                    "CREATE TABLE video_assets (video_file TEXT, image_file TEXT)"
                )
                connection.commit()
            with patch.object(video, "_out_path", side_effect=lambda rel: root / rel), \
                    patch.object(video, "jdb", side_effect=lambda: sqlite3.connect(job_db)), \
                    patch.object(video, "adb", side_effect=lambda: sqlite3.connect(asset_db)):
                result = video.reap_short_drama_native_orphans(
                    now=int(time.time()), grace_seconds=6 * 3600
                )

            self.assertTrue((root / nested_reference).is_file())
            self.assertTrue((root / recent_orphan).is_file())
            self.assertEqual([], result["deleted"])

    def test_native_orphan_reaper_fails_closed_when_reference_db_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video_root = root / "video"
            video_root.mkdir()
            orphan = video_root / "minimax_h3_raw_must_be_retained.mp4"
            orphan.write_bytes(b"native")
            old = time.time() - 8 * 3600
            os.utime(orphan, (old, old))

            def unavailable():
                raise sqlite3.OperationalError("reference database unavailable")

            with patch.object(video, "_out_path", side_effect=lambda rel: root / rel), \
                    patch.object(video, "jdb", side_effect=unavailable):
                result = video.reap_short_drama_native_orphans(
                    now=int(time.time()), grace_seconds=6 * 3600
                )

            self.assertTrue(orphan.is_file())
            self.assertEqual([], result["deleted"])
            self.assertTrue(result["errors"])

            job_db = root / "jobs.db"
            with closing(sqlite3.connect(job_db)) as connection:
                connection.execute(
                    "CREATE TABLE jobs (id INTEGER, status TEXT, result TEXT)"
                )
                connection.commit()
            with patch.object(video, "_out_path", side_effect=lambda rel: root / rel), \
                    patch.object(video, "jdb", side_effect=lambda: sqlite3.connect(job_db)), \
                    patch.object(video, "adb", side_effect=unavailable):
                asset_result = video.reap_short_drama_native_orphans(
                    now=int(time.time()), grace_seconds=6 * 3600
                )
            self.assertTrue(orphan.is_file())
            self.assertEqual([], asset_result["deleted"])
            self.assertEqual("assets_database", asset_result["errors"][0]["scope"])

    def test_native_orphan_reaper_fails_closed_on_malformed_reference_json(self):
        for malformed_source in ("shared_job", "provider_job"):
            for malformed_payload in ("{malformed", "[]"):
                with self.subTest(
                    malformed_source=malformed_source,
                    malformed_payload=malformed_payload,
                ), \
                    tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    video_root = root / "video"
                    video_root.mkdir()
                    native = video_root / "minimax_h3_raw_unknown_reference.mp4"
                    native.write_bytes(b"native")
                    old = time.time() - 8 * 3600
                    os.utime(native, (old, old))
                    job_db = root / "jobs.db"
                    asset_db = root / "assets.db"
                    with closing(sqlite3.connect(job_db)) as connection:
                        connection.execute(
                            "CREATE TABLE jobs (id INTEGER, status TEXT, result TEXT)"
                        )
                        connection.execute(
                            "CREATE TABLE short_drama_provider_shot_jobs "
                            "(result_json TEXT)"
                        )
                        connection.execute(
                            "INSERT INTO jobs VALUES (1,'done',?)",
                            (
                                malformed_payload
                                if malformed_source == "shared_job" else None,
                            ),
                        )
                        if malformed_source == "provider_job":
                            connection.execute(
                                "INSERT INTO short_drama_provider_shot_jobs VALUES (?)",
                                (malformed_payload,),
                            )
                        connection.commit()
                    with closing(sqlite3.connect(asset_db)) as connection:
                        connection.execute(
                            "CREATE TABLE video_assets "
                            "(video_file TEXT, image_file TEXT)"
                        )
                        connection.commit()
                    with patch.object(
                        video, "_out_path", side_effect=lambda rel: root / rel,
                    ), patch.object(
                        video, "jdb", side_effect=lambda: sqlite3.connect(job_db),
                    ), patch.object(
                        video, "adb", side_effect=lambda: sqlite3.connect(asset_db),
                    ):
                        result = video.reap_short_drama_native_orphans(
                            now=int(time.time()), grace_seconds=6 * 3600
                        )
                    self.assertTrue(native.is_file())
                    self.assertEqual([], result["deleted"])
                    self.assertTrue(result["errors"])

    def test_reference_request_and_20_percent_markup(self):
        image = self._image()
        body = video_minimax_h3.build_request(
            "第1张参考图仅作为人物身份参考", [image], "9:16", 15, "2k"
        )
        self.assertEqual(body["model"], "MiniMax-H3")
        self.assertEqual(body["resolution"], "2K")
        self.assertEqual(body["content"][1]["role"], "reference_image")
        with patch("content_domains.points.pricing.get_price", return_value=6):
            self.assertEqual(points.cost_of("xiaole_video", {
                "channel": "minimax", "duration": 15, "resolution": "2k",
            }), 90)

    def test_generate_rejects_remote_reference_urls_before_provider_submission(self):
        direct_targets = (
            "https://10.0.0.8/reference.png",
            "https://127.0.0.1/reference.png",
            "http://169.254.169.254/latest/meta-data",
        )
        for target in direct_targets:
            with self.subTest(target=target), patch.object(
                video_minimax_h3, "_request_json",
                side_effect=AssertionError("unsafe reference reached provider submission"),
            ) as submit:
                with self.assertRaisesRegex(ValueError, "参考图"):
                    video_minimax_h3.generate(
                        "人物走进电梯", [target], api_key="test-only-secret",
                    )
                submit.assert_not_called()

        private_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.8", 443)),
        ]
        with patch("socket.getaddrinfo", return_value=private_dns), patch.object(
            video_minimax_h3, "_request_json",
            side_effect=AssertionError("private DNS target reached provider submission"),
        ) as submit:
            with self.assertRaisesRegex(ValueError, "参考图"):
                video_minimax_h3.generate(
                    "人物走进电梯",
                    ["https://reference.example/character.png"],
                    api_key="test-only-secret",
                )
            submit.assert_not_called()

    def test_verified_metaso_text_only_2k_request_contract(self):
        self.assertEqual(
            "https://metaso.cn/api/minimax", video_minimax_h3.API_BASE
        )
        body = video_minimax_h3.build_request(
            "史诗级太空歌剧院线预告", [], "16:9", 5, "2K"
        )
        self.assertEqual({
            "model": "MiniMax-H3",
            "content": [{"type": "text", "text": "史诗级太空歌剧院线预告"}],
            "resolution": "2K",
            "duration": 5,
            "ratio": "16:9",
        }, body)

        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"task_id":"verified-task"}'

        class Opener:
            def open(self, request, timeout):
                captured.update(request=request, timeout=timeout)
                return Response()

        created = video_minimax_h3._request_json(
            Opener(), "POST", "/v2/video_generation", body,
            timeout=120, api_key="test-only-secret",
        )
        self.assertEqual({"task_id": "verified-task"}, created)
        self.assertEqual(
            "https://metaso.cn/api/minimax/v2/video_generation",
            captured["request"].full_url,
        )
        self.assertEqual(body, json.loads(captured["request"].data.decode("utf-8")))

    def test_metaso_result_host_is_allowed_without_trusting_suffix_spoofs(self):
        public_dns = [(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
            ("8.8.8.8", 443),
        )]
        with patch("socket.getaddrinfo", return_value=public_dns):
            url = "https://files.metaso.cn/result/video.mp4"
            self.assertEqual(
                url,
                video._validate_restricted_download_url(
                    url, video_minimax_h3.RESULT_HOSTS,
                ),
            )

        with self.assertRaisesRegex(ValueError, "允许的 HTTPS CDN"):
            video._validate_restricted_download_url(
                "https://files.metaso.cn.attacker.example/video.mp4",
                video_minimax_h3.RESULT_HOSTS,
            )

    def test_new_payload_persists_metaso_origin_before_submission(self):
        with patch.object(video_minimax_h3, "available", return_value=True), \
                patch("content_domains.feature_flags.is_enabled", return_value=True):
            payload = video.validate_xiaole_video_payload({
                "channel": "minimax", "prompt": "舰队跃迁离去",
                "duration": 5, "ratio": "16:9", "resolution": "2k",
            })
        self.assertEqual(
            video_minimax_h3.ORIGIN_METASO, payload["_minimax_origin"]
        )
        self.assertNotIn("_minimax_api_base", payload)

    def test_new_768p_request_is_rejected_but_legacy_resume_remains_supported(self):
        with self.assertRaisesRegex(ValueError, "仅支持 2K"):
            video_minimax_h3.build_request(
                "旧分辨率不应创建新任务", [], "9:16", 5, "768p"
            )

    def test_legacy_hash_candidates_replay_old_768p_record(self):
        request = {
            "channel": "minimax", "prompt": "legacy paid request",
            "model": "MiniMax-H3", "duration": 5,
            "ratio": "9:16", "resolution": "768p",
        }
        candidates = video.minimax_idempotency_replay_bodies(request)
        old = next(
            item for item in candidates
            if item.get("_minimax_api_base") == video_minimax_h3.METASO_API_BASE
        )
        with tempfile.TemporaryDirectory() as folder:
            database = str(Path(folder) / "idempotency.db")

            def factory():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            self.assertEqual(
                ("new", None),
                submission_idempotency.begin(
                    factory, "alice", "/api/gen/xiaole_video", "legacy-key-001", old,
                ),
            )
            submission_idempotency.complete(
                factory, "alice", "/api/gen/xiaole_video", "legacy-key-001",
                {"job_id": 88},
            )
            state, response = submission_idempotency.replay_existing(
                factory, "alice", "/api/gen/xiaole_video", "legacy-key-001",
                candidates,
            )
        self.assertEqual(("replay", 88), (state, response["job_id"]))

    def test_omitted_resolution_replays_both_current_and_legacy_hashes(self):
        request = {
            "channel": "minimax", "prompt": "legacy omitted resolution",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
        }
        candidates = video.minimax_idempotency_replay_bodies(request)
        self.assertEqual(
            {"2k", "768p"},
            {item["resolution"] for item in candidates},
        )
        self.assertEqual(
            "2k", video.minimax_idempotency_claim_body(request)["resolution"],
        )

    def test_unmarked_historical_origin_is_inferred_once_or_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                video_minimax_h3.ORIGIN_LEGACY,
                video_minimax_h3.historical_origin_from_environment(),
            )
        with patch.dict(os.environ, {
            "MINIMAX_API_BASE": video_minimax_h3.METASO_API_BASE,
        }, clear=True):
            self.assertEqual(
                video_minimax_h3.ORIGIN_METASO,
                video_minimax_h3.historical_origin_from_environment(),
            )
        with patch.dict(os.environ, {
            "MINIMAX_API_BASE": "https://custom.example/minimax",
        }, clear=True), self.assertRaises(video_minimax_h3.MiniMaxOriginUnknown):
            video_minimax_h3.historical_origin_from_environment()
        with self.assertRaises(video_minimax_h3.MiniMaxOriginUnknown):
            video_minimax_h3.origin_from_payload({})

    def test_historical_origin_backfill_is_persisted_in_running_job(self):
        with tempfile.TemporaryDirectory() as folder:
            database = str(Path(folder) / "jobs.db")

            def factory():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            connection = factory()
            connection.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,payload TEXT,status TEXT,updated_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO jobs(id,payload,status,updated_at) VALUES(8,?,'running',0)",
                (json.dumps({"channel": "minimax", "resolution": "768p"}),),
            )
            connection.commit()
            connection.close()
            with patch.object(video, "jdb", factory):
                video._persist_minimax_origin(
                    8, video_minimax_h3.ORIGIN_METASO,
                )
            connection = factory()
            stored = json.loads(connection.execute(
                "SELECT payload FROM jobs WHERE id=8"
            ).fetchone()[0])
            connection.close()
        self.assertEqual(
            video_minimax_h3.ORIGIN_METASO, stored["_minimax_origin"]
        )
        self.assertNotIn("_minimax_api_base", stored)

    def test_task_query_uses_its_persisted_provider_origin(self):
        captured = []

        def request(_opener, method, path, body=None, timeout=90, api_key=None,
                    api_base=None):
            captured.append((method, path, api_base))
            return {"task": {
                "status": "succeeded",
                "content": {"url": "https://cdn.example/task.mp4"},
            }}

        with patch.object(video_minimax_h3, "_request_json", side_effect=request), \
                patch.object(video_minimax_h3, "_opener", return_value=object()):
            video_minimax_h3.resume(
                "legacy-task", api_key="secret", resolution="768p",
                api_base=video_minimax_h3.LEGACY_API_BASE,
                sleep=lambda _seconds: None,
            )
            video_minimax_h3.resume(
                "metaso-task", api_key="secret", resolution="2k",
                api_base=video_minimax_h3.API_BASE,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(video_minimax_h3.LEGACY_API_BASE, captured[0][2])
        self.assertEqual(video_minimax_h3.API_BASE, captured[1][2])

    def test_query_url_is_built_from_the_task_origin_and_rejects_unknown_hosts(self):
        urls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"task":{"status":"running"}}'

        class Opener:
            def open(self, request, timeout):
                urls.append(request.full_url)
                return Response()

        video_minimax_h3.query_task(
            "legacy-task", "secret", Opener(),
            api_base=video_minimax_h3.LEGACY_API_BASE,
        )
        video_minimax_h3.query_task(
            "metaso-task", "secret", Opener(),
            api_base=video_minimax_h3.API_BASE,
        )
        self.assertEqual([
            "https://api.minimaxi.com/v2/query/video_generation/legacy-task",
            "https://metaso.cn/api/minimax/v2/query/video_generation/metaso-task",
        ], urls)
        with self.assertRaisesRegex(ValueError, "任务来源无效"):
            video_minimax_h3.query_task(
                "tampered-task", "secret", Opener(),
                api_base="https://example.invalid/provider",
            )

    def test_credential_probe_reuses_the_accepted_task_list_endpoint(self):
        with patch.object(video_minimax_h3, "_request_json", return_value={}) as request:
            self.assertTrue(video_minimax_h3.check_credentials("test-only-secret", opener=object()))
        self.assertEqual("GET", request.call_args.args[1])
        self.assertEqual(
            "/v2/query/video_generation?page_num=1&page_size=1",
            request.call_args.args[2],
        )
        self.assertEqual("test-only-secret", request.call_args.kwargs["api_key"])

    def test_create_once_then_resume_only_queries(self):
        image = self._image()
        succeeded = {"task": {
            "status": "succeeded", "content": {"url": "https://cdn.example/h3.mp4"},
            "duration": 5, "ratio": "9:16",
        }}
        calls = []

        def request(_opener, method, path, body=None, timeout=90, api_key=None,
                    api_base=None):
            calls.append((method, path))
            return {"task_id": "h3-task-1"} if method == "POST" else succeeded

        with patch.object(video_minimax_h3, "_request_json", side_effect=request), \
                patch.object(video_minimax_h3, "_opener", return_value=object()):
            created = video_minimax_h3.generate(
                "人物走进电梯", [image], duration=5, api_key="secret", sleep=lambda _s: None
            )
            resumed = video_minimax_h3.resume(
                "h3-task-1", duration=5, api_key="secret", sleep=lambda _s: None
            )
        self.assertEqual(created["source_video_url"], "https://cdn.example/h3.mp4")
        self.assertEqual(resumed["request_id"], "h3-task-1")
        self.assertEqual([method for method, _path in calls], ["POST", "GET", "GET"])

    def test_generate_uses_one_persisted_origin_for_create_and_first_poll(self):
        calls = []

        def request(_opener, method, path, body=None, timeout=90, api_key=None,
                    api_base=None):
            calls.append((method, path, api_base))
            if method == "POST":
                return {"task_id": "stable-origin-task"}
            return {"task": {
                "status": "succeeded",
                "content": {"url": "https://cdn.example/stable.mp4"},
            }}

        with patch.object(video_minimax_h3, "_request_json", side_effect=request), \
                patch.object(video_minimax_h3, "_opener", return_value=object()):
            video_minimax_h3.generate(
                "a ship leaves the port", duration=5, api_key="secret",
                api_base=video_minimax_h3.API_BASE,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(
            [video_minimax_h3.API_BASE, video_minimax_h3.API_BASE],
            [item[2] for item in calls],
        )

    def test_jpeg_reference_is_normalized_to_clean_png(self):
        body = video_minimax_h3.build_request(
            "人物走进电梯", [self._image("JPEG", (257, 455))], duration=5
        )
        normalized = body["content"][1]["image_url"]["url"]
        self.assertTrue(normalized.startswith("data:image/png;base64,"))

    def test_invalid_image_and_provider_2013_are_user_readable(self):
        corrupt = "data:image/jpeg;base64," + base64.b64encode(b"not-jpeg").decode()
        with self.assertRaisesRegex(ValueError, "无法识别"):
            video_minimax_h3.build_request("人物走进电梯", [corrupt], duration=5)
        self.assertEqual(
            "麦克视频请求参数或参考图无法识别，请检查参数及 JPG/PNG 图片",
            video_minimax_h3._human_error(400, "media metadata is invalid (2013)"),
        )

    def test_legacy_768p_task_resume_preserves_its_resolution(self):
        succeeded = {
            "task": {
                "status": "succeeded",
                "content": {"url": "https://cdn.example/legacy.mp4"},
            }
        }
        with patch.object(video_minimax_h3, "_request_json", return_value=succeeded), \
                patch.object(video_minimax_h3, "_opener", return_value=object()):
            result = video_minimax_h3.resume(
                "legacy-h3-task", api_key="secret", resolution="768p",
                sleep=lambda _seconds: None,
            )
        self.assertEqual("768p", result["resolution"])

    def test_shared_video_job_uses_minimax_adapter(self):
        rendered = {
            "request_id": "h3-task-1", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 15, "ratio": "9:16",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
                patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm-key", "secret": "secret"}), \
                patch.object(video.provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video_minimax_h3, "generate", return_value=rendered) as generate, \
                patch.object(video, "_download_video_file_direct", return_value="video/h3.mp4") as download, \
                patch.object(video, "_extract_first_frame_cover", return_value=None), \
                patch.object(video, "public_url", return_value="https://cos.example/h3.mp4"):
            result = video.gen_xiaole_video({
                "_job_id": 8, "channel": "minimax", "prompt": "人物走进电梯",
                "model": "MiniMax-H3", "duration": 15, "ratio": "9:16",
                "resolution": "2k", "reference_images": ["data:image/png;base64,cG5n"],
                "_minimax_origin": video_minimax_h3.ORIGIN_METASO,
            })
        generate.assert_called_once()
        self.assertEqual("2K", generate.call_args.kwargs["resolution"])
        self.assertEqual(
            video_minimax_h3.METASO_API_BASE,
            generate.call_args.kwargs["api_base"],
        )
        self.assertEqual(video_minimax_h3.RESULT_HOSTS, download.call_args.kwargs["allowed_hosts"])
        self.assertEqual(video_minimax_h3.RESULT_MAX_BYTES, download.call_args.kwargs["max_bytes"])
        self.assertEqual(result["provider_video_id"], "h3-task-1")
        self.assertEqual(result["provider"], "minimax_h3_cn")

    def test_shared_video_download_exhaustion_is_not_wrapped_as_transient(self):
        rendered = {
            "request_id": "h3-task-1", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        exhausted = video.CompletedVideoDownloadError("bounded download exhausted")
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
                patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm-key", "secret": "secret"}), \
                patch.object(video.provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video_minimax_h3, "generate", return_value=rendered), \
                patch.object(video, "_download_video_file_direct", side_effect=exhausted):
            with self.assertRaises(video.CompletedVideoDownloadError) as caught:
                video.gen_xiaole_video({
                    "_job_id": 9000999, "channel": "minimax", "prompt": "actor opens door",
                    "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
                    "resolution": "2k", "reference_images": [],
                    "_minimax_origin": video_minimax_h3.ORIGIN_METASO,
                })
        self.assertIs(caught.exception, exhausted)

    def test_restricted_minimax_retries_temporary_dns_without_provider_resubmit(self):
        body = b"\x00\x00\x00\x18ftypisom" + b"valid-video-payload"
        rendered = {
            "request_id": "h3-paid-dns",
            "source_video_url": "https://filecdn.minimax.chat/h3.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        existing = {
            "request_id": "h3-paid-dns", "provider_key_id": "mm-key",
            "provider": "minimax", "resolution": "2k", "ratio": "9:16",
            "phase": "minimax_downloading",
        }
        payload = {
            "_job_id": 23, "channel": "minimax", "prompt": "paid dns retry",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "reference_images": [],
            "_minimax_origin": video_minimax_h3.ORIGIN_METASO,
        }
        response_headers = {
            "Content-Length": str(len(body)), "Content-Type": "video/mp4",
            "ETag": '"dns-retry-version"',
        }
        requests = []

        class Opener:
            def open(self, request, timeout=None):
                self_outer.assertEqual(300, timeout)
                requests.append(request)
                return _DownloadResponse(body, response_headers)

        self_outer = self
        generate = Mock(side_effect=AssertionError("paid task must not be submitted again"))
        resume = Mock(return_value=rendered)
        public_dns = [(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
            ("8.8.8.8", 443),
        )]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "get_resumable_grok_request", return_value=existing), \
                    patch.object(video, "_bound_provider_key", return_value={"id": "mm-key", "secret": "secret"}), \
                    patch.object(video, "update_video_asset_phase"), \
                    patch.object(video_minimax_h3, "generate", generate), \
                    patch.object(video_minimax_h3, "resume", resume), \
                    patch.object(socket, "getaddrinfo", side_effect=[
                        socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"),
                        public_dns,
                    ]) as resolve, \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(video, "_validate_downloaded_video_file"), \
                    patch.object(video, "_extract_first_frame_cover", return_value=None), \
                    patch.object(video, "public_url", return_value="https://cos.example/h3.mp4"), \
                    patch.object(video.time, "sleep") as sleep:
                result = video.gen_xiaole_video(dict(payload))

            self.assertTrue((output_root / result["video_file"]).is_file())
        self.assertEqual(2, resolve.call_count)
        self.assertEqual(1, len(requests))
        generate.assert_not_called()
        resume.assert_called_once()
        self.assertEqual(3, sleep.call_args_list[0].args[0])

    def test_restricted_minimax_fake_mp4_is_terminal_without_provider_resubmit(self):
        body = b"not-an-mp4"
        rendered = {
            "request_id": "h3-paid-invalid", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        existing = {
            "request_id": "h3-paid-invalid", "provider_key_id": "mm-key",
            "provider": "minimax", "resolution": "2k", "ratio": "9:16",
            "phase": "minimax_downloading",
        }
        payload = {
            "_job_id": 19, "channel": "minimax", "prompt": "paid invalid result",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "reference_images": [],
            "_minimax_origin": video_minimax_h3.ORIGIN_METASO,
        }
        requests = []
        response_headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "video/mp4",
            "ETag": '"invalid-version"',
        }

        class Opener:
            def open(self, request, timeout=None):
                self_outer.assertEqual(300, timeout)
                requests.append(request)
                return _DownloadResponse(body, response_headers)

        self_outer = self
        generate = Mock(side_effect=AssertionError("paid task must not be submitted again"))
        resume = Mock(return_value=rendered)
        requeue = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "get_resumable_grok_request", return_value=existing), \
                    patch.object(video, "_bound_provider_key", return_value={"id": "mm-key", "secret": "secret"}), \
                    patch.object(video, "update_video_asset_phase"), \
                    patch.object(video_minimax_h3, "generate", generate), \
                    patch.object(video_minimax_h3, "resume", resume), \
                    patch.object(video, "_validate_restricted_download_url"), \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(video, "recover_official_video_paid_job") as hold, \
                    patch.object(video.time, "sleep"):
                with self.assertRaises(video.CompletedVideoDownloadError) as raised:
                    video.gen_xiaole_video(dict(payload))
                held = video.recover_paid_video_error(
                    19, "xiaole_video", payload, raised.exception, requeue=requeue,
                )

            self.assertEqual([], list((output_root / "video").iterdir()))
        self.assertFalse(held)
        self.assertNotIsInstance(raised.exception, video._CompletedVideoLocalIOError)
        self.assertEqual(1, len(requests))
        generate.assert_not_called()
        resume.assert_called_once()
        requeue.assert_not_called()
        hold.assert_not_called()

        from content_domains import core

        class Connection:
            def execute(self, *_args):
                return self

            def fetchone(self):
                return {
                    "id": 19, "kind": "xiaole_video", "username": "u",
                    "cost": 30, "payload": json.dumps(payload), "status": "pending",
                }

            def close(self):
                pass

        handler = Mock(side_effect=raised.exception)
        claim_running = Mock(side_effect=[True, False])
        set_terminal = Mock(return_value=True)
        refund = Mock(return_value=True)
        failed_asset = Mock()
        with patch.object(core, "jdb", return_value=Connection()), \
                patch.object(core.jobs_store, "claim_running", claim_running), \
                patch.object(core, "_start_job_heartbeat", return_value=Mock()), \
                patch.object(core, "HANDLERS", {"xiaole_video": handler}), \
                patch.object(core, "_domains", return_value=(None, None, video)), \
                patch.object(core, "_set_terminal", set_terminal), \
                patch.object(core, "_refund_once", refund), \
                patch.object(core, "_mark_video_asset_failed", failed_asset):
            core.run_job(19)
            core.run_job(19)

        handler.assert_called_once()
        set_terminal.assert_called_once()
        refund.assert_called_once_with(19, "u", 30)
        failed_asset.assert_called_once()

    def test_short_drama_worker_recovery_does_not_reopen_local_references(self):
        from content_domains import core

        payload = {
            "channel": "minimax",
            "prompt": "resume paid task",
            "model": "MiniMax-H3",
            "duration": 5,
            "ratio": "9:16",
            "resolution": "2k",
            "reference_images": [],
            "_short_drama_provider_binding": {
                "project_id": "project-1",
                "plan_id": "plan-1",
                "shot_key": "shot-1",
                "request_hash": "request-hash-1",
            },
        }

        class Connection:
            def execute(self, *_args):
                return self

            def fetchone(self):
                return {
                    "id": 41,
                    "kind": "xiaole_video",
                    "username": "alice",
                    "cost": 30,
                    "payload": json.dumps(payload),
                    "status": "pending",
                }

            def close(self):
                pass

        resolver = Mock(side_effect=AssertionError(
            "a resumable paid task must not reopen private reference files"
        ))
        reconcile = Mock()
        short_domain = Mock()
        short_domain.short_drama_autodraft.resolve_shared_xiaole_payload = resolver
        short_domain.short_drama_autodraft.reconcile_shared_xiaole_job = reconcile
        video_domain = Mock()
        video_domain.get_resumable_grok_request.return_value = {
            "request_id": "minimax-task-41",
            "provider": "minimax",
            "phase": "minimax_running",
        }
        handler = Mock(return_value={
            "video_file": "video/recovered.mp4",
            "video_url": "/api/gen/file/video/recovered.mp4",
        })

        with patch.object(core, "jdb", return_value=Connection()), \
                patch.object(core.jobs_store, "claim_running", return_value=True), \
                patch.object(core, "_start_job_heartbeat", return_value=Mock()), \
                patch.object(core, "HANDLERS", {"xiaole_video": handler}), \
                patch.object(core, "_domains", return_value=(None, None, video_domain)), \
                patch.object(core, "_short_drama_domain", return_value=short_domain), \
                patch.object(core, "_set_terminal", return_value=True), \
                patch.object(core.assets_store, "record_asset"):
            core.run_job(41)

        resolver.assert_not_called()
        handler.assert_called_once()
        reconcile.assert_called_once_with(ANY, 41)

    def test_restricted_minimax_ffprobe_content_failures_are_terminal(self):
        body = b"\x00\x00\x00\x18ftypisominvalid-media"
        response_headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "video/mp4",
            "ETag": '"invalid-media-version"',
        }

        class Opener:
            def open(self, _request, timeout=None):
                self_outer.assertEqual(300, timeout)
                return _DownloadResponse(body, response_headers)

        self_outer = self
        for label, probe_stdout in (
            ("no-video-stream", '{"streams": []}'),
            ("invalid-probe-json", "not-json"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                output_root = Path(temp_dir)
                (output_root / "video").mkdir()
                requeue = Mock(return_value=True)
                probe = Mock(returncode=0, stdout=probe_stdout, stderr="")
                with patch.object(video, "_validate_restricted_download_url"), \
                        patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                        patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                        patch.object(video.subprocess, "run", return_value=probe), \
                        patch.object(video, "recover_official_video_paid_job") as hold, \
                        patch.object(Path, "replace", autospec=True) as publish, \
                        patch.object(video.time, "sleep"):
                    with self.assertRaises(video.CompletedVideoDownloadError) as raised:
                        video._download_video_file_direct(
                            "https://cdn.example/minimax.mp4", "minimax_h3",
                            allowed_hosts={"cdn.example"}, max_bytes=1024,
                        )
                    held = video.recover_paid_video_error(
                        20, "xiaole_video", {"channel": "minimax"},
                        raised.exception, requeue=requeue,
                    )

                self.assertFalse(held)
                self.assertNotIsInstance(
                    raised.exception, video._CompletedVideoLocalIOError
                )
                requeue.assert_not_called()
                hold.assert_not_called()
                publish.assert_not_called()
                self.assertEqual([], list((output_root / "video").iterdir()))

    def test_restricted_minimax_invalid_http_metadata_is_terminal(self):
        body = b"{}"

        class Opener:
            def __init__(self, headers):
                self.headers = headers

            def open(self, _request, timeout=None):
                self_outer.assertEqual(300, timeout)
                return _DownloadResponse(body, self.headers)

        self_outer = self
        for label, headers in (
            ("non-video-content", {
                "Content-Length": str(len(body)), "Content-Type": "application/json",
            }),
            ("invalid-content-length", {
                "Content-Length": "not-a-number", "Content-Type": "video/mp4",
            }),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                output_root = Path(temp_dir)
                (output_root / "video").mkdir()
                requeue = Mock(return_value=True)
                with patch.object(video, "_validate_restricted_download_url"), \
                        patch.object(video, "_restricted_download_opener", return_value=Opener(headers)), \
                        patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                        patch.object(video, "recover_official_video_paid_job") as hold, \
                        patch.object(Path, "replace", autospec=True) as publish, \
                        patch.object(video.time, "sleep"):
                    with self.assertRaises(video.CompletedVideoDownloadError) as raised:
                        video._download_video_file_direct(
                            "https://cdn.example/minimax.mp4", "minimax_h3",
                            allowed_hosts={"cdn.example"}, max_bytes=1024,
                        )
                    held = video.recover_paid_video_error(
                        21, "xiaole_video", {"channel": "minimax"},
                        raised.exception, requeue=requeue,
                    )

                self.assertFalse(held)
                self.assertNotIsInstance(
                    raised.exception, video._CompletedVideoLocalIOError
                )
                requeue.assert_not_called()
                hold.assert_not_called()
                publish.assert_not_called()
                self.assertEqual([], list((output_root / "video").iterdir()))

    def test_restricted_minimax_disallowed_result_url_is_terminal(self):
        requeue = Mock(return_value=True)
        with patch.object(
                video, "_validate_restricted_download_url",
                side_effect=ValueError("下载地址不在允许的 HTTPS CDN 范围内"),
        ), patch.object(video, "_restricted_download_opener") as opener, \
                patch.object(video, "_out_path") as out_path, \
                patch.object(video, "recover_official_video_paid_job") as hold:
            with self.assertRaises(video.CompletedVideoDownloadError) as raised:
                video._download_video_file_direct(
                    "https://untrusted.example/minimax.mp4", "minimax_h3",
                    allowed_hosts={"cdn.example"}, max_bytes=1024,
                )
            held = video.recover_paid_video_error(
                22, "xiaole_video", {"channel": "minimax"},
                raised.exception, requeue=requeue,
            )

        self.assertFalse(held)
        self.assertNotIsInstance(raised.exception, video._CompletedVideoLocalIOError)
        opener.assert_not_called()
        out_path.assert_not_called()
        requeue.assert_not_called()
        hold.assert_not_called()

    def test_restricted_minimax_cleanup_failure_stays_local_io_terminal(self):
        body = b"not-an-mp4"
        response_headers = {
            "Content-Length": str(len(body)), "Content-Type": "video/mp4",
            "ETag": '"cleanup-failure-version"',
        }

        class Opener:
            def open(self, _request, timeout=None):
                self_outer.assertEqual(300, timeout)
                return _DownloadResponse(body, response_headers)

        self_outer = self
        real_unlink = Path.unlink

        def fail_partial_cleanup(path, *args, **kwargs):
            if ".part-" in path.name:
                raise OSError("restricted partial cleanup failed")
            return real_unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "_validate_restricted_download_url"), \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(Path, "unlink", autospec=True, side_effect=fail_partial_cleanup), \
                    patch.object(Path, "replace", autospec=True) as publish, \
                    patch.object(video.time, "sleep"):
                with self.assertRaises(video._CompletedVideoLocalIOError):
                    video._download_video_file_direct(
                        "https://cdn.example/minimax.mp4", "minimax_h3",
                        allowed_hosts={"cdn.example"}, max_bytes=1024,
                    )

            publish.assert_not_called()
            self.assertEqual([], list((output_root / "video").glob("*.mp4")))

    def test_restricted_minimax_success_does_not_cleanup_after_publish(self):
        body = b"\x00\x00\x00\x18ftypisom" + b"valid-video-payload"
        response_headers = {
            "Content-Length": str(len(body)), "Content-Type": "video/mp4",
            "ETag": '"published-version"',
        }

        class Opener:
            def open(self, _request, timeout=None):
                self_outer.assertEqual(300, timeout)
                return _DownloadResponse(body, response_headers)

        self_outer = self
        real_unlink = Path.unlink

        def fail_redundant_partial_cleanup(path, *args, **kwargs):
            if ".part-" in path.name:
                raise OSError("published partial must not be cleaned again")
            return real_unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "_validate_restricted_download_url"), \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(video, "_validate_downloaded_video_file"), \
                    patch.object(Path, "unlink", autospec=True,
                                 side_effect=fail_redundant_partial_cleanup), \
                    patch.object(video.time, "sleep"):
                relative = video._download_video_file_direct(
                    "https://cdn.example/minimax.mp4", "minimax_h3",
                    allowed_hosts={"cdn.example"}, max_bytes=1024,
                )

            target = output_root / relative
            self.assertTrue(target.is_file())
            self.assertEqual(body, target.read_bytes())
            self.assertEqual([], list((output_root / "video").glob("*.part-*")))

    def test_shared_new_job_submission_uses_persisted_metaso_origin(self):
        rendered = {
            "request_id": "h3-task-new", "source_video_url": "https://cdn.example/new.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "16:9",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        with patch.object(video, "get_resumable_grok_request", return_value=None), \
                patch.object(video.provider_keys, "claim_candidate", return_value={"id": "mm-key", "secret": "secret"}), \
                patch.object(video.provider_keys, "set_health"), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video_minimax_h3, "generate", return_value=rendered) as generate, \
                patch.object(video, "_download_video_file_direct", return_value="video/new.mp4"), \
                patch.object(video, "_extract_first_frame_cover", return_value=None), \
                patch.object(video, "public_url", return_value="https://cos.example/new.mp4"):
            video.gen_xiaole_video({
                "_job_id": 9, "channel": "minimax", "prompt": "a ship leaves the port",
                "model": "MiniMax-H3", "duration": 5, "ratio": "16:9",
                "resolution": "2k", "reference_images": [],
                "_minimax_origin": video_minimax_h3.ORIGIN_METASO,
            })
        self.assertEqual(video_minimax_h3.API_BASE, generate.call_args.kwargs["api_base"])

    def test_download_network_retry_reuses_provider_task_without_new_post(self):
        rendered = {
            "request_id": "h3-paid-task", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        existing = {
            "request_id": "h3-paid-task", "provider_key_id": "mm-key",
            "provider": "minimax", "resolution": "2k", "ratio": "9:16",
            "phase": "minimax_downloading",
        }
        payload = {
            "_job_id": 18, "channel": "minimax", "prompt": "paid result",
            "model": "MiniMax-H3", "duration": 5, "ratio": "9:16",
            "resolution": "2k", "reference_images": [],
            "_minimax_origin": video_minimax_h3.ORIGIN_METASO,
        }
        requeue = []
        with patch.object(video, "get_resumable_grok_request", return_value=existing), \
                patch.object(video, "_bound_provider_key", return_value={"id": "mm-key", "secret": "secret"}), \
                patch.object(video, "update_video_asset_phase"), \
                patch.object(video_minimax_h3, "generate") as generate, \
                patch.object(video_minimax_h3, "resume", return_value=rendered) as resume, \
                patch.object(video, "_download_video_file_direct", side_effect=[
                    video.HeyGenNetworkError("cdn timeout"), "video/h3-paid.mp4",
                ]) as download, \
                patch.object(video, "_extract_first_frame_cover", return_value=None), \
                patch.object(video, "public_url", return_value="https://cos.example/h3-paid.mp4"):
            with self.assertRaises(video_minimax_h3.TransientMiniMaxError) as raised:
                video.gen_xiaole_video(dict(payload))
            held = video.recover_paid_video_error(
                18, "xiaole_video", payload, raised.exception,
                requeue=lambda job_id: requeue.append(job_id) or True,
            )
            result = video.gen_xiaole_video(dict(payload))
        self.assertTrue(held)
        self.assertEqual([18], requeue)
        self.assertEqual(2, resume.call_count)
        generate.assert_not_called()
        self.assertEqual(2, download.call_count)
        self.assertEqual("h3-paid-task", result["provider_video_id"])

    def test_restricted_minimax_download_resumes_without_new_provider_submission(self):
        payload = b"\x00\x00\x00\x18ftypisomminimax-resume"
        split = 9
        requests = []

        class Response:
            def __init__(self, body, status, headers, fail_after=None):
                self.body = body
                self.status = status
                self.headers = headers
                self.fail_after = fail_after
                self.offset = 0

            def __enter__(self): return self
            def __exit__(self, *_args): return False

            def read(self, size=-1):
                if self.fail_after is not None and self.offset >= self.fail_after:
                    raise ssl.SSLError("early eof")
                end = len(self.body) if size < 0 else min(len(self.body), self.offset + size)
                if self.fail_after is not None:
                    end = min(end, self.fail_after)
                chunk = self.body[self.offset:end]
                self.offset = end
                return chunk

        class Opener:
            def open(self, request, timeout=None):
                requests.append(request)
                if len(requests) == 1:
                    self_outer.assertIsNone(request.get_header("Range"))
                    return Response(payload, 200, {
                        "Content-Length": str(len(payload)),
                        "Content-Type": "video/mp4",
                        "ETag": '"version-1"',
                    }, fail_after=split)
                self_outer.assertEqual(request.get_header("Range"), "bytes=%d-" % split)
                return Response(payload[split:], 206, {
                    "Content-Length": str(len(payload) - split),
                    "Content-Range": "bytes %d-%d/%d" % (
                        split, len(payload) - 1, len(payload),
                    ),
                    "Content-Type": "video/mp4",
                    "ETag": '"version-1"',
                })

        self_outer = self
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "_validate_restricted_download_url"), \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(video, "_validate_downloaded_video_file"), \
                    patch.object(video, "_faststart_video_file", side_effect=lambda rel: rel), \
                    patch.object(video.time, "sleep"):
                result = video._download_video_file_direct(
                    "https://cdn.example/minimax.mp4", "minimax_h3",
                    allowed_hosts={"cdn.example"}, max_bytes=1024,
                )
            self.assertEqual(payload, (output_root / result).read_bytes())
        self.assertEqual(2, len(requests))

    def test_restricted_minimax_download_wraps_local_validation_failures(self):
        payload = b"\x00\x00\x00\x18ftypisomminimax-local-close"
        requests = []

        class Response:
            status = 200
            headers = {
                "Content-Length": str(len(payload)),
                "Content-Type": "video/mp4",
                "ETag": '"version-1"',
            }

            def __init__(self): self.offset = 0
            def __enter__(self): return self
            def __exit__(self, *_args): return False

            def read(self, size=-1):
                end = len(payload) if size < 0 else min(len(payload), self.offset + size)
                chunk = payload[self.offset:end]
                self.offset = end
                return chunk

        class Opener:
            def open(self, _request, timeout=None):
                self_outer.assertEqual(300, timeout)
                requests.append(True)
                return Response()

        class ReadCloseFailingFile:
            def __init__(self, raw): self.raw = raw
            def __enter__(self): return self

            def __exit__(self, *_args):
                self.raw.close()
                raise OSError("validation close failed")

            def read(self, size=-1): return self.raw.read(size)

        self_outer = self
        real_open = Path.open

        def open_with_validation_close_failure(path, *args, **kwargs):
            raw = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if mode == "rb":
                return ReadCloseFailingFile(raw)
            return raw

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "_validate_restricted_download_url"), \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(Path, "open", autospec=True,
                                 side_effect=open_with_validation_close_failure), \
                    patch.object(video, "_validate_downloaded_video_file") as validate, \
                    patch.object(Path, "replace", autospec=True) as publish, \
                    patch.object(video.time, "sleep"):
                with self.assertRaises(video._CompletedVideoLocalIOError):
                    video._download_video_file_direct(
                        "https://cdn.example/minimax.mp4", "minimax_h3",
                        allowed_hosts={"cdn.example"}, max_bytes=1024,
                    )
            self.assertEqual([True], requests)
            validate.assert_not_called()
            publish.assert_not_called()
            self.assertEqual(list((output_root / "video").iterdir()), [])

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            (output_root / "video").mkdir()
            with patch.object(video, "_validate_restricted_download_url"), \
                    patch.object(video, "_restricted_download_opener", return_value=Opener()), \
                    patch.object(video, "_out_path", side_effect=lambda rel: output_root / rel), \
                    patch.object(video.subprocess, "run", side_effect=
                                 video.subprocess.TimeoutExpired("ffprobe", 60)), \
                    patch.object(Path, "replace", autospec=True) as publish, \
                    patch.object(video.time, "sleep"):
                with self.assertRaises(video._CompletedVideoLocalIOError):
                    video._download_video_file_direct(
                        "https://cdn.example/minimax.mp4", "minimax_h3",
                        allowed_hosts={"cdn.example"}, max_bytes=1024,
                    )
            self.assertEqual([True, True], requests)
            publish.assert_not_called()
            self.assertEqual(list((output_root / "video").iterdir()), [])

    def test_shared_resume_routes_legacy_and_new_tasks_to_their_origin(self):
        rendered = {
            "request_id": "h3-task-1", "source_video_url": "https://cdn.example/h3.mp4",
            "model": "MiniMax-H3", "duration": 5, "ratio": "16:9",
            "resolution": "2k", "provider": "minimax_h3_cn",
        }
        existing = {
            "request_id": "h3-task-1", "provider_key_id": "mm-key",
            "provider": "minimax", "resolution": "768p", "ratio": "16:9",
        }
        for marker, expected in (
            (None, video_minimax_h3.LEGACY_API_BASE),
            (video_minimax_h3.ORIGIN_METASO, video_minimax_h3.METASO_API_BASE),
        ):
            payload = {
                "_job_id": 8, "channel": "minimax", "prompt": "舰队跃迁离去",
                "model": "MiniMax-H3", "duration": 5, "ratio": "16:9",
                "resolution": "2k", "reference_images": [],
            }
            if marker:
                payload["_minimax_origin"] = marker
            with self.subTest(marker=marker), \
                    patch.object(video, "get_resumable_grok_request", return_value=existing), \
                    patch.object(video, "_bound_provider_key", return_value={"id": "mm-key", "secret": "secret"}), \
                    patch.object(video, "_persist_minimax_origin") as persist_origin, \
                    patch.object(video, "update_video_asset_phase"), \
                    patch.object(video_minimax_h3, "resume", return_value=rendered) as resume, \
                    patch.object(video, "_download_video_file_direct", return_value="video/h3.mp4"), \
                    patch.object(video, "_extract_first_frame_cover", return_value=None), \
                    patch.object(video, "public_url", return_value="https://cos.example/h3.mp4"):
                video.gen_xiaole_video(payload)
            self.assertEqual(expected, resume.call_args.kwargs["api_base"])
            if marker is None:
                persist_origin.assert_called_once_with(
                    8, video_minimax_h3.ORIGIN_LEGACY,
                )
            else:
                persist_origin.assert_not_called()

    def test_stream_download_retries_incomplete_read_without_new_submission(self):
        class Response:
            headers = {"Content-Length": "4"}

            def __init__(self, data=None, error=None):
                self.data = data
                self.error = error

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                if self.error is not None:
                    error, self.error = self.error, None
                    raise error
                data, self.data = self.data, b""
                return data

        responses = [
            Response(error=video.http.client.IncompleteRead(b"ab", 2)),
            Response(data=b"done"),
        ]
        with tempfile.TemporaryDirectory() as folder, patch.object(
            video.time, "sleep"
        ):
            destination = Path(folder) / "result.mp4"
            size = video._stream_download_retry(
                lambda: responses.pop(0), destination, "provider result", 16,
            )
            self.assertEqual((4, b"done"), (size, destination.read_bytes()))
        self.assertEqual([], responses)

    def test_stream_download_retries_short_content_length_body(self):
        class Response:
            headers = {"Content-Length": "4"}

            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                data, self.data = self.data, b""
                return data

        responses = [Response(b"bad"), Response(b"good")]
        with tempfile.TemporaryDirectory() as folder, patch.object(
            video.time, "sleep"
        ):
            destination = Path(folder) / "result.mp4"
            size = video._stream_download_retry(
                lambda: responses.pop(0), destination, "provider result", 16,
            )
            self.assertEqual((4, b"good"), (size, destination.read_bytes()))
        self.assertEqual([], responses)

    def test_ui_has_separate_people_story_entry(self):
        html = (ROOT / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        self.assertIn('data-function="minimax"', html)
        self.assertIn("麦克视频", html)
        self.assertNotIn("MiniMax H3", html)
        self.assertIn("不是动作模仿", html)
        self.assertIn("setupXiaoleRefPanel('minimax', minimaxRefData, 5)", html)
        self.assertIn("p['video.minimax_h3.768p']||6", html)
        self.assertIn("xlPayload.resolution='2k'", html)
        self.assertNotIn("请至少上传 1 张人物参考图", html)
        self.assertNotIn("必传 1–5 张", html)
        self.assertIn("可选，最多 5 张", html)
        self.assertIn("if(retry.key&&retry.body!==body)", html)
        self.assertIn("minimaxLegacyRetryCompatible(retry.body,body)", html)
        self.assertIn("var sentPayload=xlPayload", html)
        self.assertIn("prompt=String(sentPayload.prompt||prompt)", html)
        self.assertIn("xlPayload.model='MiniMax-H3'", html)
        self.assertNotIn("xlPayload.model='MiniMax-Hailuo-2.3'", html)


if __name__ == "__main__":
    unittest.main()
