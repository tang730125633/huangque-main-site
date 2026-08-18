import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES_SERVER = ROOT / "server" / "hermes_ip12" / "server.py"
AUTH_SERVER = ROOT / "server" / "auth_server.py"


class ProductionBridgeContractTests(unittest.TestCase):
    def test_default_bridge_path_matches_the_registered_auth_route(self):
        hermes = HERMES_SERVER.read_text(encoding="utf-8")
        auth = AUTH_SERVER.read_text(encoding="utf-8")
        expected_route = "/api/auth/internal/ip12/agent/action"
        self.assertIn(expected_route, auth)
        self.assertIn(expected_route, hermes)

    def test_auth_bridge_accepts_the_confirm_envelope_sent_by_ip12(self):
        hermes = HERMES_SERVER.read_text(encoding="utf-8")
        auth = AUTH_SERVER.read_text(encoding="utf-8")
        self.assertIn('"confirm": bool(confirm)', hermes)
        self.assertIn('"quote_token": quote_token', hermes)
        self.assertIn('"idempotency_key": idempotency_key', hermes)
        bridge = auth[auth.index("def _internal_ip12_agent_action"):auth.index("if p == \"/api/auth/internal/ip12/agent/catalog\"")]
        for field in ("confirm", "quote_token", "idempotency_key"):
            self.assertIn('"' + field + '"', bridge)


if __name__ == "__main__":
    unittest.main()
