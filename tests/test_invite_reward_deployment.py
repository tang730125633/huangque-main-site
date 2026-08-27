import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InviteRewardDeploymentTests(unittest.TestCase):
    def test_deployed_processor_runs_beside_invites_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "auth-service"
            target.mkdir()
            shutil.copy2(ROOT / "scripts" / "process_invite_reward_claims.py", target)
            shutil.copy2(ROOT / "server" / "invites.py", target)
            checked = subprocess.run(
                [
                    sys.executable,
                    str(target / "process_invite_reward_claims.py"),
                    "--database",
                    str(target / "users.db"),
                    "--limit",
                    "1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr or checked.stdout)
            self.assertIn('"ok": true', checked.stdout.lower())

    def test_ship_maps_auth_dependency_processor_and_timer(self):
        ship = (ROOT / "ship").read_text(encoding="utf-8")
        self.assertIn("server/invite_network.py", ship)
        self.assertIn(
            "scripts/process_invite_reward_claims.py) dest=/home/ubuntu/auth-service/",
            ship,
        )
        self.assertIn("deploy/systemd/*.timer)", ship)
        self.assertIn("systemctl enable --now $TIMERS", ship)
        self.assertLess(
            ship.index("systemctl daemon-reload"),
            ship.index("systemctl enable --now $TIMERS"),
        )

    def test_drift_sentinel_tracks_new_runtime_files(self):
        sentinel = (ROOT / "scripts" / "drift_sentinel.py").read_text(encoding="utf-8")
        self.assertIn(
            "'server/invite_network.py': '/home/ubuntu/auth-service/invite_network.py'",
            sentinel,
        )
        self.assertIn(
            "'scripts/process_invite_reward_claims.py': "
            "'/home/ubuntu/auth-service/process_invite_reward_claims.py'",
            sentinel,
        )

    def test_systemd_timer_runs_bounded_expiry_processor(self):
        service_path = (
            ROOT / "deploy" / "systemd" / "huangque-invite-reward-claims.service"
        )
        timer_path = ROOT / "deploy" / "systemd" / "huangque-invite-reward-claims.timer"
        self.assertTrue(service_path.exists())
        self.assertTrue(timer_path.exists())
        service = service_path.read_text(encoding="utf-8")
        timer = timer_path.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=ubuntu", service)
        self.assertIn("/home/ubuntu/auth-service/process_invite_reward_claims.py", service)
        self.assertIn("--database /home/ubuntu/auth-service/users.db", service)
        self.assertIn("--limit 100", service)
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_dev_server_restore_installs_invite_reward_worker(self):
        setup = (ROOT / "deploy" / "setup-dev-server.sh").read_text(encoding="utf-8")
        self.assertIn('"$R"/server/invite_network.py', setup)
        self.assertIn('"$R"/scripts/process_invite_reward_claims.py', setup)
        self.assertIn("huangque-invite-reward-claims.timer", setup)


if __name__ == "__main__":
    unittest.main()
