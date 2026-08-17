import gc
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import audio, core
from content_domains import pixelle_video


class TextVideoPersonalAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "audio.db")
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE audio_voices(
                    id INTEGER PRIMARY KEY,
                    scope TEXT NOT NULL,
                    username TEXT NOT NULL,
                    voice_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    provider_voice TEXT NOT NULL,
                    preview_file TEXT,
                    preview_url TEXT,
                    slot_id TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                );
                CREATE TABLE audio_voice_slots(
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    voice_id INTEGER,
                    created_at INTEGER,
                    updated_at INTEGER
                );
                INSERT INTO audio_voices VALUES(
                    1,'personal','alice','vip_alice','Alice voice',
                    'cosyvoice-v3.5-plus-bailian-alice',NULL,NULL,'slot_alice',1,1
                );
                INSERT INTO audio_voice_slots VALUES(
                    1,'alice','slot_alice','ready',1,1,1
                );
                INSERT INTO audio_voices VALUES(
                    2,'personal','bob','vip_bob','Bob voice',
                    'cosyvoice-v3.5-plus-bailian-bob',NULL,NULL,'slot_bob',1,1
                );
                INSERT INTO audio_voice_slots VALUES(
                    2,'bob','slot_bob','ready',2,1,1
                );
                INSERT INTO audio_voices VALUES(
                    3,'personal','alice','vip_training','Training voice',
                    'S_training',NULL,NULL,'slot_training',1,1
                );
                INSERT INTO audio_voice_slots VALUES(
                    3,'alice','slot_training','training',3,1,1
                );
                INSERT INTO audio_voices VALUES(
                    4,'public','','S_public','Public voice',
                    'longwan',NULL,NULL,NULL,1,1
                );
            """)
        self.db_patch = patch.object(core, "AUDIO_DB", self.db)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        gc.collect()
        self.tmp.cleanup()

    def test_synthesizes_owned_ready_personal_voice_without_persistence(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
             patch.object(audio.cosyvoice, "synth", return_value=b"mp3-bytes") as synth, \
             patch.object(audio, "_out_path", side_effect=AssertionError("must not write")), \
             patch.object(audio, "record_audio_asset", side_effect=AssertionError("must not persist")), \
             patch.object(audio, "public_url", side_effect=AssertionError("must not publish")):
            result = audio.synthesize_owned_voice_segment(
                "alice", "vip_alice", "第一段", speed=1.1, pitch=2, volume=4
            )

        self.assertEqual(result, {
            "content": b"mp3-bytes",
            "content_type": "audio/mpeg",
            "voice_key": "vip_alice",
            "voice_scope": "personal",
            "provider": "cosyvoice",
        })
        synth.assert_called_once_with(
            "cosyvoice-v3.5-plus-bailian-alice",
            "第一段",
            rate=1.1,
            pitch=1.0 + 2 / 24.0,
            volume=52,
        )

    def test_rejects_cross_user_voice_before_synthesis(self):
        with patch.object(audio.cosyvoice, "synth") as synth:
            with self.assertRaisesRegex(ValueError, "个人音色不存在或不可用"):
                audio.synthesize_owned_voice_segment("alice", "vip_bob", "测试")
        synth.assert_not_called()

    def test_rejects_training_voice_before_synthesis(self):
        with patch.object(audio.cosyvoice, "synth") as synth:
            with self.assertRaisesRegex(ValueError, "个人音色不存在或不可用"):
                audio.synthesize_owned_voice_segment("alice", "vip_training", "测试")
        synth.assert_not_called()

    def test_rejects_public_voice_before_synthesis(self):
        with patch.object(audio.cosyvoice, "synth") as synth:
            with self.assertRaisesRegex(ValueError, "个人音色不存在或不可用"):
                audio.synthesize_owned_voice_segment("alice", "S_public", "测试")
        synth.assert_not_called()

    def test_rejects_invalid_text_and_controls_before_synthesis(self):
        invalid = [
            {"text": ""},
            {"text": "字" * 1001},
            {"text": "测试", "speed": 0.4},
            {"text": "测试", "pitch": 13},
            {"text": "测试", "volume": 101},
        ]
        with patch.object(audio.cosyvoice, "synth") as synth:
            for case in invalid:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    audio.synthesize_owned_voice_segment(
                        "alice", "vip_alice", **case
                    )
        synth.assert_not_called()

    def test_disabled_cosyvoice_never_falls_back(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=False), \
             patch.object(audio.cosyvoice, "synth") as synth, \
             patch.object(audio, "_post_bytes", side_effect=AssertionError("no fallback")):
            with self.assertRaisesRegex(ValueError, "声音服务暂时不可用"):
                audio.synthesize_owned_voice_segment("alice", "vip_alice", "测试")
        synth.assert_not_called()

    def test_rejects_empty_audio_response(self):
        with patch.object(audio.cosyvoice, "enabled", return_value=True), \
             patch.object(audio.cosyvoice, "synth", return_value=b""):
            with self.assertRaisesRegex(RuntimeError, "返回为空"):
                audio.synthesize_owned_voice_segment("alice", "vip_alice", "测试")

    def test_talking_personal_voice_uses_confirmed_text_without_extra_tts(self):
        payload = {
            "text": "确认第一段\n\n确认第二段",
            "mode": "fixed",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial",
            "n_scenes": 2,
            "scenes": [
                {"line": "确认第一段", "scene_id": "scene_01"},
                {"line": "确认第二段", "scene_id": "scene_02"},
            ],
            "speech_rate": 1.0,
            "voice_scope": "personal",
            "voice_key": "vip_alice",
            "_username": "alice",
            "_job_id": 73,
            "talking_material": {
                "enabled": True, "ratio": 0.5,
                "default_avatar_asset_id": "local_avatar_" + "a" * 32,
                "scenes": [{"scene_id": "scene_01", "enabled": True}],
            },
        }
        audio_ids = ["audio_" + "1" * 32, "audio_" + "2" * 32]
        with patch.object(
            audio, "synthesize_owned_voice_segment",
            return_value={"content": b"mp3", "content_type": "audio/mpeg"},
        ) as synth, patch.object(
            pixelle_video, "_binary_request",
            side_effect=[{"asset_id": item} for item in audio_ids],
        ), patch.object(
            pixelle_video, "_remote_talking_material",
            return_value={
                "enabled": True, "ratio": 0.5,
                "default_avatar_asset_id": "avatar_" + "a" * 32,
                "scenes": [{"scene_id": "scene_01", "enabled": True,
                            "avatar_asset_id": ""}],
            },
        ), patch.object(
            pixelle_video, "_json_request",
            return_value={"task_id": "task-confirmed-personal"},
        ) as request:
            self.assertEqual(pixelle_video._submit(payload), "task-confirmed-personal")

        self.assertEqual(synth.call_count, 2)
        self.assertEqual([call.args[2] for call in synth.call_args_list],
                         ["确认第一段", "确认第二段"])
        video_body = request.call_args.args[2]
        self.assertEqual(video_body["text"], "确认第一段\n\n确认第二段")
        self.assertEqual([item["text"] for item in video_body["narration_segments"]],
                         ["确认第一段", "确认第二段"])
        self.assertEqual(video_body["talking_material"]["scenes"][0]["scene_id"],
                         "scene_01")

    def test_long_personal_scene_synthesizes_once_but_keeps_caption_cues(self):
        long_text = "所以轩和堂做这件事，并不是为了追风口，是为了让门店效果可验证。"
        payload = {
            "text": long_text,
            "mode": "fixed",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial",
            "n_scenes": 1,
            "scenes": [{"line": long_text, "scene_id": "scene_01"}],
            "speech_rate": 1.0,
            "voice_scope": "personal",
            "voice_key": "vip_alice",
            "_username": "alice",
            "_job_id": 74,
        }
        asset_id = "audio_" + "3" * 32
        with patch.object(
            audio, "synthesize_owned_voice_segment",
            return_value={"content": b"mp3", "content_type": "audio/mpeg"},
        ) as synth, patch.object(
            pixelle_video, "_binary_request",
            return_value={"asset_id": asset_id},
        ) as upload:
            segments = pixelle_video._personal_narration_segments(payload)

        synth.assert_called_once()
        self.assertEqual(long_text, synth.call_args.args[2])
        upload.assert_called_once()
        self.assertEqual(asset_id, segments[0]["audio_asset_id"])
        self.assertGreater(len(segments[0]["caption_cues"]), 1)
        self.assertEqual(
            long_text,
            "".join(cue["text"] for cue in segments[0]["caption_cues"]),
        )

    def test_nine_scene_personal_adapter_uploads_two_avatars_without_extra_tts(self):
        avatar_a = "local_avatar_" + "a" * 32
        avatar_b = "local_avatar_" + "b" * 32
        remote_a = "avatar_" + "1" * 32
        remote_b = "avatar_" + "2" * 32
        scene_texts = [
            "第01段确认文案", "第02段确认文案", "第03段确认文案",
            "第04段确认文案", "第05段确认文案", "第06段确认文案",
            "第07段确认文案", "第08段确认文案", "第09段确认文案",
        ]
        plan = {
            "plan_id": "talking_plan_" + "e" * 32,
            "source_hash": "f" * 64,
            "status": "active",
            "job_id": None,
            "source": {
                "text": "九分镜个人音色方案", "mode": "generate", "ratio": 1 / 3,
                "template": "1080x1920/image_default.html",
                "style": "realistic_commercial", "speech_rate": 1.0,
                "source_page": "text-video", "voice_scope": "personal",
                "voice_key": "vip_alice",
            },
            "scenes": [
                {"scene_id": "scene_%02d" % index, "text": text}
                for index, text in enumerate(scene_texts, 1)
            ],
        }
        request = {
            "text": "九分镜个人音色方案", "mode": "generate",
            "template": "1080x1920/image_default.html",
            "style": "realistic_commercial", "speech_rate": 1.0,
            "voice": "personal:vip_alice",
            "talking_material": {
                "enabled": True, "plan_id": plan["plan_id"],
                "source_hash": plan["source_hash"], "ratio": 1 / 3,
                "default_avatar_asset_id": avatar_a,
                "scenes": [
                    {
                        "scene_id": "scene_%02d" % index,
                        "enabled": index in {1, 5, 9},
                        **({"avatar_asset_id": avatar_b} if index == 5 else {}),
                    }
                    for index in range(1, 10)
                ],
            },
        }
        avatars = {
            avatar_a: {"asset_id": avatar_a, "mime": "image/png",
                       "sha256": "a" * 64, "data": b"avatar-a"},
            avatar_b: {"asset_id": avatar_b, "mime": "image/jpeg",
                       "sha256": "b" * 64, "data": b"avatar-b"},
        }
        audio_assets = [
            {"asset_id": "audio_" + format(index, "032x")}
            for index in range(1, 10)
        ]
        with patch.object(
            pixelle_video.pixelle_talking_assets, "get_plan", return_value=plan,
        ), patch.object(
            pixelle_video.pixelle_talking_assets, "read_avatar",
            side_effect=lambda _owner, asset_id: avatars[asset_id],
        ), patch.object(
            pixelle_video, "_load_remote_avatar_map", return_value={},
        ), patch.object(
            pixelle_video, "_persist_remote_avatar_map",
        ), patch.object(
            pixelle_video, "_upload_avatar_asset", side_effect=[remote_a, remote_b],
        ) as upload, patch.object(
            audio, "synthesize_owned_voice_segment",
            return_value={"content": b"mp3", "content_type": "audio/mpeg"},
        ) as synth, patch.object(
            pixelle_video, "_binary_request", side_effect=audio_assets,
        ), patch.object(
            pixelle_video, "_json_request", return_value={"task_id": "task-personal-9"},
        ) as submit:
            prepared = pixelle_video.prepare_payload(request, "alice")
            prepared["_username"] = "alice"
            prepared["_job_id"] = 80
            self.assertEqual(pixelle_video._submit(prepared), "task-personal-9")

        video_body = submit.call_args.args[2]
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(synth.call_count - len(scene_texts), 0)
        self.assertEqual(
            [call.args[2] for call in synth.call_args_list], scene_texts)
        self.assertEqual(
            [item["text"] for item in video_body["narration_segments"]],
            scene_texts,
        )
        self.assertEqual(video_body["text"], "\n\n".join(scene_texts))
        self.assertEqual(video_body["mode"], "fixed")
        self.assertEqual(video_body["n_scenes"], 9)
        self.assertEqual(
            video_body["talking_material"]["default_avatar_asset_id"], remote_a)
        self.assertEqual(len(video_body["talking_material"]["scenes"]), 3)
        self.assertEqual(
            [item["scene_id"] for item in video_body["talking_material"]["scenes"]],
            ["scene_01", "scene_05", "scene_09"],
        )
        self.assertEqual(
            video_body["talking_material"]["scenes"][1]["avatar_asset_id"],
            remote_b,
        )


if __name__ == "__main__":
    unittest.main()
