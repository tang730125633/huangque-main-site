# -*- coding: utf-8 -*-
"""试点接线回归：果肉生图/视频共用的 xiaolevideo 凭据走 provider_config 统一入口。

验证改造方案的关键语义：
- 未开启接线：行为与改造前逐字节一致（用进程启动常量）；
- 开启接线但未发布后台配置：用实时环境变量；
- 已发布后台配置：用后台版本，且此后改环境变量也不影响（不回退）；
- 果肉生图（image.py）与视频（video.py）共用同一入口；
- 缺少凭据时 `_xiaole_request` 在发起任何请求前就报"未配置"。
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from content_domains import image, provider_config as pc, video  # noqa: E402

ENV_KEY = "XIAOLEVIDEO_API_KEY"
ENV_BASE = "XIAOLEVIDEO_API_BASE"
PROCESS_KEY = "sk-process-startup"
ENV_KEY_VALUE = "sk-live-env"
BACKEND_KEY = "sk-backend-published"


class XiaoleWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiaole-wiring-"))
        self.backup = dict(os.environ)
        os.environ["ADMIN_DB"] = str(self.tmp / "admin_config.db")
        os.environ["HQ_PROVIDER_KEYS_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"0123456789abcdef0123456789abcdef").decode("ascii")
        os.environ.pop("HQ_ADMIN_CONFIG_STORE", None)
        os.environ.pop(pc.WIRING_ENV, None)
        os.environ[ENV_KEY] = ENV_KEY_VALUE
        os.environ[ENV_BASE] = "https://api.xiaolevideo.cn"
        pc.invalidate()
        pc.init_db()
        # 模拟"进程启动时读到的常量"与当前 env 不同，以便区分两条路径。
        self.old_const_key = video.XIAOLEVIDEO_API_KEY
        self.old_const_base = video.XIAOLEVIDEO_API_BASE
        video.XIAOLEVIDEO_API_KEY = PROCESS_KEY
        video.XIAOLEVIDEO_API_BASE = "https://api.xiaolevideo.cn"

    def tearDown(self):
        video.XIAOLEVIDEO_API_KEY = self.old_const_key
        video.XIAOLEVIDEO_API_BASE = self.old_const_base
        pc.invalidate()
        os.environ.clear()
        os.environ.update(self.backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish(self, secret=BACKEND_KEY, url=None):
        draft = pc.save_draft("xiaolevideo", url=url, secret=secret, actor="alice")
        pc.record_evidence("xiaolevideo", draft["seq"], {"ok": True}, actor="alice")
        return pc.publish("xiaolevideo", draft["seq"], expected_seq=0,
                          op_id="op-%s" % draft["seq"], actor="alice")

    def test_wiring_off_keeps_process_startup_constant(self):
        creds = video.xiaole_credentials()
        self.assertFalse(creds["wired"])
        self.assertEqual(creds["credential"], PROCESS_KEY)

    def test_wiring_on_without_publish_uses_live_environment(self):
        os.environ[pc.WIRING_ENV] = "xiaolevideo"
        creds = video.xiaole_credentials()
        self.assertTrue(creds["wired"])
        self.assertEqual(creds["source"], pc.SOURCE_ENV)
        self.assertEqual(creds["credential"], ENV_KEY_VALUE)
        self.assertIsNone(creds["version"])

    def test_image_guard_shares_the_same_entry(self):
        # 果肉生图（image.py）与视频（video.py）必须走同一入口：
        # 开启接线后两者都读实时环境变量，而不是各自的启动常量。
        os.environ[pc.WIRING_ENV] = "all"
        self.assertEqual(
            str(image.xiaole_credentials().get("credential") or ""), ENV_KEY_VALUE)
        self.assertEqual(
            video.xiaole_credentials()["credential"], ENV_KEY_VALUE)

    def test_xiaolevideo_target_is_not_editable(self):
        # 果肉生图已下架：不应出现可编辑按钮，也不得再建草稿
        st = pc.status("xiaolevideo")
        self.assertFalse(st["editable"])
        self.assertTrue(st["deprecated"])
        with self.assertRaises(pc.ProviderConfigError):
            pc.save_draft("xiaolevideo", secret="sk-x", actor="alice")

    def test_missing_credential_fails_before_any_request(self):
        os.environ.pop(ENV_KEY, None)
        os.environ.pop(pc.WIRING_ENV, None)
        video.XIAOLEVIDEO_API_KEY = ""
        with self.assertRaises(ValueError):
            video._xiaole_request("GET", "/api/v1/generations")

    def test_only_listed_target_is_wired(self):
        os.environ[pc.WIRING_ENV] = "image.banana.nb2"
        self.assertTrue(pc.wiring_enabled("image.banana.nb2"))
        self.assertFalse(pc.wiring_enabled("xiaolevideo"))
        self.assertEqual(video.xiaole_credentials()["wired"], False)


if __name__ == "__main__":
    unittest.main()
