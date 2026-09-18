# -*- coding: utf-8 -*-
"""content_domains 服务器热修的回归锁。

这些改动此前**只存在于生产服务器**（git 历史里从未出现），任何一次
`ship server/content_domains/*` 都会整目录 --delete 覆盖掉它们，且都不会报错。
本文件把其中行为可测的部分钉住，避免以后被静默改回去。

覆盖：
1. 一次性邀请码账号 → 模板成片只能用公网素材（09-17 老板定调）
2. 受限账号的临时硬闸：入口直接报错，绝不生成
3. 视频上传额度 6 → 20（09-12 老板拍板）
4. error_contract.STATUS_CODES：同一 HTTP 状态取**第一个**错误码（不是最后一个）
5. 内测免费化：计费关闭时全员免费音色槽位（09-12 老板拍板）
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "server")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from content_domains import audio, cli_uploads, error_contract, matrix_template_video


class PublicOnlyMaterialsTests(unittest.TestCase):
    """public_only_materials(user)：判定只依据 auth /me 透出的 single_use_invite。"""

    def test_flagged_account_is_restricted(self):
        self.assertTrue(matrix_template_video.public_only_materials({"single_use_invite": True}))

    def test_unflagged_account_is_not_restricted(self):
        # 永久码注册的老账号：字段缺失 → 不受限
        self.assertFalse(matrix_template_video.public_only_materials({}))
        self.assertFalse(matrix_template_video.public_only_materials({"single_use_invite": False}))
        self.assertFalse(matrix_template_video.public_only_materials({"single_use_invite": None}))

    def test_admin_and_non_dict_are_never_restricted(self):
        self.assertFalse(matrix_template_video.public_only_materials(
            {"single_use_invite": True, "role": "admin"}))
        self.assertFalse(matrix_template_video.public_only_materials(
            {"single_use_invite": True, "role": "ADMIN"}))
        for value in (None, [], "qilin", 0):
            self.assertFalse(matrix_template_video.public_only_materials(value), value)

    def test_material_scope_constant_is_stable(self):
        # 存进 payload 的值会回放（见 validate_payload 里的 material_scope 还原），不能改
        self.assertEqual(matrix_template_video.MATERIAL_SCOPE_PUBLIC_ONLY, "public_only")


class MaterialScopeGateTests(unittest.TestCase):
    """受限账号的临时硬闸：在入口报错，不进 preflight/jobs。"""

    TEMPLATE = {
        "id": "native-bold", "name": "模板", "description": "说明", "tags": [],
        "engine": "ffmpeg", "font_mode": "selectable", "font_selectable": True,
        "bgm_mode": "optional", "bgm_optional": True,
        "duration_mode": "fixed_12", "fixed_duration_seconds": 12,
    }
    BODY = {"top_text": "顶部标题", "bottom_text": "底部行动文案", "template_id": "native-bold"}

    def _run(self, public_only):
        """跑一次 validate_payload，返回（抛出的异常, 是否请求过渲染端）。"""
        m = matrix_template_video
        seen = {"requested": False}

        def fake_request(*args, **kwargs):
            seen["requested"] = True
            return {"payload": {}}  # 后续契约校验会不过，无所谓，只关心有没有走到这一步

        raised = None
        with mock.patch.object(m, "require_available"), \
                mock.patch.object(m, "public_templates", return_value=[dict(self.TEMPLATE)]), \
                mock.patch.object(m, "public_fonts", return_value=[]), \
                mock.patch.object(m, "_normalize_voiceover", return_value=None), \
                mock.patch.object(m, "_resolve_user_materials", return_value=[]), \
                mock.patch.object(m, "_request", side_effect=fake_request):
            try:
                m.validate_payload(dict(self.BODY), "qilin",
                                   public_only_materials=public_only)
            except Exception as exc:  # noqa: BLE001 - 只关心是不是被硬闸挡下
                raised = exc
        return raised, seen["requested"]

    def test_restricted_account_is_blocked_before_any_request(self):
        raised, requested = self._run(True)
        self.assertIsInstance(raised, ValueError)
        self.assertIn("暂时无法生成", str(raised))
        # 关键：绝不能先发出 preflight/jobs 再报错
        self.assertFalse(requested)

    def test_unrestricted_account_passes_the_gate(self):
        raised, requested = self._run(False)
        # 不受限账号必须继续走到渲染端（硬闸没有误伤）
        self.assertTrue(requested)
        self.assertNotIn("暂时无法生成", str(raised))

    def test_signature_accepts_the_flag(self):
        # core.py / cli_gateway.py 用关键字传这个参数，签名被改就会 TypeError
        import inspect
        params = inspect.signature(matrix_template_video.validate_payload).parameters
        self.assertIn("public_only_materials", params)
        self.assertFalse(params["public_only_materials"].default)


class UploadLimitTests(unittest.TestCase):
    def test_video_upload_file_limit_is_20(self):
        # 09-12 老板拍板 6→20 与图片/音频看齐（额度满曾误伤顾客）
        self.assertEqual(cli_uploads.VIDEO_MAX_USER_FILES, 20)


class ErrorContractTests(unittest.TestCase):
    def test_status_codes_keep_the_first_code_for_a_status(self):
        # 同一 HTTP 状态下多个错误码时，取目录里**先出现**的那个（setdefault 语义）。
        # 旧写法 dict 推导式会取最后一个，后台拉到的错误码会变。
        expected = {}
        for code, item in error_contract.CATALOG.items():
            expected.setdefault(item["status"], code)
        self.assertEqual(error_contract.STATUS_CODES, expected)
        for status, code in expected.items():
            self.assertEqual(error_contract.STATUS_CODES[status], code)


class AudioFreeDuringBetaTests(unittest.TestCase):
    def test_voice_slot_entitlement_is_free_when_billing_off(self):
        from content_domains import feature_flags
        with mock.patch.object(feature_flags, "points_billing_enabled", return_value=False), \
                mock.patch.object(audio, "_auth_points_request",
                                  side_effect=AssertionError("计费关闭时不该去问会员权益")):
            self.assertTrue(audio._membership_voice_slot_entitlement("qilin"))

    def test_voice_slot_entitlement_asks_membership_when_billing_on(self):
        from content_domains import feature_flags
        with mock.patch.object(feature_flags, "points_billing_enabled", return_value=True), \
                mock.patch.object(audio, "_auth_points_request",
                                  return_value={"entitlement": {"eligible": True}}) as request:
            self.assertTrue(audio._membership_voice_slot_entitlement("qilin"))
            self.assertTrue(request.called)

    def test_voice_slot_entitlement_not_entitled_when_membership_says_no(self):
        from content_domains import feature_flags
        with mock.patch.object(feature_flags, "points_billing_enabled", return_value=True), \
                mock.patch.object(audio, "_auth_points_request",
                                  return_value={"entitlement": {"eligible": False}}):
            self.assertFalse(audio._membership_voice_slot_entitlement("qilin"))


if __name__ == "__main__":
    unittest.main()
