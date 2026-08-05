import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PublicChannelBoundaryTests(unittest.TestCase):
    def test_public_home_uses_product_names_not_provider_catalog(self):
        home = (ROOT / "site/index.html").read_text(encoding="utf-8")
        for public_name in ("黄雀引擎 1", "黄雀引擎 2", "果肉视频", "数字人口播", "AI 配音"):
            self.assertIn(public_name, home)
        for private_name in (
            "xAI API", "OpenAI API", "Google Gemini API", "火山方舟 API",
            "MiniMax 中国区 API", "HeyGen API", "RunningHub API",
            "WaveSpeed API", "阿里百炼 API", "TikHub API", "腾讯云 COS",
        ):
            self.assertNotIn(private_name, home)
        self.assertNotIn("hq channels --json", home)
        self.assertEqual(home.count('<article class="channel-card" data-product-channel>'), 13)

    def test_admin_keeps_private_provider_graph_and_key_table(self):
        admin = (ROOT / "site/admin/index.html").read_text(encoding="utf-8")
        self.assertIn('id="channelMap"', admin)
        self.assertIn("API 渠道关系图", admin)
        self.assertIn("展开线路与密钥明细", admin)
        self.assertIn("channel-map-card", admin)
        self.assertIn("xAI API · 果肉视频", admin)


if __name__ == "__main__":
    unittest.main()
