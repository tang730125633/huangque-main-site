import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "deploy" / "hermes-ip12-release.sh"


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class HermesReleaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / "release"
        self.app = self.root / "app"
        self.data = self.app / "data"
        self.backups = self.root / "backups"
        self.bin = self.root / "bin"
        self.etc = self.root / "etc"
        self.state = self.root / "state"
        self.release_app = self.release / "server" / "hermes_ip12"
        self.release_deploy = self.release / "deploy"
        self.release_scripts = self.release / "scripts"
        for path in (
            self.app,
            self.data,
            self.bin,
            self.etc,
            self.state,
            self.release_app,
            self.release_deploy / "systemd",
            self.release_scripts,
        ):
            path.mkdir(parents=True, exist_ok=True)

        (self.app / "version.txt").write_text("old\n", encoding="utf-8")
        (self.release_app / "version.txt").write_text("new\n", encoding="utf-8")
        (self.release_app / "requirements.txt").write_text("", encoding="utf-8")
        (self.release_scripts / "migrate_hermes_artifacts.py").write_text(
            "# test migration stub\n", encoding="utf-8"
        )
        (self.release_deploy / "systemd" / "hermes-ip12-preview.service").write_text(
            "new unit\n", encoding="utf-8"
        )
        (self.release_deploy / "nginx-hermes-ip12-direct.conf").write_text(
            "new direct\n", encoding="utf-8"
        )
        (self.release_deploy / "nginx-huangquechuanmei.conf").write_text(
            "new site\n", encoding="utf-8"
        )

        self.unit = self.etc / "hermes.service"
        self.direct_available = self.etc / "direct.available"
        self.direct_enabled = self.etc / "direct.enabled"
        self.site_available = self.etc / "site.available"
        self.site_enabled = self.etc / "site.enabled"
        for path, content in (
            (self.unit, "old unit\n"),
            (self.direct_available, "old direct\n"),
            (self.direct_enabled, "old direct enabled\n"),
            (self.site_available, "old site\n"),
            (self.site_enabled, "old site enabled\n"),
        ):
            path.write_text(content, encoding="utf-8")

        self.env_file = self.root / "hermes.env"
        self.env_file.write_text(
            "HERMES_LEGACY_OWNER=legacy-user\nHERMES_DATA_QUOTA_MB=2048\n",
            encoding="utf-8",
        )
        (self.state / "active").write_text("active\n", encoding="utf-8")
        (self.state / "enabled").write_text("enabled\n", encoding="utf-8")
        self.log = self.state / "commands.log"
        self.deploy_user = subprocess.check_output(
            ["id", "-un"], text=True
        ).strip()
        self.deploy_group = subprocess.check_output(
            ["id", "-gn"], text=True
        ).strip()
        self.env_file.write_text(
            "HERMES_LEGACY_OWNER=legacy-user\n"
            "HERMES_DATA_QUOTA_MB=2048\n"
            f"HERMES_DEPLOY_USER={self.deploy_user}\n"
            f"HERMES_DEPLOY_GROUP={self.deploy_group}\n",
            encoding="utf-8",
        )
        self._write_tool(
            "install",
            """#!/usr/bin/env bash
set -eu
echo "install $*" >> "$FAKE_COMMAND_LOG"
args=()
while test "$#" -gt 0; do
  case "$1" in
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/install "${args[@]}"
""",
        )
        self._write_tool(
            "systemctl",
            """#!/usr/bin/env bash
set -eu
echo "$*" >> "$FAKE_COMMAND_LOG"
cmd="$1"; shift || true
if test "${HERMES_ROLLBACK_ACTIVE:-}" = 1 \
    && test "${HERMES_ROLLBACK_FAULT:-}" = systemctl \
    && test "$cmd" = restart; then
  exit 91
fi
case "$cmd" in
  is-active)
    test "$(cat "$FAKE_STATE/active")" = active
    test "${1:-}" = "--quiet" || cat "$FAKE_STATE/active"
    ;;
  is-enabled) cat "$FAKE_STATE/enabled" ;;
  stop) printf 'inactive\n' > "$FAKE_STATE/active" ;;
  restart|start) printf 'active\n' > "$FAKE_STATE/active" ;;
  enable) printf 'enabled\n' > "$FAKE_STATE/enabled" ;;
  disable) printf 'disabled\n' > "$FAKE_STATE/enabled" ;;
  daemon-reload|reload) ;;
esac
""",
        )
        self._write_tool(
            "rsync",
            """#!/usr/bin/env bash
set -eu
echo "rsync $*" >> "$FAKE_COMMAND_LOG"
if test "${HERMES_ROLLBACK_ACTIVE:-}" = 1 \
    && test "${HERMES_ROLLBACK_FAULT:-}" = rsync; then
  exit 90
fi
previous=""; last=""
for value in "$@"; do previous="$last"; last="$value"; done
src="${previous%/}"; dest="${last%/}"
mkdir -p "$dest"
case " $* " in
  *" --delete "*)
    find "$dest" -mindepth 1 -maxdepth 1 \
      ! -name data ! -name media_library ! -name knowledge \
      -exec rm -rf {} +
    ;;
esac
cp -a "$src/." "$dest/"
""",
        )
        self._write_tool(
            "python",
            """#!/usr/bin/env bash
set -eu
echo "python $*" >> "$FAKE_COMMAND_LOG"
case " $* " in
  *" -m pip "*) test "${HERMES_FAULT_AFTER:-}" != pip || exit 86 ;;
  *" -c "*) printf '76\n' ;;
esac
""",
        )
        self._write_tool(
            "curl",
            """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_COMMAND_LOG"
if test "${HERMES_ROLLBACK_ACTIVE:-}" = 1 \
    && test "${HERMES_ROLLBACK_FAULT:-}" = curl \
    && test "$*" = "-fsS http://local.test/healthz"; then
  exit 93
fi
case "$*" in
  *public.test*) test "${HERMES_FAULT_AFTER:-}" != health || exit 88 ;;
esac
exit 0
""",
        )
        self._write_tool(
            "nginx",
            """#!/usr/bin/env bash
echo "nginx $*" >> "$FAKE_COMMAND_LOG"
if test "${HERMES_ROLLBACK_ACTIVE:-}" = 1 \
    && test "${HERMES_ROLLBACK_FAULT:-}" = nginx \
    && test "$*" = "-t"; then
  exit 92
fi
exit 0
""",
        )
        for name in ("systemd-analyze", "ffmpeg", "ffprobe", "yt-dlp", "edge-tts"):
            self._write_tool(
                name,
                f"#!/usr/bin/env bash\necho \"{name} $*\" >> \"$FAKE_COMMAND_LOG\"\nexit 0\n",
            )

    def tearDown(self):
        self.temp.cleanup()

    def _write_tool(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _run(self, fault, rollback_fault=""):
        environment = os.environ.copy()
        fake_path = str(self.bin) + os.pathsep + environment.get("PATH", "")
        environment.update(
            HERMES_RELEASE_DIR=str(self.release),
            HERMES_SHA="deadbeef",
            HERMES_APP_DIR=str(self.app),
            HERMES_DATA_DIR=str(self.data),
            HERMES_BACKUP_ROOT=str(self.backups),
            HERMES_LAST_BACKUP_FILE=str(self.root / "last-backup"),
            HERMES_ENV_FILE=str(self.env_file),
            HERMES_SYSTEMD_TARGET=str(self.unit),
            HERMES_NGINX_DIRECT_AVAILABLE=str(self.direct_available),
            HERMES_NGINX_DIRECT_ENABLED=str(self.direct_enabled),
            HERMES_NGINX_SITE_AVAILABLE=str(self.site_available),
            HERMES_NGINX_SITE_ENABLED=str(self.site_enabled),
            HERMES_PYTHON=str(self.bin / "python"),
            HERMES_SUDO="",
            HERMES_COMMAND_PATH=fake_path,
            HERMES_LOCAL_HEALTH_URL="http://local.test/healthz",
            HERMES_PUBLIC_HEALTH_URLS="http://public.test/healthz",
            HERMES_FAULT_AFTER=fault,
            HERMES_ROLLBACK_FAULT=rollback_fault,
            FAKE_COMMAND_LOG=str(self.log),
            FAKE_STATE=str(self.state),
            PATH=fake_path,
        )
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )

    def _assert_rolled_back(self, fault):
        result = self._run(fault)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertIn("Hermes rollback completed:", result.stderr)
        self.assertNotIn("Hermes rollback FAILED", result.stderr)
        self.assertEqual((self.app / "version.txt").read_text(), "old\n")
        self.assertFalse((self.app / "scripts").exists())
        self.assertEqual(self.unit.read_text(), "old unit\n")
        self.assertEqual(self.direct_available.read_text(), "old direct\n")
        self.assertEqual(self.direct_enabled.read_text(), "old direct enabled\n")
        self.assertEqual(self.site_available.read_text(), "old site\n")
        self.assertEqual(self.site_enabled.read_text(), "old site enabled\n")
        self.assertEqual((self.state / "active").read_text().strip(), "active")
        self.assertEqual((self.state / "enabled").read_text().strip(), "enabled")
        self.assertFalse((self.root / "last-backup").exists())
        commands = self.log.read_text(encoding="utf-8")
        self.assertIn("daemon-reload", commands)
        self.assertIn("restart hermes-ip12-preview.service", commands)
        self.assertIn("curl -fsS http://local.test/healthz", commands)

    def _assert_rollback_failure(self, fault, expected_step):
        result = self._run("rsync", fault)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertIn("Hermes rollback FAILED", result.stderr)
        self.assertIn("manual recovery required", result.stderr)
        self.assertIn(f"failed_steps={expected_step}", result.stderr)
        self.assertIn(f"backup={self.backups}", result.stderr)
        self.assertNotIn("Hermes rollback completed:", result.stderr)
        commands = self.log.read_text(encoding="utf-8")
        self.assertIn("daemon-reload", commands)
        self.assertIn("nginx -t", commands)
        self.assertIn("curl -fsS http://local.test/healthz", commands)
        return commands

    def test_creates_a_fresh_private_backup_tree_owned_by_deploy_account(self):
        self.assertFalse(self.backups.exists())
        result = self._run("")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backup = Path((self.root / "last-backup").read_text().strip())
        self.assertTrue((backup / "code").is_dir())
        commands = self.log.read_text(encoding="utf-8")
        ownership = (
            f"install -d -o {self.deploy_user} -g {self.deploy_group} "
            f"-m 0700 {self.backups}"
        )
        self.assertIn(ownership, commands)

    def test_rolls_back_when_failure_occurs_after_rsync(self):
        self._assert_rolled_back("rsync")

    def test_rolls_back_when_dependency_installation_fails(self):
        self._assert_rolled_back("pip")

    def test_rolls_back_when_health_gate_fails(self):
        self._assert_rolled_back("health")

    def test_reports_manual_recovery_when_rollback_rsync_fails(self):
        commands = self._assert_rollback_failure(
            "rsync", "restore application files with rsync"
        )
        self.assertIn("restart hermes-ip12-preview.service", commands)

    def test_reports_manual_recovery_when_rollback_systemctl_fails(self):
        commands = self._assert_rollback_failure(
            "systemctl", "restart restored service"
        )
        self.assertIn("is-active --quiet hermes-ip12-preview.service", commands)

    def test_reports_manual_recovery_when_rollback_nginx_validation_fails(self):
        commands = self._assert_rollback_failure(
            "nginx", "validate restored nginx configuration"
        )
        self.assertNotIn("reload nginx", commands)
        self.assertIn("restart hermes-ip12-preview.service", commands)

    def test_reports_manual_recovery_when_rollback_health_probe_fails(self):
        self._assert_rollback_failure("curl", "verify restored service health")


if __name__ == "__main__":
    unittest.main()
