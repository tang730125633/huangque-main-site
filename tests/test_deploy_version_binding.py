# -*- coding: utf-8 -*-
"""任务-代码版本绑定：.deploy-version → health.deploy_sha + jobs.service_sha。"""
import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

jobs_store = importlib.import_module("content_domains.jobs_store")

try:
    core = importlib.import_module("content_domains.core")
    _CORE_IMPORT_ERROR = None
except Exception as error:  # Windows 没有 fcntl，core 整条链路起不来（与既有基线一致）
    core = None
    _CORE_IMPORT_ERROR = error

try:
    import fcntl  # noqa: F401  # health 端点要拉起 video 域，Windows 上不可能
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

requires_core = unittest.skipIf(
    core is None, "content core 依赖 posix(fcntl)，Windows 跳过: %s" % _CORE_IMPORT_ERROR)
requires_health_stack = unittest.skipIf(
    core is None or not _HAS_FCNTL, "health 端点依赖 video 域(fcntl)，仅 Linux 可跑")


class DeployShaReadTests(unittest.TestCase):
    """read_deploy_sha：有文件返回 SHA，无文件/读失败返回 unknown，绝不抛异常。"""

    def test_reads_sha_from_version_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".deploy-version"
            path.write_text("cea444fa97b4a18a7e5402c9771e791fcd114de0\n", encoding="utf-8")
            with patch.object(jobs_store, "DEPLOY_VERSION_FILE", str(path)):
                self.assertEqual(
                    jobs_store.read_deploy_sha(),
                    "cea444fa97b4a18a7e5402c9771e791fcd114de0")

    def test_missing_version_file_returns_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / ".deploy-version")
            with patch.object(jobs_store, "DEPLOY_VERSION_FILE", missing):
                self.assertEqual(jobs_store.read_deploy_sha(), "unknown")

    def test_empty_version_file_returns_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".deploy-version"
            path.write_text("  \n", encoding="utf-8")
            with patch.object(jobs_store, "DEPLOY_VERSION_FILE", str(path)):
                self.assertEqual(jobs_store.read_deploy_sha(), "unknown")


@requires_health_stack
class HealthDeployShaTests(unittest.TestCase):
    """GET /api/gen/health 带 deploy_sha，既有字段不受影响。"""

    def _fetch_health(self, version_file):
        importlib.import_module("content_domains.registry")
        server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(jobs_store, "DEPLOY_VERSION_FILE", version_file):
                url = "http://127.0.0.1:%d/api/gen/health" % server.server_address[1]
                with urllib.request.urlopen(url, timeout=10) as resp:
                    self.assertEqual(resp.status, 200)
                    return json.loads(resp.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_health_returns_deploy_sha_when_version_file_exists(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".deploy-version"
            path.write_text("abc123def456\n", encoding="utf-8")
            body = self._fetch_health(str(path))
        self.assertEqual(body["deploy_sha"], "abc123def456")
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "huangque-content")

    def test_health_returns_unknown_when_version_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            body = self._fetch_health(str(Path(td) / ".deploy-version"))
        self.assertEqual(body["deploy_sha"], "unknown")
        self.assertTrue(body["ok"])


class ServiceShaColumnTests(unittest.TestCase):
    """jobs.service_sha：老库升级幂等加列、历史行不动、建新任务写入。"""

    OLD_SCHEMA = """CREATE TABLE jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT, username TEXT, cost INTEGER,
        status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
        created_at INTEGER, updated_at INTEGER, owner TEXT)"""

    def _jdb(self, path):
        def factory():
            conn = sqlite3.connect(str(path), timeout=30)
            conn.row_factory = sqlite3.Row
            return conn
        return factory

    def _columns(self, path):
        with closing(sqlite3.connect(str(path))) as conn:
            return {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    def test_ensure_column_upgrades_old_db_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "jobs.db"
            with closing(sqlite3.connect(str(db))) as conn:
                conn.execute(self.OLD_SCHEMA)
                conn.execute("INSERT INTO jobs(kind,username,cost,created_at,updated_at,owner)"
                             " VALUES('image','alice',12,1,1,'content')")
                conn.commit()
            jdb = self._jdb(db)
            jobs_store.ensure_service_sha_column(jdb)
            jobs_store.ensure_service_sha_column(jdb)  # 第二次调用必须还是 no-op
            self.assertIn("service_sha", self._columns(db))
            with closing(sqlite3.connect(str(db))) as conn:
                row = conn.execute("SELECT kind,service_sha FROM jobs").fetchone()
            self.assertEqual(row[0], "image")          # 历史行不动
            self.assertIsNone(row[1])                  # 历史任务 service_sha 为 NULL（版本未知）

    @requires_core
    def test_core_init_db_adds_service_sha_column(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(core, "JOB_DB", str(Path(td) / "content.db")), \
                    patch.object(core, "AUDIO_DB", str(Path(td) / "audio.db")), \
                    patch.object(core.feature_flags, "init_db", lambda: None), \
                    patch.object(core, "init_audio_db", lambda: None):
                core.init_db()
                core.init_db()  # 幂等
                self.assertIn("service_sha", self._columns(Path(td) / "content.db"))

    def test_create_paid_job_writes_service_sha(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "jobs.db"
            jdb = self._jdb(db)
            with closing(jdb()) as conn:
                conn.execute(self.OLD_SCHEMA.replace("owner TEXT)", "owner TEXT, service_sha TEXT)"))
                conn.commit()
            with patch.object(jobs_store, "SERVICE_SHA", "deadbeefcafe"):
                job_id, _left = jobs_store.create_paid_job(
                    jdb, lambda u, c, r: 88, lambda *a, **k: True,
                    "image", "alice", 12, {"prompt": "x"}, "content")
            with closing(sqlite3.connect(str(db))) as conn:
                row = conn.execute("SELECT service_sha FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(row[0], "deadbeefcafe")

    def test_create_paid_job_without_version_file_stores_null(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "jobs.db"
            jdb = self._jdb(db)
            with closing(jdb()) as conn:
                conn.execute(self.OLD_SCHEMA.replace("owner TEXT)", "owner TEXT, service_sha TEXT)"))
                conn.commit()
            with patch.object(jobs_store, "SERVICE_SHA", None):
                job_id, _left = jobs_store.create_paid_job(
                    jdb, lambda u, c, r: 88, lambda *a, **k: True,
                    "image", "alice", 12, {"prompt": "x"}, "content")
            with closing(sqlite3.connect(str(db))) as conn:
                row = conn.execute("SELECT service_sha FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
