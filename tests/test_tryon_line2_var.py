# -*- coding: utf-8 -*-
"""换装两条线路的第一个上传框（#537）。

线路一收换装视频，线路二收人物照片，但原来共用一个 tryonVideoData。
后果：用户在线路一传了视频 → 切到线路二 → applyTryonLine() 不清数据 →
提交时把视频 base64 当 person_image_data 发给后端 → 扣点后失败退点、白等。

而且换装走 gen_tryon，不经过 core.py 里 kind=="image" 的 validate_image_payload，
享受不到 #534 的扣点前魔数校验 —— 这个错发是实打实扣点的。

守的不变量：
1. 两条线路各存各的变量
2. 提交时线路二发 tryonPersonImageData、线路一发 tryonVideoData
3. 用户切线路时清空第一个框（数据 + 预览 + input.value）
4. 清理只在「真的换了线路」时发生：初始化与「重新生成」回填也会调 applyTryonLine，
   那两种场景绝不能清数据
5. 第一个框的预览与 accept 都跟着线路走（线路二是图片，得用 <img> + image accept）
"""
import pathlib
import re
import unittest


VIDEO_HTML = (pathlib.Path(__file__).resolve().parents[1] / "site/workbench/video.html")
SRC = VIDEO_HTML.read_text(encoding="utf-8")


def _idx(needle):
    i = SRC.find(needle)
    assert i >= 0, "video.html 里找不到: %r" % needle
    return i


class SeparateVariablesTests(unittest.TestCase):
    def test_person_image_has_its_own_variable(self):
        self.assertIn("tryonPersonImageData", SRC)

    def test_accessors_route_by_line(self):
        self.assertIn("function tryonPrimaryData(){ return tryonIsLine2()?tryonPersonImageData:tryonVideoData; }", SRC)
        self.assertIn("function setTryonPrimaryData(v){ if(tryonIsLine2()) tryonPersonImageData=v; else tryonVideoData=v; }", SRC)

    def test_upload_callback_uses_accessor_not_raw_variable(self):
        """bindTryonFile 的 video 分支不能再直接写 tryonVideoData。"""
        self.assertIn("if(kind==='video') setTryonPrimaryData(data);", SRC)

    def test_regen_prefill_also_routes_by_line(self):
        """「重新生成」回填走的是 setTryonPrefill，同样不能写死 tryonVideoData。"""
        self.assertIn("if(kind==='video') setTryonPrimaryData(dataUrl);", SRC)


class PayloadTests(unittest.TestCase):
    def test_line2_sends_person_image_data(self):
        self.assertIn("person_image_data:tryonPersonImageData", SRC)

    def test_line2_never_sends_video_as_person_image(self):
        self.assertNotIn("person_image_data:tryonVideoData", SRC)

    def test_line1_still_sends_video(self):
        self.assertIn("person_video_data:tryonVideoData", SRC)

    def test_line2_submit_guard_checks_person_image(self):
        self.assertIn("if(!tryonPersonImageData){ toast('线路二请先上传人物照片'); return; }", SRC)


class LineSwitchResetTests(unittest.TestCase):
    def test_reset_clears_both_variables(self):
        self.assertIn("tryonVideoData=''; tryonPersonImageData='';", SRC)

    def test_reset_also_clears_preview_and_input_value(self):
        """只清变量不够 —— 框里还留着旧预览，用户会以为图还在。"""
        reset = SRC[_idx("function resetTryonPrimary()"):]
        reset = reset[:reset.find("\n  }") + 4]
        self.assertIn("classList.remove('has-media'", reset)
        self.assertIn("input.value=''", reset)
        self.assertIn("innerHTML=", reset)

    def test_reset_is_called_on_line_switch(self):
        self.assertIn("resetTryonPrimary();", SRC)
        self.assertLess(_idx("selectedTryonLine=b.dataset.line;"), _idx("resetTryonPrimary();"))

    def test_reset_not_called_from_applyTryonLine(self):
        """初始化与重生成回填都会调 applyTryonLine —— 那时清数据会把回填的图清掉。"""
        body = SRC[_idx("function applyTryonLine()"):]
        body = body[:body.find("\n  //")]
        self.assertNotIn("resetTryonPrimary", body)

    def test_clicking_current_line_does_not_wipe_uploads(self):
        self.assertIn("if(selectedTryonLine===b.dataset.line) return;", SRC)


class PreviewByLineTests(unittest.TestCase):
    def test_markup_switches_img_and_video(self):
        markup = SRC[_idx("function tryonPrimaryMarkup(src)"):]
        markup = markup[:markup.find("\n  }") + 4]
        self.assertIn("<img src=", markup)
        self.assertIn("<video src=", markup)
        self.assertIn("tryonIsLine2()", markup)

    def test_accept_follows_line_in_rebuilt_input(self):
        """重建 input 时若写死 video accept，会把 applyTryonLine 设的 image accept 覆盖回去。"""
        markup = SRC[_idx("function tryonPrimaryMarkup(src)"):]
        markup = markup[:markup.find("\n  }") + 4]
        self.assertIn("tryonIsLine2()?TRYON_IMG_ACCEPT:TRYON_VID_ACCEPT", markup)

    def test_no_hardcoded_video_accept_left_in_primary_box(self):
        """tryonVideoFile 的 accept 不能再有写死的 video 列表。"""
        self.assertNotRegex(
            SRC,
            r'id="tryonVideoFile"[^>]*accept="video/mp4',
            "第一个上传框仍有写死的 video accept",
        )


class HintTests(unittest.TestCase):
    def test_hint_uses_accessor(self):
        self.assertIn("hasPrimary=!!tryonPrimaryData()", SRC)

    def test_line2_hint_does_not_say_video(self):
        self.assertIn("text='请先上传人物照片'", SRC)


if __name__ == "__main__":
    unittest.main()
