# -*- coding: utf-8 -*-
"""视频号(channels)采集：封面拼 token + 提取文案解密后 ASR。

线上实测(2026-07-11, https://weixin.qq.com/sph/Arfd4zMMPP)：
- 封面 coverUrl+coverUrlToken → HTTP 200 有效 JPEG(裸 coverUrl 缺 token 报 400)。
- transcript 下载加密流 → :3001 Isaac64 解密 → whisper → 转出 285 字口播文案。

本测试守结构不变量(不联网)：真实链路用上面的线上实测背书。
"""
import pathlib
import re
import unittest

TIKHUB = pathlib.Path(__file__).resolve().parents[1] / "server/tikhub.py"
LEADS = pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/leads.py"
SRC = TIKHUB.read_text(encoding="utf-8")
LSRC = LEADS.read_text(encoding="utf-8")


class CoverTokenTests(unittest.TestCase):
    def test_cover_helper_appends_token(self):
        """_ch_cover_url = coverUrl + coverUrlToken；裸 coverUrl 缺 token 会 400。"""
        # 抽出纯函数用真实逻辑跑（无网络）
        m = re.search(r"def _ch_cover_url\(media\):.*?return .*?\n", SRC, re.S)
        self.assertTrue(m, "找不到 _ch_cover_url")
        ns = {}
        exec("def _ch_cover_url(media):\n" + m.group(0).split(":", 1)[1].split("\n", 1)[1], ns)
        f = ns["_ch_cover_url"]
        self.assertEqual(f({"coverUrl": "http://x/c?encfilekey=K", "coverUrlToken": "&token=T"}),
                         "http://x/c?encfilekey=K&token=T")
        self.assertEqual(f({"coverUrl": "http://x/c", "fullCoverUrlToken": "&token=F"}),
                         "http://x/c&token=F")
        self.assertIsNone(f({}))

    def test_ch_detail_uses_cover_helper(self):
        self.assertIn('"cover": _ch_cover_url(media)', SRC)
        self.assertNotIn('"cover": _url0(media.get("coverUrl")', SRC)


class TranscriptTests(unittest.TestCase):
    def test_channels_no_longer_hard_returns_none(self):
        """视频号不再一刀切 return None，改为下载→解密→whisper。"""
        block = SRC[SRC.index('if det.get("platform") == "channels":'):]
        block = block[:block.index("if det.get(\"subtitle_url\")")]
        self.assertIn("_ch_download_decrypt", block)
        self.assertIn("_whisper(", block)
        # 仍保留「缺播放地址/密钥 → None」的安全兜底
        self.assertIn("return None", block)

    def test_decrypt_helper_present(self):
        self.assertIn("def _ch_download_decrypt(", SRC)
        self.assertIn("/api/decrypt", SRC)            # :3001 Isaac64
        self.assertIn("channels.weixin.qq.com", SRC)  # 下载带微信 Referer


class LeadsCoverCosTests(unittest.TestCase):
    def test_channels_cover_transcoded_to_cos(self):
        self.assertIn("public_url_from_remote", LSRC)
        self.assertIn("collect/channels/cover_", LSRC)


if __name__ == "__main__":
    unittest.main()
