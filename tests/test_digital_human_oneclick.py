import importlib
import base64
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PNG_2X2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMU0bBhYGBgYgADAAWiAHylyrQdAAAAAElFTkSuQmCC"
)
JPEG_2X2 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDyOiiiuw5D/9k="
)
WEBP_2X2 = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvAUAAAAdQlFKUp/+BiOh/AAA=")


class DigitalHumanOneClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.domain = importlib.import_module("content_domains.digital_human_oneclick")
        cls.points = importlib.import_module("content_domains.points")
        cls.banana_provider = importlib.import_module("content_domains.banana_provider")
        cls.cli_uploads = importlib.import_module("content_domains.cli_uploads")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "jobs.db"
        self.consent_db = self.root / "digital-human-consent.db"
        connection = sqlite3.connect(self.db)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, username TEXT, kind TEXT,
            cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL,
            result TEXT, error TEXT, refunded INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0, owner TEXT,
            deleted INTEGER DEFAULT 0
        )""")
        for job_id in range(1, 4):
            rel = "videos/%d.mp4" % job_id
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            connection.execute(
                "INSERT INTO jobs(id,username,kind,status,payload,result) VALUES(?,?,?,?,?,?)",
                (job_id, "yuelei", "video", "done", "{}", json.dumps({"video_file": rel})),
            )
        for job_id in range(11, 17):
            rel = "images/%d.png" % job_id
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            connection.execute(
                "INSERT INTO jobs(id,username,kind,status,payload,result) VALUES(?,?,?,?,?,?)",
                (job_id, "yuelei", "image", "done", "{}", json.dumps({"file": rel})),
            )
        connection.commit()
        connection.close()
        self.patches = [
            mock.patch.object(self.domain, "OUT_DIR", self.root),
            mock.patch.object(self.domain, "jdb", self._connection),
            mock.patch.object(self.domain, "CONSENT_DB", self.consent_db),
        ]
        for patcher in self.patches:
            patcher.start()

    def _connection(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent_connection(self):
        connection = sqlite3.connect(self.consent_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent_record(self, plan_digest, **overrides):
        record = {
            "id": "dhc_" + "1" * 32,
            "username": "yuelei",
            "run_id": "dh-run-test-001",
            "purpose": self.domain.CONSENT_PURPOSE,
            "plan_digest": plan_digest,
        }
        record.update(overrides)
        return record

    def _bind_child_jobs(self, record, segment_count=3):
        connection = sqlite3.connect(self.db)
        try:
            for job_id in range(1, segment_count + 1):
                payload = {
                    "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
                    "digital_human_stage": "talking",
                    "digital_human_consent_id": record["id"],
                    "digital_human_run_id": record["run_id"],
                    "digital_human_plan_digest": record["plan_digest"],
                    "digital_human_segment_count": segment_count,
                    "digital_human_item_index": job_id - 1,
                }
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=?",
                    (json.dumps(payload), job_id),
                )
            for job_id in range(11, 11 + segment_count * 2):
                payload = {
                    "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
                    "digital_human_stage": "material",
                    "digital_human_consent_id": record["id"],
                    "digital_human_run_id": record["run_id"],
                    "digital_human_plan_digest": record["plan_digest"],
                    "digital_human_segment_count": segment_count,
                    "digital_human_item_index": job_id - 11,
                }
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=?",
                    (json.dumps(payload), job_id),
                )
            connection.commit()
        finally:
            connection.close()

    def _insert_gesture_jobs(self, record, statuses=None, kind="image",
                             username="yuelei", overrides=None):
        statuses = statuses or ["pending", "running", "done"]
        overrides = overrides or {}
        connection = sqlite3.connect(self.db)
        try:
            for offset, status in enumerate(statuses):
                job_id = 21 + offset
                payload = {
                    "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
                    "digital_human_stage": "gesture",
                    "digital_human_consent_id": record["id"],
                    "digital_human_run_id": record["run_id"],
                    "digital_human_plan_digest": record["plan_digest"],
                    "digital_human_item_index": offset,
                }
                payload.update(overrides)
                result = "{}"
                if status == "done":
                    rel = "images/gesture-%d.png" % job_id
                    path = self.root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"gesture")
                    result = json.dumps({"file": rel})
                connection.execute(
                    "INSERT INTO jobs(id,username,kind,status,payload,result) "
                    "VALUES(?,?,?,?,?,?)",
                    (job_id, username, kind, status, json.dumps(payload), result),
                )
            connection.commit()
        finally:
            connection.close()
        return [21, 22, 23]

    def _recovery_body(self, consent, consent_payload, stage, field, ids):
        return {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": stage,
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": consent_payload["script"],
            "digital_human_segment_count": consent_payload.get("segment_count", 3),
            field: self._indexed_jobs(ids),
        }

    @staticmethod
    def _indexed_jobs(ids):
        return [{"index": index, "job_id": job_id}
                for index, job_id in enumerate(ids)]

    def _compose_request(self, script, planned, record):
        segment_count = planned.get("segment_count", 3)
        material_count = planned.get("material_count", segment_count * 2)
        return {
            "pipeline": self.domain.PIPELINE,
            "script": script,
            "plan_digest": planned["plan_digest"],
            "video_job_ids": list(range(1, segment_count + 1)),
            "material_job_ids": list(range(11, 11 + material_count)),
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "compose",
            "digital_human_run_id": record["run_id"],
            "digital_human_plan_digest": record["plan_digest"],
            "digital_human_consent_id": record["id"],
            "digital_human_script": script,
            "digital_human_segment_count": segment_count,
            "digital_human_item_index": 0,
        }

    def _consent_payload(self, **overrides):
        script = overrides.pop("script", "第一段介绍问题。第二段说明方案。第三段邀请行动。")
        segment_count = overrides.pop("segment_count", 3)
        planned = self.domain.plan(script, segment_count)
        payload = {
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-run-test-001",
            "plan_digest": planned["plan_digest"],
            "script": script,
            "segment_count": segment_count,
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "existing",
            "voice_ref": "vip_ready_voice",
            "voice_sha256": "",
        }
        payload.update(overrides)
        return payload

    def test_consent_is_persisted_without_raw_media_and_is_idempotent(self):
        first = self.domain.create_consent(
            self._consent_payload(), "yuelei", "test-signing-secret", now=1000,
            db_factory=self._consent_connection,
        )
        second = self.domain.create_consent(
            self._consent_payload(), "yuelei", "test-signing-secret", now=1010,
            db_factory=self._consent_connection,
        )
        self.assertEqual(first["consent_token"], second["consent_token"])
        connection = self._consent_connection()
        try:
            row = dict(connection.execute("SELECT * FROM digital_human_consents").fetchone())
        finally:
            connection.close()
        self.assertEqual(row["username"], "yuelei")
        self.assertEqual(row["photo_sha256"], hashlib.sha256(PNG_2X2).hexdigest())
        self.assertNotIn("portrait", json.dumps(row))
        self.assertNotIn(first["consent_token"], json.dumps(row))

    def test_consent_rejects_unconfirmed_and_changed_binding(self):
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "确认") as missing:
            self.domain.create_consent(
                self._consent_payload(confirmed=False), "yuelei", "secret",
                db_factory=self._consent_connection,
            )
        self.assertEqual(missing.exception.code, "consent_required")
        self.domain.create_consent(
            self._consent_payload(), "yuelei", "secret",
            db_factory=self._consent_connection,
        )
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "变化") as changed:
            self.domain.create_consent(
                self._consent_payload(photo_sha256="b" * 64), "yuelei", "secret",
                db_factory=self._consent_connection,
            )
        self.assertEqual(changed.exception.code, "consent_binding_conflict")

    def test_consent_service_requires_a_signing_secret_without_creating_db(self):
        with self.assertRaises(self.domain.DigitalHumanRequestError) as unavailable:
            self.domain.create_consent(
                self._consent_payload(run_id="dh-run-no-secret-001"),
                "yuelei", "",
            )
        self.assertEqual("consent_service_unavailable", unavailable.exception.code)
        self.assertFalse(self.consent_db.exists())

    def test_gesture_and_talking_submissions_require_matching_consent(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        common = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": consent_payload["script"],
            "digital_human_item_index": 0,
        }
        gesture = dict(common, digital_human_stage="gesture", prompt="safe",
                       provider="openai",
                       images=[base64.b64encode(b"forged-portrait").decode("ascii")],
                       reference_images=[base64.b64encode(PNG_2X2).decode("ascii")])
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            checked = self.domain.verify_child_submission(gesture, "yuelei", "image")
            self.assertEqual(checked["digital_human_consent_id"], consent["consent_id"])
            self.assertEqual(checked["provider"], "banana")
            self.assertEqual(checked["model"], "nb2")
            self.assertEqual(checked["quality"], "std")
            self.assertEqual(checked["images"], gesture["reference_images"])
            self.assertNotIn("reference_images", checked)
            self.assertEqual(
                self.points.cost_of("image", checked),
                self.points.pricing.get_price("image.banana.nb2.std"),
            )
            self.assertNotIn("digital_human_consent_token", checked)
            missing_provider = dict(gesture)
            missing_provider.pop("provider")
            self.assertEqual(
                self.domain.verify_child_submission(
                    missing_provider, "yuelei", "image",
                )["provider"],
                "banana",
            )
            material_reference = base64.b64encode(JPEG_2X2).decode("ascii")
            material = dict(
                common, digital_human_stage="material", provider="openai",
                images=[base64.b64encode(b"forged-material").decode("ascii")],
                reference_images=[material_reference],
            )
            checked_material = self.domain.verify_child_submission(
                material, "yuelei", "image",
            )
            self.assertEqual(checked_material["provider"], "seedream")
            self.assertEqual(checked_material["variant"], "std")
            self.assertEqual(checked_material["quality"], "std")
            self.assertEqual(checked_material["count"], 1)
            self.assertEqual(checked_material["ratio"], "9:16")
            self.assertEqual(
                checked_material["reference_images"], [material_reference],
            )
            self.assertNotIn("images", checked_material)
            self.assertNotIn("model", checked_material)
            self.assertEqual(
                self.points.cost_of("image", checked_material),
                self.points.pricing.get_price("image.seedream.std.std"),
            )
            if sys.platform != "win32":
                image_domain = importlib.import_module("content_domains.image")
                validated_material = image_domain.validate_image_payload(
                    checked_material,
                )
                self.assertEqual(validated_material["provider"], "seedream")
                self.assertEqual(validated_material["variant"], "std")
                self.assertEqual(
                    validated_material["reference_images"],
                    [material_reference],
                )
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "照片"):
                self.domain.verify_child_submission(
                    dict(gesture, reference_images=[base64.b64encode(JPEG_2X2).decode("ascii")]),
                    "yuelei", "image",
                )
            with self.assertRaises(self.domain.DigitalHumanRequestError) as extra_photo:
                self.domain.verify_child_submission(
                    dict(gesture, reference_images=[
                        base64.b64encode(PNG_2X2).decode("ascii"),
                        base64.b64encode(WEBP_2X2).decode("ascii"),
                    ]),
                    "yuelei", "image",
                )
            self.assertEqual("consent_photo_mismatch", extra_photo.exception.code)
            with self.assertRaises(self.domain.DigitalHumanRequestError) as negative_index:
                self.domain.verify_child_submission(
                    dict(gesture, digital_human_item_index=-1), "yuelei", "image",
                )
            self.assertEqual("consent_plan_mismatch", negative_index.exception.code)
            gesture_file = self.root / "images" / "authorized-gesture.png"
            gesture_file.parent.mkdir(parents=True, exist_ok=True)
            gesture_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"authorized")
            record = self._consent_record(
                consent_payload["plan_digest"], id=consent["consent_id"],
            )
            gesture_ids = self._insert_gesture_jobs(
                record, statuses=["done", "done", "done"],
            )
            connection = self._connection()
            connection.execute(
                "UPDATE jobs SET result=? WHERE id=?",
                (json.dumps({"file": "images/authorized-gesture.png"}), gesture_ids[0]),
            )
            connection.commit()
            connection.close()
            talking = dict(
                common, digital_human_stage="talking", voice="vip_ready_voice",
                gesture_job_id=gesture_ids[0],
                image_data="data:image/png;base64," + base64.b64encode(b"other").decode("ascii"),
            )
            fake_video = SimpleNamespace(
                subtitle_runtime_preflight=lambda: {
                    "ok": True, "no_charge": True,
                },
            )
            loaded_video = sys.modules.get("content_domains.video")
            preflight_patch = (
                mock.patch.object(
                    loaded_video, "subtitle_runtime_preflight",
                    side_effect=fake_video.subtitle_runtime_preflight,
                )
                if loaded_video is not None else
                mock.patch.dict(sys.modules, {"content_domains.video": fake_video})
            )
            with preflight_patch:
                self.assertIn(
                    "digital_human_consent_id",
                    self.domain.verify_child_submission(
                        talking, "yuelei", "video",
                    ),
                )
                checked_talking = self.domain.verify_child_submission(
                    talking, "yuelei", "video",
                )
            self.assertNotIn("gesture_job_id", checked_talking)
            self.assertEqual(
                "data:image/png;base64," + base64.b64encode(gesture_file.read_bytes()).decode("ascii"),
                checked_talking["image_data"],
            )
            self.assertEqual({
                "resolution": "1080p", "ratio": "9:16", "motion": "low",
                "speed": 1.0, "pitch": 0, "volume": 1,
                "delivery": "natural", "subtitle": False,
            }, {key: checked_talking[key] for key in (
                "resolution", "ratio", "motion", "speed", "pitch", "volume",
                "delivery", "subtitle",
            )})
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "声音"):
                self.domain.verify_child_submission(
                    dict(talking, voice="vip_wrong"), "yuelei", "video",
                )
            with self.assertRaises(self.domain.DigitalHumanRequestError) as wrong_gesture:
                self.domain.verify_child_submission(
                    dict(talking, gesture_job_id=99999), "yuelei", "video",
                )
            self.assertEqual(
                "talking_gesture_binding_invalid", wrong_gesture.exception.code,
            )

    def test_provider_references_reject_invalid_content_and_preserve_upload_bytes(self):
        invalid = b"not-an-image"
        invalid_payload = self._consent_payload(
            run_id="dh-run-invalid-image-001",
            photo_sha256=hashlib.sha256(invalid).hexdigest(),
        )
        invalid_consent = self.domain.create_consent(
            invalid_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        invalid_gesture = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "gesture",
            "digital_human_run_id": invalid_consent["run_id"],
            "digital_human_plan_digest": invalid_payload["plan_digest"],
            "digital_human_consent_token": invalid_consent["consent_token"],
            "digital_human_script": invalid_payload["script"],
            "digital_human_item_index": 0,
            "reference_images": [base64.b64encode(invalid).decode("ascii")],
        }
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            checked_invalid = self.domain.verify_child_submission(
                invalid_gesture, "yuelei", "image",
            )
        with self.assertRaisesRegex(ValueError, "cannot be decoded"):
            self.banana_provider.validate_payload(checked_invalid)

        consent_payload = self._consent_payload(run_id="dh-run-upload-mime-001")
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        common = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "material",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": consent_payload["script"],
            "digital_human_item_index": 0,
            "provider": "banana",
            "images": [base64.b64encode(PNG_2X2).decode("ascii")],
        }
        upload_root = self.root / "private-uploads"
        with mock.patch.object(self.cli_uploads, "UPLOAD_ROOT", upload_root):
            uploaded = self.cli_uploads.store_image(
                io.BytesIO(WEBP_2X2), len(WEBP_2X2), "yuelei", "image/webp",
                hashlib.sha256(WEBP_2X2).hexdigest(), now=100,
            )
            expanded = self.cli_uploads.expand_image_payload(
                dict(common, reference_upload_ids=[uploaded["upload_id"]]),
                "yuelei", now=101,
            )
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            checked = self.domain.verify_child_submission(expanded, "yuelei", "image")
        self.assertNotIn("images", checked)
        self.assertNotIn("model", checked)
        self.assertEqual("seedream", checked["provider"])
        self.assertEqual("std", checked["variant"])
        self.assertEqual("std", checked["quality"])
        self.assertEqual(1, checked["count"])
        self.assertEqual("9:16", checked["ratio"])
        self.assertEqual(
            WEBP_2X2, base64.b64decode(checked["reference_images"][0]),
        )

    def test_clone_consent_binds_audio_hash_and_slot(self):
        sample = b"voice-sample"
        consent_payload = self._consent_payload(
                run_id="dh-run-clone-001", voice_mode="clone", voice_ref="slot_123",
                voice_sha256=hashlib.sha256(sample).hexdigest(),
            )
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        body = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "voice_clone",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": consent_payload["script"],
            "slot_id": "slot_123", "audio": base64.b64encode(sample).decode("ascii"),
        }
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            checked = self.domain.verify_clone_submission(body, "yuelei")
            self.assertEqual(checked["digital_human_consent_id"], consent["consent_id"])
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "声音样本"):
                self.domain.verify_clone_submission(
                    dict(body, audio=base64.b64encode(b"other").decode("ascii")), "yuelei",
                )
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "字段不完整"):
                self.domain.verify_clone_submission({
                    "digital_human_consent_token": consent["consent_token"],
                    "slot_id": "slot_123", "audio": body["audio"],
                }, "yuelei")

    def test_gesture_recovery_accepts_only_current_authorized_recoverable_jobs(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret",
            now=int(self.domain.time.time()), db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        # Existing OpenAI children remain recoverable; retries are new
        # submissions and are forced to xiaole by the server.
        job_ids = self._insert_gesture_jobs(
            record, overrides={"provider": "openai"},
        )
        body = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "gesture_recovery",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "gesture_job_ids": self._indexed_jobs(job_ids),
        }
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            result = self.domain.validate_gesture_recovery(body, "yuelei")
        self.assertEqual(
            result["gesture_jobs"],
            [
                {"index": 0, "job_id": 21, "status": "pending"},
                {"index": 1, "job_id": 22, "status": "running"},
                {"index": 2, "job_id": 23, "status": "done"},
            ],
        )

    def test_sparse_recovery_preserves_original_plan_indexes(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret",
            now=int(self.domain.time.time()), db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        self._insert_gesture_jobs(record)
        self._bind_child_jobs(record)
        sparse = {
            "gesture_recovery": ("gesture_job_ids", [
                {"index": 0, "job_id": 21}, {"index": 2, "job_id": 23},
            ], self.domain.validate_gesture_recovery, "gesture_jobs"),
            "material_recovery": ("material_job_ids", [
                {"index": 0, "job_id": 11}, {"index": 2, "job_id": 13},
            ], self.domain.validate_material_recovery, "material_jobs"),
            "video_recovery": ("video_job_ids", [
                {"index": 0, "job_id": 1}, {"index": 2, "job_id": 3},
            ], self.domain.validate_video_recovery, "video_jobs"),
        }
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            for stage, (field, entries, validator, result_field) in sparse.items():
                with self.subTest(stage=stage):
                    body = self._recovery_body(
                        consent, consent_payload, stage, field, [],
                    )
                    body[field] = entries
                    result = validator(body, "yuelei")
                    self.assertEqual(
                        [(item["index"], item["job_id"])
                         for item in result[result_field]],
                        [(item["index"], item["job_id"]) for item in entries],
                    )
            for bad_entries in (
                    [{"index": -1, "job_id": 21}],
                    [{"index": 0, "job_id": 21}, {"index": 0, "job_id": 23}],
                    [{"index": 0.5, "job_id": 21}],
                    [{"index": 0, "job_id": "21"}]):
                with self.subTest(bad_entries=bad_entries):
                    body = self._recovery_body(
                        consent, consent_payload, "gesture_recovery",
                        "gesture_job_ids", [],
                    )
                    body["gesture_job_ids"] = bad_entries
                    with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                        self.domain.validate_gesture_recovery(body, "yuelei")
                    self.assertEqual("gesture_recovery_invalid", rejected.exception.code)

    def test_gesture_recovery_rejects_old_consent_run_and_plan(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret",
            now=int(self.domain.time.time()), db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        base = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "gesture_recovery",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
        }
        cases = [
            {"digital_human_consent_id": "dhc_" + "9" * 32},
            {"digital_human_run_id": "dh-run-old-001"},
            {"digital_human_plan_digest": "b" * 64},
        ]
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    connection = sqlite3.connect(self.db)
                    connection.execute("DELETE FROM jobs WHERE id>=21")
                    connection.commit()
                    connection.close()
                    ids = self._insert_gesture_jobs(record, overrides=overrides)
                    with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                        self.domain.validate_gesture_recovery(
                            dict(base, gesture_job_ids=self._indexed_jobs(ids)), "yuelei",
                        )
                    self.assertEqual(rejected.exception.code, "gesture_recovery_invalid")

    def test_gesture_recovery_rejects_wrong_owner_type_and_status(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret",
            now=int(self.domain.time.time()), db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        body = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "gesture_recovery",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": consent_payload["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
        }
        cases = [
            {"username": "other"},
            {"kind": "video"},
            {"statuses": ["done", "failed", "done"]},
        ]
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            for case in cases:
                with self.subTest(case=case):
                    connection = sqlite3.connect(self.db)
                    connection.execute("DELETE FROM jobs WHERE id>=21")
                    connection.commit()
                    connection.close()
                    ids = self._insert_gesture_jobs(record, **case)
                    with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                        self.domain.validate_gesture_recovery(
                            dict(body, gesture_job_ids=self._indexed_jobs(ids)), "yuelei",
                        )
                    self.assertEqual(rejected.exception.code, "gesture_recovery_invalid")

    def test_gesture_recovery_rejects_empty_missing_and_deleted_done_files(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        body = self._recovery_body(
            consent, consent_payload, "gesture_recovery", "gesture_job_ids", [21],
        )
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            for result, deleted in (("{}", 0), (json.dumps({"file": "images/missing.png"}), 0),
                                    (json.dumps({"file": "images/gesture-21.png"}), 1)):
                with self.subTest(result=result, deleted=deleted):
                    connection = self._connection()
                    connection.execute("DELETE FROM jobs WHERE id>=21")
                    connection.commit()
                    connection.close()
                    self._insert_gesture_jobs(record, statuses=["done"])
                    connection = self._connection()
                    connection.execute(
                        "UPDATE jobs SET result=?,deleted=? WHERE id=21", (result, deleted),
                    )
                    connection.commit()
                    connection.close()
                    with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                        self.domain.validate_gesture_recovery(body, "yuelei")
                    self.assertEqual("gesture_recovery_invalid", rejected.exception.code)
                    self.assertEqual([21], rejected.exception.invalid_job_ids)

    def test_material_and_video_recovery_validate_binding_files_and_precise_job(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        self._bind_child_jobs(record)
        connection = self._connection()
        old_material = json.loads(connection.execute(
            "SELECT payload FROM jobs WHERE id=11",
        ).fetchone()[0])
        old_material["provider"] = "openai"
        connection.execute(
            "UPDATE jobs SET payload=? WHERE id=11",
            (json.dumps(old_material),),
        )
        connection.commit()
        connection.close()
        material_body = self._recovery_body(
            consent, consent_payload, "material_recovery", "material_job_ids",
            [11, 12, 13, 14, 15, 16],
        )
        video_body = self._recovery_body(
            consent, consent_payload, "video_recovery", "video_job_ids", [1, 2, 3],
        )
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            self.assertEqual(
                6, len(self.domain.validate_material_recovery(material_body, "yuelei")["material_jobs"]),
            )
            self.assertEqual(
                3, len(self.domain.validate_video_recovery(video_body, "yuelei")["video_jobs"]),
            )
            with self.assertRaises(self.domain.DigitalHumanRequestError) as swapped_material:
                self.domain.validate_material_recovery(
                    dict(material_body, material_job_ids=self._indexed_jobs(
                        [12, 11, 13, 14, 15, 16],
                    )),
                    "yuelei",
                )
            self.assertEqual("material_recovery_invalid", swapped_material.exception.code)
            with self.assertRaises(self.domain.DigitalHumanRequestError) as swapped_video:
                self.domain.validate_video_recovery(
                    dict(video_body, video_job_ids=self._indexed_jobs([2, 1, 3])),
                    "yuelei",
                )
            self.assertEqual("video_recovery_invalid", swapped_video.exception.code)
            connection = self._connection()
            payload = json.loads(connection.execute(
                "SELECT payload FROM jobs WHERE id=13",
            ).fetchone()[0])
            payload["digital_human_run_id"] = "dh-run-old-001"
            connection.execute("UPDATE jobs SET payload=? WHERE id=13", (json.dumps(payload),))
            connection.commit()
            connection.close()
            with self.assertRaises(self.domain.DigitalHumanRequestError) as material_rejected:
                self.domain.validate_material_recovery(material_body, "yuelei")
            self.assertEqual("material_recovery_invalid", material_rejected.exception.code)
            self.assertEqual([13], material_rejected.exception.invalid_job_ids)

            self._bind_child_jobs(record)
            connection = self._connection()
            payload = json.loads(connection.execute(
                "SELECT payload FROM jobs WHERE id=2",
            ).fetchone()[0])
            payload["digital_human_plan_digest"] = "b" * 64
            connection.execute("UPDATE jobs SET payload=? WHERE id=2", (json.dumps(payload),))
            connection.commit()
            connection.close()
            with self.assertRaises(self.domain.DigitalHumanRequestError) as video_rejected:
                self.domain.validate_video_recovery(video_body, "yuelei")
            self.assertEqual("video_recovery_invalid", video_rejected.exception.code)
            self.assertEqual([2], video_rejected.exception.invalid_job_ids)

    def test_video_recovery_reports_every_invalid_talking_job_at_once(self):
        consent_payload = self._consent_payload()
        consent = self.domain.create_consent(
            consent_payload, "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            consent_payload["plan_digest"], id=consent["consent_id"],
        )
        self._bind_child_jobs(record)
        connection = self._connection()
        connection.execute("UPDATE jobs SET status='error' WHERE id IN (1,2,3)")
        connection.commit()
        connection.close()
        video_body = self._recovery_body(
            consent, consent_payload, "video_recovery", "video_job_ids", [1, 2, 3],
        )
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                self.domain.validate_video_recovery(video_body, "yuelei")
        self.assertEqual("video_recovery_invalid", rejected.exception.code)
        self.assertEqual([1, 2, 3], rejected.exception.invalid_job_ids)
        self.assertIn("3 段口播均未生成成功", str(rejected.exception))

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def test_plan_is_deterministic_and_has_three_segments_six_materials(self):
        script = "第一部分介绍问题。第二部分说明方案。第三部分给出结果。第四部分补充证据。第五部分强调价值。第六部分发出行动邀请。"
        first = self.domain.plan(script)
        second = self.domain.plan(script)
        self.assertEqual(first, second)
        self.assertEqual(len(first["segments"]), 3)
        self.assertEqual(len(first["materials"]), 6)
        self.assertEqual("".join(item["text"] for item in first["segments"]), script)
        self.assertEqual(len({item["gesture_prompt"] for item in first["segments"]}), 3)
        self.assertTrue(all("眼神稳定直视镜头" in item["gesture_prompt"] for item in first["segments"]))
        self.assertTrue(all("嘴唇自然闭合" in item["gesture_prompt"] for item in first["segments"]))
        self.assertEqual([item["role"] for item in first["segments"]], ["hook", "explain", "cta"])
        self.assertTrue(all(item["source_policy"] == "customer_then_feishu_then_ai" for item in first["materials"]))
        self.assertTrue(all(item["material_query"] for item in first["materials"]))

    def test_plan_supports_one_two_or_three_matching_gestures_and_videos(self):
        script = "开场介绍核心问题。接着说明解决方案。最后邀请用户立即行动。"
        expected_roles = {
            1: ["complete"],
            2: ["hook", "explain_cta"],
            3: ["hook", "explain", "cta"],
        }
        for count in (1, 2, 3):
            with self.subTest(count=count):
                planned = self.domain.plan(script, count)
                self.assertEqual(count, planned["segment_count"])
                self.assertEqual(count * 2, planned["material_count"])
                self.assertEqual(count, len(planned["segments"]))
                self.assertEqual(count * 2, len(planned["materials"]))
                self.assertEqual(script, "".join(
                    item["text"] for item in planned["segments"]
                ))
                self.assertEqual(
                    expected_roles[count],
                    [item["role"] for item in planned["segments"]],
                )
        for invalid in (
                0, 4, True, "1", 1.0, 1.5, 2.9, 3.9,
                float("nan"), float("inf"), float("-inf"), "bad"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(self.domain.DigitalHumanRequestError):
                    self.domain.plan(script, invalid)

    def test_plan_enforces_downstream_tts_limit_for_every_selected_segment(self):
        one_at_limit = "甲" * self.domain.MAX_TTS_SEGMENT_CHARS
        planned = self.domain.plan(one_at_limit, 1)
        self.assertEqual(self.domain.MAX_TTS_SEGMENT_CHARS,
                         len(planned["segments"][0]["text"]))

        with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
            self.domain.plan(one_at_limit + "甲", 1)
        self.assertEqual("segment_tts_limit_exceeded", rejected.exception.code)
        self.assertIn("增加生成数量或缩短文案", str(rejected.exception))

        for count in (2, 3):
            with self.subTest(count=count, boundary="accepted"):
                script = ("甲" * (self.domain.MAX_TTS_SEGMENT_CHARS - 1) + "。") * count
                planned = self.domain.plan(script, count)
                self.assertTrue(all(
                    len(item["text"]) <= self.domain.MAX_TTS_SEGMENT_CHARS
                    for item in planned["segments"]
                ))
            with self.subTest(count=count, boundary="rejected"):
                script = ("甲" * self.domain.MAX_TTS_SEGMENT_CHARS + "。") * count
                with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                    self.domain.plan(script, count)
                self.assertEqual("segment_tts_limit_exceeded", rejected.exception.code)

    def test_plan_tts_overflow_is_rejected_before_paid_job_or_point_creation(self):
        script_to_video = importlib.import_module("content_domains.script_to_video")

        class Handler:
            path = self.domain.PLAN_PATH

            def __init__(self):
                self.sent = None

            @staticmethod
            def _token():
                return "token"

            @staticmethod
            def _json_body_strict():
                return {"script": "甲" * 1001, "segment_count": 1}

            def _send(self, status, payload):
                self.sent = (status, payload)

        handler = Handler()
        with mock.patch("content_domains.jobs_store.create_job_after_charge") as create_job, \
             mock.patch.object(self.points, "deduct_points") as deduct_points:
            handled = script_to_video.dispatch_http(
                handler, "POST",
                lambda _token: {"username": "yuelei"},
                lambda _user: False,
            )

        self.assertTrue(handled)
        self.assertEqual(400, handler.sent[0])
        self.assertEqual("segment_tts_limit_exceeded", handler.sent[1]["code"])
        create_job.assert_not_called()
        deduct_points.assert_not_called()

    def test_prepare_freezes_only_owned_completed_child_jobs(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        record = self._consent_record(planned["plan_digest"])
        self._bind_child_jobs(record)
        payload = self.domain.prepare_compose_payload(
            self._compose_request(script, planned, record),
            "yuelei", consent_record=record,
        )
        self.assertEqual(payload["video_files"], ["videos/1.mp4", "videos/2.mp4", "videos/3.mp4"])
        self.assertEqual(len(payload["material_files"]), 6)
        self.assertEqual(payload["digital_human_consent_id"], record["id"])
        self.assertNotIn("_script_to_video_state", payload)

    def test_prepare_uses_selected_segment_count_through_final_compose(self):
        script = "开场介绍核心问题。接着说明解决方案。最后邀请用户立即行动。"
        for count in (1, 2, 3):
            with self.subTest(count=count):
                planned = self.domain.plan(script, count)
                record = self._consent_record(planned["plan_digest"])
                self._bind_child_jobs(record, count)
                payload = self.domain.prepare_compose_payload(
                    self._compose_request(script, planned, record),
                    "yuelei", consent_record=record,
                )
                self.assertEqual(count, payload["digital_human_segment_count"])
                self.assertEqual(count * 2, payload["material_count"])
                self.assertEqual(count, len(payload["video_files"]))
                self.assertEqual(count * 2, len(payload["material_files"]))

    def test_prepare_rejects_digest_drift_and_foreign_job(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "方案已变化"):
            self.domain.prepare_compose_payload({
                "pipeline": self.domain.PIPELINE, "script": script,
                "plan_digest": "0" * 64, "video_job_ids": [1, 2, 3],
                "material_job_ids": [11, 12, 13, 14, 15, 16],
            }, "yuelei")
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE jobs SET username='other' WHERE id=3")
        connection.commit()
        connection.close()
        planned = self.domain.plan(script)
        record = self._consent_record(planned["plan_digest"])
        self._bind_child_jobs(record)
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "不属于当前账号"):
            self.domain.prepare_compose_payload(
                self._compose_request(script, planned, record),
                "yuelei", consent_record=record,
            )

    def test_prepare_rejects_old_consent_and_cross_run_or_plan_children(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        current = self._consent_record(planned["plan_digest"])
        request = self._compose_request(script, planned, current)
        cases = (
            ("digital_human_consent_id", "dhc_" + "2" * 32),
            ("digital_human_run_id", "dh-run-old-002"),
            ("digital_human_plan_digest", "b" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self._bind_child_jobs(current)
                connection = sqlite3.connect(self.db)
                payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=1"
                ).fetchone()[0])
                payload[field] = value
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=1", (json.dumps(payload),),
                )
                connection.commit()
                connection.close()
                with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                    self.domain.prepare_compose_payload(
                        request, "yuelei", consent_record=current,
                    )
                self.assertEqual("child_consent_binding_mismatch", rejected.exception.code)

    def test_prepare_rejects_missing_or_corrupt_child_binding(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        record = self._consent_record(planned["plan_digest"])
        request = self._compose_request(script, planned, record)
        for payload_text in ("{}", "{broken-json"):
            with self.subTest(payload=payload_text):
                self._bind_child_jobs(record)
                connection = sqlite3.connect(self.db)
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=11", (payload_text,),
                )
                connection.commit()
                connection.close()
                with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                    self.domain.prepare_compose_payload(
                        request, "yuelei", consent_record=record,
                    )
                self.assertIn(rejected.exception.code, {
                    "child_consent_binding_invalid",
                    "child_consent_binding_mismatch",
                })

    def test_compose_binding_rejection_precedes_charge_and_job_creation(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        consent = self.domain.create_consent(
            self._consent_payload(script=script, plan_digest=planned["plan_digest"]),
            "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            planned["plan_digest"], id=consent["consent_id"],
        )
        self._bind_child_jobs(record)
        connection = sqlite3.connect(self.db)
        payload = json.loads(connection.execute(
            "SELECT payload FROM jobs WHERE id=1"
        ).fetchone()[0])
        payload["digital_human_consent_id"] = "dhc_" + "9" * 32
        connection.execute(
            "UPDATE jobs SET payload=? WHERE id=1", (json.dumps(payload),),
        )
        before_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        connection.commit()
        connection.close()

        request = self._compose_request(script, planned, record)
        request["digital_human_consent_token"] = consent["consent_token"]

        class Handler:
            path = "/api/gen/script_to_video"
            headers = {}

            def __init__(self):
                self.sent = None

            def _token(self):
                return "token"

            def _json_body_strict(self):
                return dict(request)

            def _send(self, status, payload):
                self.sent = (status, payload)

        handler = Handler()
        cost_of = mock.Mock(return_value=0)
        points_domain = SimpleNamespace(cost_of=cost_of)
        core_module = importlib.import_module("content_domains.core")
        with mock.patch.object(self.domain, "cdb", self._consent_connection), \
             mock.patch.object(self.domain, "jdb", self._connection), \
             mock.patch.dict(core_module.HANDLERS, {"script_to_video": lambda _payload: {}}), \
             mock.patch("content_domains.core._domains", return_value=(SimpleNamespace(), points_domain, SimpleNamespace())), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei", "points": 100}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch("content_domains.core.miniprogram_security.check_payload"), \
             mock.patch("content_domains.jobs_store.create_job_after_charge") as create_job:
            core_module.H.do_POST(handler)
        self.assertEqual(409, handler.sent[0])
        self.assertEqual("child_consent_binding_mismatch", handler.sent[1]["code"])
        cost_of.assert_not_called()
        create_job.assert_not_called()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(before_jobs, connection.execute(
                "SELECT COUNT(*) FROM jobs"
            ).fetchone()[0])
        finally:
            connection.close()

        core = (Path(__file__).resolve().parents[1] / "server" / "content_domains" / "core.py").read_text(encoding="utf-8")
        paid = core[core.index("if kind is not None:"):]
        self.assertLess(
            paid.index("prepare_script_to_video_payload("),
            paid.index("cost = points_domain.cost_of(kind, body)"),
        )
        self.assertLess(
            paid.index("prepare_script_to_video_payload("),
            paid.index("jobs_store.create_job_after_charge("),
        )

    def test_security_outage_precedes_charge_and_job_creation(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        consent = self.domain.create_consent(
            self._consent_payload(script=script, plan_digest=planned["plan_digest"]),
            "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            planned["plan_digest"], id=consent["consent_id"],
        )
        self._bind_child_jobs(record)
        request = self._compose_request(script, planned, record)
        request["digital_human_consent_token"] = consent["consent_token"]
        connection = sqlite3.connect(self.db)
        before_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        connection.close()

        class Handler:
            path = "/api/gen/script_to_video"
            headers = {}

            def __init__(self):
                self.sent = None

            def _token(self):
                return "token"

            def _json_body_strict(self):
                return dict(request)

            def _send(self, status, payload):
                self.sent = (status, payload)

        handler = Handler()
        cost_of = mock.Mock(return_value=0)
        points_domain = SimpleNamespace(cost_of=cost_of)
        core_module = importlib.import_module("content_domains.core")
        outage = core_module.miniprogram_security.SecurityUnavailable(
            "内容安全服务令牌暂时不可用，请稍后重试", "token",
        )
        with mock.patch.object(self.domain, "cdb", self._consent_connection), \
             mock.patch.object(self.domain, "jdb", self._connection), \
             mock.patch.dict(core_module.HANDLERS, {"script_to_video": lambda _payload: {}}), \
             mock.patch("content_domains.core._domains", return_value=(SimpleNamespace(), points_domain, SimpleNamespace())), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei", "points": 100}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch("content_domains.core.miniprogram_security.check_payload", side_effect=outage), \
             mock.patch("content_domains.jobs_store.create_job_after_charge") as create_job:
            core_module.H.do_POST(handler)

        self.assertEqual(503, handler.sent[0])
        self.assertEqual("content_security_token_unavailable", handler.sent[1]["code"])
        cost_of.assert_not_called()
        create_job.assert_not_called()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(before_jobs, connection.execute(
                "SELECT COUNT(*) FROM jobs"
            ).fetchone()[0])
        finally:
            connection.close()

    def test_paid_child_queue_full_returns_queryable_refund_tracker(self):
        core = importlib.import_module("content_domains.core")
        request = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "gesture",
        }

        class Handler:
            path = "/api/gen/image"
            headers = {"Idempotency-Key": "dh-gesture-queue-full-001"}

            def __init__(self):
                self.sent = None

            def _token(self):
                return "token"

            def _json_body(self):
                return dict(request)

            def _send(self, status, payload):
                self.sent = (status, payload)
                return self.sent

        points_domain = SimpleNamespace(
            AuthPointsError=type("AuthPointsError", (Exception,), {}),
            cost_of=mock.Mock(return_value=5),
            deduct_points=mock.Mock(return_value=95),
            refund_points=mock.Mock(side_effect=RuntimeError("auth unavailable")),
        )
        image_stub = SimpleNamespace(validate_image_payload=mock.Mock(return_value=request))
        video_stub = SimpleNamespace(SeedanceReferenceUnavailable=())
        record = self._consent_record("a" * 64)
        with mock.patch.object(core, "jdb", self._connection), \
             mock.patch.dict(sys.modules, {"content_domains.image": image_stub}), \
             mock.patch.object(sys.modules["content_domains"], "image", image_stub,
                               create=True), \
             mock.patch.dict(core.HANDLERS, {"image": lambda _payload: {}}), \
             mock.patch("content_domains.core._domains", return_value=(SimpleNamespace(), points_domain, video_stub)), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei", "points": 100}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(self.domain, "verify_child_submission_with_record", return_value=(request, record)), \
             mock.patch("content_domains.core.miniprogram_security.check_payload"), \
             mock.patch("content_domains.core._user_video_submit_limit", return_value=None), \
             mock.patch("content_domains.core._user_active_job_count", return_value=0), \
             mock.patch("content_domains.jobs_store.create_paid_job", return_value=(31, 95)), \
             mock.patch("content_domains.core._reject_pending_job"), \
             mock.patch("content_domains.core._compensation_tracking_response", return_value={
                 "job_id": 31, "cost": 5, "detail": "任务队列已满，请稍后再试",
                 "refund_state": "pending", "points_left": 95,
             }), \
             mock.patch("content_domains.core.enqueue_job", return_value=False):
            handler = Handler()
            core.H.do_POST(handler)
        self.assertEqual(202, handler.sent[0], handler.sent)
        self.assertEqual("pending", handler.sent[1]["refund_state"])
        self.assertGreater(handler.sent[1]["job_id"], 0)

    def test_local_compose_has_zero_additional_points(self):
        body = {"pipeline": self.domain.PIPELINE}
        self.assertEqual(self.points.cost_of("script_to_video", body), 0)
        self.assertEqual(body["cost_breakdown"]["local_compose"], 0)

    def test_compose_matches_vertical_delivery_rate_and_labels_ai_materials(self):
        source = Path(self.domain.__file__).read_text(encoding="utf-8")
        self.assertIn("fps=30", source)
        self.assertIn("r=30", source)
        self.assertNotIn("fps=25", source)
        self.assertIn("CONCEPT / AI FILL", source)

    def test_final_verification_requires_audio_sync_and_rejects_long_black_frame(self):
        media = self.root / "final.mp4"
        media.write_bytes(b"video")
        video_domain = SimpleNamespace(
            _resolve_out_file=lambda rel: media,
            _probe_video_size=lambda path: (1080, 1920),
            _probe_video_duration=lambda rel: 12.0,
        )
        audio = SimpleNamespace(returncode=0, stdout=b"audio\n", stderr=b"")
        clear = SimpleNamespace(returncode=0, stdout=b"", stderr=b"no black frames")
        with mock.patch.object(self.domain.subprocess, "run", side_effect=[audio, clear]):
            result = self.domain._verify_final_video(video_domain, "final.mp4", 12.1)
        self.assertEqual(result[1:], (1080, 1920, 12.0))
        black = SimpleNamespace(returncode=0, stdout=b"", stderr=b"black_duration:11.2")
        with mock.patch.object(self.domain.subprocess, "run", side_effect=[audio, black]):
            with self.assertRaisesRegex(RuntimeError, "持续黑帧"):
                self.domain._verify_final_video(video_domain, "final.mp4", 12.0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg and ffprobe")
    def test_three_presenters_and_six_materials_render_a_verified_final_video(self):
        """Run the complete local compositor with real media, not placeholder bytes."""
        video_dir = self.root / "videos"
        image_dir = self.root / "images"
        video_dir.mkdir(exist_ok=True)
        image_dir.mkdir(exist_ok=True)
        for index, color in enumerate(("#8B5CF6", "#0EA5E9", "#F97316"), 1):
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=%s:s=360x640:r=30:d=0.45" % color,
                "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=0.45" % (320 + index * 80),
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(video_dir / ("%d.mp4" % index)),
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for index, color in enumerate(("red", "orange", "yellow", "green", "blue", "purple"), 11):
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=%s:s=360x640" % color,
                "-frames:v", "1", str(image_dir / ("%d.png" % index)),
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        out_dir = self.root / "rendered"
        out_dir.mkdir()

        class VideoDomain:
            VIDEO_OUT_DIR = out_dir

            @staticmethod
            def _resolve_out_file(rel):
                path = self.root / rel
                return path if path.is_file() else None

            @staticmethod
            def _probe_video_size(path):
                output = subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=width,height", "-of", "csv=p=0:s=x", str(path),
                ], text=True).strip()
                return tuple(map(int, output.split("x")))

            @staticmethod
            def _probe_video_duration(rel):
                path = self.root / rel
                return float(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                    "default=nw=1:nk=1", str(path),
                ], text=True).strip())

            @staticmethod
            def burn_subtitle(rel, **_kwargs):
                source = self.root / rel
                target = out_dir / "verified-final.mp4"
                shutil.copy2(source, target)
                return target.relative_to(self.root).as_posix()

        planned = self.domain.plan("开场说明问题。中段解释解决方案和关键价值。结尾邀请用户采取行动。")
        payload = {
            "_job_id": 230, "copy": planned["copy"], "segments": planned["segments"],
            "video_job_ids": [1, 2, 3], "material_job_ids": [11, 12, 13, 14, 15, 16],
            "video_files": ["videos/1.mp4", "videos/2.mp4", "videos/3.mp4"],
            "material_files": ["images/%d.png" % index for index in range(11, 17)],
        }
        package = importlib.import_module("content_domains")
        with mock.patch.object(package, "video", VideoDomain, create=True), mock.patch.dict(
            sys.modules, {"content_domains.video": VideoDomain},
        ):
            result = self.domain.compose(payload)
        self.assertEqual(result["child_jobs"], {"videos": [1, 2, 3], "materials": [11, 12, 13, 14, 15, 16]})
        self.assertEqual((result["width"], result["height"]), (1080, 1920))
        self.assertTrue(result["verification"]["audio_stream"])
        self.assertTrue((self.root / result["video_file"]).is_file())

        @staticmethod
        def failed_subtitle(_rel, **_kwargs):
            raise ModuleNotFoundError("faster_whisper")

        VideoDomain.burn_subtitle = failed_subtitle
        fallback_payload = dict(payload, _job_id=231)
        with mock.patch.object(
            package, "video", VideoDomain, create=True,
        ), mock.patch.dict(sys.modules, {"content_domains.video": VideoDomain}):
            fallback = self.domain.compose(fallback_payload)
        self.assertTrue(fallback["subtitle_retryable"])
        self.assertEqual("unavailable", fallback["verification"]["subtitle"])
        self.assertIn("faster_whisper", fallback["subtitle_error"])
        self.assertTrue((self.root / fallback["video_file"]).is_file())


class DigitalHumanOneClickUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)

    def test_oneclick_image_jobs_use_seedream_standard_for_materials(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        materials = page[page.index("function generateMaterials(epoch)"):page.index("function generateTalking(voiceKey,epoch)")]

        self.assertIn("provider:'seedream'", materials)
        self.assertIn("variant:'std'", materials)
        self.assertIn("quality:'std'", materials)
        self.assertIn("count:1", materials)
        self.assertIn("ratio:'9:16'", materials)
        self.assertNotIn("function generateImages", page)
        self.assertNotIn("dh-gesture-v2", page)
        self.assertNotIn("provider:'banana'", materials)
        self.assertNotIn("model:'nb2'", materials)
        self.assertNotIn("provider:'xiaole'", materials)
        self.assertNotIn("provider:'openai'", materials)

    def test_page_exposes_required_inputs_and_real_pipeline_calls(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        for marker in (
            'id="photo"', 'id="voice"', 'id="script"',
            'id="customerMaterials"', 'id="customerMaterialList"',
            "digital-human-material-state.js?v=",
            "digital-human-voice-state.js?v=",
            "digital-human-setup-state.js?v=",
            "digital-human-oneclick-state.js?v=",
            "digital-human-submit.js?v=",
            "/api/gen/digital-human-v2/plan", "/api/gen/audio/clone-vip",
            "/api/gen/digital-human-v2/audio-upload",
            "/api/gen/digital-human-v2/material-resolve",
            "/api/gen/digital-human-v2/history?limit=20",
            "/api/gen/script_to_video/material-upload",
            "reference_images:photoData?[photoData]:[]", "motion:'low'", "speed:1,pitch:0,volume:1", "delivery:'natural'", "resolution:'1080p'", "ratio:'9:16'", "subtitle:false",
            "body.allow_ai_materials=allowAi",
            "body.customer_upload_ids=uploadIds",
            "fields.digital_human_customer_upload_ids=plan.customer_upload_ids||[]",
            "DigitalHumanMaterialState.restore(state.customerUploads,state.phase)",
            "DigitalHumanOneClickState.persistableMaterials(state,customerUploads,materialRecoveryValid)",
            "resume:DigitalHumanOneClickState.resumeJob",
            "return DigitalHumanOneClickState.resumeJob({jobId:Number(state.compose_job)||0",
            "DigitalHumanMaterialState.canChange(state.phase,customerMaterialBusy)",
            "DigitalHumanMaterialState.canAnalyze(state.phase,customerMaterialBusy)",
            "DigitalHumanMaterialState.canStart(state.phase,customerMaterialBusy,!!plan)",
            "DigitalHumanMaterialState.restoreStartButton($('start'),state.phase)",
            "DigitalHumanSetupState.runJobs({items:items",
            "epoch:epoch,currentEpoch:function(){return generationEpoch;}",
            "return generateMaterials(epoch)",
            "digital_human_material_v2", "video_job_ids", "material_job_ids",
            "material_asset_ids", "/api/gen/digital-human-v2/consent",
            "digital_human_consent_token",
            "photo_sha256", "voice_sha256", "consent_version",
            'id="historyList"', "loadDigitalHumanHistory()",
        ):
            self.assertIn(marker, page)
        self.assertIn("合成本身 0 点", page)
        self.assertIn("Whisper 字幕", page)
        self.assertIn("分析并预览方案", page)
        self.assertIn("确认方案并生成", page)
        self.assertIn(
            "顾客上传素材（必须全部使用） → 黄雀已审核素材库 → AI 生成图片",
            page,
        )
        self.assertNotIn("测试服固定本地素材库", page)
        self.assertIn("上传的每一张图片都会按顺序直接进入成片", page)
        self.assertIn('id="allowAiMaterials" type="checkbox"', page)
        self.assertNotIn('id="allowAiMaterials" type="checkbox" checked', page)
        self.assertNotIn("全网公开可用素材", page)
        self.assertIn('id="voiceSource"', page)
        self.assertIn("已有声音无需再次复刻", page)
        self.assertIn("DigitalHumanVoiceState.resolveLoaded(before,data.items||[])", page)
        self.assertIn("DigitalHumanVoiceState.loadFailed(before,error.message)", page)
        self.assertIn("if(!clone&&state.voiceKey){setStep('voice','done','已复用')", page)
        self.assertIn("重新上传样音并复刻", page)
        self.assertIn('id="restartSetup"', page)
        self.assertIn("$('restartSetup').onclick=handleRestartSetup", page)
        self.assertNotIn("$('restartSetup').onclick=restartSetup", page)
        self.assertIn("放弃上次任务并重新设置", page)
        self.assertIn("重新生成可能再次扣点", page)
        self.assertIn("声音来源和样音已随当前任务锁定", page)
        self.assertIn("DigitalHumanSetupState.applyControls(setupNodes(),activePhase,state)", page)
        self.assertIn("DigitalHumanSetupState.restart(state,window.confirm", page)
        self.assertIn("DigitalHumanSubmit.withSecurityRetry", page)
        self.assertIn("DigitalHumanSubmit.withCapacityRetry", page)
        self.assertIn("return DigitalHumanSetupState.runJobs({items:items", page)
        self.assertIn("DigitalHumanSubmit.describe(error)", page)
        self.assertIn("安全检查重试 '+attempt+'/2", page)
        self.assertIn("{material:'materials',video:'talking'}", page)
        voice_state = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-voice-state.js").read_text(encoding="utf-8")
        self.assertNotIn("error.code==='voice_clone_in_progress'", voice_state)
        self.assertIn("throw error;", voice_state)
        self.assertIn("DigitalHumanVoiceState.restoredCloneDecision", page)
        self.assertIn("function restoredCloneDecision(response,markers)", voice_state)
        self.assertIn("DigitalHumanVoiceState.runCloneRecovery", page)
        self.assertNotIn("if(data.status==='ready')", page)
        for step in ("plan", "voice", "materials", "talking", "compose"):
            self.assertIn('data-step-error="%s"' % step, page)
        self.assertIn("DigitalHumanSetupState.validatePhotoAttachment", page)
        self.assertIn("function validatePhotoRecovery(photo,epoch)", page)
        self.assertLess(
            page.index("validatePhotoRecovery(photo,epoch)", page.index("function start()")),
            page.index("heygenPreflight(epoch)", page.index("function start()")),
        )
        self.assertIn("state.materialAssets=Array.isArray(state.materialAssets)?state.materialAssets:[]", page)
        self.assertIn("setStep('talking','failed','需重新附加')", page)
        self.assertIn("restoreFailedSteps(photoRecovery,restoredMaterials.valid||materialJobsRecoverable)", page)
        self.assertIn("上次数字人口播子任务失败，点击继续后仅重试失败项", page)
        self.assertIn("上次成片合成任务失败，点击继续后仅重试合成", page)
        self.assertIn("state.voiceSha256||digest!==state.voiceSha256", page)
        self.assertIn("state.voiceCloneSubmitted=true", page)
        self.assertIn("state.voiceCloneAccepted=true;save()", page)
        self.assertIn("state.voiceCloneProgress=true;save()", page)
        self.assertIn("voiceCloneKey:''", page)
        self.assertIn("if(file&&!state.voiceCloneKey){state.voiceCloneKey=key('dh-voice-clone');save();}", page)
        self.assertIn("headers:{'Idempotency-Key':state.voiceCloneKey}", page)
        self.assertIn("DigitalHumanVoiceState.submitCloneWithIdempotency", page)
        self.assertIn("function rotateFailedVoiceAttempt(response)", page)
        self.assertIn("state.voiceCloneKey=transition.key;state.voiceCloneSubmitted=false;state.voiceCloneAccepted=false;state.voiceCloneProgress=false;save()", page)
        self.assertIn("if(normalized.status==='failed'){if(preserveFailedClone)return response;if(rotateFailedVoiceAttempt(response))", page)
        self.assertIn("{preserveFailedClone:true}", page)
        self.assertIn("if(preserveFailedClone)return response", page)
        self.assertIn("if(normalized.status==='failed'){rotateFailedVoiceAttempt(response);decision={action:'reattach'", page)
        self.assertIn("if(error&&error.voiceAttemptRotated)throw error", page)
        self.assertIn("forceSubmit:!!file&&(!hadSubmitted||!hadAccepted)", page)
        self.assertIn("state.phase==='approved'&&clone&&!state.voiceCloneAccepted&&!voice", page)
        self.assertIn("请重新附加本次授权的原声音样本后重试", page)
        self.assertIn("尚未创建任务、未扣点", (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-submit.js").read_text(encoding="utf-8"))
        self.assertNotIn('data-step-error="gestures"', page)
        self.assertIn("scrollIntoView", page)
        self.assertIn("提交后系统将记录本次授权时间及素材校验值", page)
        domain = (Path(__file__).resolve().parents[1] / "server" / "content_domains" / "digital_human_oneclick.py").read_text(encoding="utf-8")
        self.assertIn('cleaned["digital_human_consent_id"]', domain)
        self.assertNotIn("选择剪辑风格", page)

    def test_start_preflights_heygen_before_consent_and_child_jobs(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        preflight = page.index("function heygenPreflight(epoch)")
        start = page.index("function start()", preflight)
        self.assertLess(page.index("heygenPreflight(epoch)", start), page.index("prepareConsent(photo,voice,clone,epoch)", start))
        self.assertIn("HeyGen 通道检查失败，未扣点", page)
        self.assertNotIn("_digitalHumanOriginalStart", page)

    def test_primary_actions_are_visible_before_long_material_fields(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        self.assertLess(page.index('id="analyze"'), page.index('id="photo"'))
        self.assertLess(page.index('id="start"'), page.index('id="script"'))
        self.assertIn(".action-dock{position:sticky", page)
        self.assertIn("资料填好后，先分析时长与镜头方案", page)
        self.assertIn("建议腰部以上，双手自然放在身体前方", page)
        self.assertIn("原图将直接用于全部真人出镜片段", page)
        self.assertNotIn('name="segmentCount"', page)

    def test_director_flush_workspace_remains_vertically_scrollable(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        self.assertIn('body .hq-main-scroll-flush{overflow-x:hidden;overflow-y:auto}', page)
        self.assertIn('.hq-content[data-active="script"]{min-height:max-content}', page)

    def test_consent_enforcement_runs_before_security_validation_and_charge(self):
        root = Path(__file__).resolve().parents[1]
        core = (root / "server" / "content_domains" / "core.py").read_text(
            encoding="utf-8"
        )
        paid = core[core.index("if kind is not None:"):]
        consent_at = paid.index("verify_child_submission")
        security_at = paid.index("miniprogram_security.check_payload(body)")
        charge_at = paid.index("points_domain.cost_of(kind, body)")
        self.assertLess(consent_at, security_at)
        self.assertLess(security_at, charge_at)
        clone = core[
            core.index('if p == "/api/gen/audio/clone-vip"'):
            core.index('if p == "/api/gen/video/avatar-name"')
        ]
        self.assertLess(
            clone.index("verify_clone_submission"),
            clone.index("validate_clone_vip_payload"),
        )
        entry = (root / "server" / "content_api.py").read_text(encoding="utf-8")
        self.assertIn("digital_human_oneclick.init_db()", entry)

    def test_clone_vip_idempotency_starts_provider_once_and_replays_lost_response(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path: sys.path.insert(0, server_dir)
        core = importlib.import_module("content_domains.core")
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "clone-idempotency.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        request = {
            "digital_human_pipeline": domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
            "clone_attempt_id": "dh-voice-clone-stable-001",
        }

        class Handler:
            path = "/api/gen/audio/clone-vip"
            headers = {"Idempotency-Key": "dh-voice-clone-stable-001"}
            def __init__(self): self.sent = None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(request)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        validated = dict(request, digital_human_consent_id="dhc_" + "1" * 32)
        audio = SimpleNamespace(
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

        with mock.patch("content_domains.core._domains", return_value=(audio, SimpleNamespace(), SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(domain, "verify_clone_submission", return_value=validated), \
             mock.patch.object(core, "jdb", connection), \
             mock.patch("content_domains.core.threading.Thread", Thread):
            first = Handler(); core.H.do_POST(first)
            replay = Handler(); core.H.do_POST(replay)

        self.assertEqual(200, first.sent[0])
        self.assertEqual(first.sent, replay.sent)
        self.assertEqual(1, audio.mark_clone_training.call_count)
        self.assertEqual(1, len(started))
        tmp.cleanup()

    def test_clone_vip_idempotency_rejects_processing_duplicate_and_payload_conflict(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path: sys.path.insert(0, server_dir)
        core = importlib.import_module("content_domains.core")
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "clone-idempotency.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        key = "dh-voice-clone-stable-002"
        request = {"slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3"}
        claim_body = dict(request, digital_human_consent_id="dhc_" + "2" * 32)
        core.submission_idempotency.begin(connection, "yuelei", "/api/gen/audio/clone-vip", key, claim_body)

        class Handler:
            path = "/api/gen/audio/clone-vip"
            headers = {"Idempotency-Key": key}
            def __init__(self, body): self.body, self.sent = body, None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(self.body)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        audio = SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            validate_clone_vip_payload=mock.Mock(), mark_clone_training=mock.Mock(),
            clone_vip_voice_background=mock.Mock(),
        )
        def verified(body, _username):
            return dict(body, digital_human_consent_id="dhc_" + "2" * 32)
        with mock.patch("content_domains.core._domains", return_value=(audio, SimpleNamespace(), SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(domain, "verify_clone_submission", side_effect=verified), \
             mock.patch.object(core, "jdb", connection):
            duplicate = Handler(request); core.H.do_POST(duplicate)
            conflict = Handler(dict(request, audio="b3RoZXI=")); core.H.do_POST(conflict)

        self.assertEqual((409, "idempotency_in_progress"), (duplicate.sent[0], duplicate.sent[1]["code"]))
        self.assertEqual((409, "idempotency_conflict"), (conflict.sent[0], conflict.sent[1]["code"]))
        audio.validate_clone_vip_payload.assert_not_called()
        audio.mark_clone_training.assert_not_called()
        tmp.cleanup()

    def test_clone_vip_processing_provider_training_never_restarts_provider(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path: sys.path.insert(0, server_dir)
        core = importlib.import_module("content_domains.core")
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "clone-provider-training.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        key = "dh-voice-clone-provider-training-001"
        request = {"digital_human_pipeline": domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
            "clone_attempt_id": key}
        verified = dict(request, digital_human_consent_id="dhc_" + "3" * 32)
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
        audio = SimpleNamespace(
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
        with mock.patch("content_domains.core._domains", return_value=(audio, SimpleNamespace(), SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(domain, "verify_clone_submission", return_value=verified), \
             mock.patch.object(core, "jdb", connection), \
             mock.patch("content_domains.core.threading.Thread") as thread:
            handler = Handler(); core.H.do_POST(handler)
        self.assertEqual((409, "idempotency_in_progress"),
            (handler.sent[0], handler.sent[1]["code"]))
        audio.check_clone_status.assert_called_once_with("yuelei", "slot_123", key)
        audio.mark_clone_training.assert_not_called()
        thread.assert_not_called()
        tmp.cleanup()

    def test_plan_uses_natural_low_motion_profile_for_every_segment(self):
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        payload = domain.plan("\u5f00\u573a\u5148\u6293\u4f4f\u6ce8\u610f\u529b\u3002\u63a5\u7740\u628a\u65b9\u6848\u8bb2\u6e05\u695a\u3002\u6700\u540e\u7ed9\u51fa\u884c\u52a8\u53f7\u53ec\u3002")
        profiles = [item["speech_profile"] for item in payload["segments"]]
        expected = domain.natural_mouth_talking_profile()
        self.assertEqual([expected, expected, expected], profiles)

    def test_provider_boundary_enforces_profile_for_both_oneclick_pipelines(self):
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        for pipeline in (domain.CONSENT_PURPOSE, "digital_human_material_v2"):
            with self.subTest(pipeline=pipeline):
                original = {
                    "digital_human_pipeline": pipeline,
                    "digital_human_stage": "talking",
                    "resolution": "720p",
                    "ratio": "16:9",
                    "motion": "high",
                    "speed": 1.5,
                    "pitch": 4,
                    "volume": 2,
                    "delivery": "energetic",
                    "subtitle": True,
                }
                enforced = domain.enforce_natural_mouth_talking_profile(original)
                self.assertEqual(domain.natural_mouth_talking_profile(), {
                    key: enforced[key]
                    for key in domain.natural_mouth_talking_profile()
                })
                self.assertEqual("high", original["motion"])

    def test_provider_boundary_does_not_change_non_talking_jobs(self):
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        original = {
            "digital_human_pipeline": domain.CONSENT_PURPOSE,
            "digital_human_stage": "material",
            "motion": "high",
        }
        self.assertEqual(original, domain.enforce_natural_mouth_talking_profile(original))

    def test_v2_server_uses_the_same_natural_mouth_profile(self):
        source = (Path(__file__).resolve().parents[1] / "server" / "content_domains" /
                  "digital_human_v2.py").read_text(encoding="utf-8")
        self.assertIn("cleaned.update(legacy.natural_mouth_talking_profile())", source)
        self.assertNotIn('"motion": "high" if segment["role"]', source)

    def test_photo_and_precision_modes_live_under_the_director(self):
        root = Path(__file__).resolve().parents[1]
        video_page = (root / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        photo_page = (root / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        self.assertNotIn('href="digital-human-oneclick.html"', video_page)
        self.assertIn('data-function="talking">数字化 IP', video_page)
        self.assertIn('data-active="script"', photo_page)
        self.assertIn('id="dhPhotoModeTab"', photo_page)
        self.assertIn('id="dhVideoModeTab"', photo_page)
        self.assertIn('id="dhVideoMode"', photo_page)
        self.assertIn('真人视频 Precision', photo_page)
        self.assertIn("digital-human-unified.js", photo_page)
        self.assertNotIn("私域批量成片", photo_page)
        self.assertNotIn("照片数字人", photo_page)


if __name__ == "__main__":
    unittest.main()
