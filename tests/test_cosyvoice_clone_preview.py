# -*- coding: utf-8 -*-
"""CosyVoice 复刻后生成试听样音(#voice-clone-preview)。

CosyVoice 的 create_voice 不返样音，原来 _clone_via_cosyvoice 把 preview 置 NULL，
前端(audio.html/video.html)只在有 preview_url 时才显示试听按钮 → 复刻后没试听。
现在复刻后自己合成一句(_cosy_clone_preview)存 COS，preview_url 落库。

线上实测(2026-07-11)：对真实复刻音色 synth 一句 61113 字节 mp3；失败降级 (None,None) 不阻断复刻。
本测试守结构不变量(不联网)。
"""
import pathlib
import unittest

AUDIO = pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/audio.py"
SRC = AUDIO.read_text(encoding="utf-8")


class ClonePreviewTests(unittest.TestCase):
    def test_clone_generates_preview_not_null(self):
        """复刻不再把 preview 一律置 NULL，而是调 _cosy_clone_preview。"""
        block = SRC[SRC.index("def _clone_via_cosyvoice"):]
        block = block[:block.index("return {")]
        self.assertIn("_cosy_clone_preview(voice_id)", block)
        # UPDATE 用变量 preview_file/preview_url，不再写死 NULL
        self.assertIn("SET display_name=?, provider_voice=?, preview_file=?", block)
        self.assertNotIn("preview_file=NULL,\n            preview_url=NULL, slot_id=?, updated_at=? WHERE username=? AND scope='personal'", block)

    def test_preview_helper_safe_and_uploads_cos(self):
        helper = SRC[SRC.index("def _cosy_clone_preview"):]
        helper = helper[:helper.index("def _clone_via_cosyvoice")]
        self.assertIn("cosyvoice.synth(voice_id", helper)      # 用复刻音色合成
        self.assertIn("public_url(", helper)                   # 存 COS 直链
        self.assertIn("return None, None", helper)             # 失败降级不阻断
        self.assertIn("voice_preview_", helper)                # 不可猜键


if __name__ == "__main__":
    unittest.main()
