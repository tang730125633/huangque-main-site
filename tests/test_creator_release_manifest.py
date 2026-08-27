import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/creator-agent-release.sh"


class CreatorReleaseManifestTests(unittest.TestCase):
    @staticmethod
    def bash():
        candidates = [shutil.which("bash"), "D:/Git/bin/bash.exe"]
        return next((item for item in candidates if item and pathlib.Path(item).exists()), "")

    def test_manifest_includes_every_runtime_module_and_missing_file_fails(self):
        bash = self.bash()
        if not bash:
            self.skipTest("bash is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            staging = pathlib.Path(directory)
            files = (
                "server/creator_agent_api.py",
                "server/creator_agent/__init__.py",
                "server/creator_agent/store.py",
                "server/creator_agent/planner.py",
                "server/creator_agent/profile_agent.py",
                "server/creator_agent/model_usage.py",
                "server/creator_agent/service.py",
                "deploy/systemd/huangque-creator-agent.service",
                "deploy/nginx-huangquechuanmei.conf",
            )
            for relative in files:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("placeholder", encoding="utf-8")
            environment = {
                **os.environ,
                "CREATOR_AGENT_RELEASE_DIR": str(staging),
                "CREATOR_AGENT_BACKUP_DIR": str(staging / "release-backup"),
                "CREATOR_AGENT_VALIDATE_ONLY": "1",
            }
            (staging / "release-backup").mkdir()
            complete = subprocess.run(
                [bash, str(SCRIPT)], env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(complete.returncode, 0, complete.stderr)
            (staging / "server/creator_agent/profile_agent.py").unlink()
            missing = subprocess.run(
                [bash, str(SCRIPT)], env=environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("missing release file", missing.stderr)
            self.assertIn("profile_agent.py", missing.stderr)


if __name__ == "__main__":
    unittest.main()
