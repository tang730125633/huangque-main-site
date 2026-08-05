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
head="${FAKE_GIT_HEAD:-1111111111111111111111111111111111111111}"
remote="${FAKE_GIT_REMOTE_HEAD:-$head}"
if [ -n "$FAKE_GIT_LOG" ]; then printf '%s\n' "$*" >> "$FAKE_GIT_LOG"; fi
if [ "$1" = "branch" ] && [ "$2" = "--show-current" ]; then
  printf '%s\n' "${FAKE_GIT_BRANCH:-main}"
  exit 0
fi
if [ "$1" = "status" ]; then
  if [ -n "$FAKE_GIT_STATUS" ]; then printf '%s\n' "$FAKE_GIT_STATUS"; fi
  exit 0
fi
if [ "$1" = "rev-parse" ] && [ "$2" = "HEAD" ]; then printf '%s\n' "$head"; exit 0; fi
if [ "$1" = "rev-parse" ] && [ "$2" = "--short" ]; then printf '%.7s\n' "$head"; exit 0; fi
if [ "$1" = "ls-remote" ]; then printf '%s\trefs/heads/main\n' "$remote"; exit 0; fi
if [ "$1" = "ls-files" ] && [ "$2" = "--error-unmatch" ]; then exit 0; fi
if [ "$1" = "ls-files" ] && [ "$2" = "server/content_domains" ]; then
  printf '%s\n' "${FAKE_DOMAIN_FILES:-server/content_domains/core.py}"
  exit 0
fi
if [ "$1" = "ls-files" ] && [ "$2" = "server/providers" ]; then
  printf '%s\n' "${FAKE_PROVIDER_FILES:-server/providers/lipsync/base.py}"
  exit 0
fi
exit 0
""",
        )
        self._write_executable(
            "ssh",
            """#!/bin/sh
