# -*- coding: utf-8 -*-
"""ship 的重启闸门：import 冒烟 + 「重启真发生 / 配置真加载」后置断言。

守的不变量（健康检查 gate 由 test_ship_health_gate.py 覆盖，这里不重复）：
1. 每个待重启服务都先做 import 冒烟，且冒烟失败必须中止（不重启）
2. 冒烟发生在 systemctl restart 之前——否则挡不住崩溃循环
3. 入口从 systemd ExecStart 反查，不硬编码，避免与 ship 映射表二次漂移
4. 重启基准时刻 T0 取「服务器」时钟，且在 restart 之前取
5. 重启后断言：启动时间晚于 T0（restart 真发生）+ 无 EnvironmentFile 比启动新（配置真加载）
"""
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIP = ROOT / "ship"
SRC = SHIP.read_text(encoding="utf-8")


def _idx(needle):
    i = SRC.find(needle)
    assert i >= 0, "ship 里找不到: %r" % needle
    return i


class SmokeImportTests(unittest.TestCase):
    def test_provider_dependency_tree_is_deployed_and_monitored(self):
        drift = (ROOT / "scripts/drift_sentinel.py").read_text(encoding="utf-8")
        setup = (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8")
        self.assertIn("push_dir server/providers/ /home/ubuntu/content-api/providers/", SRC)
        self.assertIn("git_ls_tree('server/providers')", drift)
        self.assertIn("HQ_PROVIDERS_RUNTIME", drift)
        self.assertIn('rsync -a --delete "$R"/server/providers/', setup)

    def test_auth_dependencies_are_deployed_and_monitored(self):
        drift = (ROOT / "scripts/drift_sentinel.py").read_text(encoding="utf-8")
        self.assertIn("server/auth_server.py|server/hq_cli_api.py|server/invites.py|server/invite_network.py|server/business_cards.py|server/wxpay.py", SRC)
        self.assertIn(
            "'server/invites.py': '/home/ubuntu/auth-service/invites.py'",
            drift,
        )
        self.assertIn(
            "'server/hq_cli_api.py': '/home/ubuntu/auth-service/hq_cli_api.py'",
            drift,
        )
        self.assertIn(
            "'server/business_cards.py': '/home/ubuntu/auth-service/business_cards.py'",
            drift,
        )
        self.assertIn(
            "'server/invite_network.py': '/home/ubuntu/auth-service/invite_network.py'",
            drift,
        )
        for name in ("__init__.py", "cos.py", "miniprogram_security.py"):
            self.assertIn(
                "'server/content_domains/%s': '/home/ubuntu/auth-service/content_domains/%s'" % (name, name),
                drift,
            )
        self.assertIn("runtimes = git_path_to_runtimes(gp)", drift)
        self.assertIn(
            '"$R"/server/invites.py',
            (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"$R"/server/hq_cli_api.py',
            (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"$R"/server/business_cards.py',
            (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"$R"/server/invite_network.py',
            (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8"),
        )

    def test_defined_and_called_per_service(self):
        self.assertIn("smoke_import(){", SRC)
        self.assertRegex(SRC, r"for s in \$RESTART; do smoke_import")

    def test_failure_aborts_before_restart(self):
        """冒烟失败必须 exit 1，且整段在 restart 之前。"""
        self.assertRegex(SRC, r'smoke_import "\$s" \|\| exit 1')
        self.assertLess(_idx("do smoke_import"), _idx("sudo systemctl restart $RESTART"))

    def test_entrypoint_resolved_from_systemd_not_hardcoded(self):
        """入口从 ExecStart/WorkingDirectory 反查，不写死模块名。"""
        self.assertIn("systemctl show -p ExecStart", SRC)
        self.assertIn("systemctl show -p WorkingDirectory", SRC)

    def test_content_smoke_checks_the_copy_cross_module_contract(self):
        self.assertIn('if [ "$svc" = "huangque-content" ]', SRC)
        self.assertIn("text.validate_copy_payload", SRC)
        self.assertIn("文案跨模块契约失败", SRC)
        self.assertLess(_idx("text.validate_copy_payload"), _idx("sudo systemctl restart $RESTART"))


class RestartEffectiveTests(unittest.TestCase):
    def test_defined_and_called_after_restart(self):
        self.assertIn("check_restart_effective(){", SRC)
        self.assertLess(
            _idx("sudo systemctl restart $RESTART"),
            _idx("do check_restart_effective"),
        )

    def test_t0_from_server_clock_before_restart(self):
        """T0 必须取服务器时间（ActiveEnterTimestamp 是服务器时钟），且在 restart 之前。"""
        self.assertRegex(SRC, r'T0=\$\(\$SSHC "\$REMOTE" "date \+%s"\)')
        self.assertLess(_idx("T0=$($SSHC"), _idx("sudo systemctl restart $RESTART"))

    def test_asserts_restart_actually_happened(self):
        self.assertIn("ActiveEnterTimestamp", SRC)
        self.assertIn("restart 没有真的发生", SRC)

    def test_asserts_env_files_not_newer_than_process(self):
        """抓「配置写在重启之后、进了文件没进进程」。"""
        self.assertIn("systemctl show -p EnvironmentFiles", SRC)
        self.assertIn("新配置没被加载", SRC)

    def test_failure_aborts(self):
        self.assertRegex(SRC, r'check_restart_effective "\$s" "\$T0" \|\| exit 1')


if __name__ == "__main__":
    unittest.main()
