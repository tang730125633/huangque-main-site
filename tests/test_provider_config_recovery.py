# -*- coding: utf-8 -*-
"""跨进程任务恢复：配置版本随任务持久化，进程重启/环境变量变更后仍用原版本。

关键点（对应交接意见）：
- 任务创建时把版本号写进任务记录（这里用一个 JSON 文件模拟 jobs.payload 的保留键）。
- **另起一个进程**读取该记录并解析凭据 —— 不依赖创建进程的内存状态。
- 首次（还没有任何后台发布）也要有可恢复版本：环境变量的 URL/凭据被**快照**进版本，
  所以之后改环境变量不会改变老任务的解析结果。
- 发布新版本后，老任务仍解析到旧凭据；新任务才用新凭据。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
for p in (str(SERVER), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from content_domains import provider_config as pc  # noqa: E402

TARGET = "image.seedream"
ENV_KEY = "ARK_API_KEY"
ENV_BASE = "ARK_BASE"
SECRET_ENV_A = "sk-ark-env-A-1111"
SECRET_B = "sk-ark-published-B-2222"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3"

# 子进程里执行的解析脚本：只依赖“任务记录 + 配置存储”，不依赖父进程内存。
CHILD = r"""
import base64, json, os, sys
from content_domains import provider_config as pc
ref = json.load(open(%(rec)r, encoding="utf-8"))
out = pc.resolve_pinned(ref["target_id"], ref["version"],
                        legacy_key=os.environ.get(%(key)r, ""),
                        legacy_url=os.environ.get(%(base)r, ""))
print(json.dumps({"credential": out["credential"], "version": out["version"],
                  "source": out["source"]}))
