# -*- coding: utf-8 -*-
"""试点闭环：后台编辑 → 验证 → 发布 → 新任务实际使用 → 在途任务不变 → 回滚。

试点线路：image.seedream（黄雀引擎 1 / 火山方舟，环境变量型、仍在生产使用）。
覆盖改造方案第八节中需要「闭环」才能验证的项：
  1/2/3 单改 URL、单改 Key、原子生效
  4     错误凭据验证失败不改生产
  5     验证后改字段则旧证据失效
  6     重复提交（同 op_id）不重复发布
  7     并发修改，旧版本提交被拒绝
  8     服务未加载新版本 → 不显示「配置已生效」
  9     新任务用新版本，在途任务继续用固定版本
  10    回滚后新任务使用恢复版本
  11    刷新后仍是后端真实状态
  12    输出中不出现明文 Key
"""

from __future__ import annotations

import base64
import json
import os
import shutil
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

TARGET = "image.seedream"          # 试点：黄雀引擎 1（火山方舟）
ENV_KEY_NAME = "ARK_API_KEY"
ENV_URL_NAME = "ARK_BASE"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3"
SECRET_A = "sk-ark-A-1111"
SECRET_B = "sk-ark-B-2222"


def probe_all_ok(url, key):
    return {"connection": {"ok": True, "status": 200},
            "auth": {"ok": True, "status": 200}}


def probe_auth_fail(url, key):
    return {"connection": {"ok": True, "status": 401},
            "auth": {"ok": False, "status": 401, "note": "凭据被拒绝"}}


class SeedreamLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="provider-loop-"))
        self.backup = dict(os.environ)
        os.environ["ADMIN_DB"] = str(self.tmp / "admin_config.db")
        os.environ["HQ_PROVIDER_KEYS_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"0123456789abcdef0123456789abcdef").decode("ascii")
        os.environ.pop("HQ_ADMIN_CONFIG_STORE", None)
        os.environ.pop(pc.WIRING_ENV, None)
        os.environ[ENV_KEY_NAME] = SECRET_A          # 生产起点：环境变量 A
        os.environ[ENV_URL_NAME] = ARK_URL
        pc.invalidate()
        pc.init_db()

    def tearDown(self):
        pc.invalidate()
        os.environ.clear()
        os.environ.update(self.backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 辅助 ----------

    def _draft_and_validate(self, secret=None, url=None, probe=probe_all_ok):
        draft = pc.save_draft(TARGET, url=url, secret=secret, actor="alice")
        result = pc.validate_draft(TARGET, draft["seq"], actor="alice", probe=probe)
        return draft, result

    def _publish(self, draft, expected, op_id):
        return pc.publish(TARGET, draft["seq"], expected_seq=expected,
                          op_id=op_id, actor="alice")

    def _new_task(self):
        """任务创建：固定版本 + 取本次要用的凭据。"""
        pin = pc.pin(TARGET)
        creds = pc.credentials_for(TARGET, legacy_key=SECRET_A, legacy_url=ARK_URL)
        return {"pinned_version": pin["version"], "credential": creds["credential"],
                "version": creds["version"], "source": creds["source"]}

    # ---------- 闭环主流程 ----------

    def test_full_loop(self):
        os.environ[pc.WIRING_ENV] = "all"

        # 起点：没有后台配置 → 用环境变量 A，但任务仍会固定到一个可恢复的「基线版本」
        task_a = self._new_task()
        self.assertEqual(task_a["source"], pc.SOURCE_ENV)
        self.assertEqual(task_a["credential"], SECRET_A)
        self.assertIsNotNone(task_a["pinned_version"], "任务必须有可恢复的版本引用")
        baseline_seq = task_a["pinned_version"]

        # 后台编辑（只改 Key）→ 验证 → 发布 B
        draft, validation = self._draft_and_validate(secret=SECRET_B)
        self.assertTrue(validation["ok"])
        self._publish(draft, expected=baseline_seq, op_id="loop-publish-B")

        # 发布后、服务尚未加载新版本 → 绝不能显示「配置已生效」
        eff = pc.effective_status(TARGET)
        self.assertNotEqual(eff["state"], pc.STATE_EFFECTIVE)
        self.assertNotEqual(eff["label"], "配置已生效")
        self.assertIn(eff["state"], (pc.STATE_PUBLISHING, pc.STATE_UNCONFIRMED))

        # 新任务用 B
        task_b = self._new_task()
        self.assertEqual(task_b["source"], pc.SOURCE_BACKEND)
        self.assertEqual(task_b["credential"], SECRET_B)
        self.assertEqual(task_b["version"], draft["seq"])
        self.assertEqual(task_b["pinned_version"], draft["seq"])

        # 在途任务（发布前创建的 task_a，pin 版本为空 = 环境变量）不受发布影响
        in_flight = pc.resolve_pinned(TARGET, task_a["pinned_version"],
                                      legacy_key=SECRET_A, legacy_url=ARK_URL)
        self.assertEqual(in_flight["credential"], SECRET_A)

        # 运行服务上报自己已加载新版本 → 才显示「配置已生效」
        pc.report_loaded(TARGET, draft["seq"], pc.SOURCE_BACKEND)   # 同进程自动实例 id
        eff = pc.effective_status(TARGET)
        self.assertEqual(eff["state"], pc.STATE_EFFECTIVE)
        self.assertEqual(eff["label"], "配置已生效")

        # 另一个实例还没刷新 → 仍只是「正在生效」
        pc.report_loaded(TARGET, None, pc.SOURCE_ENV, instance_id="svc-2")
        eff = pc.effective_status(TARGET)
        self.assertEqual(eff["state"], pc.STATE_PUBLISHING)
        self.assertIn("svc-2", eff["not_loaded_instances"])

        # 回滚 → 回到环境变量 A（首个后台发布时，上一可用状态就是环境变量）
        rb = pc.rollback(TARGET, expected_seq=draft["seq"], op_id="loop-rollback",
                         actor="alice")
        self.assertEqual(rb["source"], "env")
        self.assertEqual(rb["rolled_back_from"], draft["seq"])
        task_after_rb = self._new_task()
        self.assertEqual(task_after_rb["credential"], SECRET_A)
        self.assertEqual(task_after_rb["version"], rb["seq"])

        # 在途任务仍能按自己固定的版本解析（版本只增不删）
        pinned_b = pc.resolve_pinned(TARGET, draft["seq"])
        self.assertEqual(pinned_b["credential"], SECRET_B)
        self.assertIn(draft["seq"], pc.retained_versions(TARGET))

    # ---------- 验收 4：错误凭据 ----------

    def test_wrong_key_cannot_publish_and_production_unchanged(self):
        os.environ[pc.WIRING_ENV] = "all"
        good, _ = self._draft_and_validate(secret=SECRET_B)
        self._publish(good, expected=0, op_id="ok-B")

        bad, validation = self._draft_and_validate(secret="sk-wrong", probe=probe_auth_fail)
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["checks"]["auth"]["ok"], False)
        with self.assertRaises(pc.NotVerified):
            self._publish(bad, expected=good["seq"], op_id="bad-publish")
        # 生产仍是 B
        self.assertEqual(self._new_task()["credential"], SECRET_B)

    def test_wrong_url_is_rejected_before_any_draft(self):
        with self.assertRaises(pc.ProviderConfigError):
            pc.save_draft(TARGET, url="http://ark.cn-beijing.volces.com/api/v3",
                          secret=SECRET_B, actor="alice")
        with self.assertRaises(pc.ProviderConfigError):
            pc.save_draft(TARGET, url="https://evil.example.com/v1",
                          secret=SECRET_B, actor="alice")
        self.assertIsNone(pc.active_version(TARGET))

    # ---------- 验收 5 ----------

    def test_editing_after_validate_invalidates_evidence(self):
        first, first_validation = self._draft_and_validate(secret=SECRET_B)
        self.assertTrue(first_validation["ok"])
        # 验证后又改了字段 → 产生新版本，且新版本没有证据
        second = pc.save_draft(TARGET, secret="sk-ark-B2", actor="alice")
        self.assertNotEqual(first["seq"], second["seq"])
        versions = {v["seq"]: v for v in pc.list_versions(TARGET)}
        self.assertIsNone(versions[second["seq"]]["evidence"])
        with self.assertRaises(pc.NotVerified):
            self._publish(second, expected=0, op_id="after-edit")
        # 而旧版本自己的证据仍然有效，仍可发布（它没被改过）
        published = self._publish(first, expected=0, op_id="publish-first")
        self.assertEqual(published["seq"], first["seq"])

    # ---------- 验收 6：重复提交 / 发布响应超时后重试 ----------

    def test_duplicate_submit_and_timeout_retry_are_idempotent(self):
        draft, _ = self._draft_and_validate(secret=SECRET_B)
        first = self._publish(draft, expected=0, op_id="dup-op")
        retry = self._publish(draft, expected=0, op_id="dup-op")   # 模拟超时后重试
        self.assertFalse(first["idempotent"])
        self.assertTrue(retry["idempotent"])
        self.assertEqual(first["seq"], retry["seq"])
        published = [v for v in pc.list_versions(TARGET)
                     if v["status"] == pc.STATUS_PUBLISHED]
        self.assertEqual(len(published), 1)

    # ---------- 验收 7：并发 ----------

    def test_concurrent_edit_is_rejected(self):
        draft, _ = self._draft_and_validate(secret=SECRET_B)
        self._publish(draft, expected=0, op_id="c1")
        other, _ = self._draft_and_validate(secret="sk-ark-C")
        with self.assertRaises(pc.VersionConflict):
            self._publish(other, expected=0, op_id="c2")   # 仍以为是 0

    # ---------- 验收 11/12 ----------

    def test_state_persists_and_never_leaks_plaintext(self):
        draft, _ = self._draft_and_validate(secret=SECRET_B)
        self._publish(draft, expected=0, op_id="persist")
        pc.invalidate()
        snapshot = pc.status(TARGET)
        self.assertEqual(snapshot["version"], draft["seq"])
        self.assertEqual(snapshot["key_last4"], SECRET_B[-4:])
        blobs = [json.dumps(snapshot, ensure_ascii=False),
                 json.dumps(pc.list_versions(TARGET), ensure_ascii=False),
                 json.dumps(pc.effective_status(TARGET), ensure_ascii=False)]
        for blob in blobs:
            self.assertNotIn(SECRET_B, blob)

    # ---------- 验收：图片/视频独立 ----------

    def test_image_pilot_does_not_change_video_line(self):
        os.environ[pc.WIRING_ENV] = "all"
        draft, _ = self._draft_and_validate(secret=SECRET_B)
        self._publish(draft, expected=0, op_id="img-only")
        # 图片线路用 B
        self.assertEqual(pc.credentials_for("image.seedream", legacy_key=SECRET_A
                                            )["credential"], SECRET_B)
        # 视频侧（号池 provider=seedance）不走本入口，环境变量原样保留
        self.assertEqual(os.environ[ENV_KEY_NAME], SECRET_A)

    def test_deprecated_target_cannot_be_edited(self):
        with self.assertRaises(pc.ProviderConfigError):
            pc.save_draft("xiaolevideo", secret="sk-x", actor="alice")


if __name__ == "__main__":
    unittest.main()
