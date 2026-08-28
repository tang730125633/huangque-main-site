import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "hq-cli" / "src"))

from hq_cli.catalog import CAPABILITIES  # noqa: E402
from server.content_domains import function_registry  # noqa: E402


class HQCLISiteCoverageTests(unittest.TestCase):
    def test_every_registered_site_operation_is_discoverable_by_an_agent(self):
        registered = set()
        for page in function_registry.list_pages():
            registered.update(
                mode["key"]
                for feature in page.get("functions") or []
                for mode in feature.get("modes") or []
            )
            registered.update(item["key"] for item in page.get("browser_journeys") or [])
            registered.update(
                item["key"] for item in page.get("auxiliary_actions") or [] if item.get("key")
            )
        discovered = {
            operation
            for capability in CAPABILITIES.values()
            for operation in capability["agent"]["website_operations"]
        }
        self.assertEqual(registered, discovered)

    def test_navigation_fallbacks_are_explicit_and_non_executing(self):
        for capability in CAPABILITIES.values():
            operations = capability["agent"]["website_operations"]
            if not operations:
                continue
            self.assertEqual("navigation", capability["kind"])
            self.assertEqual("navigate", capability["agent"]["operation"])
            self.assertEqual("navigate", capability["agent"]["website_access"])
            self.assertFalse(capability["confirmation_required"])
        matrix = CAPABILITIES["matrix-template"]
        self.assertEqual(
            ["matrix_template.single", "matrix_template.batch"],
            matrix["agent"]["website_operations"],
        )
        self.assertEqual("/workbench/matrix-template.html", matrix["deep_link"]["path"])
