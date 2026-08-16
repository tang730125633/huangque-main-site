import unittest
from pathlib import Path


class AdminErrorCodeUITests(unittest.TestCase):
    def test_request_table_exposes_hq_error_code(self):
        html = (Path(__file__).parents[1] / "site/admin/index.html").read_text(encoding="utf-8")
        self.assertIn("<th>HTTP</th><th>黄雀码</th>", html)
        self.assertIn("errorCatalog[x.hq_code]", html)
        self.assertIn("路径 / 用户 / 功能 / 黄雀错误码 / 请求号", html)


if __name__ == "__main__":
    unittest.main()
