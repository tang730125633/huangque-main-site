import unittest
from pathlib import Path


class AdminErrorCodeUITests(unittest.TestCase):
    def test_request_stream_exposes_hq_error_code(self):
        html = (Path(__file__).parents[1] / "site/admin/index.html").read_text(encoding="utf-8")
        self.assertIn("if(x.hq_code)route.push", html)
        self.assertIn("esc(x.hq_code)", html)
        self.assertIn("errorCatalog[x.hq_code]", html)
        self.assertIn("taskRawField('错误说明',errorInfo.message||x.hq_code||'无')", html)


if __name__ == "__main__":
    unittest.main()
