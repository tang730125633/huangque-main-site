import json
import shutil
import subprocess
import unittest
from pathlib import Path


PAGE = Path(__file__).resolve().parents[1] / "site" / "login.html"


class LoginReturnPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_next_is_used_before_legacy_redirect(self):
        source = self.html[self.html.index("function enterWorkbench"):self.html.index("function saveToken")]
        self.assertIn('var next=safeNext(params.get("next"))', source)
        self.assertIn("if(next){ location.href=next; return; }", source)
        self.assertLess(source.index('params.get("next")'), source.index('params.get("redirect")'))

    def test_only_same_origin_workbench_html_paths_are_accepted(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        helper = self.html[self.html.index("function safeNext"):self.html.index("function enterWorkbench")]
        values = [
            "/workbench/ip12.html?project=p1&module=2&step=3",
            "/workbench/ip12-report.html?project=p1",
            "/workbench/ip12?project=p1&module=2&step=3",
            "/workbench/ip12-report?project=p1",
            "https://evil.example/workbench/ip12.html",
            "//evil.example/workbench/ip12.html",
            "/api/gen/digital-ip/projects",
            "javascript:alert(1)",
        ]
        script = 'const location={origin:"https://huangquechuanmei.com"};\n' + helper + \
            "\nconsole.log(JSON.stringify(" + json.dumps(values) + ".map(safeNext)));"
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), [values[0], values[1], values[2], values[3], "", "", "", ""])


if __name__ == "__main__":
    unittest.main()
