import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ship-zelong"
SRC = SHIP.read_text(encoding="utf-8")


def call_map(path: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SHIP_ZELONG_LIB_ONLY"] = "1"
    return subprocess.run(
        ["bash", "-c", 'source "$1"; map_path "$2"', "_", str(SHIP), path],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def baseline_files() -> list[str]:
    env = os.environ.copy()
    env["SHIP_ZELONG_LIB_ONLY"] = "1"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; load_adspower_baseline; printf "%s\\n" "${files[@]}"', "_", str(SHIP)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.splitlines()


def unchanged_paths_match(repo: Path, source: str, requested: str, paths: list[str]) -> bool:
    env = os.environ.copy()
    env["SHIP_ZELONG_LIB_ONLY"] = "1"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; validate_unchanged_paths "$@"',
            "_",
            str(SHIP),
            source,
            requested,
            *paths,
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


class ZelongDeploymentSafetyTests(unittest.TestCase):
    def test_shell_syntax(self):
        result = subprocess.run(["bash", "-n", str(SHIP)], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_target_is_hard_locked(self):
        self.assertIn('readonly REMOTE="zelong"', SRC)
        self.assertIn('readonly DOMAIN="zelong.huangquechuanmei.com"', SRC)
        self.assertNotIn("dapeng-server", SRC)
        self.assertNotIn("https://huangquechuanmei.com", SRC)

    def test_adspower_baseline_is_exact_and_commit_locked(self):
        self.assertIn('readonly ADSPOWER_BASELINE_SOURCE_COMMIT="fb59e511c16f11edef1617b07fe6f2160c14e78e"', SRC)
        self.assertEqual(
            [
                "site/workbench/banana.html",
                "site/workbench/video.html",
                "server/auth_server.py",
                "server/wxpay.py",
                "server/content_domains/core.py",
                "server/content_domains/image.py",
                "server/content_domains/video.py",
                "site/assets/cloud/virtual-pay-item-200.png",
                "server/wechat_virtual_pay.py",
                "server/content_domains/miniprogram_security.py",
                "deploy/systemd/huangque-content.service.d/hardening.conf",
            ],
            baseline_files(),
        )
        self.assertIn("--adspower-baseline 禁止与 --file 混用", SRC)

    def test_adspower_baseline_accepts_unchanged_newer_commit_and_rejects_changed_content(self):
        # CI 使用浅克隆，不能假设历史中的 fb59e51 对象已被检出。用临时仓库
        # 验证同一 helper 的 fail-closed 行为，不进行网络 fetch，也不放宽部署规则。
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "CI"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "ci@example.invalid"], cwd=repo, check=True)

            (repo / "target.txt").write_text("audited\n", encoding="utf-8")
            subprocess.run(["git", "add", "target.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
            source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            (repo / "unrelated.txt").write_text("safe\n", encoding="utf-8")
            subprocess.run(["git", "add", "unrelated.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=repo, check=True)
            unchanged = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            (repo / "target.txt").write_text("changed\n", encoding="utf-8")
            subprocess.run(["git", "add", "target.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "changed"], cwd=repo, check=True)
            changed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            self.assertTrue(unchanged_paths_match(repo, source, source, ["target.txt"]))
            self.assertTrue(unchanged_paths_match(repo, source, unchanged, ["target.txt"]))
            self.assertFalse(unchanged_paths_match(repo, source, changed, ["target.txt"]))
            self.assertFalse(unchanged_paths_match(repo, "0" * 40, changed, ["target.txt"]))

        self.assertIn(
            'validate_unchanged_paths "$ADSPOWER_BASELINE_SOURCE_COMMIT" "$requested" "$@"',
            SRC,
        )

    def test_only_auth_and_content_can_restart(self):
        restarts = re.findall(r"systemctl restart ([A-Za-z0-9_-]+)", SRC)
        self.assertTrue(restarts)
        self.assertEqual({"huangque-auth", "huangque-content"}, set(restarts))

    def test_rejects_non_whitelisted_backend_and_sensitive_paths(self):
        for path in (
            "server/admin_api.py",
            "server/imggen_api.py",
            "deploy/systemd/huangque-admin.service",
            "server/secret.env",
            "data/users.db",
            "../site/index.html",
        ):
            with self.subTest(path=path):
                self.assertNotEqual(0, call_map(path).returncode)

    def test_maps_dependencies_before_entrypoints_and_html(self):
        cases = {
            "server/tikhub.py": (10, "/home/ubuntu/content-api/tikhub.py", "content", 0),
            "server/content_domains/core.py": (20, "/home/ubuntu/content-api/content_domains/core.py", "content", 0),
            "server/content_api.py": (30, "/home/ubuntu/content-api/content_api.py", "content", 0),
            "site/workbench/cloud-shell.js": (40, "/var/www/huangquechuanmei/workbench/cloud-shell.js", "-", 0),
            "site/workbench/video.html": (50, "/var/www/huangquechuanmei/workbench/video.html", "-", 0),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                result = call_map(path)
                self.assertEqual(0, result.returncode, result.stderr)
                rank, target, service, reload = result.stdout.strip().split("|")
                self.assertEqual(expected, (int(rank), target, service, int(reload)))

    def test_systemd_change_requires_reload(self):
        result = call_map("deploy/systemd/huangque-content.service.d/hardening.conf")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("1", result.stdout.strip().split("|")[-1])
        self.assertIn("systemctl daemon-reload", SRC)

    def test_dry_run_plan_is_required_for_apply(self):
        self.assertIn('test -n "$supplied" || die', SRC)
        self.assertIn('cmp -s "$payload" "$dir/$id.plan"', SRC)
        self.assertIn("【DRY-RUN】未上传文件、未备份、未重启", SRC)

    def test_fixed_commit_and_exact_sha_checks_are_present(self):
        self.assertIn("git cat-file -e", SRC)
        self.assertIn("git ls-remote --exit-code origin refs/heads/main", SRC)
        self.assertIn("拒绝 stale ref 或未 push", SRC)
        self.assertIn("git show", SRC)
        self.assertGreaterEqual(SRC.count("sha256sum"), 5)
        self.assertIn("/api/auth/health", SRC)
        self.assertIn("/api/gen/health", SRC)

    def test_health_checks_wait_through_the_service_startup_window(self):
        retry_options = (
            "--retry 15 --retry-delay 2 --retry-max-time 35 "
            "--retry-connrefused --max-time 5"
        )
        # deploy has remote + caller-side checks, and rollback has remote checks;
        # both auth and content must tolerate the brief nginx 502 startup window.
        self.assertEqual(6, SRC.count(retry_options))
        self.assertNotIn("curl -fsS --max-time 15", SRC)

    def test_online_sqlite_backup_and_missing_markers(self):
        self.assertIn('sqlite3 "$db" ".backup', SRC)
        self.assertIn("PRESENT\\t%s", SRC)
        self.assertIn("MISSING\\t%s", SRC)
        self.assertIn("--parents", SRC)

    def test_no_implicit_git_mutation(self):
        self.assertIsNone(re.search(r"\bgit\s+(?:fetch|pull|commit|push)\b", SRC))


if __name__ == "__main__":
    unittest.main()
