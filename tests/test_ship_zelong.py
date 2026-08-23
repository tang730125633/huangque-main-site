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


def ip12_preview_files() -> list[str]:
    env = os.environ.copy()
    env["SHIP_ZELONG_LIB_ONLY"] = "1"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; files=(); load_ip12_preview "$(git rev-parse HEAD)"; printf "%s\\n" "${files[@]}"',
            "_",
            str(SHIP),
        ],
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


def manifest_rows(path: str) -> list[list[str]]:
    env = os.environ.copy()
    env["SHIP_ZELONG_LIB_ONLY"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        manifest = Path(tmp) / "manifest.tsv"
        file_list = Path(tmp) / "files.txt"
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; make_manifest "$(git rev-parse HEAD)" "$2" "$3" "$4" "$5"',
                "_",
                str(SHIP),
                str(stage),
                str(manifest),
                str(file_list),
                path,
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return [row.split("\t") for row in manifest.read_text(encoding="utf-8").splitlines()]


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

    def test_only_expected_huangque_services_can_restart(self):
        restarts = re.findall(r"systemctl restart ([A-Za-z0-9_-]+)", SRC)
        self.assertTrue(restarts)
        self.assertEqual({"huangque-auth", "huangque-content", "huangque-imggen-api",
                          "huangque-leadgen-api", "huangque-admin",
                          "hermes-ip12-preview"}, set(restarts))

    def test_rejects_non_whitelisted_backend_and_sensitive_paths(self):
        for path in (
            "deploy/systemd/huangque-admin.service",
            "server/secret.env",
            "data/users.db",
            "../site/index.html",
        ):
            with self.subTest(path=path):
                self.assertNotEqual(0, call_map(path).returncode)

    def test_maps_dependencies_before_entrypoints_and_html(self):
        cases = {
            "server/hq_cli_api.py": (10, "/home/ubuntu/auth-service/hq_cli_api.py", "auth", 0),
            "server/invites.py": (10, "/home/ubuntu/auth-service/invites.py", "auth", 0),
            "server/invite_network.py": (10, "/home/ubuntu/auth-service/invite_network.py", "auth", 0),
            "server/business_cards.py": (10, "/home/ubuntu/auth-service/business_cards.py", "auth", 0),
            "server/wechat_subscribe.py": (10, "/home/ubuntu/auth-service/wechat_subscribe.py", "auth", 0),
            "server/tikhub.py": (10, "/home/ubuntu/content-api/tikhub.py", "content", 0),
            "server/content_domains/core.py": (20, "/home/ubuntu/content-api/content_domains/core.py", "content", 0),
            "server/providers/lipsync/runtime.py": (20, "/home/ubuntu/content-api/providers/lipsync/runtime.py", "content", 0),
            "server/providers/short_drama_visual/heygen_cinematic.py": (20, "/home/ubuntu/content-api/providers/short_drama_visual/heygen_cinematic.py", "content", 0),
            "server/content_api.py": (30, "/home/ubuntu/content-api/content_api.py", "content", 0),
            "server/imggen_api.py": (30, "/home/ubuntu/content-api/imggen_api.py", "imggen", 0),
            "server/leadgen_api.py": (30, "/home/ubuntu/content-api/leadgen_api.py", "leadgen", 0),
            "server/admin_api.py": (30, "/home/ubuntu/content-api/admin_api.py", "admin", 0),
            "server/inspiration_cases.py": (10, "/home/ubuntu/content-api/inspiration_cases.py", "admin", 0),
            "server/content_domains/function_registry.py": (20, "/home/ubuntu/content-api/content_domains/function_registry.py", "admin", 0),
            "server/hermes_ip12/server.py": (20, "/home/ubuntu/hermes-preview/server.py", "hermes", 0),
            "server/hermes_ip12/templates/index_clean.html": (20, "/home/ubuntu/hermes-preview/templates/index_clean.html", "hermes", 0),
            "deploy/zelong/run-hermes-ip12-preview.sh": (25, "/home/ubuntu/hermes-preview/run-preview.sh", "hermes", 0),
            "deploy/zelong/hermes-ip12-preview-requirements.txt": (25, "/home/ubuntu/hermes-preview/preview-requirements.txt", "hermes", 0),
            "deploy/zelong/hermes-preview-cli": (25, "/home/ubuntu/hermes-preview/bin/hermes-preview-cli", "hermes", 0),
            "deploy/zelong/nginx-hermes-ip12-preview.conf": (35, "/etc/nginx/snippets/hermes-ip12-preview.conf", "hermes", 0),
            "deploy/zelong/hermes-ip12-preview.service": (35, "/etc/systemd/system/hermes-ip12-preview.service", "hermes", 1),
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

    def test_shared_auth_modules_are_deployed_to_both_services(self):
        for name in ("pricing.py", "feature_flags.py"):
            rows = manifest_rows("server/content_domains/" + name)
            self.assertEqual(
                {
                    "/home/ubuntu/auth-service/content_domains/" + name,
                    "/home/ubuntu/content-api/content_domains/" + name,
                },
                {row[2] for row in rows},
            )
            self.assertEqual({"auth", "content"}, {row[4] for row in rows})

    def test_public_installer_may_keep_its_executable_git_mode(self):
        self.assertEqual("site/downloads/hq/install.sh", manifest_rows("site/downloads/hq/install.sh")[0][1])

    def test_non_hermes_deploy_keeps_remote_argument_eight(self):
        self.assertIn('requirements_hash="-"', SRC)

    def test_dry_run_plan_is_required_for_apply(self):
        self.assertIn('test -n "$supplied" || die', SRC)
        self.assertIn('cmp -s "$payload" "$dir/$id.plan"', SRC)
        self.assertIn("【DRY-RUN】未上传文件、未备份、未重启", SRC)

    def test_fixed_commit_and_exact_sha_checks_are_present(self):
        self.assertIn("git cat-file -e", SRC)
        self.assertIn('git ls-remote --exit-code origin "refs/heads/$source_ref"', SRC)
        for source_ref, expected in (("main", 0), ("dev/zelong", 0), ("feature/nope", 1)):
            with self.subTest(source_ref=source_ref):
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; validate_source_ref "$2"', "_", str(SHIP), source_ref],
                    cwd=ROOT,
                    env={**os.environ, "SHIP_ZELONG_LIB_ONLY": "1"},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(expected, result.returncode)
        for source_ref, expected in (
            ("codex/ip12-preview-choice-copy", 0),
            ("codex/ip12-preview-a.b_1", 0),
            ("codex/other-preview", 1),
            ("feature/ip12-preview-test", 1),
            ("codex/ip12-preview-../bad", 1),
        ):
            with self.subTest(preview_ref=source_ref):
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; validate_preview_ref "$2"', "_", str(SHIP), source_ref],
                    cwd=ROOT,
                    env={**os.environ, "SHIP_ZELONG_LIB_ONLY": "1"},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(expected, result.returncode)
        self.assertIn("--adspower-baseline 只允许 origin/main", SRC)
        self.assertIn('test "$source_set" = 0 || die "--rollback 禁止与 --source 混用"', SRC)
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
        # Managed services tolerate the brief nginx 502 startup window.
        self.assertEqual(15, SRC.count(retry_options))
        self.assertEqual(3, SRC.count("/workbench/ip12/healthz"))
        self.assertNotIn("curl -fsS --max-time 15", SRC)
        self.assertEqual(
            2,
            SRC.count('"http://127.0.0.1:8100/api/gen/leadgen/health"'),
        )
        self.assertNotIn('"https://$domain/api/gen/leadgen/health"', SRC)

    def test_online_sqlite_backup_and_missing_markers(self):
        self.assertIn('sqlite3 "$db" ".backup', SRC)
        self.assertIn('chmod 0644 "$backup/manifest.tsv"', SRC)
        self.assertIn("PRESENT\\t%s", SRC)
        self.assertIn("MISSING\\t%s", SRC)
        self.assertIn("--parents", SRC)

    def test_rollback_refuses_pending_refunds(self):
        self.assertIn("status='error' AND refunded=2", SRC)
        self.assertIn("refuse rollback:", SRC)

    def test_no_implicit_git_mutation(self):
        self.assertIsNone(re.search(r"\bgit\s+(?:fetch|pull|commit|push)\b", SRC))

    def test_ip12_preview_preset_is_isolated_and_complete(self):
        files = ip12_preview_files()
        self.assertIn("server/hermes_ip12/server.py", files)
        self.assertIn("server/hermes_ip12/prompt.md", files)
        self.assertIn("server/hermes_ip12/requirements.txt", files)
        self.assertIn("server/hermes_ip12/templates/index_clean.html", files)
        self.assertIn("deploy/zelong/hermes-ip12-preview.service", files)
        self.assertIn("deploy/zelong/hermes-ip12-preview-requirements.txt", files)
        self.assertIn("deploy/zelong/hermes-preview-cli", files)
        self.assertIn("deploy/zelong/nginx-hermes-ip12-preview.conf", files)
        self.assertIn("deploy/zelong/run-hermes-ip12-preview.sh", files)
        self.assertTrue(all(not path.endswith((".env", ".db")) for path in files))
        self.assertIn("--ip12-preview 只允许 origin/main", SRC)
        self.assertIn("--preview-branch 只能与 --ip12-preview 一起使用", SRC)
        self.assertIn("预览分支不得修改部署脚手架或删除 Agent 文件", SRC)
        preview_runner = (ROOT / "deploy/zelong/run-hermes-ip12-preview.sh").read_text()
        self.assertIn("HERMES_ENABLE_INTERNAL_TOOLS=1", preview_runner)
        self.assertIn('HERMES_PREVIEW_MODEL:-qwen-plus', preview_runner)
        self.assertIn('DASHSCOPE_API_KEY', preview_runner)
        self.assertIn("models_url", preview_runner)
        self.assertIn("configured Hermes preview model is unavailable", preview_runner)
        self.assertIn('if models:', preview_runner)
        self.assertIn('"max_tokens": 1', preview_runner)
        self.assertIn("/home/ubuntu/hermes-preview-deps", preview_runner)
        self.assertIn("/home/ubuntu/hermes-preview/bin", preview_runner)
        self.assertIn("missing preview media modules", preview_runner)
        self.assertIn("missing preview media commands", preview_runner)
        self.assertIn("missing preview Chromium", preview_runner)
        self.assertIn("playwright.chromium.launch", preview_runner)
        self.assertIn("--target /home/ubuntu/hermes-preview-deps", SRC)
        self.assertIn("-m playwright install chromium", SRC)
        self.assertIn("ln -sfn hermes-preview-cli", SRC)
        self.assertIn("python3 -m pip download", SRC)
        self.assertIn("--platform manylinux_2_28_x86_64", SRC)
        self.assertIn("async-timeout>=4,<6", SRC)
        self.assertIn("exceptiongroup>=1.0.2", SRC)
        self.assertIn("SHIP_ZELONG_WHEELHOUSE", SRC)
        self.assertIn("SHIP_ZELONG_CHROMIUM_ARCHIVE", SRC)
        self.assertIn("/home/ubuntu/hermes-preview/.requirements-sha", SRC)
        self.assertIn("sudo cat /home/ubuntu/hermes-preview/.requirements-sha", SRC)
        self.assertIn("--no-index --find-links", SRC)
        self.assertIn("/home/ubuntu/hermes-preview/wheelhouse/", SRC)
        self.assertIn("chown -R ubuntu:ubuntu", SRC)
        self.assertIn("archive.extract(member", SRC)
        self.assertIn("member.external_attr", SRC)
        self.assertIn("os.chmod", SRC)
        self.assertIn("sudo test -x \"$browser_path\"", SRC)
        self.assertIn("-m playwright install-deps chromium", SRC)
        self.assertIn("/home/ubuntu/hermes-preview/.playwright-system-deps", SRC)
        self.assertIn("chown ubuntu:ubuntu /home/ubuntu/hermes-preview/.ip12-release-sha", SRC)
        preview_requirements = (ROOT / "deploy/zelong/hermes-ip12-preview-requirements.txt").read_text()
        self.assertEqual("-r requirements.txt", preview_requirements.strip())
        full_requirements = (ROOT / "server/hermes_ip12/requirements.txt").read_text()
        for dependency in ("Pillow", "playwright", "edge-tts", "faster-whisper", "yt-dlp"):
            self.assertIn(dependency, full_requirements)
        self.assertGreaterEqual(SRC.count('get("release_sha", "")'), 2)


if __name__ == "__main__":
    unittest.main()
