import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy/ship-creator-stage.sh"


class ShipCreatorStageTests(unittest.TestCase):
    @staticmethod
    def bash():
        candidates = [shutil.which("bash"), "D:/Git/bin/bash.exe"]
        return next((item for item in candidates if item and pathlib.Path(item).exists()), "")

    def run_harness(self, body, fail_once=False):
        bash = self.bash()
        if not bash:
            self.skipTest("bash is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            fake_ssh = root / "fake-ssh"
            log = root / "ssh.log"
            marker = root / "fail-once"
            fake_ssh.write_text(
                "#!/usr/bin/env bash\n"
                "payload=$(cat)\n"
                "printf 'ARGS:%s\\nSCRIPT:%s\\n' \"$*\" \"$payload\" >> \"$FAKE_SSH_LOG\"\n"
                "if [ -f \"$FAKE_SSH_FAIL_ONCE\" ]; then\n"
                "  rm -f \"$FAKE_SSH_FAIL_ONCE\"\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            if fail_once:
                marker.write_text("fail", encoding="ascii")
            environment = {
                **os.environ,
                "FAKE_SSH_LOG": log.as_posix(),
                "FAKE_SSH_FAIL_ONCE": marker.as_posix(),
            }
            script = (
                'set -u\nsource "%s"\nSSHC="%s"\nREMOTE=fake-remote\n%s'
                % (HELPER.as_posix(), fake_ssh.as_posix(), body)
            )
            result = subprocess.run(
                [bash, "-lc", script], env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            return result, log.read_text(encoding="utf-8") if log.exists() else ""

    def test_success_cleanup_uses_sudo_and_clears_stage_only_after_success(self):
        stage = "/tmp/huangque-creator-agent-abcdef123-101"
        result, log = self.run_harness(
            'CREATOR_REMOTE_STAGE="%s"\n'
            'cleanup_creator_remote_stage\n'
            'test -z "$CREATOR_REMOTE_STAGE"\n' % stage,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bash -s -- %s" % stage, log)
        self.assertIn('sudo rm -rf -- "$stage"', log)

    def test_failed_cleanup_keeps_stage_for_exit_trap_retry(self):
        stage = "/tmp/huangque-creator-agent-abcdef123-202"
        result, log = self.run_harness(
            'CREATOR_REMOTE_STAGE="%s"\n'
            'if cleanup_creator_remote_stage; then exit 9; fi\n'
            'test "$CREATOR_REMOTE_STAGE" = "%s"\n'
            'cleanup_creator_remote_stage\n'
            'test -z "$CREATOR_REMOTE_STAGE"\n' % (stage, stage),
            fail_once=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.count("bash -s -- %s" % stage), 2)

    def test_invalid_stage_is_rejected_without_remote_delete(self):
        result, log = self.run_harness(
            'CREATOR_REMOTE_STAGE="/tmp/not-a-creator-release"\n'
            'if cleanup_creator_remote_stage; then exit 9; fi\n'
            'test -n "$CREATOR_REMOTE_STAGE"\n',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log, "")


if __name__ == "__main__":
    unittest.main()
