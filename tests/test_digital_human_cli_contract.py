import importlib
import sys
import unittest
from pathlib import Path


class DigitalHumanCLIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.api = importlib.import_module("hq_cli_api")
        cls.contract = importlib.import_module("director_workflow_contract")

    def test_normal_mode_actions_are_available_but_precision_remains_planned(self):
        actions = {
            item["id"]: item for item in self.contract.DIGITAL_HUMAN_ONECLICK_ACTIONS
        }
        for identifier in (
            "digital-human-oneclick-capability", "digital-human-oneclick-plan",
            "digital-human-oneclick-consent", "digital-human-oneclick-material-upload",
            "digital-human-oneclick-audio-upload",
            "digital-human-oneclick-start", "digital-human-oneclick-status",
            "digital-human-oneclick-recover", "digital-human-oneclick-abandon",
            "digital-human-oneclick-history",
        ):
            self.assertEqual("available", actions[identifier]["availability"])
        self.assertEqual("planned", actions["digital-human-precision-start"]["availability"])

    def test_action_plans_use_server_owned_run_routes_and_scopes(self):
        capability = self.api.action_plan("digital-human-oneclick-capability", {})
        self.assertEqual("digital-human-oneclick:read", capability["scope"])
        self.assertEqual("/api/gen/digital-human-v2/capability", capability["path"])

        start = self.api.action_plan("digital-human-oneclick-start", {
            "request_id": "request-0001", "consent_token": "c" * 32,
            "plan_digest": "a" * 64, "script": "这是一段足够长的数字人口播测试文案。",
            "narration_mode": "text", "allow_ai_materials": False,
            "customer_upload_ids": [], "portrait_upload_id": "img_" + "b" * 32,
            "voice_key": "voice-ready",
        })
        self.assertEqual("generation", start["kind"])
        self.assertEqual("digital-human-oneclick:generate", start["scope"])
        self.assertEqual("/api/gen/digital-human-v2/runs/quote", start["quote_endpoint"])
        self.assertEqual("/api/gen/digital-human-v2/runs", start["endpoint"])

        status = self.api.action_plan(
            "digital-human-oneclick-status", {"run_id": "dh-run-test-0001"},
        )
        self.assertEqual("digital-human-oneclick:read", status["scope"])
        self.assertTrue(status["path"].endswith("/dh-run-test-0001"))

    def test_material_upload_is_dedicated_and_owner_scoped(self):
        item = next(
            item for item in self.api.ACTION_CATALOG
            if item["action"] == "digital-human-oneclick-material-upload"
        )
        self.assertEqual("dedicated_upload", item["transport"]["kind"])
        self.assertTrue(item["confirmation_required"])
        self.assertEqual("free", item["billing"])


if __name__ == "__main__":
    unittest.main()
