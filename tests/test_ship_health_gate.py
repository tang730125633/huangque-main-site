import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ship"


class ShipHealthGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self._write_executable(
            "git",
            """#!/bin/sh
if [ "$1" = "diff" ]; then exit 0; fi
if [ "$1" = "rev-parse" ]; then echo abc1234; exit 0; fi
exit 0
""",
        )
        self._write_executable(
            "ssh",
            """#!/bin/sh
case "$*" in
  *"import content_api"*)
    if [ "$FAKE_IMPORT_FAIL" = "1" ]; then exit 1; fi
    ;;
  *"systemctl is-active"*)
    if [ "$FAKE_SERVICE_INACTIVE" = "1" ]; then exit 1; fi
    ;;
esac
exit 0
""",
        )
        self._write_executable(
            "curl",
            """#!/bin/sh
if [ "$FAKE_CURL_FAIL" = "1" ]; then printf 000; exit 7; fi
printf %s "${FAKE_CURL_CODE:-200}"
""",
        )
        self._write_executable(
            "rsync",
            """#!/bin/sh
if [ -n "$FAKE_RSYNC_LOG" ]; then printf '%s\n' "$*" >> "$FAKE_RSYNC_LOG"; fi
exit 0
""",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_executable(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run_ship(self, target="unknown.file", **overrides):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HOME": str(self.home),
            "HQ_REMOTE": "fake-server",
            "HQ_SERVICE_WAIT_SECONDS": "1",
        })
        env.update(overrides)
        return subprocess.run(
            ["bash", str(SHIP), "test deployment", target],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )

    def test_http_200_allows_success(self):
        result = self._run_ship(FAKE_CURL_CODE="200")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("健康检查: HTTP 200", result.stdout)
        self.assertIn("上线完成", result.stdout)

    def test_http_502_blocks_deployment_success(self):
        result = self._run_ship(FAKE_CURL_CODE="502")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("健康检查未通过: HTTP 502", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_curl_failure_blocks_deployment_success(self):
        result = self._run_ship(FAKE_CURL_FAIL="1")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("健康检查请求失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_service_must_be_active_after_restart(self):
        result = self._run_ship(
            target="server/content_api.py",
            FAKE_SERVICE_INACTIVE="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("未进入 active", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_content_domain_change_syncs_directory_and_imports_before_restart(self):
        rsync_log = Path(self.tmp.name) / "rsync.log"
        result = self._run_ship(
            target="server/content_domains/core.py",
            FAKE_CURL_CODE="200",
            FAKE_RSYNC_LOG=str(rsync_log),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("整目录同步", result.stdout)
        self.assertIn("content_api import 通过", result.stdout)
        self.assertIn("server/content_domains/", rsync_log.read_text(encoding="utf-8"))

    def test_content_import_failure_stops_before_restart(self):
        result = self._run_ship(
            target="server/content_domains/core.py",
            FAKE_IMPORT_FAIL="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("content_api import 失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)


if __name__ == "__main__":
    unittest.main()
