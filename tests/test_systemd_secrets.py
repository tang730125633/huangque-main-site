# -*- coding: utf-8 -*-
"""CI 门禁：禁止把明文密钥写进 systemd 单元。

背景：线上 /etc/systemd/system/huangque-content.service.d/doubao.conf 里直接写着
    Environment="DOUBAO_TOKEN=..."
而且权限是 644 —— ubuntu 用户无需 sudo 就能读到（已收紧为 600）。
密钥应当走 EnvironmentFile 指向 600 的 env 文件，那种文件本来就不进 git。

把仓库里的 systemd 单元当作正本之后，很容易顺手把线上的 doubao.conf 一起同步进来。
这条门禁就是拦这个的。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_validate  # noqa: E402


class SystemdSecretGuardTests(unittest.TestCase):
    def _run(self, rel_path, content):
        """把内容写进仓库内的临时文件，跑门禁，返回错误列表。"""
        from pathlib import PurePosixPath
        target = ROOT / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        try:
            return ci_validate.check_systemd_secrets([PurePosixPath(rel_path)])
        finally:
            target.unlink()

    def test_blocks_plaintext_token(self):
        errs = self._run("deploy/systemd/huangque-content.service.d/_t.conf",
                         '[Service]\nEnvironment="DOUBAO_TOKEN=realsecret123"\n')
        self.assertEqual(len(errs), 1)
        self.assertIn("明文密钥", errs[0])
        self.assertIn("DOUBAO_TOKEN", errs[0])

    def test_blocks_various_secret_key_names(self):
        for key in ("COS_SECRET_KEY", "HQ_INTERNAL_TOKEN", "DB_PASSWORD", "OPENAI_API_KEY", "SESSION_COOKIE"):
            errs = self._run("deploy/systemd/huangque-auth.service.d/_t.conf",
                             '[Service]\nEnvironment="%s=abc123"\n' % key)
            self.assertEqual(len(errs), 1, key)

    def test_allows_non_secret_environment(self):
        errs = self._run("deploy/systemd/huangque-content.service.d/_t.conf",
                         '[Service]\nEnvironment="WHISPER_MODEL=small"\nEnvironment="HF_HUB_OFFLINE=1"\n')
        self.assertEqual(errs, [])

    def test_allows_environment_file(self):
        errs = self._run("deploy/systemd/huangque-auth.service.d/_t.conf",
                         "[Service]\nEnvironmentFile=-/home/ubuntu/auth-service/auth.env\n")
        self.assertEqual(errs, [])

    def test_example_files_are_exempt(self):
        """.example 里放占位符是合法的 —— 那正是我们希望大家提交的东西。"""
        errs = self._run("deploy/systemd/huangque-content.service.d/_t.conf.example",
                         '[Service]\nEnvironment="DOUBAO_TOKEN=realsecret123"\n')
        self.assertEqual(errs, [])

    def test_placeholder_value_is_allowed(self):
        errs = self._run("deploy/systemd/huangque-content.service.d/_t.conf",
                         '[Service]\nEnvironment="DOUBAO_TOKEN=<从 content.env 取>"\n')
        self.assertEqual(errs, [])

    def test_empty_value_is_allowed(self):
        errs = self._run("deploy/systemd/huangque-content.service.d/_t.conf",
                         '[Service]\nEnvironment="DOUBAO_TOKEN="\n')
        self.assertEqual(errs, [])

    def test_ignores_files_outside_deploy_systemd(self):
        errs = self._run("scripts/_t.conf", '[Service]\nEnvironment="DOUBAO_TOKEN=x"\n')
        self.assertEqual(errs, [])

    # --- 真实仓库自检：现有的单元与 drop-in 里不该有明文密钥 ---
    def test_repo_systemd_files_are_clean(self):
        from pathlib import PurePosixPath
        tracked = [PurePosixPath(p) for p in subprocess.run(
            ["git", "ls-files", "deploy/systemd"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()]
        self.assertTrue(tracked, "deploy/systemd 下应有被跟踪的文件")
        self.assertEqual(ci_validate.check_systemd_secrets(tracked), [])


if __name__ == "__main__":
    unittest.main()
