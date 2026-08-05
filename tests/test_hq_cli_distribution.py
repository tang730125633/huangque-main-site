import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "site/downloads/hq/install.sh"
RELEASE = ROOT / "site/downloads/hq/v0.6.0"
WHEEL = RELEASE / "huangque_hq_cli-0.6.0-py3-none-any.whl"
SOURCE = ROOT / "tools/hq-cli/src/hq_cli"


class HQCLIDistributionTests(unittest.TestCase):
    def test_release_checksum_and_installer_are_pinned(self):
        expected, filename = (RELEASE / "SHA256SUMS").read_text().split()
        self.assertEqual(WHEEL.name, filename)
        self.assertEqual(expected, hashlib.sha256(WHEEL.read_bytes()).hexdigest())
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('wheel_sha256="%s"' % expected, source)
        self.assertIn('wheel_url="https://huangquechuanmei.com/downloads/hq/v0.6.0/$wheel_name"', source)
        self.assertNotIn("sudo", source)
        self.assertNotIn("eval", source)
        self.assertNotIn("HQ_INSTALL", source)

    def test_installer_refuses_regular_file_and_uses_versioned_target(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('target_dir="$data_root/$version"', source)
        self.assertIn('[ ! -L "$link_path" ]', source)
        self.assertIn('--force-reinstall "$wheel_path"', source)
        self.assertIn('ln -sfn "$target_dir/venv/bin/hq" "$link_path"', source)

    def test_moved_venv_entrypoint_can_be_repaired_from_final_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            target = Path(tmp) / "0.6.0"
            subprocess.run([sys.executable, "-m", "venv", stage / "venv"], check=True)
            subprocess.run(
                [stage / "venv/bin/python", "-m", "pip", "install", "--no-index", "--no-deps", WHEEL],
                check=True,
            )
            stage.rename(target)
            subprocess.run(
                [
                    target / "venv/bin/python", "-m", "pip", "install", "--no-index", "--no-deps",
                    "--force-reinstall", WHEEL,
                ],
                check=True,
            )
            subprocess.run([target / "venv/bin/hq", "version", "--json"], check=True)

    def test_release_wheel_contains_exact_cli_source(self):
        expected = sorted(path for path in SOURCE.glob("*.py"))
        with zipfile.ZipFile(WHEEL) as archive:
            packaged = sorted(name for name in archive.namelist() if name.startswith("hq_cli/"))
            self.assertEqual(["hq_cli/" + path.name for path in expected], packaged)
            for path in expected:
                self.assertEqual(path.read_bytes(), archive.read("hq_cli/" + path.name))


if __name__ == "__main__":
    unittest.main()
