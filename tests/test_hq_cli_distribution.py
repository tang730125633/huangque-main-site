import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "site/downloads/hq/install.sh"
WINDOWS_INSTALLER = ROOT / "site/downloads/hq/install.ps1"
WINDOWS_UNINSTALLER = ROOT / "site/downloads/hq/uninstall.ps1"
VERSION = "0.11.3"
OLD_VERSION = "0.11.2"
RELEASE = ROOT / ("site/downloads/hq/v" + VERSION)
WHEEL = RELEASE / ("huangque_hq_cli-%s-py3-none-any.whl" % VERSION)
OLD_WHEEL = ROOT / ("site/downloads/hq/v%s/huangque_hq_cli-%s-py3-none-any.whl" % (
    OLD_VERSION, OLD_VERSION))
SOURCE = ROOT / "tools/hq-cli/src/hq_cli"
PYTHON = shutil.which("python3.11") or shutil.which("python3.10") or sys.executable


class HQCLIDistributionTests(unittest.TestCase):
    def test_release_sources_match_version_and_keep_shell_installer_executable(self):
        readme = (ROOT / "tools/hq-cli/README.md").read_text(encoding="utf-8")
        for installer in ("install.ps1", "install.sh"):
            with self.subTest(installer=installer):
                self.assertIn(
                    "https://huangquechuanmei.com/downloads/hq/%s" % installer,
                    readme,
                )
        self.assertNotIn(
            "raw.githubusercontent.com/tang730125633/huangque-cli/", readme,
        )

        stage = subprocess.run(
            ["git", "ls-files", "--stage", "--", "site/downloads/hq/install.sh"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        self.assertEqual("100755", stage[0])

        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "v%s\\huangque_hq_cli-%s-py3-none-any.whl" % (VERSION, VERSION),
            workflow,
        )
        self.assertIn('$result.cli_version -ne "%s"' % VERSION, workflow)

    def test_release_checksum_and_installer_are_pinned(self):
        expected, filename = (RELEASE / "SHA256SUMS").read_text().split()
        self.assertEqual(WHEEL.name, filename)
        self.assertEqual(expected, hashlib.sha256(WHEEL.read_bytes()).hexdigest())
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('wheel_sha256="%s"' % expected, source)
        self.assertIn('wheel_url="https://huangquechuanmei.com/downloads/hq/v%s/$wheel_name"' % VERSION, source)
        self.assertNotIn("sudo", source)
        self.assertNotIn("eval", source)
        self.assertNotIn("HQ_INSTALL", source)

        powershell = WINDOWS_INSTALLER.read_text(encoding="utf-8")
        self.assertIn('$WheelSha256 = "%s"' % expected, powershell)
        self.assertIn('https://huangquechuanmei.com/downloads/hq/v%s/$WheelName' % VERSION, powershell)
        self.assertIn("catch {\n            $CandidateExitCode = 1\n        }", powershell)
        self.assertIn('PowerShell 5.1', (ROOT / "tools/hq-cli/README.md").read_text(encoding="utf-8"))

    def test_previous_release_remains_immutable(self):
        self.assertEqual(
            "2361b2b73bdade243c378776cf13486bb1180f2115360c61a0e2058a3e34ca23",
            hashlib.sha256(OLD_WHEEL.read_bytes()).hexdigest(),
        )

    def test_windows_install_and_uninstall_are_managed(self):
        install = WINDOWS_INSTALLER.read_text(encoding="utf-8")
        uninstall = WINDOWS_UNINSTALLER.read_text(encoding="utf-8")
        self.assertIn('"Huangque\\hq-cli"', install)
        self.assertIn(':: Huangque HQ CLI managed launcher', install)
        self.assertIn('[Environment]::SetEnvironmentVariable("Path", $NewPath, "User")', install)
        self.assertIn('$PurgeCredentials', uninstall)
        self.assertIn('refusing to delete', uninstall)

    def test_homepage_offers_windows_and_mac_cli_downloads(self):
        home = (ROOT / "site/index.html").read_text(encoding="utf-8")
        self.assertIn('href="/downloads/hq/install.ps1">Windows CLI', home)
        self.assertIn('href="/downloads/hq/install.sh">Mac CLI', home)
        self.assertIn('>WINDOWS</strong><code data-command>irm ', home)
        self.assertIn('>MAC</strong><code data-command>curl ', home)
        self.assertEqual(2, home.count('<button type="button" data-copy>'))
        self.assertIn("copyButton.closest('.install-line').querySelector('[data-command]')", home)

    def test_installer_refuses_regular_file_and_uses_versioned_target(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('target_dir="$data_root/$version"', source)
        self.assertIn('current_target="$(readlink "$link_path")"', source)
        self.assertIn('"$data_root"/*/venv/bin/hq)', source)
        self.assertIn('--force-reinstall "$wheel_path"', source)
        self.assertIn('ln -sfn "$target_dir/venv/bin/hq" "$link_path"', source)

    @unittest.skipIf(os.name == "nt", "POSIX installer entrypoints are verified on Linux CI")
    def test_moved_venv_entrypoint_can_be_repaired_from_final_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            target = Path(tmp) / VERSION
            subprocess.run([PYTHON, "-m", "venv", stage / "venv"], check=True)
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

    @unittest.skipIf(os.name == "nt", "POSIX upgrade flow is verified on Linux CI")
    def test_installer_upgrades_existing_0105_and_exposes_agent_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            data_home = root / "data"
            bin_root = home / ".local/bin"
            old_target = data_home / "hq-cli" / OLD_VERSION
            fake_bin = root / "fake-bin"
            bin_root.mkdir(parents=True)
            fake_bin.mkdir()
            subprocess.run([PYTHON, "-m", "venv", old_target / "venv"], check=True)
            subprocess.run([
                old_target / "venv/bin/python", "-m", "pip", "install",
                "--no-index", "--no-deps", OLD_WHEEL,
            ], check=True)
            (bin_root / "hq").symlink_to(old_target / "venv/bin/hq")
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-o\" ]; then shift; cp \"$HQ_TEST_WHEEL\" \"$1\"; exit 0; fi\n"
                "  shift\n"
                "done\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(data_home),
                       HQ_TEST_WHEEL=str(WHEEL),
                       PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
            before = json.loads(subprocess.check_output(
                [bin_root / "hq", "version", "--json"], text=True, env=env))
            self.assertEqual(OLD_VERSION, before["cli_version"])
            subprocess.run(["sh", INSTALLER], check=True, env=env)
            after = json.loads(subprocess.check_output(
                [bin_root / "hq", "version", "--json"], text=True, env=env))
            capabilities = json.loads(subprocess.check_output(
                [bin_root / "hq", "capabilities", "--json"], text=True, env=env))
            self.assertEqual(VERSION, after["cli_version"])
            self.assertIn("text-video-generate", {
                item["id"] for item in capabilities["capabilities"]})
            self.assertIn("text-video-plan", {
                item["id"] for item in capabilities["capabilities"]})
            self.assertIn("text-video-avatar-import", {
                item["id"] for item in capabilities["capabilities"]})
            self.assertIn("agent", next(
                item for item in capabilities["capabilities"] if item["id"] == "ip12-project"
            ))
            self.assertIn("matrix-template-generate", {
                item["id"] for item in capabilities["capabilities"]})
            self.assertIn("audio-upload", {
                item["id"] for item in capabilities["capabilities"]})
            self.assertEqual(
                (data_home / "hq-cli" / VERSION / "venv/bin/hq").resolve(),
                (bin_root / "hq").resolve(),
            )

    def test_release_wheel_has_the_pinned_version(self):
        with zipfile.ZipFile(WHEEL) as archive:
            metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            metadata_bytes = archive.read(metadata)
            self.assertIn(("Version: " + VERSION).encode(), metadata_bytes)
            self.assertIn(b"License-File: LICENSE", metadata_bytes)
            normalized_metadata = metadata_bytes.replace(b"\r\n", b"\n")
            normalized_readme = (ROOT / "tools/hq-cli/README.md").read_bytes().replace(b"\r\n", b"\n")
            self.assertIn(normalized_readme, normalized_metadata)
            self.assertNotIn(b"raw.githubusercontent.com/tang730125633/huangque-cli/", metadata_bytes)
            license_file = next(name for name in archive.namelist() if name.endswith(".dist-info/licenses/LICENSE"))
            self.assertEqual((ROOT / "tools/hq-cli/LICENSE").read_bytes(), archive.read(license_file))

    def test_release_wheel_contains_exact_cli_source(self):
        expected = sorted(path for path in SOURCE.glob("*.py"))
        with zipfile.ZipFile(WHEEL) as archive:
            packaged = sorted(name for name in archive.namelist() if name.startswith("hq_cli/"))
            self.assertEqual(["hq_cli/" + path.name for path in expected], packaged)
            for path in expected:
                self.assertEqual(path.read_bytes(), archive.read("hq_cli/" + path.name))


if __name__ == "__main__":
    unittest.main()
