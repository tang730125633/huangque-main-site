import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASE = (ROOT / "site" / "case.html").read_text(encoding="utf-8")


class CaseSharingTests(unittest.TestCase):
    def test_case_path_only_accepts_positive_numeric_ids(self):
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        helper = CASE[CASE.index("function caseIdFromPath"):CASE.index("function safeMediaUrl")]
        values = ["/case/63", "/case/1000001/", "/case/0", "/case/abc", "/workbench/63"]
        script = helper + "\nconsole.log(JSON.stringify(" + json.dumps(values) + ".map(caseIdFromPath)));"
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), [63, 1000001, 0, 0, 0])

    def test_share_flow_uses_case_id_and_safe_registration_return(self):
        gallery = (ROOT / "site" / "workbench" / "inspiration.html").read_text(encoding="utf-8")
        banana = (ROOT / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")
        login = (ROOT / "site" / "login.html").read_text(encoding="utf-8")
        self.assertIn("location.origin+'/case/'", gallery)
        self.assertIn("?inspiration='+encodeURIComponent(x.id)", gallery)
        self.assertIn("function loadInspiration(caseId)", banana)
        self.assertIn('get("mode")==="register"', login)
        self.assertIn("'/login?mode=register&next='", CASE)

    def test_both_nginx_configs_route_clean_case_links(self):
        route = 'location ~ "^/case/[1-9][0-9]{0,14}/?$"'
        for relative in ("deploy/nginx-huangquechuanmei.conf", "server/nginx-huangquechuanmei.conf"):
            self.assertIn(route, (ROOT / relative).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