if [ -n "$FAKE_SSH_LOG" ]; then printf '%s\n' "$*" >> "$FAKE_SSH_LOG"; fi
# smoke_import / check_restart_effective 把脚本从 stdin 喂给远端 bash -s，
# 参数里只有服务名等，所以按「第二个位置参数」区分二者：
#   smoke_import         → bash -s -- <svc> <python 路径>
#   check_restart_effective → bash -s -- <svc> <时间戳>
case "$*" in
  *"test -f"*)
    if [ "$FAKE_REMOTE_FILE_MISSING" = "1" ]; then exit 1; fi
    ;;
  *"bash -s"*)
    remote_script=$(cat)
    case "$*" in
      *python3*)
        if [ "$FAKE_IMPORT_FAIL" = "1" ]; then echo "    ❌ import 失败 —— 中止，不重启"; exit 1; fi
        if [ "$FAKE_SEEDANCE_CONTRACT_FAIL" = "1" ] && printf '%s' "$remote_script" | grep -q before_charge; then
          echo "    ❌ Seedance 参考图跨模块契约失败 —— 中止，不重启"
          exit 1
        fi
        echo "    ✓ import 通过"
        exit 0
        ;;
      *)
        if [ "$FAKE_RESTART_INEFFECTIVE" = "1" ]; then echo "    ❌ restart 没有真的发生"; exit 1; fi
        if [ "$FAKE_ENV_NEWER" = "1" ]; then echo "    ❌ 新配置没被加载"; exit 1; fi
        echo "    ✓ 重启生效、配置已加载"
        exit 0
        ;;
    esac
    ;;
  *"date +%s"*)
    echo 1700000000
    exit 0
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

    def _run_ship(self, target="site/index.html", exact_content_domains=False, **overrides):
        targets = [target] if isinstance(target, str) else target
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HOME": str(self.home),
            "HQ_REMOTE": "fake-server",
            "HQ_SERVICE_WAIT_SECONDS": "1",
        })
        env.update(overrides)
        command = ["bash", str(SHIP)]
        if exact_content_domains:
            command.append("--exact-content-domains")
        return subprocess.run(
            [*command, "test deployment", *targets],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )

    def test_clean_current_main_with_http_200_allows_success(self):
        result = self._run_ship(FAKE_CURL_CODE="200")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("发布源已锁定", result.stdout)
        self.assertIn("健康检查: HTTP 200", result.stdout)
        self.assertIn("上线完成", result.stdout)

    def test_feature_branch_is_rejected(self):
        result = self._run_ship(FAKE_GIT_BRANCH="codex/feature")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("只允许从本地 main 发布", result.stdout)

    def test_dirty_main_is_rejected(self):
        result = self._run_ship(FAKE_GIT_STATUS=" M server/content_domains/audio.py")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("工作区有改动，拒绝发布", result.stdout)

    def test_stale_main_is_rejected(self):
        result = self._run_ship(
            FAKE_GIT_HEAD="1111111111111111111111111111111111111111",
            FAKE_GIT_REMOTE_HEAD="2222222222222222222222222222222222222222",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("不是实时 origin/main", result.stdout)

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

    def test_systemd_dropin_targets_owning_service(self):
        rsync_log = Path(self.tmp.name) / "rsync.log"
        ssh_log = Path(self.tmp.name) / "ssh.log"
        result = self._run_ship(
            target="deploy/systemd/huangque-content.service.d/points.conf",
            FAKE_CURL_CODE="200",
            FAKE_RSYNC_LOG=str(rsync_log),
            FAKE_SSH_LOG=str(ssh_log),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn(
            "fake-server:/etc/systemd/system/huangque-content.service.d/",
            rsync_log.read_text(encoding="utf-8"),
        )
        ssh = ssh_log.read_text(encoding="utf-8")
        self.assertRegex(ssh, r"sudo systemctl restart\s+huangque-content(?:\s|$)")
        self.assertNotIn("huangque-content.service.d.service", ssh)

    def test_content_domain_change_syncs_and_verifies_all_tracked_domains(self):
        domain_files = subprocess.check_output(
            ["git", "ls-files", "server/content_domains"],
            cwd=ROOT,
            text=True,
        ).splitlines()
        provider_files = subprocess.check_output(
            ["git", "ls-files", "server/providers"],
            cwd=ROOT,
            text=True,
        ).splitlines()
        rsync_log = Path(self.tmp.name) / "rsync.log"
        ssh_log = Path(self.tmp.name) / "ssh.log"
        result = self._run_ship(
            target="server/content_domains/audio.py",
            FAKE_CURL_CODE="200",
            FAKE_DOMAIN_FILES="\n".join(domain_files),
            FAKE_PROVIDER_FILES="\n".join(provider_files),
            FAKE_RSYNC_LOG=str(rsync_log),
            FAKE_SSH_LOG=str(ssh_log),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("整目录同步", result.stdout)
        self.assertIn("import 通过", result.stdout)
        self.assertIn("server/content_domains/", rsync_log.read_text(encoding="utf-8"))
        self.assertIn("server/providers/", rsync_log.read_text(encoding="utf-8"))
        ssh_lines = ssh_log.read_text(encoding="utf-8").splitlines()
        for marker in ("--verify-deploy", "--bless-deploy"):
            command = next(line for line in ssh_lines if marker in line)
            for path in domain_files:
                self.assertIn(path, command)
            for path in provider_files:
                self.assertIn(path, command)

    def test_exact_content_domains_only_pushes_requested_existing_files(self):
        targets = [
            "server/content_domains/core.py",
            "server/content_domains/points.py",
        ]
        rsync_log = Path(self.tmp.name) / "rsync.log"
        ssh_log = Path(self.tmp.name) / "ssh.log"
        result = self._run_ship(
            target=targets,
            exact_content_domains=True,
            FAKE_CURL_CODE="200",
            FAKE_RSYNC_LOG=str(rsync_log),
            FAKE_SSH_LOG=str(ssh_log),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("file-only 模式", result.stdout)
        lines = rsync_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lines))
        for path, line in zip(targets, lines):
            self.assertIn(path, line)
        self.assertNotIn("server/content_domains/ fake-server:", "\n".join(lines))
        verify = next(
            line for line in ssh_log.read_text(encoding="utf-8").splitlines()
            if "--verify-deploy" in line
        )
        for path in targets:
            self.assertIn(path, verify)
        self.assertNotIn("server/content_domains/video.py", verify)

    def test_exact_content_domains_refuses_new_remote_module(self):
        result = self._run_ship(
            target="server/content_domains/digital_ip.py",
            exact_content_domains=True,
            FAKE_REMOTE_FILE_MISSING="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("只允许覆盖已存在", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_exact_content_domains_refuses_content_api(self):
        result = self._run_ship(
            target=["server/content_api.py", "server/content_domains/core.py"],
            exact_content_domains=True,
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("不能发布 server/content_api.py", result.stdout)
        self.assertNotIn("发布源已锁定", result.stdout)

    def test_canvas_module_directory_keeps_its_relative_deployment_path(self):
        canvas_files = [
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "site/workbench/canvas").iterdir())
            if path.is_file()
        ]
        rsync_log = Path(self.tmp.name) / "rsync.log"
        result = self._run_ship(
            target=[*canvas_files, "site/workbench/canvas.html"],
            FAKE_CURL_CODE="200",
            FAKE_RSYNC_LOG=str(rsync_log),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        lines = rsync_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(canvas_files) + 1, len(lines))
        for path, line in zip(canvas_files, lines):
            self.assertIn(path, line)
            self.assertTrue(
                line.endswith("fake-server:/var/www/huangquechuanmei/workbench/canvas/"),
                line,
            )
        self.assertTrue(
            lines[-1].endswith("fake-server:/var/www/huangquechuanmei/workbench/"),
            lines[-1],
        )

    def test_content_import_failure_stops_before_restart(self):
        result = self._run_ship(
            target="server/content_domains/core.py",
            FAKE_IMPORT_FAIL="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("import 失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_seedance_contract_failure_stops_before_restart(self):
        result = self._run_ship(
            target="server/content_domains/jobs_store.py",
            exact_content_domains=True,
            FAKE_SEEDANCE_CONTRACT_FAIL="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("Seedance 参考图跨模块契约失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_silent_restart_blocks_deployment_success(self):
        """restart 没真的发生（启动时间早于 T0）→ 阻断，不打印上线完成。"""
        result = self._run_ship(
            target="server/content_domains/core.py",
            FAKE_RESTART_INEFFECTIVE="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("restart 没有真的发生", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_stale_env_not_loaded_blocks_deployment_success(self):
        """env 文件比进程启动还新（配置进了文件没进进程）→ 阻断。"""
        result = self._run_ship(
            target="server/content_domains/core.py",
            FAKE_ENV_NEWER="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("新配置没被加载", result.stdout)
        self.assertNotIn("上线完成", result.stdout)


if __name__ == "__main__":
    unittest.main()
