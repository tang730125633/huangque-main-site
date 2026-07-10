# -*- coding: utf-8 -*-
"""「重新生成」回填换装任务时按素材类型恢复线路。

#545 给两条线路拆了独立变量（tryonVideoData / tryonPersonImageData），setTryonPrefill
改走 setTryonPrimaryData() —— 它按**当前** selectedTryonLine 选变量和预览标签。

但回填走 GET /api/gen/job/{id}，返回里没有 line 字段，页面默认线路是二。于是重新生成
一个**线路一**的换装任务时，视频会被存进 tryonPersonImageData，还用 <img> 渲染视频
（根本显示不出来）；用户再点线路一，resetTryonPrimary 又把它清了。

（#545 之前两条线路共用 tryonVideoData，所以回填"碰巧"总是对的。这是 #545 引入的回归。）

素材本身能证明线路：线路一存 .mp4/.mov/.webm，线路二存 .jpg/.png/.webp，
fetchFilePrefill 的 dataUrl 也自带 MIME。

守的不变量：
1. tryonLineFromAsset 按 MIME 判线路，MIME 缺失时退回看扩展名
2. 回填时**先**恢复线路**再** setTryonPrefill（顺序反了就白搭）
3. switchTryonLine 只同步 tab 高亮与 accept/文案，**不清数据**
   （清理只在用户点 tab 时做 —— 回填时清就把刚填的素材清掉了）
"""
import json
import pathlib
import re
import shutil
import subprocess
import unittest


VIDEO_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/video.html"
SRC = VIDEO_HTML.read_text(encoding="utf-8")


def _idx(needle):
    i = SRC.find(needle)
    assert i >= 0, "video.html 里找不到: %r" % needle
    return i


class LineDetectionBehaviourTests(unittest.TestCase):
    """用 node 跑真实的 tryonLineFromAsset。"""

    CASES = [
        # (dataUrl, url, 期望线路)
        ("data:video/mp4;base64,AAAA", "", "1"),
        ("data:video/quicktime;base64,AAAA", "", "1"),
        ("data:image/jpeg;base64,AAAA", "", "2"),
        ("data:image/png;base64,AAAA", "", "2"),
        ("data:image/webp;base64,AAAA", "", "2"),
        ("DATA:VIDEO/MP4;base64,AAAA", "", "1"),          # 大小写不敏感
        # blob.type 缺失 → 退回扩展名
        ("", "/api/gen/file/tryon_person_abc.mp4", "1"),
        ("", "/api/gen/file/tryon_person_abc.webm", "1"),
        ("", "/api/gen/file/tryon_person_img_abc.jpg", "2"),
        ("", "/api/gen/file/tryon_person_img_abc.PNG", "2"),
        ("", "/api/gen/file/x.webp?fresh=123", "2"),       # 带 query
        # 都判不出来 → 空，交给调用方保持当前线路
        ("", "", ""),
        ("data:application/octet-stream;base64,AA", "/x.bin", ""),
    ]

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("no node")
        m = re.search(r"function tryonLineFromAsset\(dataUrl, url\)\{[\s\S]*?\n  \}", SRC)
        assert m, "找不到 tryonLineFromAsset"
        cls.fn = m.group(0)

    def test_all_cases(self):
        args = [[c[0], c[1]] for c in self.CASES]
        script = "%s\nconsole.log(JSON.stringify(%s.map(function(a){return tryonLineFromAsset(a[0],a[1]);})));" % (
            self.fn, json.dumps(args))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        got = json.loads(out.stdout.strip())
        for (data_url, url, want), actual in zip(self.CASES, got):
            self.assertEqual(want, actual, "tryonLineFromAsset(%r, %r)" % (data_url, url))


class PrefillOrderTests(unittest.TestCase):
    def test_line_restored_before_prefill(self):
        """顺序反了，setTryonPrefill 就还是按旧线路存。"""
        self.assertLess(
            _idx("switchTryonLine(tryonLineFromAsset(v.dataUrl, d.reference_video_url));"),
            _idx("setTryonPrefill('video',v.dataUrl,v.previewUrl);"),
        )

    def test_prefill_uses_asset_type_not_current_line(self):
        self.assertIn("switchTryonLine(tryonLineFromAsset(v.dataUrl, d.reference_video_url));", SRC)


class SwitchTryonLineTests(unittest.TestCase):
    def test_switch_does_not_clear_data(self):
        """回填时若清数据，会把刚填进去的素材清掉。"""
        body = SRC[_idx("function switchTryonLine(line)"):]
        body = body[:body.find("\n  }") + 4]
        self.assertNotIn("resetTryonPrimary", body)
        self.assertNotIn("tryonVideoData=''", body)

    def test_switch_syncs_tab_and_ui(self):
        body = SRC[_idx("function switchTryonLine(line)"):]
        body = body[:body.find("\n  }") + 4]
        self.assertIn("classList.toggle('active'", body)
        self.assertIn("applyTryonLine()", body)

    def test_switch_ignores_invalid_and_noop(self):
        body = SRC[_idx("function switchTryonLine(line)"):]
        body = body[:body.find("\n  }") + 4]
        self.assertIn("if(line!=='1' && line!=='2') return;", body)
        self.assertIn("if(selectedTryonLine===line) return;", body)


class DurationCopyTests(unittest.TestCase):
    """时长文案按线上实测写，别再给「约 1 分钟」这种预期落差（#539 提出）。"""

    def test_line2_duration_is_honest(self):
        self.assertIn("约 5-10 分钟 · 人物照片+衣服图", SRC)
        self.assertNotIn("约 1 分钟 · 人物照片+衣服图", SRC)

    def test_line1_duration_is_honest(self):
        self.assertIn("约 15-25 分钟 · 人物视频+衣服图", SRC)
        self.assertNotIn("约 8 分钟 · 人物视频+衣服图", SRC)


if __name__ == "__main__":
    unittest.main()
