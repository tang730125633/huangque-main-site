"""Run with python3 tests/test_readonly_mcp_logs.py."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "readonly_mcp", Path(__file__).resolve().parents[1] / "tools/readonly-mcp/server.py"
)
mcp = importlib.util.module_from_spec(spec)
with patch.dict("os.environ", {"MCP_TOKEN_FILE": "/nonexistent"}):
    spec.loader.exec_module(mcp)


class JournalTests(unittest.TestCase):
    def test_all_log_paths_use_unprivileged_journal_and_redact(self):
        result = subprocess.CompletedProcess([], 0, "2026-09-22 12:00:00 API key=TEST_SECRET\n", "")
        with patch.object(mcp.subprocess, "run", return_value=result) as run, \
                patch.object(mcp, "run", return_value="backup listing"):
            for name, args in (("recent_logs", {"service": "huangque-content", "lines": "999"}),
                               ("error_summary", {"hours": "999"}), ("backup_status", {})):
                out, error = mcp.call_tool(name, args)
                self.assertFalse(error)
                self.assertNotIn("TEST_SECRET", out)
                command = run.call_args.args[0]
                self.assertEqual(command[0], "journalctl")
                self.assertNotIn("sudo", command)
                self.assertTrue(run.call_args.kwargs["check"])
                self.assertNotIn("shell", run.call_args.kwargs)
            self.assertIn("200", run.call_args_list[0].args[0])
            self.assertIn("72 hours ago", run.call_args_list[1].args[0])

    def test_command_failure_is_a_tool_error(self):
        with patch.object(mcp.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "journalctl")):
            out, error = mcp.call_tool("error_summary", {"hours": "24"})
            self.assertTrue(error)
            self.assertIn("CalledProcessError", out)

    def test_partial_permission_is_not_success(self):
        result = subprocess.CompletedProcess([], 0, "", "You are currently not seeing messages from other users")
        with patch.object(mcp.subprocess, "run", return_value=result):
            self.assertTrue(mcp.call_tool("recent_logs", {"service": "huangque-content"})[1])


if __name__ == "__main__":
    unittest.main()
