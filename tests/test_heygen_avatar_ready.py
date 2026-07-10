# -*- coding: utf-8 -*-
"""HeyGen 动作模仿：必须等 photo avatar look 真正 completed 才提交生成。

线上根因（2026-07-10 用失败任务 id=1403 的原始素材复现）：

    [heygen] FAIL POST /videos -> HTTP 400 {"error":{"code":"invalid_parameter",
      "message":"Avatar look f7fb4dcacbef4d2fa9327378ac2f543a is not ready (status: pending)"}}
    [heygen] video_id=8137... status=pending → processing → failed
    RuntimeError: HeyGen视频生成失败: {"status":"failed","title":"follow_reference_motion"}

`/v3/avatars` 与 `/v3/avatars/{group}` 返回的是 **avatar 组**，组的 preview_image_url 在 look 仍
pending 时就已有值，且 URL 里正好含 look id：
    https://files2.heygen.ai/talking_photo/<look_id>/xxx.WEBP
老代码 `_avatar_ready_from_payload` 用「有 preview_image_url」判定就绪 → wait 立刻返回 → 提交被拒。
靠重试等到不再 400 也没用：avatar 没训练完，生成照样静默 failed 且 error:null。
HeyGen 动作模仿约 26% 的成功率，就是这个竞态的产物。

look 级状态只在 v2：GET /v2/photo_avatar/{look_id} → status ∈ {pending, completed, failed}。
实测：GET /v2/photo_avatar/f7fb... → {"status":"completed","group_id":"e1011d..."}
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

video = importlib.import_module("content_domains.video")

LOOK = "f7fb4dcacbef4d2fa9327378ac2f543a"
GROUP = "e1011d4d14bb40fa9d4b8bb353aefd97"
# 组接口返回的真实形态：有 preview_image_url（URL 里含 look id），但**没有** look 的状态
GROUP_PAYLOAD = {"data": {"id": GROUP, "looks_count": 1, "name": "huangque_photo_avatar_1783682448",
                          "preview_image_url": "https://files2.heygen.ai/talking_photo/%s/x.WEBP" % LOOK}}


class NoPreviewImageShortcutTests(unittest.TestCase):
    """回归守卫：绝不能再把「有预览图」当成 look 就绪。"""

    def test_old_broken_helper_is_gone(self):
        self.assertFalse(hasattr(video, "_avatar_ready_from_payload"),
                         "_avatar_ready_from_payload 用组的 preview_image_url 判 look 就绪，已删除，别再加回来")

    def test_readiness_never_keys_off_preview_image_url(self):
        """只看可执行代码（用 AST 剥掉 docstring/注释），preview_image_url 不得参与就绪判定。"""
        import ast
        tree = ast.parse(Path(video.__file__).read_text(encoding="utf-8"))
        for name in ("_heygen_look_status", "_heygen_wait_photo_avatar"):
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            body = list(fn.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]                       # 去掉 docstring
            code = "\n".join(ast.unparse(n) for n in body)
            self.assertNotIn("preview_image_url", code, name)


class LookStatusTests(unittest.TestCase):
    def test_direct_queries_v2_photo_avatar_endpoint(self):
        seen = {}

        def fake(method, url, body=None, ctype=None, timeout=None):
            seen.update(method=method, url=url)
            return {"data": {"id": LOOK, "status": "completed"}}

        with patch.object(video, "_heygen_direct_req", fake):
            status, msg = video._heygen_look_status(LOOK, GROUP, direct=True)
        self.assertEqual(status, "completed")
        self.assertEqual(seen["method"], "GET")
        self.assertTrue(seen["url"].endswith("/v2/photo_avatar/" + LOOK), seen["url"])
        self.assertNotIn("/v3/avatars", seen["url"])   # 组接口给不出 look 状态

    def test_direct_reports_moderation_message_on_failure(self):
        with patch.object(video, "_heygen_direct_req",
                          lambda *a, **k: {"data": {"status": "failed", "moderation_msg": "face not detected"}}):
            status, msg = video._heygen_look_status(LOOK, GROUP, direct=True)
        self.assertEqual(status, "failed")
        self.assertEqual(msg, "face not detected")

    def test_relay_group_payload_without_status_returns_unknown(self):
        """中转只转发 v3；组里没有 look 状态 → 返回未知，而不是谎报就绪。"""
        with patch.object(video, "_heygen_request_json", lambda *a, **k: GROUP_PAYLOAD):
            status, msg = video._heygen_look_status(LOOK, GROUP, direct=False)
        self.assertEqual(status, "")     # 关键：有 preview_image_url 也不算就绪

    def test_relay_without_group_id_still_queries_avatars(self):
        """必须真的发请求——否则鉴权失败会被『继续轮询』掩盖成超时。"""
        seen = {}

        def fake(method, path, **k):
            seen["path"] = path
            return {"data": []}

        with patch.object(video, "_heygen_request_json", fake):
            self.assertEqual(video._heygen_look_status(LOOK, "", direct=False), ("", ""))
        self.assertEqual(seen["path"], "/avatars")


class WaitPhotoAvatarTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(video.time, "sleep", lambda *_: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_waits_through_pending_until_completed(self):
        seq = iter([("pending", ""), ("pending", ""), ("completed", "")])
        with patch.object(video, "_heygen_look_status", lambda *a, **k: next(seq)):
            self.assertTrue(video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True))

    def test_pending_alone_is_not_ready(self):
        """老 bug 的直接守卫：只要状态是 pending，就绝不能返回 True，哪怕组里有预览图。"""
        with patch.object(video, "_heygen_look_status", lambda *a, **k: ("pending", "")), \
             patch.object(video, "HEYGEN_TIMEOUT", 0.001):
            with self.assertRaises(TimeoutError):
                video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True)

    def test_group_payload_with_preview_image_never_releases(self):
        """端到端复现老 bug 的输入：组有 preview_image_url（URL 含 look id）但 look 无状态。
        直连路径必须继续等，不得放行。"""
        with patch.object(video, "_heygen_request_json", lambda *a, **k: GROUP_PAYLOAD), \
             patch.object(video, "_heygen_direct_req", lambda *a, **k: {"data": {"status": "pending"}}), \
             patch.object(video, "HEYGEN_TIMEOUT", 0.001):
            with self.assertRaises(TimeoutError):
                video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True)

    def test_failed_raises_with_moderation_reason(self):
        with patch.object(video, "_heygen_look_status", lambda *a, **k: ("failed", "no face detected")):
            with self.assertRaises(RuntimeError) as ctx:
                video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True)
        self.assertIn("no face detected", str(ctx.exception))

    def test_query_error_propagates_and_is_not_swallowed(self):
        """既有不变量(test_avatar_poll_does_not_hide_request_error)：401 之类的错误不能被
        『继续轮询』吃掉伪装成超时。"""
        with patch.object(video, "_heygen_look_status",
                          side_effect=RuntimeError("HeyGen接口失败: HTTP 401 invalid key")):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True)

    def test_timeout_when_never_ready(self):
        with patch.object(video, "_heygen_look_status", lambda *a, **k: ("pending", "")), \
             patch.object(video, "HEYGEN_TIMEOUT", 0.001):
            with self.assertRaises(TimeoutError):
                video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True)

    def test_relay_unknown_status_releases_after_grace(self):
        """中转拿不到状态时不能死等到 900s，盲等一段后放行，交给 create 的 400 重试兜底。"""
        with patch.object(video, "_heygen_look_status", lambda *a, **k: ("", "")), \
             patch.object(video, "HEYGEN_AVATAR_UNKNOWN_GRACE", -1):
            self.assertTrue(video._heygen_wait_photo_avatar(LOOK, GROUP, direct=False))

    def test_direct_never_blind_releases(self):
        """直连能拿到真状态，绝不允许盲等放行。"""
        with patch.object(video, "_heygen_look_status", lambda *a, **k: ("", "")), \
             patch.object(video, "HEYGEN_AVATAR_UNKNOWN_GRACE", -1), \
             patch.object(video, "HEYGEN_TIMEOUT", 0.001):
            with self.assertRaises(TimeoutError):
                video._heygen_wait_photo_avatar(LOOK, GROUP, direct=True)


if __name__ == "__main__":
    unittest.main()
