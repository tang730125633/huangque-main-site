import base64
import hashlib
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


PNG_2X2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMU0bBhYGBgYgADAAWiAHylyrQdAAAAAElFTkSuQmCC"
)


class DigitalHumanV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.domain = importlib.import_module("content_domains.digital_human_v2")
        cls.legacy = importlib.import_module("content_domains.digital_human_oneclick")
        cls.points = importlib.import_module("content_domains.points")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs_db = self.root / "jobs.db"
        self.consent_db = self.root / "consents.db"
        connection = sqlite3.connect(self.jobs_db)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, username TEXT, kind TEXT, status TEXT,
            payload TEXT, result TEXT, deleted INTEGER DEFAULT 0,
            created_at INTEGER, updated_at INTEGER
        )""")
        connection.commit()
        connection.close()
        self.video = types.ModuleType("content_domains.video")
        self.video.subtitle_runtime_preflight = mock.Mock(return_value={"ok": True})
        self.patches = [
            mock.patch.object(self.domain, "OUT_DIR", self.root),
            mock.patch.object(self.domain, "jdb", self._jobs_connection),
            mock.patch.object(self.legacy, "OUT_DIR", self.root),
            mock.patch.object(self.legacy, "CONSENT_DB", self.consent_db),
            mock.patch.object(self.legacy, "cdb", self._consent_connection),
            mock.patch.dict(sys.modules, {"content_domains.video": self.video}),
            mock.patch.object(sys.modules["content_domains"], "video", self.video, create=True),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def _jobs_connection(self):
        connection = sqlite3.connect(self.jobs_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent_connection(self):
        connection = sqlite3.connect(self.consent_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _store_audio(self, raw, run_id="dh-v2-run-audio-reupload-001", now=1000):
        transcript = [{
            "start": 0.0, "end": 12.0,
            "text": "这是用于验证过期录音重新上传的完整口播。",
        }]

        def create_slice(command, **_kwargs):
            Path(command[-1]).write_bytes(b"verified-audio-slice")

        with mock.patch.object(self.domain.time, "time", return_value=now), \
                mock.patch.object(self.domain, "_probe_audio_duration", return_value=12.0), \
                mock.patch.object(self.domain, "_transcribe_audio", return_value=transcript), \
                mock.patch.object(self.legacy, "_run", side_effect=create_slice):
            return self.domain.audio_upload_response(
                io.BytesIO(raw), len(raw), "yuelei", run_id, "audio/mpeg",
                hashlib.sha256(raw).hexdigest(),
            )

    def _consent(self, script, portrait=PNG_2X2, allow_ai=None, upload_ids=None,
                 run_id="dh-v2-run-test-001"):
        plan = self.domain._bind_material_policy(
            self.domain.timeline.plan_text(script), False, [], True,
        )
        plan = self.domain._bind_material_policy(
            plan, allow_ai is True, list(upload_ids or []), True,
        )
        payload = {
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": run_id,
            "plan_digest": plan["plan_digest"],
            "script": script,
            "photo_sha256": hashlib.sha256(portrait).hexdigest(),
            "voice_mode": "existing",
            "voice_ref": "voice-owned-1",
            "voice_sha256": "",
            "narration_mode": "text",
        }
        payload["allow_ai_materials"] = plan["allow_ai_materials"]
        payload["customer_upload_ids"] = plan["customer_upload_ids"]
        with mock.patch.object(self.domain, "_validate_customer_uploads"):
            consent = self.domain.create_consent(
                payload, "yuelei", "test-signing-secret", db_factory=self._consent_connection,
            )
        return plan, consent

    def _metadata(self, plan, consent, stage, index):
        return {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": stage,
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"],
            "digital_human_item_index": index,
            **({
                "digital_human_allow_ai_materials": plan["allow_ai_materials"],
                "digital_human_customer_upload_ids": plan["customer_upload_ids"],
            } if "allow_ai_materials" in plan else {}),
        }

    def test_consent_is_bound_to_duration_driven_plan(self):
        plan, consent = self._consent("普通人使用人工智能时，先把目标讲清楚，再选择合适工具。" * 8)
        self.assertEqual(consent["purpose"], self.domain.CONSENT_PURPOSE)
        self.assertGreater(plan["segment_count"], 1)
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.create_consent({
                "confirmed": True,
                "consent_version": self.domain.CONSENT_VERSION,
                "purpose": self.domain.CONSENT_PURPOSE,
                "run_id": "dh-v2-run-test-002",
                "plan_digest": "0" * 64,
                "script": plan["copy"],
                "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
                "voice_mode": "existing", "voice_ref": "voice-owned-1",
                "voice_sha256": "", "narration_mode": "text",
            }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        self.assertEqual(caught.exception.code, "consent_plan_mismatch")

    def test_legacy_v2_consent_cannot_enter_v3_resolve_or_compose(self):
        old_record = {
            "id": "dhc_" + "1" * 32, "username": "yuelei",
            "run_id": "dh-v2-legacy-consent-001",
            "consent_version": "digital-human-material-v2",
            "purpose": "digital_human_material_v2",
            "plan_digest": "a" * 64,
        }
        with mock.patch.object(
                self.legacy, "_load_consent", return_value=old_record):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.resolve_material_response({
                    "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
                    "digital_human_stage": "material_resolve",
                    "digital_human_run_id": old_record["run_id"],
                    "digital_human_plan_digest": old_record["plan_digest"],
                    "digital_human_consent_token": "legacy-token",
                    "digital_human_script": "旧授权不能静默切换素材来源。" * 8,
                    "digital_human_item_index": 0,
                }, "yuelei")
        self.assertEqual("consent_binding_mismatch", caught.exception.code)

        current_plan = self.domain._bind_material_policy(
            self.domain.timeline.plan_text("新素材来源必须重新分析并确认授权。" * 8),
            False, [], True,
        )
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.prepare_compose_payload({
                "pipeline": self.domain.PIPELINE, "mode": self.domain.PIPELINE,
                "script": current_plan["copy"],
                "plan_digest": current_plan["plan_digest"],
                "video_job_ids": [], "material_job_ids": [],
                "material_asset_ids": [],
            }, "yuelei", old_record)
        self.assertEqual("consent_required", caught.exception.code)

    def test_v3_plan_and_consent_lock_local_library_priority(self):
        self.assertEqual("digital-human-material-v3", self.domain.CONSENT_VERSION)
        self.assertEqual("digital_human_material_v3", self.domain.CONSENT_PURPOSE)
        plan, consent = self._consent(
            "新版本必须把固定本地素材库绑定进方案摘要和授权记录。" * 8,
            allow_ai=False, upload_ids=[], run_id="dh-v3-local-consent-001",
        )
        self.assertEqual(
            ["customer_upload", "local_library", "ai_optional"],
            plan["source_priority"],
        )
        self.assertEqual(self.domain.CONSENT_VERSION, consent["consent_version"])
        self.assertEqual(self.domain.CONSENT_PURPOSE, consent["purpose"])

    def test_v2_voice_clone_routes_through_legacy_entrypoint_and_keeps_bindings(self):
        sample = b"authorized-v2-voice-sample"
        script = "这是用于验证新版数字人声音复刻授权绑定的完整口播文案。"
        plan = self.domain._bind_material_policy(
            self.domain.timeline.plan_text(script), False, [], True,
        )
        consent = self.domain.create_consent({
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-v2-run-clone-001",
            "plan_digest": plan["plan_digest"],
            "script": script,
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "clone",
            "voice_ref": "slot-v2-owned-1",
            "voice_sha256": hashlib.sha256(sample).hexdigest(),
            "narration_mode": "text",
            "allow_ai_materials": False,
            "customer_upload_ids": [],
        }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        body = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "voice_clone",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"],
            "digital_human_narration_mode": "text",
            "digital_human_allow_ai_materials": False,
            "digital_human_customer_upload_ids": [],
            "slot_id": "slot-v2-owned-1",
            "audio": base64.b64encode(sample).decode("ascii"),
        }

        cleaned = self.legacy.verify_clone_submission(body, "yuelei")

        self.assertEqual(cleaned["digital_human_consent_id"], consent["consent_id"])
        self.assertNotIn("digital_human_consent_token", cleaned)
        for changed, expected_code in (
            ({"slot_id": "slot-other"}, "consent_voice_mismatch"),
            ({"audio": base64.b64encode(b"other").decode("ascii")}, "consent_voice_mismatch"),
            ({"digital_human_script": script + "篡改"}, "consent_plan_mismatch"),
        ):
            with self.subTest(changed=changed), self.assertRaises(
                    self.domain.DigitalHumanRequestError) as caught:
                self.legacy.verify_clone_submission(dict(body, **changed), "yuelei")
            self.assertEqual(caught.exception.code, expected_code)


    def test_clone_vip_requires_matching_v2_idempotency_before_provider_work(self):
        core = importlib.import_module("content_domains.core")
        base = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
        }

        class Handler:
            path = "/api/gen/audio/clone-vip"
            def __init__(self, body, headers):
                self.body, self.headers, self.sent = body, headers, None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(self.body)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        audio = types.SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            validate_clone_vip_payload=mock.Mock(), mark_clone_training=mock.Mock(),
            clone_vip_voice_background=mock.Mock(),
        )
        verifier = mock.Mock()
        cases = (
            (dict(base), {}, "必须提供 Idempotency-Key"),
            (dict(base, clone_attempt_id="dh-v2-other"),
             {"Idempotency-Key": "dh-v2-header"}, "必须与 Idempotency-Key 一致"),
        )
        for body, headers, detail in cases:
            with self.subTest(detail=detail), \
                 mock.patch("content_domains.core._domains", return_value=(audio, types.SimpleNamespace(), types.SimpleNamespace())), \
                 mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
                 mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
                 mock.patch("content_domains.core._must_change_password", return_value=False), \
                 mock.patch("content_domains.core.feature_flags.require_enabled"), \
                 mock.patch.object(self.legacy, "verify_clone_submission", verifier), \
                 mock.patch("content_domains.core.threading.Thread") as thread:
                handler = Handler(body, headers); core.H.do_POST(handler)
                self.assertEqual(400, handler.sent[0])
                self.assertIn(detail, handler.sent[1]["detail"])
                thread.assert_not_called()
        verifier.assert_not_called()
        audio.validate_clone_vip_payload.assert_not_called()
        audio.mark_clone_training.assert_not_called()

    def test_clone_vip_replays_v2_idempotency_without_restarting_provider(self):
        core = importlib.import_module("content_domains.core")
        db_path = self.root / "clone-v2-idempotency.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        key = "dh-v2-voice-clone-stable-001"
        request = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
            "clone_attempt_id": key,
        }

        class Handler:
            path = "/api/gen/audio/clone-vip"
            headers = {"Idempotency-Key": key}
            def __init__(self): self.sent = None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(request)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        validated = dict(request, digital_human_consent_id="dhc_" + "4" * 32)
        audio = types.SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            validate_clone_vip_payload=mock.Mock(return_value=validated),
            mark_clone_training=mock.Mock(return_value={"status": "training", "voice_key": "vip_slot_123"}),
            mark_clone_attempt_running=mock.Mock(return_value=True),
            clone_vip_voice_background=mock.Mock(),
        )
        started = []
        class Thread:
            def __init__(self, target, args, daemon): self.target, self.args = target, args
            def start(self): started.append(self.args)

        with mock.patch("content_domains.core._domains", return_value=(audio, types.SimpleNamespace(), types.SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(self.legacy, "verify_clone_submission", return_value=validated), \
             mock.patch.object(core, "jdb", connection), \
             mock.patch("content_domains.core.threading.Thread", Thread):
            first = Handler(); core.H.do_POST(first)
            replay = Handler(); core.H.do_POST(replay)

        self.assertEqual(200, first.sent[0])
        self.assertEqual(first.sent, replay.sent)
        self.assertEqual(1, audio.mark_clone_training.call_count)
        self.assertEqual(1, len(started))

    def test_clone_vip_v2_provider_training_recovery_never_restarts_provider(self):
        core = importlib.import_module("content_domains.core")
        db_path = self.root / "clone-v2-provider-training.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        key = "dh-v2-voice-clone-provider-training-001"
        request = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
            "clone_attempt_id": key,
        }
        verified = dict(request, digital_human_consent_id="dhc_" + "5" * 32)
        core.submission_idempotency.begin(
            connection, "yuelei", "/api/gen/audio/clone-vip", key, verified,
        )

        class Handler:
            path = "/api/gen/audio/clone-vip"
            headers = {"Idempotency-Key": key}
            def __init__(self): self.sent = None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(request)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        audio = types.SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            clone_attempt_snapshot=mock.Mock(return_value={
                "action": "provider_training", "attempt_id": key, "age": 3600,
            }),
            check_clone_status=mock.Mock(return_value={
                "status": "training", "attempt_id": key,
            }),
            validate_clone_vip_payload=mock.Mock(), mark_clone_training=mock.Mock(),
            mark_clone_attempt_running=mock.Mock(), clone_vip_voice_background=mock.Mock(),
        )
        with mock.patch("content_domains.core._domains", return_value=(audio, types.SimpleNamespace(), types.SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(self.legacy, "verify_clone_submission", return_value=verified), \
             mock.patch.object(core, "jdb", connection), \
             mock.patch("content_domains.core.threading.Thread") as thread:
            handler = Handler(); core.H.do_POST(handler)

        self.assertEqual((409, "idempotency_in_progress"),
                         (handler.sent[0], handler.sent[1]["code"]))
        audio.check_clone_status.assert_called_once_with("yuelei", "slot_123", key)
        audio.mark_clone_training.assert_not_called()
        thread.assert_not_called()
    def test_removed_gesture_stage_is_rejected_before_paid_work(self):
        plan, consent = self._consent("这是一段用于验证旧手势步骤已经删除的完整口播文案。")
        payload = self._metadata(plan, consent, "gesture", 0)
        payload["reference_images"] = [base64.b64encode(PNG_2X2).decode("ascii")]
        with self.assertRaises(self.domain.DigitalHumanRequestError):
            self.domain.verify_child_submission_with_record(payload, "yuelei", "image")

    def test_material_submission_forces_seedream_standard_route(self):
        script = "普通人学习人工智能时，应先明确问题，再选择与内容匹配的工具。" * 8
        plan, consent = self._consent(script, allow_ai=True)
        self.assertGreater(plan["material_count"], 0)
        reference = base64.b64encode(PNG_2X2).decode("ascii")
        material = self._metadata(plan, consent, "material", 0)
        material.update({
            "provider": "banana", "model": "nb2", "variant": "pro",
            "quality": "hd", "count": 2, "ratio": "1:1",
            "prompt": "forged prompt", "images": ["forged"],
            "reference_images": [reference],
        })

        cleaned, _record = self.domain.verify_child_submission_with_record(
            material, "yuelei", "image",
        )

        self.assertEqual("seedream", cleaned["provider"])
        self.assertEqual("std", cleaned["variant"])
        self.assertEqual("std", cleaned["quality"])
        self.assertEqual(1, cleaned["count"])
        self.assertEqual("9:16", cleaned["ratio"])
        self.assertEqual([reference], cleaned["reference_images"])
        self.assertNotIn("images", cleaned)
        self.assertNotIn("model", cleaned)
        self.assertEqual(
            self.points.pricing.get_price("image.seedream.std.std"),
            self.points.cost_of("image", cleaned),
        )

    def test_talking_submission_uses_authorized_portrait_and_preserves_one_voice(self):
        script = "普通人学习人工智能，不用先背很多术语，从一个真实问题开始就可以。" * 8
        plan, consent = self._consent(script)
        segment = plan["segments"][1]
        talking = self._metadata(plan, consent, "talking", 1)
        talking.update({
            "voice": "voice-owned-1", "text": "forged",
            "reference_images": [
                "data:image/png;base64," + base64.b64encode(PNG_2X2).decode("ascii")
            ],
        })
        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )
        self.assertEqual(cleaned["text"], segment["text"])
        self.assertEqual(cleaned["voice"], "voice-owned-1")
        self.assertTrue(cleaned["image_data"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("reference_images", cleaned)

        swapped = dict(talking, reference_images=[base64.b64encode(b"other").decode("ascii")])
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_child_submission_with_record(swapped, "yuelei", "video")
        self.assertEqual(caught.exception.code, "consent_photo_mismatch")

    def test_webp_portrait_is_canonicalized_before_heygen_submission(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (32, 48), (25, 60, 90)).save(buffer, format="WEBP")
        portrait = buffer.getvalue()
        plan, consent = self._consent(
            "这是一段用于验证 WebP 人物照片可以直接驱动数字人口播的完整文案。",
            portrait=portrait,
        )
        talking = self._metadata(plan, consent, "talking", 0)
        talking.update({
            "voice": "voice-owned-1",
            "reference_images": [
                "data:image/webp;base64," + base64.b64encode(portrait).decode("ascii")
            ],
        })

        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )

        self.assertTrue(cleaned["image_data"].startswith("data:image/jpeg;base64,"))
        canonical = base64.b64decode(cleaned["image_data"].split(",", 1)[1])
        self.assertTrue(canonical.startswith(b"\xff\xd8\xff"))

    def test_full_audio_plan_and_talking_use_exact_owned_slice(self):
        raw_audio = b"real-complete-audio-for-binding"
        claimed = hashlib.sha256(raw_audio).hexdigest()

        def create_slice(command, **_kwargs):
            Path(command[-1]).write_bytes(("slice:" + command[4]).encode("utf-8"))

        transcript = [
            {"start": 0.0, "end": 24.0, "text": "这是录音驱动的第一段完整口播。"},
            {"start": 24.0, "end": 42.0, "text": "这是录音驱动的第二段完整口播。"},
        ]
        with mock.patch.object(self.domain, "_probe_audio_duration", return_value=42.0), \
                mock.patch.object(self.domain, "_transcribe_audio", return_value=transcript), \
                mock.patch.object(self.legacy, "_run", side_effect=create_slice):
            uploaded = self.domain.audio_upload_response(
                io.BytesIO(raw_audio), len(raw_audio), "yuelei", "dh-v2-run-audio-001",
                "audio/mpeg", claimed,
            )
        plan = self.domain.plan_response({
            "narration_mode": "audio", "audio_upload_id": uploaded["audio_upload_id"],
        }, "yuelei")["plan"]
        self.assertEqual(plan["narration_mode"], "audio")
        self.assertEqual(plan["segment_count"], 2)
        self.assertEqual(plan["presenter_windows"], [[0.0, 3.0], [24.0, 27.0], [39.0, 42.0]])

        consent = self.domain.create_consent({
            "confirmed": True, "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE, "run_id": "dh-v2-run-audio-001",
            "plan_digest": plan["plan_digest"], "script": "",
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "audio", "voice_ref": "", "voice_sha256": "",
            "narration_mode": "audio", "audio_upload_id": uploaded["audio_upload_id"],
        }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        talking = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "talking", "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"],
            "digital_human_item_index": 0, "digital_human_narration_mode": "audio",
            "digital_human_audio_upload_id": uploaded["audio_upload_id"],
            "reference_images": [base64.b64encode(PNG_2X2).decode("ascii")],
            "mode": "audio", "audio_data": "forged",
        }
        expected_slice = self.domain._load_audio_asset(
            uploaded["audio_upload_id"], "yuelei",
        )["slices"][0]
        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )
        self.assertEqual(cleaned["mode"], "audio")
        self.assertEqual(cleaned["text"], plan["segments"][0]["text"])
        self.assertNotEqual(cleaned["audio_data"], "forged")
        decoded = base64.b64decode(cleaned["audio_data"].split(",", 1)[1])
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), expected_slice["sha256"])

    def test_expired_unbound_audio_reupload_replaces_record_after_full_validation(self):
        raw = b"expired-audio-upload-replaced-after-validation"
        first = self._store_audio(raw, now=1000)
        with closing(self._consent_connection()) as connection:
            old_row = connection.execute(
                "SELECT source_file FROM digital_human_audio_uploads WHERE asset_id=?",
                (first["audio_upload_id"],),
            ).fetchone()
        old_directory = (self.root / old_row["source_file"]).parent
        self.assertTrue(old_directory.is_dir())

        replaced = self._store_audio(
            raw, now=1000 + self.domain._AUDIO_UPLOAD_TTL_SECONDS + 1,
        )

        self.assertNotEqual(first["audio_upload_id"], replaced["audio_upload_id"])
        self.assertGreater(replaced["expires_at"], first["expires_at"])
        self.assertFalse(old_directory.exists())
        with closing(self._consent_connection()) as connection:
            rows = connection.execute(
                "SELECT asset_id,source_sha256 FROM digital_human_audio_uploads"
            ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(replaced["audio_upload_id"], rows[0]["asset_id"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), rows[0]["source_sha256"])

    def test_replacement_commit_hard_exit_recovers_old_directory_and_keeps_winner(self):
        raw = b"replacement-hard-exit-must-leave-durable-cleanup-intent"
        first = self._store_audio(raw, now=1000)
        with closing(self._consent_connection()) as connection:
            old_source = connection.execute(
                "SELECT source_file FROM digital_human_audio_uploads WHERE asset_id=?",
                (first["audio_upload_id"],),
            ).fetchone()["source_file"]
        old_directory = (self.root / old_source).parent
        new_now = 1000 + self.domain._AUDIO_UPLOAD_TTL_SECONDS + 1
        transcript = [{
            "start": 0.0, "end": 12.0,
            "text": "这是用于验证替换提交后硬退出恢复的完整口播。",
        }]

        def create_slice(command, **_kwargs):
            Path(command[-1]).write_bytes(b"replacement-winner-slice")

        class SimulatedHardExit(BaseException):
            pass

        with mock.patch.object(self.domain.time, "time", return_value=new_now), \
                mock.patch.object(self.domain, "_probe_audio_duration", return_value=12.0), \
                mock.patch.object(self.domain, "_transcribe_audio", return_value=transcript), \
                mock.patch.object(self.legacy, "_run", side_effect=create_slice), \
                mock.patch.object(
                    self.domain, "_remove_audio_asset_files",
                    side_effect=SimulatedHardExit("after replacement commit"),
                ), self.assertRaises(SimulatedHardExit):
            self.domain.audio_upload_response(
                io.BytesIO(raw), len(raw), "yuelei",
                "dh-v2-run-audio-reupload-001", "audio/mpeg",
                hashlib.sha256(raw).hexdigest(),
            )

        with closing(self._consent_connection()) as connection:
            winner = connection.execute(
                "SELECT asset_id,source_file FROM digital_human_audio_uploads"
            ).fetchone()
        self.assertNotEqual(first["audio_upload_id"], winner["asset_id"])
        winner_directory = (self.root / winner["source_file"]).parent
        self.assertTrue(old_directory.exists())
        self.assertTrue(winner_directory.exists())

        recovery_now = new_now + self.domain._AUDIO_ADMISSION_LEASE_SECONDS + 1
        removed = self.domain.cleanup_expired_assets(
            self._consent_connection, self._jobs_connection,
            now=recovery_now, limit=20,
        )

        self.assertGreaterEqual(removed, 1)
        self.assertFalse(old_directory.exists())
        self.assertTrue(winner_directory.exists())
        with mock.patch.object(self.domain.time, "time", return_value=recovery_now):
            loaded = self.domain._load_audio_asset(
                winner["asset_id"], "yuelei", db_factory=self._consent_connection,
            )
        self.assertEqual(winner["asset_id"], loaded["asset_id"])
        with closing(self._consent_connection()) as connection:
            states = {
                row["admission_id"]: row["state"] for row in connection.execute(
                    "SELECT admission_id,state FROM digital_human_audio_admissions"
                ).fetchall()
            }
        self.assertIn("committed_reaped", states.values())
        self.assertIn("committed", states.values())

    def test_expired_authorized_audio_requires_explicit_new_run(self):
        raw = b"expired-authorized-audio-cannot-be-rebound"
        first = self._store_audio(raw, now=2000)
        self.legacy.init_db(self._consent_connection)
        with closing(self._consent_connection()) as connection:
            connection.execute(
                """INSERT INTO digital_human_consents(
                    id,username,run_id,consent_version,purpose,plan_digest,
                    photo_sha256,voice_mode,voice_ref,voice_sha256,token_hash,
                    created_at,expires_at,last_used_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "dhc_expired_audio", "yuelei", "dh-v2-run-audio-reupload-001",
                    self.domain.CONSENT_VERSION, self.domain.CONSENT_PURPOSE,
                    "a" * 64, "b" * 64, "audio", first["audio_upload_id"], "",
                    "c" * 64, 2000, 2000 + self.domain.CONSENT_TTL_SECONDS, 2000,
                ),
            )
            connection.commit()
        stream = io.BytesIO(raw)

        with mock.patch.object(
                self.domain.time, "time",
                return_value=2000 + self.domain._AUDIO_UPLOAD_TTL_SECONDS + 1,
        ), self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.store_audio_upload(
                stream, len(raw), "yuelei", "dh-v2-run-audio-reupload-001",
                "audio/mpeg", hashlib.sha256(raw).hexdigest(),
            )

        self.assertEqual("audio_upload_restart_required", caught.exception.code)
        self.assertIn("放弃上次任务并重新设置", str(caught.exception))
        self.assertEqual(0, stream.tell())
        with closing(self._consent_connection()) as connection:
            row = connection.execute(
                "SELECT asset_id FROM digital_human_audio_uploads"
            ).fetchone()
        self.assertEqual(first["audio_upload_id"], row["asset_id"])

    def test_unexpired_audio_upload_keeps_idempotency_and_binding(self):
        first_raw = b"active-audio-upload-remains-idempotent"
        first = self._store_audio(first_raw, now=3000)
        duplicate = self._store_audio(first_raw, now=3001)
        self.assertEqual(first["audio_upload_id"], duplicate["audio_upload_id"])

        other = b"active-run-must-not-silently-change-audio"
        with mock.patch.object(self.domain.time, "time", return_value=3002), \
                self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.store_audio_upload(
                io.BytesIO(other), len(other), "yuelei",
                "dh-v2-run-audio-reupload-001", "audio/mpeg",
                hashlib.sha256(other).hexdigest(),
            )
        self.assertEqual("audio_upload_binding_conflict", caught.exception.code)

    def test_short_audio_boundaries_keep_material_contract(self):
        for duration, expected_count in ((6.0, 0), (6.05, 0), (6.06, 1)):
            asset = {
                "asset_id": "dhau_short_boundary",
                "source_sha256": "b" * 64,
                "duration": duration,
                "transcript": "最短合法录音",
                "slices": [{
                    "start": 0.0, "end": duration, "duration": duration,
                    "text": "最短合法录音", "sha256": "a" * 64,
                }],
            }
            with self.subTest(duration=duration):
                plan = self.domain._audio_plan(asset)
                self.assertEqual(plan["expected_duration"], duration)
                self.assertEqual(plan["material_count"], expected_count)

    def test_material_policy_binds_customer_uploads_and_optional_ai(self):
        upload_id = "img_" + "a" * 32
        script = "顾客上传素材必须全部使用，缺少的镜头先查本地库，最后才按用户选择使用人工智能补图。" * 3
        with mock.patch.object(self.domain, "_validate_customer_uploads") as validate:
            result = self.domain.plan_response({
                "narration_mode": "text", "script": script,
                "allow_ai_materials": False, "customer_upload_ids": [upload_id],
            }, "yuelei")
        plan = result["plan"]
        validate.assert_called_once_with([upload_id], "yuelei")
        self.assertFalse(plan["allow_ai_materials"])
        self.assertEqual(plan["customer_upload_ids"], [upload_id])
        self.assertEqual(plan["source_priority"], [
            "customer_upload", "local_library", "ai_optional",
        ])
        self.assertNotEqual(plan["plan_digest"], self.domain.timeline.plan_text(script)["plan_digest"])

    def test_missing_ai_policy_is_bound_as_denied_and_never_authorizes_paid_image(self):
        script = "缺失付费补图选择时必须默认拒绝，并把拒绝值绑定到方案摘要和后续授权。" * 5
        result = self.domain.plan_response({
            "narration_mode": "text", "script": script,
        }, "yuelei")
        plan = result["plan"]
        self.assertIs(plan["allow_ai_materials"], False)
        self.assertEqual(plan["customer_upload_ids"], [])
        self.assertNotEqual(
            plan["plan_digest"], self.domain.timeline.plan_text(script)["plan_digest"],
        )

        plan, consent = self._consent(
            script, run_id="dh-v2-run-missing-ai-policy-001",
        )
        resolver = self._metadata(plan, consent, "material_resolve", 0)
        with mock.patch.object(self.domain, "_local_library_material", return_value=None):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.resolve_material_response(resolver, "yuelei")
        self.assertEqual(caught.exception.code, "material_unavailable_without_ai")

        paid = self._metadata(plan, consent, "material", 0)
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_child_submission_with_record(paid, "yuelei", "image")
        self.assertEqual(caught.exception.code, "ai_material_not_allowed")

    def test_local_library_default_is_fixed_and_not_request_controlled(self):
        self.assertEqual(
            self.domain._LOCAL_LIBRARY_DEFAULT_ROOT,
            "/home/ubuntu/material-libraries/huangque-media",
        )
        self.assertEqual(
            self.domain._LOCAL_LIBRARY_ENV,
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT",
        )
        self.assertNotIn("material_root", self.domain.resolve_material_response.__code__.co_consts)

    def test_material_resolver_uses_customer_then_local_library_then_optional_ai(self):
        upload_id = "img_" + "b" * 32
        script = "顾客上传素材必须先使用，剩余镜头查本地库，最后才允许人工智能补图。" * 4
        customer_plan, customer_consent = self._consent(
            script, allow_ai=True, upload_ids=[upload_id],
            run_id="dh-v2-run-customer-001",
        )
        customer_payload = self._metadata(customer_plan, customer_consent, "material_resolve", 0)
        stored = {"asset_id": "dhm_" + "1" * 32, "media_type": "image"}
        with mock.patch.object(
                self.domain, "_customer_material",
                return_value=(PNG_2X2, "image/png", "customer_upload")) as customer, \
                mock.patch.object(self.domain, "_local_library_material") as local_library, \
                mock.patch.object(self.domain, "_store_material_asset", return_value=stored):
            result = self.domain.resolve_material_response(customer_payload, "yuelei")
        customer.assert_called_once_with(upload_id, "yuelei")
        local_library.assert_not_called()
        self.assertEqual(result["source"], "customer_upload")

        plan, consent = self._consent(
            script, allow_ai=True, upload_ids=[], run_id="dh-v2-run-local-001",
        )
        payload = self._metadata(plan, consent, "material_resolve", 0)
        with mock.patch.object(
                self.domain, "_local_library_material",
                return_value=(PNG_2X2, "image/png", "local_library")) as local_library, \
                mock.patch.object(self.domain, "_store_material_asset", return_value=stored):
            result = self.domain.resolve_material_response(payload, "yuelei")
        local_library.assert_called_once()
        self.assertEqual(result["source"], "local_library")

        with mock.patch.object(self.domain, "_local_library_material", return_value=None):
            result = self.domain.resolve_material_response(payload, "yuelei")
        self.assertTrue(result["ai_fallback"])
        self.assertEqual(result["source"], "ai")
        self.assertFalse(hasattr(self.domain, "_wikimedia_material"))

    def test_customer_material_failure_never_falls_through_to_local_library_or_ai(self):
        upload_id = "img_" + "d" * 32
        plan, consent = self._consent(
            "顾客已经上传的素材必须直接进入成片，读取失败时也不能悄悄换用其他来源。" * 4,
            allow_ai=True, upload_ids=[upload_id],
            run_id="dh-v2-run-customer-fail-001",
        )
        payload = self._metadata(plan, consent, "material_resolve", 0)
        with mock.patch.object(
                self.domain, "_customer_material",
                return_value=(PNG_2X2, "image/png", "customer_upload")), \
                mock.patch.object(
                    self.domain, "_store_material_asset",
                    side_effect=ValueError("decode failed")), \
                mock.patch.object(self.domain, "_local_library_material") as local_library:
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.resolve_material_response(payload, "yuelei")
        local_library.assert_not_called()
        self.assertEqual(caught.exception.code, "customer_material_unavailable")
        self.assertEqual(caught.exception.status, 409)

    def test_local_library_outage_stops_before_optional_paid_ai(self):
        plan, consent = self._consent(
            "本地素材库必须真正完成检索，接口故障不能被误判成没有匹配素材。" * 5,
            allow_ai=True, upload_ids=[], run_id="dh-v2-run-local-down-001",
        )
        payload = self._metadata(plan, consent, "material_resolve", 0)
        with mock.patch.object(
                self.domain, "_local_library_material", side_effect=OSError("unreadable")):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.resolve_material_response(payload, "yuelei")
        self.assertEqual(caught.exception.code, "local_material_library_unavailable")
        self.assertEqual(caught.exception.status, 503)

    def test_consent_rejects_material_policy_changed_after_plan(self):
        script = "用户是否允许人工智能补图以及顾客上传素材清单都必须绑定同一个方案摘要。" * 5
        planned = self.domain._bind_material_policy(
            self.domain.timeline.plan_text(script), False, [], True,
        )
        payload = {
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-v2-run-policy-change-001",
            "plan_digest": planned["plan_digest"],
            "script": script,
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "existing",
            "voice_ref": "voice-ready",
            "voice_sha256": "",
            "narration_mode": "text",
            "allow_ai_materials": True,
            "customer_upload_ids": [],
        }
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.create_consent(
                payload, "yuelei", "test-signing-secret",
                db_factory=self._consent_connection,
            )
        self.assertEqual(caught.exception.code, "consent_plan_mismatch")

    def test_ai_opt_out_and_customer_binding_block_paid_image_before_charge(self):
        script = "这是用于验证素材优先级和付费生图门禁的完整口播文案。" * 6
        plan, consent = self._consent(
            script, allow_ai=False, upload_ids=[], run_id="dh-v2-run-no-ai-001",
        )
        material = self._metadata(plan, consent, "material", 0)
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_child_submission_with_record(material, "yuelei", "image")
        self.assertEqual(caught.exception.code, "ai_material_not_allowed")

        upload_id = "img_" + "c" * 32
        plan, consent = self._consent(
            script, allow_ai=True, upload_ids=[upload_id],
            run_id="dh-v2-run-customer-paid-001",
        )
        material = self._metadata(plan, consent, "material", 0)
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_child_submission_with_record(material, "yuelei", "image")
        self.assertEqual(caught.exception.code, "customer_material_required")

    def test_ai_opt_out_stops_after_local_library_miss_or_invalid_asset(self):
        plan, consent = self._consent(
            "本地素材找不到时，未授权的人工智能补图不能执行。" * 6,
            allow_ai=False, upload_ids=[], run_id="dh-v2-run-no-ai-resolve-001",
        )
        payload = self._metadata(plan, consent, "material_resolve", 0)
        with mock.patch.object(self.domain, "_local_library_material", return_value=None):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.resolve_material_response(payload, "yuelei")
        self.assertEqual(caught.exception.code, "material_unavailable_without_ai")

        with mock.patch.object(
                self.domain, "_local_library_material",
                return_value=(PNG_2X2, "image/png", "local_library")), \
                mock.patch.object(
                    self.domain, "_store_material_asset",
                    side_effect=ValueError("decode failed")):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.resolve_material_response(payload, "yuelei")
        self.assertEqual(caught.exception.code, "material_unavailable_without_ai")

    def test_history_returns_only_owned_completed_v2_videos_and_recovers_old_url(self):
        fallback = self.root / "videos" / "old video.mp4"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(b"old-video")
        v2 = {"pipeline": self.domain.PIPELINE, "copy": "历史数字人口播文案"}
        rows = [
            (106, "other", "script_to_video", "done", v2,
             {"url": "/api/gen/file/videos/other.mp4"}, 0, 1060),
            (105, "yuelei", "script_to_video", "done", v2,
             {"url": "/api/gen/file/videos/deleted.mp4"}, 1, 1050),
            (104, "yuelei", "script_to_video", "error", v2,
             {"url": "/api/gen/file/videos/error.mp4"}, 0, 1040),
            (103, "yuelei", "script_to_video", "done",
             {"pipeline": "smart_montage", "copy": "其他链路"},
             {"video_url": "/api/gen/file/videos/other-pipeline.mp4"}, 0, 1030),
            (102, "yuelei", "script_to_video", "done", v2,
             {"video_url": "/api/gen/file/videos/latest.mp4", "duration": 39.8,
              "width": 1080, "height": 1920,
              "verification": {"subtitle": "whisper"}}, 0, 1020),
            (101, "yuelei", "script_to_video", "done", v2,
             {"video_file": "videos/old video.mp4", "duration": 31}, 0, 1010),
        ]
        connection = self._jobs_connection()
        try:
            connection.executemany(
                "INSERT INTO jobs(id,username,kind,status,payload,result,deleted,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(job_id, username, kind, status,
                  json.dumps(payload, ensure_ascii=False),
                  json.dumps(result, ensure_ascii=False), deleted, created_at)
                 for job_id, username, kind, status, payload, result, deleted, created_at in rows],
            )
            connection.commit()
        finally:
            connection.close()

        first = self.domain.history_response("yuelei", limit=1, offset=0)
        self.assertEqual([102], [item["job_id"] for item in first["items"]])
        self.assertTrue(first["has_more"])
        self.assertEqual("历史数字人口播文案", first["items"][0]["text"])
        self.assertEqual("/api/gen/file/videos/latest.mp4", first["items"][0]["video_url"])
        self.assertEqual("whisper", first["items"][0]["subtitle"])

        second = self.domain.history_response("yuelei", limit=1, offset=1)
        self.assertEqual([101], [item["job_id"] for item in second["items"]])
        self.assertEqual("/api/gen/file/videos/old%20video.mp4",
                         second["items"][0]["video_url"])
        self.assertFalse(second["has_more"])

    def test_history_http_route_is_authenticated_get_and_passes_pagination(self):
        script_to_video = importlib.import_module("content_domains.script_to_video")

        class Handler:
            path = self.domain.HISTORY_PATH + "?limit=7&offset=2"

            def __init__(self):
                self.sent = None

            @staticmethod
            def _token():
                return "token"

            def _send(self, status, payload):
                self.sent = (status, payload)

            def _method_not_allowed(self):
                self.sent = (405, {})

        handler = Handler()
        expected = {"items": [], "limit": 7, "offset": 2, "has_more": False}
        with mock.patch.object(
                self.domain, "history_response", return_value=expected) as history:
            handled = script_to_video.dispatch_http(
                handler, "GET", lambda _token: {"username": "yuelei"},
                lambda _user: False,
            )
        self.assertTrue(handled)
        self.assertEqual((200, expected), handler.sent)
        history.assert_called_once_with("yuelei", "7", "2")

    def test_local_v2_compose_is_zero_cost(self):
        body = {"pipeline": self.domain.PIPELINE, "material_count": 7}
        self.assertEqual(self.points.cost_of("script_to_video", body), 0)
        self.assertEqual(body["cost_breakdown"]["material_reused_count"], 7)


if __name__ == "__main__":
    unittest.main()
