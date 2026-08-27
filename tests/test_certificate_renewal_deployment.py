import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CertificateRenewalDeploymentTests(unittest.TestCase):
    def test_tls_alpn_renewal_is_recoverable_and_scheduled(self):
        nginx = (ROOT / "deploy/nginx-huangquechuanmei.conf").read_text()
        service = (ROOT / "deploy/systemd/huangque-cert-renew.service").read_text()
        timer = (ROOT / "deploy/systemd/huangque-cert-renew.timer").read_text()

        self.assertIn("/etc/nginx/certs/huangquechuanmei.com/fullchain.pem", nginx)
        self.assertIn("/usr/local/sbin/huangque-acme.sh --cron --home /root/.acme.sh", service)
        self.assertIn("ExecStopPost=/usr/bin/systemctl start nginx", service)
        self.assertIn("OnCalendar=", timer)
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
