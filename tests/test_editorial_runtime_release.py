"""口播网感模板专用事务发布器的离线回归；无生产、无 SSH、无付费调用。"""
import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/editorial_runtime_release.py"
spec = importlib.util.spec_from_file_location("editorial_runtime_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class FakeRemote:
    """模拟服务器：read_state 返回预设状态；记录 run/put_file/put_dir 调用；可注入失败。"""

    def __init__(self, states=None, health_codes=None):
        self.states = dict(states or {})
        self.calls = []
        self.fail_labels = set()
        self.fail_puts = set()
        self.health_codes = list(health_codes or [])  # 队列逐个返回；空则默认 200

    def read_state(self, dest, kind):
        return self.states.get(dest, {"exists": False})

    def run(self, command):
        self.calls.append(("run", command))
        if "api/gen/health" in command:
            code = self.health_codes.pop(0) if self.health_codes else "200"
            return subprocess.CompletedProcess([], 0 if code.startswith("2") else 1, code, "")
        return subprocess.CompletedProcess([], 0, "", "")

    def run_checked(self, command, label):
        if label in self.fail_labels:
            raise release.ReleaseError(label + " 失败")
        self.calls.append(("run", command))
        return subprocess.CompletedProcess([], 0, "", "")

    def put_file(self, local, remote):
        remote = remote.rstrip("/")
        if remote in self.fail_puts:
            raise release.ReleaseError("put_file 失败: " + remote)
        self.calls.append(("put_file", remote))

    def put_dir(self, local, remote):
        remote = remote.rstrip("/")
        if remote in self.fail_puts:
            raise release.ReleaseError("put_dir 失败: " + remote)
        self.calls.append(("put_dir", remote))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def postimage_map():
    plan = release.build_plan(ROOT, {})
    return {item["dest"]: item["postimage"] for item in plan}


def full_states(postimages):
    """把内容指纹转成远端完整状态（含 exists/mode/uid/gid）。"""
    out = {}
    for dest, fp in postimages.items():
        if fp.get("absent") or not fp:
            out[dest] = {"exists": False}
        elif "sha256" in fp:
            out[dest] = {"exists": True, "sha256": fp["sha256"],
                         "mode": "0o644", "uid": 1000, "gid": 1000}
        elif "hyperframes_version" in fp:
            out[dest] = {"exists": True, "hyperframes_version": fp["hyperframes_version"],
                         "mode": "0o755", "uid": 0, "gid": 0}
        elif "files" in fp:
            out[dest] = {"exists": True, "files": fp["files"],
                         "mode": "0o755", "uid": 0, "gid": 0}
        else:
            out[dest] = fp
    return out


def absent_states():
    return {dest: {"exists": False} for _, _, dest in release.ARTIFACTS}


def run_cmds(remote):
    return [payload for op, payload in remote.calls if op == "run"]


def assert_rollback(testcase, remote, expect_removed):
    cmds = run_cmds(remote)
    for dest in expect_removed:
        if dest == release.RUNTIME_ROOT or dest.startswith(
                release.WEBROOT + "/assets/one-click/templates/"):
            testcase.assertIn("sudo rm -rf " + dest, cmds)
        else:
            testcase.assertIn("sudo rm -f " + dest, cmds)
    testcase.assertIn("sudo rm -rf /tmp/.editorial-release-backup", cmds)
    testcase.assertIn("sudo systemctl daemon-reload", cmds)
    testcase.assertIn("sudo systemctl restart huangque-auth huangque-content", cmds)


def _fake_repo(root):
    """建一个能通过白名单校验的最小仓库结构。"""
    (root / "server/content_domains").mkdir(parents=True)
    (root / "server/content_domains/editorial_contract.py").write_text(
        "HYPERFRAMES_VERSION = '0.8.33'\n", encoding="utf-8")
    (root / "site/assets/one-click/templates/ip-editorial-serif-v1").mkdir(parents=True)
    (root / "site/assets/one-click/templates/ip-editorial-serif-v1/template.json").write_text(
        "{}", encoding="utf-8")
    (root / "tools/hyperframes/ip-editorial-serif-v1").mkdir(parents=True)
    (root / "tools/hyperframes/ip-editorial-serif-v1/package.json").write_text(
        "{}", encoding="utf-8")
    (root / "deploy/systemd/huangque-content.service.d").mkdir(parents=True)
    (root / "deploy/systemd/huangque-content.service.d/editorial-runtime.conf").write_text(
        "[Service]\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# 13 条验收测试
# ---------------------------------------------------------------------------
class ClassificationTests(unittest.TestCase):
    def test_01_all_preimage_installs_everything(self):
        remote = FakeRemote(absent_states())
        result = release.Transaction(remote, ROOT).run()
        self.assertEqual(result, {"result": "done", "writes": 5, "restarts": 2})
        puts = [p for op, p in remote.calls if op in ("put_file", "put_dir")]
        # 2 份契约 + drop-in + npm 的 package.json/lock = 5 个安装文件 + 1 个备份清单
        put_files = [p for op, p in remote.calls if op == "put_file"]
        self.assertEqual(len(put_files), 6)
        self.assertIn("/tmp/.editorial-release-backup/manifest.json", put_files)
        self.assertEqual(len([p for op, p in remote.calls if op == "put_dir"]), 1)
        cmds = run_cmds(remote)
        self.assertIn("cd /opt/huangque/editorial-hyperframes-0.8.33 && sudo -u ubuntu npm ci --no-audit --no-fund", cmds)
        # 重启顺序：auth 先于 content
        self.assertLess(cmds.index("sudo systemctl restart huangque-auth"),
                        cmds.index("sudo systemctl restart huangque-content"))

    def test_02_mixed_preimage_postimage_installs_only_missing(self):
        post = postimage_map()
        # auth 契约与模板已到位（postimage），其余 absent
        states = absent_states()
        states[release.AUTH_CONTRACT] = full_states(
            {release.AUTH_CONTRACT: post[release.AUTH_CONTRACT]})[release.AUTH_CONTRACT]
        states[release.TEMPLATE_DEST] = full_states(
            {release.TEMPLATE_DEST: post[release.TEMPLATE_DEST]})[release.TEMPLATE_DEST]
        remote = FakeRemote(states)
        result = release.Transaction(remote, ROOT).run()
        self.assertEqual(result["result"], "done")
        self.assertEqual(result["writes"], 3)  # content 契约 + npm + drop-in
        self.assertNotIn("sudo install -m 0644 /tmp/.editorial-stage " + release.AUTH_CONTRACT,
                         run_cmds(remote))

    def test_03_drifted_refuses_before_backup_write_npm_restart(self):
        states = absent_states()
        states[release.CONTENT_CONTRACT] = {"exists": True, "sha256": "deadbeef" * 8,
                                            "mode": "0o644", "uid": 1000, "gid": 1000}
        remote = FakeRemote(states)
        with self.assertRaises(release.Drifted):
            release.Transaction(remote, ROOT).run()
        self.assertEqual(remote.calls, [])  # 零备份、零写入、零 npm、零重启

    def test_04_all_postimage_zero_write_zero_restart(self):
        remote = FakeRemote(full_states(postimage_map()))
        result = release.Transaction(remote, ROOT).run()
        self.assertEqual(result, {"result": "already_postimage", "writes": 0, "restarts": 0})
        self.assertEqual(remote.calls, [])

    def test_05_file_write_failure_rolls_back_completely(self):
        cases = [
            # (失败的「安装」label, 失败前已安装的 dest)
            ("安装 " + release.AUTH_CONTRACT, []),
            ("安装 " + release.CONTENT_CONTRACT, [release.AUTH_CONTRACT]),
            ("安装 " + release.DROPIN_DEST,
             [release.AUTH_CONTRACT, release.CONTENT_CONTRACT,
              release.TEMPLATE_DEST, release.RUNTIME_ROOT]),
        ]
        for label, expect_installed in cases:
            remote = FakeRemote(absent_states())
            remote.fail_labels.add(label)
            with self.assertRaises(release.ReleaseError):
                release.Transaction(remote, ROOT).run()
            assert_rollback(self, remote, expect_installed)

    def test_06_npm_or_version_failure_rolls_back(self):
        for label in ("npm ci 安装 HyperFrames", "校验 HyperFrames 版本为 0.8.33"):
            remote = FakeRemote(absent_states())
            remote.fail_labels.add(label)
            with self.assertRaises(release.ReleaseError):
                release.Transaction(remote, ROOT).run()
            assert_rollback(self, remote,
                            [release.AUTH_CONTRACT, release.CONTENT_CONTRACT,
                             release.TEMPLATE_DEST])

    def test_07_import_failure_rolls_back(self):
        for label in ("auth editorial_contract import", "content editorial_contract import"):
            remote = FakeRemote(absent_states())
            remote.fail_labels.add(label)
            with self.assertRaises(release.ReleaseError):
                release.Transaction(remote, ROOT).run()
            assert_rollback(self, remote,
                            [release.AUTH_CONTRACT, release.CONTENT_CONTRACT,
                             release.TEMPLATE_DEST, release.RUNTIME_ROOT])

    def test_08_restart_failure_rolls_back(self):
        for label in ("重启 huangque-auth", "重启 huangque-content"):
            remote = FakeRemote(absent_states())
            remote.fail_labels.add(label)
            with self.assertRaises(release.ReleaseError):
                release.Transaction(remote, ROOT).run()
            assert_rollback(self, remote,
                            [release.AUTH_CONTRACT, release.CONTENT_CONTRACT,
                             release.TEMPLATE_DEST, release.RUNTIME_ROOT, release.DROPIN_DEST])

    def test_09_health_check_timeout_rolls_back(self):
        remote = FakeRemote(absent_states(), health_codes=["000"] * 200)
        txn = release.Transaction(remote, ROOT, health_timeout=0.3, health_interval=0.01)
        with self.assertRaises(release.ReleaseError):
            txn.run()
        assert_rollback(self, remote,
                        [release.AUTH_CONTRACT, release.CONTENT_CONTRACT,
                         release.TEMPLATE_DEST, release.RUNTIME_ROOT, release.DROPIN_DEST])
        self.assertTrue(any(e.startswith("健康检查") for e in txn.events))

    def test_health_retry_succeeds_after_delayed_start(self):
        remote = FakeRemote(absent_states(), health_codes=["000", "000", "200"])
        txn = release.Transaction(remote, ROOT, health_timeout=10, health_interval=0.01)
        result = txn.run()
        self.assertEqual(result["result"], "done")
        health_logs = [e for e in txn.events if e.startswith("健康检查")]
        self.assertEqual(len(health_logs), 3)

    def test_health_transient_non_2xx_recovers(self):
        remote = FakeRemote(absent_states(), health_codes=["502", "503", "200"])
        txn = release.Transaction(remote, ROOT, health_timeout=10, health_interval=0.01)
        result = txn.run()
        self.assertEqual(result["result"], "done")

    def test_success_restarts_auth_then_content_exactly_once(self):
        remote = FakeRemote(absent_states())
        release.Transaction(remote, ROOT).run()
        cmds = run_cmds(remote)
        self.assertEqual(cmds.count("sudo systemctl restart huangque-auth"), 1)
        self.assertEqual(cmds.count("sudo systemctl restart huangque-content"), 1)
        self.assertLess(cmds.index("sudo systemctl restart huangque-auth"),
                        cmds.index("sudo systemctl restart huangque-content"))

    def test_rollback_restores_dropin_if_existed(self):
        remote = FakeRemote(absent_states())
        txn = release.Transaction(remote, ROOT)
        txn.preimage_states = {release.DROPIN_DEST: {"exists": True, "sha256": "ab" * 32,
                                                     "mode": "0o644", "uid": 0, "gid": 0}}
        txn.installed = [release.DROPIN_DEST]
        txn.rollback()
        cmds = run_cmds(remote)
        restore = ("sudo cp -a /tmp/.editorial-release-backup/etc/systemd/system/"
                   "huangque-content.service.d/editorial-runtime.conf " + release.DROPIN_DEST)
        self.assertIn(restore, cmds)
        self.assertNotIn("sudo rm -f " + release.DROPIN_DEST, cmds)

    def test_rollback_deletes_dropin_if_absent(self):
        remote = FakeRemote(absent_states())
        txn = release.Transaction(remote, ROOT)
        txn.preimage_states = {release.DROPIN_DEST: {"exists": False}}
        txn.installed = [release.DROPIN_DEST]
        txn.rollback()
        cmds = run_cmds(remote)
        self.assertIn("sudo rm -f " + release.DROPIN_DEST, cmds)


class WhitelistTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows 建符号链接需管理员权限；Linux CI 覆盖")
    def test_10a_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(pathlib.Path(tmp))
            target = root / "server/content_domains/editorial_contract.py"
            target.unlink()
            target.symlink_to(root / "tools/hyperframes/ip-editorial-serif-v1/package.json")
            with self.assertRaises(release.ReleaseError):
                release.validate_whitelist(root)

    def test_10_escape_and_non_regular_rejected(self):
        # 路径逃逸
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaises(release.ReleaseError):
                release._assert_within_root(root, root / ".." / "etc" / "passwd")
        # 非普通文件（应为文件处放目录）
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(pathlib.Path(tmp))
            target = root / "server/content_domains/editorial_contract.py"
            target.unlink()
            target.mkdir()
            with self.assertRaises(release.ReleaseError):
                release.validate_whitelist(root)

    def test_11_shared_contract_reaches_both_auth_and_content(self):
        dests = {dest for _, _, dest in release.ARTIFACTS}
        self.assertIn(release.AUTH_CONTRACT, dests)
        self.assertIn(release.CONTENT_CONTRACT, dests)
        # 两份契约都来自同一个源文件
        srcs = {source for _, source, dest in release.ARTIFACTS
                if dest in (release.AUTH_CONTRACT, release.CONTENT_CONTRACT)}
        self.assertEqual(srcs, {release.CONTRACT_SOURCE})

    def test_12_publisher_touches_no_business_code_db_points_userdata(self):
        dests = {dest for _, _, dest in release.ARTIFACTS}
        self.assertEqual(dests, {
            release.AUTH_CONTRACT, release.CONTENT_CONTRACT,
            release.TEMPLATE_DEST, release.RUNTIME_ROOT, release.DROPIN_DEST,
        })
        self.assertEqual(release.RESTART_SERVICES, ("huangque-auth", "huangque-content"))
        src = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("sqlite3", "deduct_points", "refund_points",
                          "INSERT INTO", "DELETE FROM", "points"):
            self.assertNotIn(forbidden, src)


class CiBindingTests(unittest.TestCase):
    def test_13_linux_ci_binds_release_pr_exact_head(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        # 检出 PR 的精确 Head，而非 merge ref
        self.assertIn("github.event.pull_request.head.sha", ci)
        # 本测试被 CI 编排收录，随 Exact Head 一起跑
        self.assertIn("test_editorial_runtime_release.py", ci)


if __name__ == "__main__":
    unittest.main()
