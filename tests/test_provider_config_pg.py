# -*- coding: utf-8 -*-
"""PostgreSQL 权威下的 provider_config 集成测试。

**不跳过**：没有 ``HQ_DATABASE_URL`` 时，由 ``tests/pg_harness.py`` 用 embedded-postgres
拉起一个隔离实例并跑 Alembic；两者都不可用则用例**失败**（不是 skip），
避免“因为没有数据库所以看起来是绿的”。

覆盖：迁移建表、并发基线创建、幂等发布、版本冲突、事务回滚（失败不留痕）、
跨进程恢复、以及 source=env 基线的快照语义。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
for p in (str(SERVER), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests import pg_harness  # noqa: E402

TARGET = "image.seedream"
ENV_KEY = "ARK_API_KEY"
ENV_BASE = "ARK_BASE"
SECRET_A = "sk-ark-env-A-1111"
SECRET_B = "sk-ark-pub-B-2222"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3"

_pg_url = None


def setUpModule():
    """建立 PG 权威；失败直接报错（不 skip）。"""
    global _pg_url
    _pg_url = pg_harness.postgres_url(fresh=True)
    os.environ["HQ_DATABASE_URL"] = _pg_url
    os.environ["HQ_ADMIN_CONFIG_STORE"] = "postgres"
    os.environ["HQ_PROVIDER_KEYS_MASTER_KEY"] = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef").decode("ascii")
    os.environ[ENV_KEY] = SECRET_A
    os.environ[ENV_BASE] = ARK_URL
    from content_domains import provider_config as pc
    pc.invalidate()


class ProviderConfigPglike(unittest.TestCase):
    pass


class ProviderConfigPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from content_domains import provider_config as pc
        cls.pc = pc
        cls.psycopg = __import__("psycopg")

    def setUp(self):
        os.environ["HQ_ADMIN_CONFIG_STORE"] = "postgres"
        os.environ["HQ_DATABASE_URL"] = _pg_url
        os.environ[ENV_KEY] = SECRET_A
        os.environ[ENV_BASE] = ARK_URL
        self.pc.invalidate()

    def tearDown(self):
        self.conn().close() if False else None
        self.pc.invalidate()

    def conn(self):
        return self.psycopg.connect(_pg_url, autocommit=True)

    def _reset_target(self):
        with self.conn() as c:
            c.execute("DELETE FROM ops.provider_config_runtime WHERE target_id=%s", (TARGET,))
            c.execute("DELETE FROM ops.provider_config_versions WHERE target_id=%s", (TARGET,))
        self.pc.invalidate()

    # ---------- 迁移 ----------

    def test_migration_created_both_tables(self):
        with self.conn() as c:
            rows = c.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='ops' AND table_name IN "
                "('provider_config_versions','provider_config_runtime')").fetchall()
        self.assertEqual(sorted(r[0] for r in rows),
                         ["provider_config_runtime", "provider_config_versions"])

    # ---------- 并发基线创建 ----------

    def test_concurrent_baseline_creation_makes_exactly_one_version(self):
        self._reset_target()
        results, errors = [], []

        def worker():
            try:
                results.append(self.pc.ensure_baseline_version(TARGET, actor="w")["seq"])
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1, "并发下只能有一条基线版本")
        with self.conn() as c:
            n = c.execute(
                "SELECT count(*) FROM ops.provider_config_versions "
                "WHERE target_id=%s AND status='published'", (TARGET,)).fetchone()[0]
        self.assertEqual(n, 1)

    # ---------- 幂等 / 冲突 ----------

    def _publish_b(self, op_id="pg-pub-B"):
        draft = self.pc.save_draft(TARGET, secret=SECRET_B, actor="admin")
        self.pc.validate_draft(TARGET, draft["seq"], actor="admin",
                               probe=lambda u, k: {"connection": {"ok": True},
                                                   "auth": {"ok": True}})
        base = self.pc.active_version(TARGET)
        return draft, self.pc.publish(TARGET, draft["seq"],
                                      expected_seq=int(base["seq"]), op_id=op_id,
                                      actor="admin")

    def test_publish_is_idempotent_and_conflict_is_rejected(self):
        self._reset_target()
        self.pc.ensure_baseline_version(TARGET, actor="w")
        draft, first = self._publish_b("pg-dup")
        again = self.pc.publish(TARGET, draft["seq"],
                                expected_seq=int(first["seq"]), op_id="pg-dup",
                                actor="admin")
        self.assertFalse(first.get("idempotent"))
        self.assertTrue(again.get("idempotent"))
        # 过期 expected_seq → VersionConflict
        other = self.pc.save_draft(TARGET, secret="sk-ark-C", actor="admin")
        self.pc.validate_draft(TARGET, other["seq"], actor="admin",
                               probe=lambda u, k: {"connection": {"ok": True},
                                                   "auth": {"ok": True}})
        with self.assertRaises(self.pc.VersionConflict):
            self.pc.publish(TARGET, other["seq"], expected_seq=int(first["seq"]) - 1,
                            op_id="pg-stale", actor="admin")

    # ---------- 事务回滚 ----------

    def test_failed_publish_leaves_no_partial_state(self):
        self._reset_target()
        self.pc.ensure_baseline_version(TARGET, actor="w")
        before = self.pc.active_version(TARGET)
        draft = self.pc.save_draft(TARGET, secret=SECRET_B, actor="admin")
        # 没写证据 → 未验证 → 必须整体回滚
        with self.assertRaises(self.pc.NotVerified):
            self.pc.publish(TARGET, draft["seq"], expected_seq=int(before["seq"]),
                            op_id="pg-nv", actor="admin")
        with self.conn() as c:
            rows = c.execute(
                "SELECT seq, status FROM ops.provider_config_versions "
                "WHERE target_id=%s ORDER BY seq", (TARGET,)).fetchall()
        statuses = {r[0]: r[1] for r in rows}
        self.assertEqual(statuses[draft["seq"]], "draft", "草稿不该被改状态")
        self.assertEqual(self.pc.active_version(TARGET)["seq"], before["seq"])

    # ---------- 跨进程恢复 ----------

    def test_cross_process_recovery_uses_pinned_version(self):
        self._reset_target()
        payload = {}
        self.pc.pin_payload(TARGET, payload, actor="creator")   # 基线快照（env A）
        baseline = payload["_provider_config"]["version"]
        self._publish_b("pg-rec-B")                              # 发布 B
        os.environ[ENV_KEY] = "sk-ark-env-CHANGED"               # 环境变量再变

        rec = Path(tempfile.mkdtemp(prefix="pg-rec-")) / "ref.json"
        rec.write_text(json.dumps(payload["_provider_config"]), encoding="utf-8")
        code = (
            "import json,os,sys\n"
            "from content_domains import provider_config as pc\n"
            "ref=json.load(open(sys.argv[1],encoding='utf-8'))\n"
            "out=pc.resolve_pinned(ref['target_id'],ref['version'],\n"
            "  legacy_key=os.environ.get('ARK_API_KEY',''),legacy_url=os.environ.get('ARK_BASE',''))\n"
            "print(json.dumps({'credential':out['credential'],'version':out['version']}))\n"
        )
        env = dict(os.environ)
        env["HQ_ADMIN_CONFIG_STORE"] = "postgres"
        env["HQ_DATABASE_URL"] = _pg_url
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SERVER), str(ROOT), os.environ.get("PYTHONPATH", ""), os.environ.get("HQ_PG_TOOLS_DIR")
             or r"E:\AI\pg-tools\embedded-pg"])
        proc = subprocess.run([sys.executable, "-c", code, str(rec)],
                              env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["credential"], SECRET_A)   # 仍是快照时的 A
        self.assertEqual(got["version"], baseline)


if __name__ == "__main__":
    unittest.main()