"""


def _child_code(root, rec_path):
    return CHILD % {"root": str(root), "rec": str(rec_path),
                    "key": ENV_KEY, "base": ENV_BASE}


class CrossProcessRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-recovery-"))
        self.backup = dict(os.environ)
        self.db = self.tmp / "admin_config.db"
        os.environ["ADMIN_DB"] = str(self.db)
        os.environ["HQ_PROVIDER_KEYS_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"0123456789abcdef0123456789abcdef").decode("ascii")
        from tests.provider_config_fixture import configure
        configure(self, pc)
        os.environ.pop(pc.WIRING_ENV, None)
        os.environ[ENV_KEY] = SECRET_ENV_A
        os.environ[ENV_BASE] = ARK_URL
        pc.invalidate()
        pc.init_db()

    def tearDown(self):
        pc.invalidate()
        os.environ.clear()
        os.environ.update(self.backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_child(self, rec_path):
        env = dict(os.environ)
        env["ADMIN_DB"] = str(self.db)
        env["HQ_PROVIDER_KEYS_MASTER_KEY"] = os.environ["HQ_PROVIDER_KEYS_MASTER_KEY"]
        env["PYTHONPATH"] = os.pathsep.join([str(SERVER), str(ROOT), os.environ.get('PYTHONPATH', '')])
        proc = subprocess.run(
            [sys.executable, "-c", _child_code(ROOT, rec_path)],
            capture_output=True, text=True, env=env, timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_env_baseline_is_snapshotted_and_survives_env_change(self):
        # 任务创建：写版本引用（模拟 jobs.payload._provider_config）
        payload = {}
        pc.pin_payload(TARGET, payload, actor="creator")
        rec = self.tmp / "job-ref.json"
        rec.write_text(json.dumps(payload["_provider_config"]), encoding="utf-8")
        self.assertIsNotNone(payload["_provider_config"]["version"],
                             "首次使用也必须给出可恢复版本（环境变量基线）")

        # 之后环境变量被改掉 + 发布了一个新版本
        os.environ[ENV_KEY] = "sk-ark-env-CHANGED"
        draft = pc.save_draft(TARGET, secret=SECRET_B, actor="admin")
        pc.validate_draft(TARGET, draft["seq"], actor="admin",
                          probe=lambda u, k: {"connection": {"ok": True},
                                              "auth": {"ok": True}})
        pc.publish(TARGET, draft["seq"], expected_seq=payload["_provider_config"]["version"],
                   op_id="pub-B", actor="admin")
        pc.invalidate()

        # 新进程按任务记录里的版本解析 → 仍是**快照时的** A，不是改后的 env，也不是 B
        out = self._run_child(rec)
        self.assertEqual(out["credential"], SECRET_ENV_A)
        self.assertEqual(out["version"], payload["_provider_config"]["version"])

        # 新任务用新版本 B
        fresh = {}
        pc.pin_payload(TARGET, fresh, actor="creator")
        new_out = pc.resolve_pinned(TARGET, fresh["_provider_config"]["version"],
                                    legacy_key=os.environ[ENV_KEY], legacy_url=ARK_URL)
        self.assertEqual(new_out["credential"], SECRET_B)

    def test_restart_keeps_old_task_on_old_version(self):
        ref = {}
        pc.pin_payload(TARGET, ref, actor="creator")
        rec = self.tmp / "job-ref2.json"
        rec.write_text(json.dumps(ref["_provider_config"]), encoding="utf-8")

        draft = pc.save_draft(TARGET, secret=SECRET_B, actor="admin")
        pc.validate_draft(TARGET, draft["seq"], actor="admin",
                          probe=lambda u, k: {"connection": {"ok": True},
                                              "auth": {"ok": True}})
        pc.publish(TARGET, draft["seq"], expected_seq=ref["_provider_config"]["version"],
                   op_id="pub-B2", actor="admin")
        pc.invalidate()
        # 模拟重启：清进程内缓存；子进程再从零读库
        before = self._run_child(rec)
        after = self._run_child(rec)
        self.assertEqual(before["credential"], SECRET_ENV_A)
        self.assertEqual(after["credential"], SECRET_ENV_A)
        # 版本只增不删：老版本仍可被在途任务引用
        self.assertIn(ref["_provider_config"]["version"], pc.retained_versions(TARGET))

    def test_task_payload_key_is_reserved_not_leaked_into_credential(self):
        payload = {"prompt": "x", "ratio": "1:1"}
        pc.pin_payload(TARGET, payload, actor="creator")
        self.assertEqual(payload["_provider_config"]["target_id"], TARGET)
        blob = json.dumps(payload)
        self.assertNotIn(SECRET_ENV_A, blob)   # payload 里不落明文
        self.assertNotIn(SECRET_B, blob)

    def test_client_supplied_ref_is_overwritten_by_server(self):
        # 客户端伪造一个版本号 → 必须被服务端覆盖，且最终版本来自服务端解析
        fake = {"prompt": "x", "_provider_config": {"target_id": TARGET, "version": 999}}
        pc.sanitize_payload(fake)
        self.assertNotIn("_provider_config", fake)
        fake["_provider_config"] = {"target_id": "attacker", "version": 999}
        pc.pin_payload(TARGET, fake, actor="creator")
        self.assertEqual(fake["_provider_config"]["target_id"], TARGET)
        self.assertNotEqual(fake["_provider_config"]["version"], 999)

    def test_pinned_version_wins_even_when_switch_is_off(self):
        """关闭灰度开关不能使**已固定版本**的任务偷偷回到当前环境变量。"""
        ref = {}
        pc.pin_payload(TARGET, ref, actor="creator")      # 基线快照（env A）
        pinned = ref["_provider_config"]["version"]
        draft = pc.save_draft(TARGET, secret=SECRET_B, actor="admin")
        pc.validate_draft(TARGET, draft["seq"], actor="admin",
                          probe=lambda u, k: {"connection": {"ok": True},
                                              "auth": {"ok": True}})
        pc.publish(TARGET, draft["seq"], expected_seq=pinned, op_id="sw-off", actor="admin")
        os.environ[ENV_KEY] = "sk-ark-env-CHANGED"
        os.environ.pop(pc.WIRING_ENV, None)               # 关开关
        pc.invalidate()
        # 未带版本的解析（开关关闭）→ 按开关语义走进程启动常量/环境变量
        self.assertEqual(
            pc.credentials_for(TARGET, legacy_key="legacy")["source"], pc.SOURCE_ENV)
        # 带固定版本的任务：仍用**它自己的版本**，不受开关影响
        out = pc.resolve_pinned(TARGET, pinned, legacy_key=os.environ[ENV_KEY],
                                legacy_url=ARK_URL)
        self.assertEqual(out["credential"], SECRET_ENV_A)
        self.assertEqual(out["version"], pinned)


if __name__ == "__main__":
    unittest.main()
