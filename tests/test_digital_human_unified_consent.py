import base64
import importlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DigitalHumanUnifiedConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(ROOT / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.domain = importlib.import_module("content_domains.digital_human_oneclick")
        cls.audio = importlib.import_module("content_domains.audio")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.consent_db = pathlib.Path(self.temporary.name) / "consent.db"
        self.jobs_db = pathlib.Path(self.temporary.name) / "jobs.db"
        with closing(self._job_connection()) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,username TEXT,kind TEXT,status TEXT,
                payload TEXT,deleted INTEGER DEFAULT 0
            )""")
            connection.commit()
        self.cdb_patch = mock.patch.object(self.domain, "cdb", self._consent_connection)
        self.jdb_patch = mock.patch.object(self.domain, "jdb", self._job_connection)
        self.cdb_patch.start()
        self.jdb_patch.start()
        self.addCleanup(self.cdb_patch.stop)
        self.addCleanup(self.jdb_patch.stop)
        self.addCleanup(self.temporary.cleanup)
        self.sample = b"authoritative voice sample"
        self.script = "这是一段绑定真人源视频、样音、槽位和完整制作流程的测试口播文案。"
        self.slot = {
            "slot_id": "slot-1", "status": "active", "voice_name": None,
            "voice_id": None, "provider_voice": None, "reclone_count": 0,
            "updated_at": 100, "voice_updated_at": None,
        }
        self.consent = self._create()

    def _consent_connection(self):
        connection = sqlite3.connect(self.consent_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _job_connection(self):
        connection = sqlite3.connect(self.jobs_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _payload(self, **changes):
        payload = {
            "confirmed": True,
            "consent_version": self.domain.UNIFIED_VIDEO_CONSENT_VERSION,
            "purpose": self.domain.UNIFIED_VIDEO_CONSENT_PURPOSE,
            "run_id": "dhv-unified-test-001",
            "script": self.script,
            "slot_id": "slot-1",
            "overwrite_confirmed": False,
            "overwrite_voice_name": "",
            "video_asset_id": 41,
        }
        payload.update(changes)
        return payload

    def _create(self, payload=None, slot=None, now=None):
        return self.domain.create_unified_video_consent(
            payload or self._payload(), "yuelei", "test-signing-secret",
            video_asset_id=41, video_sha256="a" * 64,
            sample_sha256=self.domain.hashlib.sha256(self.sample).hexdigest(),
            slot_preimage=slot or self.slot, now=now,
        )

    def _metadata(self, stage, **changes):
        payload = {
            "digital_human_pipeline": self.domain.UNIFIED_VIDEO_CONSENT_PURPOSE,
            "digital_human_stage": stage,
            "digital_human_run_id": self.consent["run_id"],
            "digital_human_consent_token": self.consent["consent_token"],
            "digital_human_script": self.script,
            "digital_human_video_asset_id": "41",
            "digital_human_video_sha256": "a" * 64,
            "digital_human_sample_sha256": self.domain.hashlib.sha256(self.sample).hexdigest(),
            "digital_human_slot_id": "slot-1",
            "clone_attempt_id": self.consent["clone_attempt_id"],
        }
        payload.update(changes)
        return payload

    def test_ready_slot_requires_exact_named_overwrite_confirmation(self):
        ready = {
            "slot_id": "ready-1", "status": "ready", "voice_name": "已有岳磊音色",
            "voice_id": 10, "provider_voice": "cosyvoice-ready-10",
            "reclone_count": 1, "updated_at": 100, "voice_updated_at": 100,
        }
        payload = self._payload(
            run_id="dhv-ready-test-001", slot_id="ready-1",
        )
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self._create(payload, ready)
        self.assertEqual("voice_overwrite_confirmation_required", caught.exception.code)
        payload.update(overwrite_confirmed=True, overwrite_voice_name="伪造名称")
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self._create(payload, ready)
        self.assertEqual("voice_overwrite_binding_mismatch", caught.exception.code)
        payload["overwrite_voice_name"] = "已有岳磊音色"
        consent = self._create(payload, ready)
        self.assertTrue(consent["clone_attempt_id"].startswith("dh-video-clone-"))

    def test_existing_consent_table_migrates_slot_version_columns(self):
        legacy = pathlib.Path(self.temporary.name) / "legacy-consent.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("""CREATE TABLE digital_human_video_consents(
                id TEXT PRIMARY KEY,username TEXT NOT NULL,run_id TEXT NOT NULL,
                consent_version TEXT NOT NULL,purpose TEXT NOT NULL,
                video_asset_id INTEGER NOT NULL,video_sha256 TEXT NOT NULL,
                sample_sha256 TEXT NOT NULL,slot_id TEXT NOT NULL,
                slot_preimage_status TEXT NOT NULL,
                slot_preimage_voice_name TEXT NOT NULL,
                script_sha256 TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,UNIQUE(username,run_id))""")
            self.domain._ensure_unified_video_consent_table(connection)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(digital_human_video_consents)"
                )
            }
        self.assertTrue({
            "slot_preimage_voice_id", "slot_preimage_provider_voice",
            "slot_preimage_reclone_count", "slot_preimage_updated_at",
            "slot_preimage_voice_updated_at", "slot_preimage_version",
        }.issubset(columns))

    def test_forged_consent_boolean_and_slot_drift_are_rejected(self):
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self._create(self._payload(confirmed=False))
        self.assertEqual("consent_required", caught.exception.code)

        body = self._metadata(
            "voice_clone", slot_id="slot-1",
            audio=base64.b64encode(self.sample).decode("ascii"), audio_format="mp3",
        )
        changed_slot = dict(
            self.slot, status="ready", voice_name="后来写入的音色",
            voice_id=12, provider_voice="cosyvoice-later-12",
            reclone_count=1, updated_at=200, voice_updated_at=200,
        )
        with mock.patch.object(
                self.audio, "clone_attempt_snapshot", return_value={"action": "mismatch"}), \
             mock.patch.object(
                self.audio, "list_user_audio_voice_slots", return_value=[changed_slot]):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.verify_unified_video_clone_submission(body, "yuelei")
        self.assertEqual("consent_slot_changed", caught.exception.code)

    def test_clone_rejects_forged_boolean_cross_account_and_changed_bindings(self):
        body = self._metadata(
            "voice_clone", slot_id="slot-1",
            audio=base64.b64encode(self.sample).decode("ascii"), audio_format="mp3",
        )
        with mock.patch.object(
                self.audio, "clone_attempt_snapshot", return_value={"action": "mismatch"}), \
             mock.patch.object(
                self.audio, "list_user_audio_voice_slots", return_value=[self.slot]):
            cleaned = self.domain.verify_unified_video_clone_submission(body, "yuelei")
            self.assertEqual(self.consent["consent_id"], cleaned["digital_human_consent_id"])
            for key, value, code in (
                ("digital_human_video_asset_id", "42", "consent_binding_mismatch"),
                ("digital_human_sample_sha256", "b" * 64, "consent_binding_mismatch"),
                ("digital_human_slot_id", "slot-2", "consent_binding_mismatch"),
                ("digital_human_script", self.script + "篡改", "consent_plan_mismatch"),
            ):
                forged = dict(body, **{key: value})
                with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                    self.domain.verify_unified_video_clone_submission(forged, "yuelei")
                self.assertEqual(code, caught.exception.code)
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.verify_unified_video_clone_submission(body, "other-user")
            self.assertEqual("consent_invalid", caught.exception.code)

    def test_expired_consent_and_audio_reclone_version_are_rejected_before_paid_work(self):
        audio = self._metadata("full_audio", text=self.script, voice="vip_slot-1")
        with mock.patch.object(
                self.audio, "clone_attempt_snapshot",
                return_value={
                    "action": "ready", "attempt_id": self.consent["clone_attempt_id"],
                    "voice_id": 21, "provider_voice": "cosyvoice-confirmed-21",
                    "voice_updated_at": 201,
                }):
            cleaned, record = self.domain.verify_unified_video_child_submission(
                audio, "yuelei", "audio",
            )
        self.assertEqual(self.consent["consent_id"], record["id"])
        self.assertNotIn("digital_human_consent_token", cleaned)
        self.assertEqual("cosyvoice-confirmed-21", cleaned["digital_human_provider_voice"])
        with closing(self._consent_connection()) as connection:
            connection.execute(
                "UPDATE digital_human_video_consents SET slot_preimage_version=''"
            )
            connection.commit()
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_unified_video_child_submission(audio, "yuelei", "audio")
        self.assertEqual("consent_slot_version_invalid", caught.exception.code)
        with closing(self._consent_connection()) as connection:
            connection.execute(
                "UPDATE digital_human_video_consents SET slot_preimage_version=?,expires_at=?",
                (self.domain._unified_slot_preimage(self.slot)["slot_preimage_version"],
                 int(time.time()) - 1),
            )
            connection.commit()
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_unified_video_child_submission(audio, "yuelei", "audio")
        self.assertEqual("consent_expired", caught.exception.code)

    def test_precision_requires_audio_job_from_the_same_signed_consent(self):
        audio_payload = self._metadata("full_audio", text=self.script, voice="vip_slot-1")
        with mock.patch.object(
                self.audio, "clone_attempt_snapshot",
                return_value={
                    "action": "ready", "attempt_id": self.consent["clone_attempt_id"],
                    "voice_id": 21, "provider_voice": "cosyvoice-confirmed-21",
                    "voice_updated_at": 201,
                }):
            cleaned_audio, _ = self.domain.verify_unified_video_child_submission(
                audio_payload, "yuelei", "audio",
            )
        with closing(self._job_connection()) as connection:
            connection.execute(
                "INSERT INTO jobs(id,username,kind,status,payload,deleted) VALUES(?,?,?,?,?,0)",
                (71, "yuelei", "audio", "done", json.dumps(cleaned_audio)),
            )
            connection.commit()
        precision = self._metadata(
            "precision", mode="lipsync", video_asset_id=41, audio_asset_id=91,
        )
        with mock.patch.object(
                self.audio, "get_audio_asset",
                return_value={"id": 91, "job_id": 71, "username": "yuelei"}):
            cleaned, record = self.domain.verify_unified_video_child_submission(
                precision, "yuelei", "video",
            )
            self.assertEqual(self.consent["consent_id"], record["id"])
            with closing(self._job_connection()) as connection:
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=71",
                    (json.dumps(dict(cleaned_audio, digital_human_consent_id="dhvc_forged")),),
                )
                connection.commit()
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.verify_unified_video_child_submission(
                    precision, "yuelei", "video",
                )
            self.assertEqual("consent_audio_mismatch", caught.exception.code)

    def test_full_audio_requires_the_same_ready_clone_attempt(self):
        payload = self._metadata("full_audio", text=self.script, voice="vip_slot-1")
        for action in ("mismatch", "processing", "provider_training", "failed"):
            with self.subTest(action=action), mock.patch.object(
                    self.audio, "clone_attempt_snapshot",
                    return_value={"action": action}) as snapshot:
                with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                    self.domain.verify_unified_video_child_submission(
                        payload, "yuelei", "audio",
                    )
                self.assertEqual("consent_clone_not_ready", caught.exception.code)
                snapshot.assert_called_once_with(
                    "yuelei", "slot-1", self.consent["clone_attempt_id"],
                )
        with mock.patch.object(
                self.audio, "clone_attempt_snapshot",
                return_value={
                    "action": "ready", "voice_id": 21,
                    "provider_voice": "cosyvoice-confirmed-21",
                    "voice_updated_at": 201,
                }) as snapshot:
            cleaned, record = self.domain.verify_unified_video_child_submission(
                payload, "yuelei", "audio",
            )
        self.assertEqual(self.consent["consent_id"], record["id"])
        self.assertEqual(self.consent["consent_id"], cleaned["digital_human_consent_id"])
        self.assertEqual("cosyvoice-confirmed-21", cleaned["digital_human_provider_voice"])
        snapshot.assert_called_once_with(
            "yuelei", "slot-1", self.consent["clone_attempt_id"],
        )

    def test_ready_slot_same_name_and_status_rejects_version_drift(self):
        ready = {
            "slot_id": "slot-ready", "status": "ready",
            "voice_name": "真人视频原声音色", "voice_id": 10,
            "provider_voice": "cosyvoice-provider-10", "reclone_count": 1,
            "updated_at": 100, "voice_updated_at": 100,
        }
        payload = self._payload(
            run_id="dhv-ready-version-001", slot_id="slot-ready",
            overwrite_confirmed=True,
            overwrite_voice_name="真人视频原声音色",
        )
        self.slot = ready
        self.consent = self._create(payload, ready)
        clone = self._metadata(
            "voice_clone", slot_id="slot-ready",
            digital_human_slot_id="slot-ready",
            audio=base64.b64encode(self.sample).decode("ascii"),
            audio_format="mp3",
        )
        changed = dict(
            ready, voice_id=99, provider_voice="cosyvoice-provider-99",
            reclone_count=2, updated_at=200, voice_updated_at=200,
        )
        with mock.patch.object(
                self.audio, "clone_attempt_snapshot", return_value={"action": "mismatch"}), \
             mock.patch.object(
                self.audio, "list_user_audio_voice_slots", return_value=[changed]):
            with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
                self.domain.verify_unified_video_clone_submission(clone, "yuelei")
        self.assertEqual("consent_slot_changed", caught.exception.code)

    def test_mark_clone_training_atomically_claims_one_slot_version(self):
        audio_db = pathlib.Path(self.temporary.name) / "audio.db"

        def audio_connection():
            connection = sqlite3.connect(audio_db, timeout=5)
            connection.row_factory = sqlite3.Row
            return connection

        with closing(audio_connection()) as connection:
            connection.execute("""CREATE TABLE audio_voice_slots(
                id INTEGER PRIMARY KEY,username TEXT,slot_id TEXT,status TEXT,
                voice_id INTEGER,reclone_count INTEGER,clone_started_at INTEGER,
                updated_at INTEGER,clone_upload_at INTEGER,clone_error TEXT,
                clone_attempt_id TEXT,clone_attempt_phase TEXT,
                clone_attempt_updated_at INTEGER)""")
            connection.execute("""CREATE TABLE audio_voices(
                id INTEGER PRIMARY KEY,username TEXT,scope TEXT,voice_key TEXT,
                display_name TEXT,provider_voice TEXT,slot_id TEXT,
                created_at INTEGER,updated_at INTEGER,
                UNIQUE(username,scope,voice_key))""")
            connection.execute("""INSERT INTO audio_voices VALUES(
                10,'yuelei','personal','vip_slot-ready','真人视频原声音色',
                'cosyvoice-provider-10','slot-ready',100,100)""")
            connection.execute("""INSERT INTO audio_voice_slots VALUES(
                1,'yuelei','slot-ready','ready',10,1,90,100,100,NULL,
                'previous-attempt','ready',100)""")
            connection.commit()
        expected = {
            "status": "ready", "voice_name": "真人视频原声音色",
            "voice_id": 10, "provider_voice": "cosyvoice-provider-10",
            "reclone_count": 1, "updated_at": 100,
            "voice_updated_at": 100, "version": "signed-version",
        }
        successes = []
        failures = []
        barrier = threading.Barrier(2)

        def claim(attempt_id):
            barrier.wait()
            try:
                successes.append(self.audio.mark_clone_training(
                    "yuelei", "slot-ready", "真人视频原声音色",
                    attempt_id, expected_preimage=expected,
                ))
            except Exception as exc:
                failures.append(exc)

        with mock.patch.object(self.audio, "adb", audio_connection), \
             mock.patch.object(self.audio, "clear_voice_preview", return_value=0):
            workers = [
                threading.Thread(target=claim, args=("attempt-concurrent-a",)),
                threading.Thread(target=claim, args=("attempt-concurrent-b",)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(failures))
        with closing(audio_connection()) as connection:
            row = connection.execute(
                "SELECT status,clone_attempt_id,reclone_count FROM audio_voice_slots"
            ).fetchone()
        self.assertEqual("training", row["status"])
        self.assertIn(row["clone_attempt_id"], {
            "attempt-concurrent-a", "attempt-concurrent-b",
        })
        self.assertEqual(2, row["reclone_count"])

        with closing(audio_connection()) as connection:
            connection.execute("""UPDATE audio_voice_slots SET
                status='ready',voice_id=10,reclone_count=3,updated_at=300,
                clone_attempt_id='newer-attempt',clone_attempt_phase='ready'""")
            connection.execute("""UPDATE audio_voices SET
                provider_voice='cosyvoice-provider-99',updated_at=300 WHERE id=10""")
            connection.commit()
        with mock.patch.object(self.audio, "adb", audio_connection), \
             mock.patch.object(self.audio, "clear_voice_preview", return_value=0):
            with self.assertRaises(self.audio.CloneAttemptError) as caught:
                self.audio.mark_clone_training(
                    "yuelei", "slot-ready", "真人视频原声音色",
                    "attempt-stale-preimage", expected_preimage=expected,
                )
        self.assertEqual("clone_slot_preimage_mismatch", caught.exception.code)

    def test_core_verifies_consent_before_security_idempotency_and_charge(self):
        core = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        paid = core[core.index('if p.startswith("/api/gen/")'):]
        consent = paid.index("verify_child_submission_with_record")
        security = paid.index("miniprogram_security.check_payload")
        idempotency = paid.index("_idempotency_begin", security)
        charge = paid.index("create_paid_job", idempotency)
        self.assertLess(consent, security)
        self.assertLess(security, idempotency)
        self.assertLess(idempotency, charge)

    def test_sample_entry_rejects_forged_boolean_then_persists_server_hashes(self):
        core = importlib.import_module("content_domains.core")
        request = {
            "video_asset_id": 41,
            "slot_id": "slot-1",
            "script": self.script,
            "run_id": "dhv-unified-entry-001",
            "consent_confirmed": False,
            "consent_version": self.domain.UNIFIED_VIDEO_CONSENT_VERSION,
            "purpose": self.domain.UNIFIED_VIDEO_CONSENT_PURPOSE,
            "overwrite_confirmed": False,
            "overwrite_voice_name": "",
        }

        class Handler:
            path = "/api/gen/video/lipsync-voice-sample"
            headers = {}

            def __init__(self):
                self.sent = None

            def _token(self):
                return "token"

            def _json_body_strict(self):
                return dict(request)

            def _send(self, status, payload):
                self.sent = (status, payload)
                return self.sent

        audio_domain = SimpleNamespace(
            list_user_audio_voice_slots=mock.Mock(return_value=[self.slot]),
        )
        video_domain = SimpleNamespace(
            extract_lipsync_voice_sample=mock.Mock(return_value={
                "video_asset_id": 41,
                "video_sha256": "a" * 64,
                "audio": base64.b64encode(self.sample).decode("ascii"),
                "audio_format": "mp3",
                "sha256": self.domain.hashlib.sha256(self.sample).hexdigest(),
                "duration": 1.25,
            }),
        )
        patches = (
            mock.patch.object(core, "_domains", return_value=(
                audio_domain, SimpleNamespace(), video_domain,
            )),
            mock.patch.object(core.cli_gateway, "handle_image_upload", return_value=False),
            mock.patch.object(core.cli_gateway, "handle_quote", return_value=False),
            mock.patch.object(core, "verify", return_value={"username": "yuelei"}),
            mock.patch.object(core, "_must_change_password", return_value=False),
            mock.patch.object(core.feature_flags, "require_enabled"),
            mock.patch.dict(os.environ, {"HQ_INTERNAL_TOKEN": "test-signing-secret"}),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rejected = Handler()
            core.H.do_POST(rejected)
            self.assertEqual(403, rejected.sent[0])
            video_domain.extract_lipsync_voice_sample.assert_not_called()

            request["consent_confirmed"] = True
            accepted = Handler()
            core.H.do_POST(accepted)

        self.assertEqual(200, accepted.sent[0])
        self.assertEqual("a" * 64, accepted.sent[1]["sample"]["video_sha256"])
        self.assertTrue(accepted.sent[1]["consent"]["consent_token"].startswith("dhvc_"))
        video_domain.extract_lipsync_voice_sample.assert_called_once_with("yuelei", 41)


if __name__ == "__main__":
    unittest.main()
