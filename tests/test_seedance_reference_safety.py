import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class SeedanceReferenceSafetyTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import feature_flags, video, video_seedance
        self.video = video
        patchers = [
            patch.object(feature_flags, "is_enabled", return_value=True),
            patch.object(video_seedance, "available", return_value=True),
            patch.object(video.provider_keys, "claim_candidate",
                         return_value={"id": "seedance-test", "secret": "secret"}),
            patch.object(video.provider_keys, "set_health"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


    @staticmethod
    def _seedance_png_data(tag=b""):
        import io as _io
        from PIL import Image
        shade = (tag[0] if tag else 0) % 256
        buf = _io.BytesIO()
        Image.new("RGB", (8, 8), (shade, 128, 255 - shade)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def _stage_mocks(self, put_side_effect=None):
        from content_domains import cos

        signed = "https://bucket-1250000000.cos.ap-guangzhou.myqcloud.com/seedance/reference/x?q-sign-algorithm=sha1&q-sign-time=1"
        return [
            patch.object(self.video, "seedance_reference_upload_is_open", return_value=True),
            patch.object(cos, "enabled", return_value=True),
            patch.object(cos, "put_bytes", side_effect=put_side_effect),
            patch.object(self.video, "_seedance_cos_presign", return_value=signed),
            patch.object(self.video, "_persist_staging_cleanup_intent"),
            patch.object(self.video, "_enqueue_pending_cleanup"),
            patch.object(self.video, "_remove_cleanup_record"),
        ]

    def test_validate_micro_keeps_data_images_local_until_staging(self):
        from content_domains import cos

        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(4)]
        with patch.object(cos, "put_bytes") as put:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": refs,
            }, "fang")
        put.assert_not_called()   # 校验阶段不做任何网络上传
        self.assertEqual(refs, body["reference_images"])

    def test_stage_seedance_references_uploads_private_and_returns_cos_keys(self):
        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(4)]
        patches = self._stage_mocks()
        with patches[0], patches[1], patches[2] as put, patches[3] as presign, \
             patches[4], patches[5], patches[6]:
            first = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            second = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            first_keys = self.video.stage_seedance_references(first, "fang", "idem-token-a")
            second_keys = self.video.stage_seedance_references(second, "fang", "idem-token-b")
            repeat = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            repeat_keys = self.video.stage_seedance_references(repeat, "fang", "idem-token-a")

        self.assertEqual(12, put.call_count)
        for call in put.call_args_list:
            self.assertIs(call.kwargs.get("private"), True)   # 强制私有 ACL
        presign.assert_not_called()   # 提交时不签名，签名推迟到 worker 提交时
        first_keys_uploaded = [call.args[1] for call in put.call_args_list[:4]]
        second_keys_uploaded = [call.args[1] for call in put.call_args_list[4:8]]
        repeat_keys_uploaded = [call.args[1] for call in put.call_args_list[8:]]
        # 跨提交不共享对象键（消除失败清理竞态）；同幂等键重试覆盖同一对象
        self.assertNotEqual(first_keys_uploaded, second_keys_uploaded)
        self.assertNotEqual(first_keys_uploaded, repeat_keys_uploaded)
        self.assertEqual(4, len(set(first_keys_uploaded)))
        self.assertRegex(first_keys_uploaded[0],
                         r"^seedance/reference/[0-9a-f]{16}/[0-9a-f]{32}-[0-9a-f]{16}\.png$")
        self.assertEqual(first_keys, first_keys_uploaded)
        self.assertEqual(first_keys, first["_seedance_staged_keys"])   # 随 payload 落库供终态清理
        # payload 只存 cos-key:// 内部引用，不存签名 URL；跨提交引用不同（键不同）
        self.assertEqual(["cos-key://" + k for k in first_keys], first["reference_images"])
        self.assertNotEqual(first["reference_images"], second["reference_images"])
        self.assertNotIn("data:", str(first["reference_images"]))
        self.assertNotIn("q-sign-algorithm", str(first["reference_images"]))

    def test_staging_token_is_unique_per_physical_attempt(self):
        # 仅标点不同的两个合法 Idempotency-Key 不得映射为同一 token
        first = self.video._seedance_staging_token("same-idempotency-key")
        retry = self.video._seedance_staging_token("same-idempotency-key")
        self.assertNotEqual(first, retry)
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertRegex(retry, r"^[0-9a-f]{32}$")

    def test_duplicate_images_in_one_batch_get_distinct_object_keys(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, cos

        image = self._seedance_png_data(b"same")
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False), \
             patch.object(self.video, "seedance_reference_upload_is_open", return_value=True), \
             patch.object(cos, "put_bytes") as put:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": [image, image],
            }, "fang")
            keys = self.video.stage_seedance_references(
                body, "fang", "same-idempotency-key"
            )
            self.assertEqual(2, put.call_count)
            self.assertEqual(2, len(keys))
            self.assertEqual(2, len(set(keys)))
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                self.assertEqual(
                    2, db.execute("SELECT COUNT(*) FROM seedance_pending_cleanup").fetchone()[0]
                )

    def test_validate_micro_strips_client_internal_fields(self):
        body = self.video.validate_xiaole_video_payload({
            "channel": "micro", "prompt": "demo", "duration": 5,
            "_seedance_staged_keys": ["seedance/reference/0000000000000000/evil.png"],
            "_username": "admin", "_job_id": 1,
        }, "fang")
        self.assertNotIn("_seedance_staged_keys", body)   # 注入的暂存键不得进入任务 payload
        self.assertNotIn("_username", body)
        self.assertNotIn("_job_id", body)

    def test_validate_micro_rejects_client_cos_key_reference(self):
        with self.assertRaisesRegex(ValueError, "公网|asset://"):
            self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": ["cos-key://seedance/reference/0000000000000000/evil.png"],
            }, "fang")

    def test_worker_signs_cos_key_references_at_submit_time(self):
        import hashlib as _hashlib

        owner = _hashlib.sha256(b"fang").hexdigest()[:16]
        key = "seedance/reference/%s/%s.png" % (owner, "t" * 24 + "-" + "c" * 16)
        signed = "https://bucket-1250000000.cos.ap-guangzhou.myqcloud.com/x?q-sign-algorithm=sha1"
        fake = {"request_id": "seedance-1", "source_video_url": "https://example.com/micro.mp4",
                "model": "doubao-seedance-2-0-260128"}
        with patch("content_domains.video_seedance.generate", return_value=fake) as generate, \
             patch.object(self.video, "_seedance_cos_presign", return_value=signed) as presign, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/seedance.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None):
            self.video.gen_xiaole_video({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": ["cos-key://" + key], "_username": "fang",
            })
        presign.assert_called_once_with(key)   # worker 提交时才生成新鲜签名 URL
        self.assertEqual([signed], generate.call_args.kwargs["reference_images"])

    def test_worker_rejects_cos_key_of_other_owner(self):
        with patch("content_domains.video_seedance.generate") as generate, \
             patch.object(self.video, "_seedance_cos_presign") as presign:
            with self.assertRaisesRegex(ValueError, "对象键不合法"):
                self.video.gen_xiaole_video({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": ["cos-key://seedance/reference/0000000000000000/x.png"],
                    "_username": "fang",
                })
        generate.assert_not_called()
        presign.assert_not_called()

    def test_stage_seedance_references_partial_failure_cleans_uploaded_batch(self):
        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(3)]
        patches = self._stage_mocks(put_side_effect=[None, None, RuntimeError("cos boom")])
        with patches[0], patches[1], patches[2] as put, patches[3], patches[4], patches[5], patches[6], \
             patch.object(self.video, "_seedance_cos_delete") as delete:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "上传失败.*未扣点"):
                self.video.stage_seedance_references(body, "fang")

        self.assertEqual(3, put.call_count)
        attempted_keys = [call.args[1] for call in put.call_args_list]
        self.assertEqual(3, delete.call_count)
        self.assertCountEqual(attempted_keys, [call.args[0] for call in delete.call_args_list])
        self.assertEqual(refs, body["reference_images"])   # 失败不回退、不改写

    def test_stage_seedance_references_unavailable_is_explicit(self):
        from content_domains import cos

        with patch.object(self.video, "seedance_reference_upload_is_open", return_value=False), \
             patch.object(cos, "put_bytes") as put:
            body = {"channel": "micro", "reference_images": [self._seedance_png_data()]}
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "未配置.*未扣点"):
                self.video.stage_seedance_references(body, "fang")
        put.assert_not_called()

    def test_stage_seedance_references_upload_failure_never_falls_back_to_data_url(self):
        patches = self._stage_mocks(put_side_effect=ModuleNotFoundError("qcloud_cos"))
        ref = self._seedance_png_data()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            body = {"channel": "micro", "reference_images": [ref]}
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "上传失败.*未扣点"):
                self.video.stage_seedance_references(body, "fang")
        self.assertEqual([ref], body["reference_images"])   # body 未被半成品 URL 污染

    def test_stage_seedance_references_skips_non_micro_channels(self):
        from content_domains import cos

        with patch.object(cos, "put_bytes") as put:
            body = {"channel": "grok", "reference_images": ["data:image/png;base64,AAAA"]}
            self.assertEqual([], self.video.stage_seedance_references(body, "fang"))
        put.assert_not_called()

    def test_cleanup_staged_seedance_references_is_best_effort(self):
        import tempfile
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False), \
             patch.object(self.video, "_seedance_cos_delete",
                          side_effect=[None, RuntimeError("already gone")]) as delete:
            self.video.cleanup_staged_seedance_references(["k1", "k2"])
        self.assertEqual(2, delete.call_count)   # 单个失败不阻断其余清理

    def test_cleanup_failure_persists_and_retry_converges(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        def pending_rows():
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                return db.execute("SELECT key,job_id,attempts,state FROM seedance_pending_cleanup").fetchall()

        def make_due():
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_pending_cleanup SET next_attempt_at=0")
                db.commit()

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            # 删除失败 → 持久化待清理
            with patch.object(self.video, "_seedance_cos_delete", side_effect=RuntimeError("cos down")):
                self.video.cleanup_staged_seedance_references(["k1"], job_id=7)
            self.assertEqual([("k1", 7, 1, "cleanup_pending")], [tuple(r) for r in pending_rows()])
            # 重试仍失败 → attempts+1，记录保留
            with patch.object(self.video, "_seedance_cos_delete", side_effect=RuntimeError("still down")):
                make_due()
                self.video.retry_pending_seedance_cleanups()
            self.assertEqual([("k1", 7, 2, "cleanup_pending")], [tuple(r) for r in pending_rows()])
            # 故障恢复 → 重试成功 → 记录移除
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                make_due()
                self.video.retry_pending_seedance_cleanups()
            delete.assert_called_once_with("k1")
            self.assertEqual([], pending_rows())

    def test_cleanup_scan_does_not_starve_eligible_rows(self):
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(self.video._cleanup_db()) as db:
                for index in range(100):
                    db.execute(
                        "INSERT INTO seedance_pending_cleanup(key,job_id,created_at,attempts,state,next_attempt_at,updated_at) VALUES(?,?,?,?,'cleanup_pending',0,0)",
                        ("exhausted-%03d" % index, None, index,
                         self.video.SEEDANCE_CLEANUP_MAX_ATTEMPTS),
                    )
                db.execute(
                    "INSERT INTO seedance_pending_cleanup(key,job_id,created_at,attempts,state,next_attempt_at,updated_at) VALUES('eligible',NULL,1000,0,'cleanup_pending',0,0)"
                )
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                processed = sum(
                    self.video.retry_pending_seedance_cleanups(limit=50)
                    for _ in range(3)
                )
            self.assertEqual(101, processed)
            self.assertIn("eligible", [call.args[0] for call in delete.call_args_list])

    def test_cleanup_keeps_retrying_after_alert_threshold(self):
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(self.video._cleanup_db()) as db:
                db.execute(
                    "INSERT INTO seedance_pending_cleanup(key,job_id,created_at,attempts,state,next_attempt_at,updated_at) VALUES('recover-after-five',NULL,0,?,'cleanup_pending',0,0)",
                    (self.video.SEEDANCE_CLEANUP_MAX_ATTEMPTS,),
                )
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("recover-after-five")

    def test_orphaned_staging_intent_is_recovered_after_grace(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            self.video._persist_staging_cleanup_intent("orphan-key")
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_pending_cleanup SET created_at=0,next_attempt_at=0")
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("orphan-key")

    def test_terminal_cas_crash_window_converges_after_restart_scan(self):
        import json as _json
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, jobs_store

        key = "seedance/reference/%s/terminal.png" % __import__("hashlib").sha256(b"fang").hexdigest()[:16]
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(self.video._cleanup_db()):
                pass
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY,kind TEXT,username TEXT,status TEXT,
                    payload TEXT,result TEXT,error TEXT,cost INTEGER DEFAULT 0,
                    refunded INTEGER DEFAULT 0,updated_at INTEGER)""")
                db.execute("INSERT INTO jobs VALUES(1,'xiaole_video','fang','running',?,NULL,NULL,0,0,0)",
                           (_json.dumps({"_seedance_staged_keys": [key]}),))
                db.execute("""INSERT INTO seedance_pending_cleanup
                    (key,job_id,created_at,attempts,state,next_attempt_at,updated_at)
                    VALUES(?,1,0,0,'linked',0,0)""", (key,))
                db.commit()
            # Simulate the process dying immediately after terminal CAS: the normal
            # after_terminal_seedance_cleanup callback is deliberately not called.
            self.assertTrue(jobs_store.set_terminal_with_video_outbox(
                core.jdb, 1, "done", {"ok": True}))
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with(key)
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                self.assertEqual("done", db.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0])
                self.assertEqual(0, db.execute("SELECT COUNT(*) FROM seedance_pending_cleanup").fetchone()[0])

    def test_restart_releases_stale_precharge_idempotency_attempt(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, submission_idempotency

        endpoint, idem_key = "/api/gen/xiaole-video", "restart-upload-123"
        request = {"channel": "micro", "reference_images": ["data:image/png;base64,AA=="]}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            self.assertEqual("new", submission_idempotency.begin(
                core.jdb, "fang", endpoint, idem_key, request)[0])
            self.video._reserve_seedance_staging_attempt("fang", endpoint, idem_key)
            self.video._persist_staging_cleanup_intent("orphan-upload-key")
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_staging_attempts SET updated_at=0")
                db.execute("UPDATE seedance_pending_cleanup SET created_at=0,next_attempt_at=0")
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("orphan-upload-key")
            self.assertEqual("new", submission_idempotency.begin(
                core.jdb, "fang", endpoint, idem_key, request)[0])

    def test_restart_releases_attempt_crashed_before_deduct_call(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, submission_idempotency

        endpoint, idem_key = "/api/gen/xiaole-video", "charging-boundary-123"
        request = {"channel": "micro", "reference_images": ["data:image/png;base64,AA=="]}
        staged = {"channel": "micro", "_seedance_staged_keys": ["before-deduct-key"]}

        class Points:
            refunds = []

            @staticmethod
            def get_points_transaction(_transaction_key):
                return None

            @classmethod
            def refund_points(cls, *_args, **_kwargs):
                cls.refunds.append((_args, _kwargs))

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            self.assertEqual("new", submission_idempotency.begin(
                core.jdb, "fang", endpoint, idem_key, request)[0])
            self.video._reserve_seedance_staging_attempt("fang", endpoint, idem_key)
            self.video._mark_seedance_staging_attempt_ready("fang", endpoint, idem_key)
            self.video._persist_staging_cleanup_intent("before-deduct-key")
            self.video.mark_seedance_reference_charging(
                "fang", endpoint, idem_key, "xiaole_video", 150, staged,
                "content", "job-charge:before-deduct")
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_staging_attempts SET updated_at=0")
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.video.retry_pending_seedance_cleanups(points_domain=Points())
            delete.assert_called_once_with("before-deduct-key")
            self.assertEqual([], Points.refunds)
            self.assertEqual("new", submission_idempotency.begin(
                core.jdb, "fang", endpoint, idem_key, request)[0])
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                self.assertEqual(0, db.execute(
                    "SELECT COUNT(*) FROM seedance_staging_attempts").fetchone()[0])

    def test_restart_refunds_charge_committed_before_jobs_commit(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, submission_idempotency

        endpoint, idem_key = "/api/gen/xiaole-video", "charged-no-job-123"
        request = {"channel": "micro", "reference_images": ["data:image/png;base64,AA=="]}
        staged = {"channel": "micro", "_seedance_staged_keys": ["after-deduct-key"]}

        class Points:
            refunds = []

            @staticmethod
            def get_points_transaction(transaction_key):
                self.assertEqual("job-charge:after-deduct", transaction_key)
                return {"username": "fang", "delta": -150, "after_points": 850}

            @classmethod
            def refund_points(cls, username, amount, reason, transaction_key=""):
                cls.refunds.append((username, amount, reason, transaction_key))
                return 1000

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            self.assertEqual("new", submission_idempotency.begin(
                core.jdb, "fang", endpoint, idem_key, request)[0])
            self.video._reserve_seedance_staging_attempt("fang", endpoint, idem_key)
            self.video._mark_seedance_staging_attempt_ready("fang", endpoint, idem_key)
            self.video._persist_staging_cleanup_intent("after-deduct-key")
            self.video.mark_seedance_reference_charging(
                "fang", endpoint, idem_key, "xiaole_video", 150, staged,
                "content", "job-charge:after-deduct")
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_staging_attempts SET updated_at=0")
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.video.retry_pending_seedance_cleanups(points_domain=Points())
                self.video.retry_pending_seedance_cleanups(points_domain=Points())
            delete.assert_called_once_with("after-deduct-key")
            self.assertEqual(1, len(Points.refunds))
            self.assertEqual(("fang", 150), Points.refunds[0][:2])
            self.assertTrue(Points.refunds[0][3].startswith(
                "job-charge-refund:"))
            state, response = submission_idempotency.begin(
                core.jdb, "fang", endpoint, idem_key, request)
            self.assertEqual("replay", state)
            self.assertEqual("seedance_charge_recovered", response["code"])
            self.assertTrue(response["operation_terminal"])
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                self.assertEqual(0, db.execute(
                    "SELECT COUNT(*) FROM seedance_staging_attempts").fetchone()[0])

    def test_charge_boundary_precedes_deduct_and_job_link_is_atomic(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, jobs_store

        endpoint, idem_key, key = "/api/gen/xiaole-video", "atomic-link-123", "atomic-key"
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(self.video._cleanup_db()):
                pass
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,
                    cost INTEGER,payload TEXT,created_at INTEGER,updated_at INTEGER,owner TEXT)""")
                db.commit()
            self.video._reserve_seedance_staging_attempt("fang", endpoint, idem_key)
            self.video._mark_seedance_staging_attempt_ready("fang", endpoint, idem_key)
            self.video._persist_staging_cleanup_intent(key)

            def deduct(*_args, **_kwargs):
                with _closing(sqlite3.connect(core.JOB_DB)) as db:
                    self.assertEqual("charging", db.execute(
                        "SELECT state FROM seedance_staging_attempts").fetchone()[0])
                return 999

            job_id, points_left = jobs_store.create_paid_job(
                core.jdb, deduct, lambda *_args, **_kwargs: True,
                "xiaole_video", "fang", 150, {"channel": "micro"}, "content",
                before_charge=lambda: self.video.mark_seedance_reference_charging(
                    "fang", endpoint, idem_key, "xiaole_video", 150,
                    {"channel": "micro", "_seedance_staged_keys": [key]},
                    "content", "job-charge:atomic-link"),
                before_commit=lambda connection, jid: self.video.link_staged_seedance_references(
                    connection, [key], jid, "fang", endpoint, idem_key),
            )
            self.assertEqual(999, points_left)
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                self.assertEqual(0, db.execute("SELECT COUNT(*) FROM seedance_staging_attempts").fetchone()[0])
                self.assertEqual((job_id, "linked"), db.execute(
                    "SELECT job_id,state FROM seedance_pending_cleanup WHERE key=?", (key,)).fetchone())

    def test_outcome_unknown_keeps_reference_until_signed_url_expires(self):
        from content_domains import video_seedance
        payload = {"channel": "micro"}
        delay = self.video.seedance_reference_cleanup_delay(
            "xiaole_video", payload, video_seedance.CreateOutcomeUnknown("unknown")
        )
        self.assertGreater(delay, self.video.SEEDANCE_REFERENCE_SIGN_EXPIRE)
        self.assertEqual(0, self.video.seedance_reference_cleanup_delay(
            "xiaole_video", payload, video_seedance.SeedanceRejected("rejected")
        ))

    def test_delayed_cleanup_is_durable_and_runs_once_when_due(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False), \
             patch.object(self.video, "_seedance_cos_delete") as delete:
            self.video._persist_staging_cleanup_intent("unknown-key")
            self.video.cleanup_staged_seedance_references(
                ["unknown-key"], job_id=8, delay_seconds=120
            )
            delete.assert_not_called()
            self.assertEqual(0, self.video.retry_pending_seedance_cleanups())
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                row = db.execute(
                    "SELECT state,job_id FROM seedance_pending_cleanup WHERE key='unknown-key'"
                ).fetchone()
                self.assertEqual(("cleanup_pending", 8), row)
                db.execute("UPDATE seedance_pending_cleanup SET next_attempt_at=0")
                db.commit()
            self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("unknown-key")
            self.assertEqual(0, self.video.retry_pending_seedance_cleanups())

    def test_terminal_cleanup_refuses_foreign_or_wrong_kind_keys(self):
        import hashlib as _hashlib
        import json as _json
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        own = "seedance/reference/%s/t.png" % _hashlib.sha256(b"fang").hexdigest()[:16]
        foreign = "seedance/reference/%s/t.png" % _hashlib.sha256(b"other").hexdigest()[:16]
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT, username TEXT, payload TEXT)")
                db.execute("INSERT INTO jobs VALUES(1,'xiaole_video','fang',?)",
                           (_json.dumps({"_seedance_staged_keys": [own, foreign, " arbitrary/key.png"]}),))
                db.execute("INSERT INTO jobs VALUES(2,'video','fang',?)",
                           (_json.dumps({"_seedance_staged_keys": [own]}),))
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.video.cleanup_job_staged_seedance_references(1)
                self.assertEqual([own], [c.args[0] for c in delete.call_args_list])  # 只删本账号前缀的键
                delete.reset_mock()
                self.video.cleanup_job_staged_seedance_references(2)   # 非 xiaole_video 一律不删
                delete.assert_not_called()

    def test_validate_micro_accepts_public_and_authorized_asset_references(self):
        import tempfile
        from content_domains import assets_store

        with tempfile.TemporaryDirectory() as td, \
             patch.object(assets_store, "ASSET_DB", str(Path(td) / "assets.db")), \
             patch.object(assets_store, "_initialized", False):
            assets_store.init_assets()
            from contextlib import closing as _closing
            import json as _json
            with _closing(assets_store.adb()) as c:
                # 显式登记了 Seedance provider 映射的素材（meta.seedance_asset_id）
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at) VALUES(120,'collect','material','fang',?,1)",
                          (_json.dumps({"seedance_asset_id": "prov-abc123"}),))
                # 普通 copy 资产：没有 provider 映射，哪怕编号相同也不得授权
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at) VALUES(130,'copy','work','fang',?,1)",
                          (_json.dumps({"text": "普通文案资产"}),))
                # 别人的 provider 映射
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at) VALUES(140,'collect','material','other',?,1)",
                          (_json.dumps({"seedance_asset_id": "prov-other-9"}),))
                # 已删除的映射
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at,deleted) VALUES(150,'collect','material','fang',?,1,1)",
                          (_json.dumps({"seedance_asset_id": "prov-deleted"}),))
                c.commit()
            refs = ["https://cdn.example/ref.jpg", "asset://asset-prov-abc123"]
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": refs,
            }, "fang")
            self.assertEqual(refs, body["reference_images"])
            for ref, owner in (("asset://asset-130", "fang"),          # 普通 copy 资产同编号误授权探针
                               ("asset://asset-prov-other-9", "fang"), # 别人的 provider 素材
                               ("asset://asset-prov-deleted", "fang"), # 已删除的映射
                               ("asset://asset-999", "fang"),          # 不存在的编号
                               ("asset://asset-prov-abc123", "other")):# 归属不符
                with self.subTest(ref=ref, owner=owner):
                    with self.assertRaisesRegex(ValueError, "不存在或未授权"):
                        self.video.validate_xiaole_video_payload({
                            "channel": "micro", "prompt": "demo", "duration": 5,
                            "reference_images": [ref],
                        }, owner)

    def test_validate_micro_rejects_local_or_malformed_references(self):
        for ref in ("/api/gen/file/ref.jpg", "file:///tmp/ref.jpg", "http://127.0.0.1/ref.jpg",
                    "http://localhost/ref.jpg", "asset://reference/2"):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "公网|asset://"):
                    self.video.validate_xiaole_video_payload({
                        "channel": "micro", "prompt": "demo", "duration": 5,
                        "reference_images": [ref],
                    }, "fang")

    def test_validate_micro_rejects_spoofed_or_corrupt_images(self):
        import io as _io
        from PIL import Image

        def data_url(mime, raw):
            return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))

        png_buf, jpg_buf = _io.BytesIO(), _io.BytesIO()
        Image.new("RGB", (8, 8)).save(png_buf, "PNG")
        Image.new("RGB", (8, 8)).save(jpg_buf, "JPEG")
        truncated_jpg = jpg_buf.getvalue()[:-64]
        cases = [
            data_url("image/png", jpg_buf.getvalue()),     # 损坏/伪装探针：JPEG 声明成 image/png
            data_url("image/jpeg", png_buf.getvalue()),    # PNG 声明成 image/jpeg
            data_url("image/jpeg", truncated_jpg),         # 截断的 JPEG
            data_url("image/png", b"\x89PNG\r\n\x1a\n" + b"not-a-real-png"),  # 只有魔数
        ]
        for ref in cases:
            with self.subTest(ref=ref[:40]):
                with self.assertRaisesRegex(ValueError, "无效|损坏|不一致"):
                    self.video.validate_xiaole_video_payload({
                        "channel": "micro", "prompt": "demo", "duration": 5,
                        "reference_images": [ref],
                    }, "fang")

    def test_validate_micro_fails_closed_when_pillow_missing(self):
        ref = self._seedance_png_data()
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "校验组件不可用.*未扣点"):
                self.video.validate_xiaole_video_payload({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": [ref],
                }, "fang")

    def test_seedance_worker_defense_rejects_unstaged_data_url(self):
        with patch("content_domains.video_seedance.generate") as generate:
            with self.assertRaisesRegex(ValueError, "公网 URL"):
                self.video.gen_xiaole_video({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": [self._seedance_png_data()],
                })
        generate.assert_not_called()

    def test_seedance_reference_health_requires_cos_and_sdk(self):
        from content_domains import cos

        with patch.object(cos, "enabled", return_value=True), \
             patch.object(self.video.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(self.video.seedance_reference_upload_is_open())
        with patch.object(cos, "enabled", return_value=False):
            self.assertFalse(self.video.seedance_reference_upload_is_open())
        with patch.object(cos, "enabled", return_value=True), \
             patch.object(self.video.importlib.util, "find_spec", return_value=None):
            self.assertFalse(self.video.seedance_reference_upload_is_open())

    def test_content_service_dependency_manifest_pins_cos_sdk(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "deploy/requirements-content.txt").read_text(encoding="utf-8")
        self.assertIn("cos-python-sdk-v5==1.9.44", requirements)
        self.assertIn("Pillow==", requirements)   # 真实解码校验的运行依赖必须闭环

    def test_ci_installs_content_python_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip install -r deploy/requirements-content.txt", workflow)


if __name__ == "__main__":
    unittest.main()
