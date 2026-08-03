import unittest
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from server.content_domains import short_drama_assembly_render as render
from server.content_domains import cos
from server.content_domains import startup_recovery


class ShortDramaAssemblyRenderTests(unittest.TestCase):
    def test_corrupt_reusable_audio_is_staled_then_rebuilt_in_same_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cached = root / "cached.wav"
            cached.write_bytes(b"cached")
            context = {
                "project": {"id": "project-1", "ratio": "9:16"},
                "snapshot": {
                    "input_hash": "d1",
                    "audio_subtitle": {"input_hash": "d2"},
                    "master_audio": {"master_audio_hash": "m" * 64},
                    "config": {},
                    "media_plan": {},
                },
                "shot_inputs": {},
                "payload": {"_job_id": 7},
            }
            result = {"artifacts": [], "manifest": {}}
            with (
                mock.patch.object(render, "_jdb", return_value=object()),
                mock.patch.object(render, "_output_root", return_value=root),
                mock.patch.object(
                    render.artifacts,
                    "claim_build",
                    return_value={
                        "status": "claimed", "claim_token": "1" * 32
                    },
                ),
                mock.patch.object(
                    render.artifacts,
                    "reusable_audio_files",
                    return_value={
                        "source_input_hash": "d2-old",
                        "files": {
                            ("master_audio", ""): {
                                "file": "cached.wav",
                                "file_hash": "a" * 64,
                            }
                        },
                    },
                ),
                mock.patch.object(
                    render.d2_engine,
                    "build_bundle",
                    side_effect=[
                        render.d2_engine.ReusableAudioCacheError(),
                        result,
                    ],
                ) as build,
                mock.patch.object(
                    render.artifacts, "mark_reusable_audio_stale"
                ) as mark_stale,
                mock.patch.object(render.artifacts, "record_ready") as record,
                mock.patch.object(
                    render.artifacts,
                    "ready_files",
                    return_value={("master_audio", ""): "new.wav"},
                ),
            ):
                ready = render._ensure_d2(context, {"ffmpeg": "x"})
            self.assertEqual({("master_audio", ""): "new.wav"}, ready)
            self.assertEqual(2, build.call_count)
            self.assertTrue(build.call_args_list[0].kwargs["cached_audio_files"])
            self.assertEqual(
                {}, build.call_args_list[1].kwargs["cached_audio_files"]
            )
            mark_stale.assert_called_once_with(
                mock.ANY,
                "project-1",
                "d2-old",
                "audio_cache_hash_mismatch",
            )
            record.assert_called_once()

    def test_vertical_preview_command_has_locked_720p_contract(self):
        command = render.build_preview_command(
            [
                {"file": Path("shot-a.mp4"), "duration_ms": 5000},
                {"file": Path("shot-b.mp4"), "duration_ms": 5000},
            ],
            Path("master.wav"), Path("subtitles.ass"), "9:16",
            Path("preview.mp4"),
        )
        joined = " ".join(str(item) for item in command)
        self.assertIn("scale=720:1280", joined)
        self.assertIn("crop=720:1280", joined)
        self.assertIn("concat=n=2:v=1:a=0", joined)
        self.assertIn("subtitles=filename=", joined)
        self.assertIn("fontsdir=", joined)
        self.assertIn("-c:v libx264", joined)
        self.assertIn("-pix_fmt yuv420p", joined)
        self.assertIn("-r 30", joined)
        self.assertIn("-c:a aac", joined)
        self.assertIn("-ar 48000", joined)
        self.assertIn("-ac 2", joined)
        self.assertIn("-movflags +faststart", joined)

    def test_horizontal_preview_uses_1280_by_720_without_shell(self):
        command = render.build_preview_command(
            [{"file": "shot.mp4", "duration_ms": 30000}],
            "master.wav", "subtitles.ass", "16:9", "preview.mp4",
        )
        self.assertIsInstance(command, list)
        joined = " ".join(str(item) for item in command)
        self.assertIn("scale=1280:720", joined)
        self.assertIn("-t 30.000", joined)

    def test_external_vtt_preview_does_not_burn_ass_subtitles(self):
        command = render.build_preview_command(
            [{"file": "shot.mp4", "duration_ms": 5000}],
            "master.wav", "subtitles.ass", "9:16", "preview.mp4",
            burn_subtitles=False,
        )
        joined = " ".join(str(item) for item in command)
        self.assertNotIn("subtitles=filename=", joined)
        self.assertIn("[joined]null[vout]", joined)

    def test_final_command_rebuilds_1080p_high_profile_from_sources(self):
        command = render.build_final_command(
            [{"file": "shot.mp4", "duration_ms": 5000}],
            "master.wav", "subtitles.ass", "9:16", "final.part.mp4",
        )
        joined = " ".join(str(item) for item in command)
        self.assertIn("scale=1080:1920", joined)
        self.assertIn("crop=1080:1920", joined)
        self.assertIn("-profile:v high", joined)
        self.assertIn("-crf 20", joined)
        self.assertIn("-b:a 192k", joined)

    def test_toolchain_requires_strict_noto_font_preflight(self):
        font = {
            "family": "Noto Sans CJK SC",
            "file": "/fonts/NotoSansCJK-Regular.ttc",
            "font_dir": "/fonts",
        }
        with (
            mock.patch.object(
                render, "_run",
                return_value=mock.Mock(stdout="ffmpeg version test\n"),
            ),
            mock.patch.object(
                render.media_plan, "inspect_ffprobe",
                return_value="ffprobe version test",
            ),
            mock.patch.object(
                render.subtitles, "inspect_font", return_value=font
            ) as inspect_font,
        ):
            tools = render._toolchain()
        inspect_font.assert_called_once_with()
        self.assertEqual("/fonts/NotoSansCJK-Regular.ttc", tools["font"])
        self.assertEqual("/fonts", tools["font_dir"])

    def test_final_upload_reuses_verified_content_addressed_object(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "final.mp4"
            source.write_bytes(b"verified-final")
            digest = render._file_sha256(source)
            head = {
                "Content-Length": str(source.stat().st_size),
                "x-cos-meta-sha256": digest,
            }
            with (
                mock.patch.object(cos, "enabled", return_value=True),
                mock.patch.object(cos, "head", return_value=head),
                mock.patch.object(
                    cos, "object_url", return_value="signed-url"
                ) as object_url,
                mock.patch.object(cos, "upload") as upload,
            ):
                self.assertEqual(
                    "signed-url",
                    render._verified_private_upload(
                        source, "final/key.mp4", "video/mp4", digest
                    ),
                )
            upload.assert_not_called()
            object_url.assert_called_once_with(
                "final/key.mp4", private=True
            )

    def test_final_upload_uses_cos_metadata_header_and_retries_head_only(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "final.mp4"
            source.write_bytes(b"new-final")
            digest = render._file_sha256(source)
            verified = {
                "Content-Length": str(source.stat().st_size),
                "X-COS-META-SHA256": digest,
                "x-cos-request-id": "request-1",
            }
            with (
                mock.patch.object(cos, "enabled", return_value=True),
                mock.patch.object(
                    cos, "head",
                    side_effect=[
                        FileNotFoundError(),
                        {"Content-Length": str(source.stat().st_size)},
                        verified,
                    ],
                ),
                mock.patch.object(
                    cos, "upload", return_value="signed-url"
                ) as upload,
                mock.patch.object(render.time, "sleep") as sleep,
            ):
                self.assertEqual(
                    "signed-url",
                    render._verified_private_upload(
                        source, "final/key.mp4", "video/mp4", digest
                    ),
                )
            upload.assert_called_once_with(
                source,
                "final/key.mp4",
                "video/mp4",
                private=True,
                metadata={"x-cos-meta-sha256": digest},
            )
            sleep.assert_called_once_with(0.5)

    def test_final_upload_reports_missing_remote_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "final.mp4"
            source.write_bytes(b"missing-hash")
            digest = render._file_sha256(source)
            head = {
                "Content-Length": str(source.stat().st_size),
                "x-cos-request-id": "request-2",
            }
            with (
                mock.patch.object(cos, "enabled", return_value=True),
                mock.patch.object(
                    cos, "head",
                    side_effect=[FileNotFoundError(), head, head, head, head],
                ),
                mock.patch.object(cos, "upload", return_value="signed-url"),
                mock.patch.object(render.time, "sleep"),
            ):
                with self.assertRaises(render.PreviewRenderError) as raised:
                    render._verified_private_upload(
                        source, "final/key.mp4", "video/mp4", digest
                    )
            self.assertEqual("upload_hash_missing", raised.exception.code)
            self.assertIn("SHA256 元数据缺失", str(raised.exception))
            self.assertIn("request-2", str(raised.exception))

    def test_final_upload_reports_remote_size_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "final.mp4"
            source.write_bytes(b"wrong-size")
            digest = render._file_sha256(source)
            head = {
                "Content-Length": str(source.stat().st_size + 1),
                "x-cos-meta-sha256": digest,
            }
            with (
                mock.patch.object(cos, "enabled", return_value=True),
                mock.patch.object(
                    cos, "head",
                    side_effect=[FileNotFoundError(), head, head, head, head],
                ),
                mock.patch.object(cos, "upload", return_value="signed-url"),
                mock.patch.object(render.time, "sleep"),
            ):
                with self.assertRaises(render.PreviewRenderError) as raised:
                    render._verified_private_upload(
                        source, "final/key.mp4", "video/mp4", digest
                    )
            self.assertEqual("upload_size_mismatch", raised.exception.code)
            self.assertIn("文件大小不一致", str(raised.exception))

    def test_startup_requeues_local_preview_without_refund(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"

            def db():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(db()) as connection:
                connection.execute(
                    "CREATE TABLE jobs(id INTEGER PRIMARY KEY,username TEXT,"
                    "cost INTEGER,kind TEXT,status TEXT,owner TEXT,"
                    "updated_at INTEGER,error TEXT)"
                )
                connection.execute(
                    "INSERT INTO jobs VALUES "
                    "(1,'alice',0,'short_drama_preview','running','content',1,NULL)"
                )
                connection.commit()
            terminal = []
            refunds = []
            handled = startup_recovery.reclaim_orphaned_running(
                jdb=db, service_owner="content", domains=lambda: (),
                set_terminal=lambda *args, **kwargs: terminal.append(args),
                refund_once=lambda *args: refunds.append(args),
                mark_video_asset_failed=lambda *args: None,
                requeue_job=lambda job_id: startup_recovery.requeue_running_job(
                    db, job_id
                ),
                logger=lambda *args, **kwargs: None,
            )
            self.assertEqual(1, handled)
            self.assertEqual([], terminal)
            self.assertEqual([], refunds)
            with closing(db()) as connection:
                self.assertEqual(
                    "pending",
                    connection.execute(
                        "SELECT status FROM jobs WHERE id=1"
                    ).fetchone()["status"],
                )


if __name__ == "__main__":
    unittest.main()
