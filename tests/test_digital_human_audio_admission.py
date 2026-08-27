from contextlib import closing
import io
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import digital_human_v2, script_to_video


class DigitalHumanAudioAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.consent_db = self.root / "consent.db"
        self.jobs_db = self.root / "jobs.db"
        with closing(self.db()) as connection:
            digital_human_v2._ensure_audio_table(connection)
            connection.execute("""CREATE TABLE digital_human_consents(
                username TEXT NOT NULL,run_id TEXT NOT NULL,
                voice_ref TEXT NOT NULL DEFAULT '',expires_at INTEGER NOT NULL DEFAULT 0
            )""")
            connection.commit()
        with closing(self.jobs()) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY,username TEXT,payload TEXT,result TEXT
            )""")
            connection.commit()
        self.out_patch = mock.patch.object(digital_human_v2, "OUT_DIR", self.root / "out")
        self.out_patch.start()
        digital_human_v2.OUT_DIR.mkdir()

    def tearDown(self):
        self.out_patch.stop()
        self.temporary.cleanup()

    def db(self):
        connection = sqlite3.connect(self.consent_db)
        connection.row_factory = sqlite3.Row
        return connection

    def jobs(self):
        connection = sqlite3.connect(self.jobs_db)
        connection.row_factory = sqlite3.Row
        return connection

    def test_user_global_and_daily_limits_are_durable_before_write(self):
        first = digital_human_v2._begin_audio_admission(
            "alice", "192.0.2.1", 10, self.db, now=2_000_000_000,
        )
        with self.assertRaisesRegex(digital_human_v2.DigitalHumanRequestError, "繁忙") as caught:
            digital_human_v2._begin_audio_admission(
                "alice", "192.0.2.1", 10, self.db, now=2_000_000_000,
            )
        self.assertEqual(429, caught.exception.status)
        second = digital_human_v2._begin_audio_admission(
            "bob", "192.0.2.2", 10, self.db, now=2_000_000_000,
        )
        with self.assertRaises(digital_human_v2.DigitalHumanRequestError):
            digital_human_v2._begin_audio_admission(
                "carol", "192.0.2.3", 10, self.db, now=2_000_000_000,
            )
        digital_human_v2._finish_audio_admission(first, False, self.db)
        digital_human_v2._finish_audio_admission(second, False, self.db)

        for index in range(digital_human_v2._AUDIO_USER_DAILY_COUNT):
            token = digital_human_v2._begin_audio_admission(
                "alice", "198.51.100.%d" % index, 10, self.db,
                now=2_000_000_100 + index,
            )
            digital_human_v2._finish_audio_admission(token, True, self.db)
        with self.assertRaisesRegex(digital_human_v2.DigitalHumanRequestError, "额度"):
            digital_human_v2._begin_audio_admission(
                "alice", "203.0.113.1", 10, self.db, now=2_000_000_200,
            )

    def test_stale_restart_lease_is_reclaimed(self):
        digital_human_v2._begin_audio_admission(
            "alice", "192.0.2.1", 10, self.db, now=2_000_000_000,
        )
        replacement = digital_human_v2._begin_audio_admission(
            "alice", "192.0.2.1", 10, self.db,
            now=2_000_000_000 + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
        )
        self.assertTrue(replacement.startswith("dhaa_"))

    def _expired_admission_with_directory(self, username="alice"):
        now = 2_000_000_000
        admission_id = digital_human_v2._begin_audio_admission(
            username, "192.0.2.1", 10, self.db, now=now,
            jobs_db_factory=self.jobs,
        )
        asset_id = digital_human_v2._audio_asset_id_for_admission(admission_id)
        directory = digital_human_v2._audio_asset_directory(username, asset_id)
        directory.mkdir(parents=True)
        (directory / "source.mp3").write_bytes(b"orphan-source")
        (directory / "slice_00.m4a").write_bytes(b"orphan-slice")
        return now, admission_id, asset_id, directory

    def test_expired_admission_recovers_hard_exit_orphan(self):
        now, admission_id, _asset_id, directory = self._expired_admission_with_directory()

        removed = digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs,
            now=now + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
            limit=10,
        )

        self.assertEqual(1, removed)
        self.assertFalse(directory.exists())
        with closing(self.db()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM digital_human_audio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone())

    @unittest.skipUnless(os.name == "posix", "symlink path safety requires POSIX")
    def test_orphan_recovery_leaves_symlink_and_external_file_untouched(self):
        now, admission_id, _asset_id, directory = self._expired_admission_with_directory()
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE digital_human_audio_admissions SET state='committed',lease_until=0 "
                "WHERE admission_id=?", (admission_id,),
            )
            connection.commit()
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"external-unique")
        linked = directory / "linked.mp3"
        linked.symlink_to(outside)

        removed = digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs,
            now=now + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
            limit=10,
        )

        self.assertEqual(0, removed)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(b"external-unique", outside.read_bytes())
        with closing(self.db()) as connection:
            state = connection.execute(
                "SELECT state FROM digital_human_audio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()["state"]
        self.assertEqual("reaping_committed", state)

    def test_committed_orphan_with_unexpected_file_fails_closed(self):
        now, admission_id, _asset_id, directory = self._expired_admission_with_directory()
        (directory / "unexpected.txt").write_text("operator evidence", encoding="utf-8")
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE digital_human_audio_admissions SET state='committed',lease_until=0 "
                "WHERE admission_id=?", (admission_id,),
            )
            connection.commit()

        self.assertEqual(0, digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs, now=now + 1000, limit=10,
        ))
        self.assertTrue((directory / "unexpected.txt").exists())
        self.assertTrue((directory / "source.mp3").exists())

    def test_committed_orphans_remain_recoverable_across_audit_retention(self):
        now, consent_admission, consent_asset, consent_directory = \
            self._expired_admission_with_directory("alice")
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE digital_human_audio_admissions SET state='committed',lease_until=0 "
                "WHERE admission_id=?", (consent_admission,),
            )
            connection.execute(
                "INSERT INTO digital_human_consents(username,run_id,voice_ref,expires_at) "
                "VALUES(?,?,?,?)",
                ("alice", "audio-run-consent", consent_asset, now + 3 * 86400),
            )
            connection.commit()
        job_now, job_admission, job_asset, job_directory = \
            self._expired_admission_with_directory("bob")
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE digital_human_audio_admissions SET state='committed',lease_until=0 "
                "WHERE admission_id=?", (job_admission,),
            )
            connection.commit()
        with closing(self.jobs()) as connection:
            connection.execute(
                "INSERT INTO jobs(username,payload,result) VALUES(?,?,?)",
                ("bob", '{"audio_upload_id":"%s"}' % job_asset, "{}"),
            )
            connection.commit()

        cleanup_now = max(now, job_now) + 2 * 86400 + 1
        self.assertEqual(0, digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs, now=cleanup_now, limit=10,
        ))
        self.assertTrue(consent_directory.exists())
        self.assertTrue(job_directory.exists())
        with closing(self.db()) as connection:
            states = {
                row["admission_id"]: row["state"] for row in connection.execute(
                    "SELECT admission_id,state FROM digital_human_audio_admissions"
                ).fetchall()
            }
        self.assertEqual("committed", states[consent_admission])
        self.assertEqual("committed", states[job_admission])

        with closing(self.db()) as connection:
            connection.execute(
                "DELETE FROM digital_human_consents WHERE voice_ref=?", (consent_asset,),
            )
            connection.commit()
        with closing(self.jobs()) as connection:
            connection.execute(
                "DELETE FROM jobs WHERE payload LIKE ? OR result LIKE ?",
                ("%" + job_asset + "%", "%" + job_asset + "%"),
            )
            connection.commit()

        self.assertEqual(2, digital_human_v2._reap_expired_audio_admissions(
            self.db, self.jobs, now=cleanup_now + 1, limit=10,
        ))
        self.assertFalse(consent_directory.exists())
        self.assertFalse(job_directory.exists())
        with closing(self.db()) as connection:
            states = {
                row["admission_id"]: row["state"] for row in connection.execute(
                    "SELECT admission_id,state FROM digital_human_audio_admissions"
                ).fetchall()
            }
        self.assertEqual("committed_reaped", states[consent_admission])
        self.assertEqual("committed_reaped", states[job_admission])

        self.assertEqual(0, digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs, now=cleanup_now + 2, limit=10,
        ))
        with closing(self.db()) as connection:
            remaining = connection.execute(
                "SELECT admission_id FROM digital_human_audio_admissions WHERE admission_id IN (?,?)",
                (consent_admission, job_admission),
            ).fetchall()
        self.assertEqual([], remaining)

    def test_orphan_recovery_retains_valid_asset_record(self):
        now, admission_id, asset_id, directory = self._expired_admission_with_directory()
        source = directory / "source.mp3"
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO digital_human_audio_uploads VALUES(
                ?,?,?,?, ?,1.0,'text','[]',?,?)""",
                (asset_id, "alice", "audio-run-valid", "0" * 64,
                 source.relative_to(digital_human_v2.OUT_DIR).as_posix(), now, now + 9999),
            )
            connection.commit()

        self.assertEqual(0, digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs,
            now=now + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
            limit=10,
        ))
        self.assertTrue(source.exists())
        with closing(self.db()) as connection:
            self.assertEqual("committed", connection.execute(
                "SELECT state FROM digital_human_audio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()["state"])

    def test_expired_orphan_is_reclaimed_before_disk_quota_check(self):
        now, _admission_id, _asset_id, directory = self._expired_admission_with_directory()
        orphan_bytes = sum(path.stat().st_size for path in directory.iterdir())
        with mock.patch.object(
            digital_human_v2, "_AUDIO_MANAGED_DISK_BYTES", orphan_bytes,
        ):
            replacement = digital_human_v2._begin_audio_admission(
                "bob", "192.0.2.2", orphan_bytes, self.db,
                now=now + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
                jobs_db_factory=self.jobs,
            )
        self.assertTrue(replacement.startswith("dhaa_"))
        self.assertFalse(directory.exists())
        digital_human_v2._finish_audio_admission(replacement, False, self.db)

    def test_long_processing_renews_active_admission_lease(self):
        admission_id = digital_human_v2._begin_audio_admission(
            "alice", "192.0.2.1", 10, self.db,
            jobs_db_factory=self.jobs,
        )
        with mock.patch.object(
            digital_human_v2, "_renew_audio_admission",
            wraps=digital_human_v2._renew_audio_admission,
        ) as renew:
            heartbeat = digital_human_v2._AudioAdmissionHeartbeat(
                admission_id, self.db, interval=0.01,
            ).start()
            time.sleep(0.05)
            heartbeat.close()
            heartbeat.check()
        self.assertGreaterEqual(renew.call_count, 2)
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT state,lease_until FROM digital_human_audio_admissions "
                "WHERE admission_id=?", (admission_id,),
            ).fetchone()
        self.assertEqual("active", row["state"])
        self.assertGreater(row["lease_until"], int(time.time()))
        digital_human_v2._finish_audio_admission(admission_id, False, self.db)

    def test_admission_rejection_does_not_read_or_create_upload(self):
        stream = mock.Mock(spec=io.BytesIO)
        error = digital_human_v2.DigitalHumanRequestError(
            "busy", "audio_upload_concurrency_limit", 429,
        )
        with mock.patch.object(
            digital_human_v2, "_begin_audio_admission", side_effect=error,
        ), mock.patch.object(pathlib.Path, "mkdir") as mkdir, self.assertRaises(
            digital_human_v2.DigitalHumanRequestError,
        ):
            digital_human_v2.store_audio_upload(
                stream, 10, "alice", "audio-run-0001", "audio/mpeg", "0" * 64,
                db_factory=self.db, client_ip="192.0.2.1",
            )
        stream.read.assert_not_called()
        mkdir.assert_not_called()

    def test_expired_unreferenced_files_are_collected_but_consent_is_retained(self):
        audio_root = digital_human_v2.OUT_DIR / "digital_human_audio" / "owner"
        expired = audio_root / "expired" / "source.mp3"
        expired_consent = audio_root / "expired-consent" / "source.mp3"
        retained = audio_root / "retained" / "source.mp3"
        job_retained = audio_root / "job-retained" / "source.mp3"
        expired.parent.mkdir(parents=True)
        expired_consent.parent.mkdir(parents=True)
        retained.parent.mkdir(parents=True)
        job_retained.parent.mkdir(parents=True)
        expired.write_bytes(b"old")
        expired_consent.write_bytes(b"expired-consent-media")
        retained.write_bytes(b"keep")
        job_retained.write_bytes(b"job-reference")
        with closing(self.db()) as connection:
            for asset_id, run_id, path in (
                ("dha_" + "1" * 32, "audio-run-expired", expired),
                ("dha_" + "2" * 32, "audio-run-retained", retained),
                ("dha_" + "4" * 32, "audio-run-expired-consent", expired_consent),
                ("dha_" + "5" * 32, "audio-run-job-retained", job_retained),
            ):
                connection.execute(
                    """INSERT INTO digital_human_audio_uploads VALUES(
                    ?,?,?,?, ?,1.0,'text','[]',1,2)""",
                    (asset_id, "alice", run_id, "0" * 64,
                     path.relative_to(digital_human_v2.OUT_DIR).as_posix()),
                )
            connection.execute(
                "INSERT INTO digital_human_consents(username,run_id,expires_at) VALUES(?,?,?)",
                ("alice", "audio-run-retained", 20),
            )
            connection.execute(
                "INSERT INTO digital_human_consents(username,run_id,expires_at) VALUES(?,?,?)",
                ("alice", "audio-run-expired-consent", 9),
            )
            connection.commit()
        with closing(self.jobs()) as connection:
            connection.execute(
                "INSERT INTO jobs(username,payload,result) VALUES(?,?,?)",
                ("alice", '{"audio_upload_id":"%s"}' % ("dha_" + "5" * 32), "{}"),
            )
            connection.commit()
        removed = digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs, now=10, limit=10,
        )
        self.assertEqual(2, removed)
        self.assertFalse(expired.exists())
        self.assertFalse(expired_consent.exists())
        self.assertTrue(retained.exists())
        self.assertTrue(job_retained.exists())
        with closing(self.db()) as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM digital_human_consents "
                "WHERE run_id='audio-run-expired-consent'",
            ).fetchone())

    def test_material_hard_exit_after_write_is_reconciled(self):
        class HardExit(BaseException):
            pass

        with mock.patch.object(
            digital_human_v2.subprocess, "run", side_effect=HardExit("power loss"),
        ), self.assertRaises(HardExit):
            digital_human_v2._store_material_asset(
                b"material-before-hard-exit", "image/png", "local_library",
                "alice", "material-run-hard-exit", "a" * 64, 0,
                db_factory=self.db,
            )
        with closing(self.db()) as connection:
            row = dict(connection.execute(
                "SELECT * FROM digital_human_material_admissions",
            ).fetchone())
        target = digital_human_v2.OUT_DIR / row["relative_file"]
        self.assertTrue(target.is_file())

        removed = digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs,
            now=row["lease_until"] + 1, limit=10,
        )

        self.assertEqual(1, removed)
        self.assertFalse(target.exists())
        with closing(self.db()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM digital_human_material_admissions "
                "WHERE admission_id=?", (row["admission_id"],),
            ).fetchone())

    def test_material_recovery_never_deletes_committed_concurrent_winner(self):
        admission_id, asset_id, target, relative = \
            digital_human_v2._begin_material_admission(
                "alice", "material-run-winner", ".png", self.db, now=1000,
                jobs_db_factory=self.jobs,
            )
        target.parent.mkdir(parents=True)
        target.write_bytes(b"committed-winner")
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO digital_human_material_assets VALUES(
                ?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, "alice", "material-run-winner", "b" * 64, 0,
                 relative, "image/png", "image", "local_library", 1000, 999999),
            )
            connection.commit()

        self.assertEqual(0, digital_human_v2._reap_expired_material_admissions(
            self.db, self.jobs,
            now=1000 + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
            limit=10,
        ))
        self.assertEqual(b"committed-winner", target.read_bytes())
        with closing(self.db()) as connection:
            self.assertEqual("committed", connection.execute(
                "SELECT state FROM digital_human_material_admissions "
                "WHERE admission_id=?", (admission_id,),
            ).fetchone()["state"])

    def test_material_recovery_rejects_cross_user_or_external_path(self):
        admission_id, _asset_id, target, _relative = \
            digital_human_v2._begin_material_admission(
                "alice", "material-run-path", ".png", self.db, now=1000,
                jobs_db_factory=self.jobs,
            )
        target.parent.mkdir(parents=True)
        target.write_bytes(b"owned-material")
        other_target, other_relative = digital_human_v2._material_asset_location(
            "bob", "material-run-path", _asset_id, ".png",
        )
        other_target.parent.mkdir(parents=True)
        other_target.write_bytes(b"other-user-unique")
        outside = self.root / "external.png"
        outside.write_bytes(b"external-unique")
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE digital_human_material_admissions "
                "SET relative_file=? WHERE admission_id=?",
                (other_relative, admission_id),
            )
            connection.commit()

        self.assertEqual(0, digital_human_v2._reap_expired_material_admissions(
            self.db, self.jobs,
            now=1000 + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
            limit=10,
        ))
        self.assertEqual(b"owned-material", target.read_bytes())
        self.assertEqual(b"other-user-unique", other_target.read_bytes())
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE digital_human_material_admissions "
                "SET relative_file='../external.png' WHERE admission_id=?",
                (admission_id,),
            )
            connection.commit()
        self.assertEqual(0, digital_human_v2._reap_expired_material_admissions(
            self.db, self.jobs,
            now=1000 + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 2,
            limit=10,
        ))
        self.assertEqual(b"external-unique", outside.read_bytes())

    @unittest.skipUnless(os.name == "posix", "symlink path safety requires POSIX")
    def test_material_recovery_never_follows_symlink(self):
        admission_id, _asset_id, target, _relative = \
            digital_human_v2._begin_material_admission(
                "alice", "material-run-link", ".png", self.db, now=1000,
                jobs_db_factory=self.jobs,
            )
        target.parent.mkdir(parents=True)
        outside = self.root / "outside-material.png"
        outside.write_bytes(b"external-unique")
        target.symlink_to(outside)

        self.assertEqual(0, digital_human_v2._reap_expired_material_admissions(
            self.db, self.jobs,
            now=1000 + digital_human_v2._AUDIO_ADMISSION_LEASE_SECONDS + 1,
            limit=10,
        ))
        self.assertTrue(target.is_symlink())
        self.assertEqual(b"external-unique", outside.read_bytes())
        with closing(self.db()) as connection:
            self.assertEqual("reaping", connection.execute(
                "SELECT state FROM digital_human_material_admissions "
                "WHERE admission_id=?", (admission_id,),
            ).fetchone()["state"])

    @unittest.skipUnless(os.name == "posix", "symlink path safety requires POSIX")
    def test_gc_never_follows_managed_symlink(self):
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"unique")
        directory = digital_human_v2.OUT_DIR / "digital_human_audio" / "owner" / "bad"
        directory.mkdir(parents=True)
        linked = directory / "source.mp3"
        linked.symlink_to(outside)
        with closing(self.db()) as connection:
            connection.execute(
                """INSERT INTO digital_human_audio_uploads VALUES(
                ?,?,?,?, ?,1.0,'text','[]',1,2)""",
                ("dha_" + "3" * 32, "alice", "audio-run-symlink", "0" * 64,
                 linked.relative_to(digital_human_v2.OUT_DIR).as_posix()),
            )
            connection.commit()
        self.assertEqual(0, digital_human_v2.cleanup_expired_assets(
            self.db, self.jobs, now=10, limit=10,
        ))
        self.assertEqual(b"unique", outside.read_bytes())

    def test_nginx_has_exact_audio_endpoint_limits(self):
        nginx = (ROOT / "deploy/nginx-huangquechuanmei.conf").read_text("utf-8")
        block = nginx.split(
            "location = /api/gen/digital-human-v2/audio-upload {", 1,
        )[1].split("}", 1)[0]
        self.assertIn("limit_req zone=hq_digital_human_audio_rate", block)
        self.assertIn("limit_conn hq_digital_human_audio_conn 1", block)
        self.assertIn("client_max_body_size 30m", block)

    def test_only_loopback_proxy_can_supply_ip_quota_identity(self):
        trusted = mock.Mock(
            client_address=("127.0.0.1", 1234),
            headers={"X-Real-IP": "203.0.113.9"},
        )
        direct = mock.Mock(
            client_address=("198.51.100.8", 1234),
            headers={"X-Real-IP": "203.0.113.9"},
        )
        self.assertEqual("203.0.113.9", script_to_video._trusted_client_ip(trusted))
        self.assertEqual("198.51.100.8", script_to_video._trusted_client_ip(direct))


if __name__ == "__main__":
    unittest.main()
