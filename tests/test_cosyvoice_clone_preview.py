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
    def test_clone_backfills_preview_asynchronously(self):
        """复刻先落 ready、试听【异步】回填(#602)——不用 slot_status 门控、不拖慢就绪。"""
        block = SRC[SRC.index("def _clone_via_cosyvoice"):]
        block = block[:block.index("return {")]
        self.assertIn("_cosy_backfill_preview_async(voice_id", block)   # 异步回填
        # 不再在主 UPDATE 里同步写 preview（那会被就绪窗口竞态卡住→无试听）
        self.assertNotIn("preview_file, preview_url = _cosy_clone_preview(voice_id) if slot_status", block)

    def test_preview_helper_retries_the_readiness_window(self):
        """#602 根因:复刻当刻音色未就绪，synth 会失败。preview 必须对 synth 短重试轮询——
        synth 能出声才是就绪的权威信号，比 voice_status 的 OK 更准。"""
        helper = SRC[SRC.index("def _cosy_clone_preview"):]
        helper = helper[:helper.index("def _cosy_backfill_preview_async")]
        self.assertIn("cosyvoice.synth(voice_id", helper)          # 用复刻音色合成
        self.assertIn("public_url(", helper)                       # 存 COS 直链
        self.assertIn("return None, None", helper)                 # 失败降级不阻断
        self.assertIn("voice_preview_", helper)                    # 不可猜键
        self.assertIn("for i in range(CLONE_PREVIEW_TRIES)", helper)  # 重试轮询
        self.assertIn("time.sleep", helper)                        # 给就绪留窗口

    def test_backfill_only_fills_empty_preview(self):
        """异步回填只补空 preview，别覆盖已回填/后续重刻的值。"""
        helper = SRC[SRC.index("def _cosy_backfill_preview_async"):]
        helper = helper[:helper.index("def ensure_audio_voice")]
        self.assertIn("preview_url IS NULL OR preview_url=''", helper)
        self.assertIn("threading.Thread", helper)                  # 后台线程，不阻塞复刻


class AlloyPlaceholderTests(unittest.TestCase):
    """#604: alloy 占位音色删了又回来——ensure_audio_voice 不再自动建占位 + 启动清存量。"""

    def test_ensure_audio_voice_no_longer_creates_alloy_placeholder(self):
        block = SRC[SRC.index("def ensure_audio_voice"):]
        block = block[:block.index("def resolve_audio_provider_voice")]
        # personal 分支命中已有行就返回，否则返回 None——不再 INSERT 一条 alloy 占位
        self.assertIn("return None", block)
        self.assertNotIn("VALUES('personal',?,?,?,?,?,?)", block)

    def test_startup_cleans_alloy_placeholders(self):
        self.assertIn("def _cleanup_alloy_placeholder_voices", SRC)
        self.assertIn("DELETE FROM audio_voices WHERE scope='personal' AND provider_voice='alloy'", SRC)
        # backfill 启动时先清占位（否则重放历史 audio job 又把它们建回来）
        backfill = SRC[SRC.index("def backfill_audio_assets"):]
        backfill = backfill[:backfill.index("PUBLIC_VOICE_SAMPLE_TEXT")]
        self.assertIn("_cleanup_alloy_placeholder_voices()", backfill)


if __name__ == "__main__":
    unittest.main()
