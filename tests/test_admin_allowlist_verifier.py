import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
VERIFIER = ROOT / "deploy" / "verify-admin-allowlist.py"


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_admin_allowlist", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdminAllowlistVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = load_verifier()

    def verify(self, expected, text):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "allowlist.conf"
            path.write_text(text, encoding="utf-8")
            return self.verifier.verify(path, expected)

    def test_rejects_universal_or_malformed_expected_source(self):
        for source in ("all", "0.0.0.0/0", "::/0", "999.0.2.10/33", "not-an-ip"):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.verify(source, "allow 192.0.2.10/32;\ndeny all;\n")

    def test_rejects_unsafe_or_malformed_active_grammar(self):
        cases = {
            "allow-all": "allow all;\ndeny all;\n",
            "ipv4-universal": "allow 0.0.0.0/0;\nallow 192.0.2.10/32;\ndeny all;\n",
            "ipv6-universal": "allow ::/0;\nallow 192.0.2.10/32;\ndeny all;\n",
            "early-deny": "deny all;\nallow 192.0.2.10/32;\ndeny all;\n",
            "unexpected": "allow 192.0.2.10/32;\nsatisfy any;\ndeny all;\n",
            "malformed": "allow 999.0.2.10/33;\ndeny all;\n",
            "missing-deny": "allow 192.0.2.10/32;\n",
            "extra-deny": "allow 192.0.2.10/32;\ndeny all;\ndeny all;\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.verify("192.0.2.10/32", text)

    def test_requires_expected_source_to_be_represented(self):
        with self.assertRaises(ValueError):
            self.verify("192.0.2.10/32", "allow 198.51.100.7/32;\ndeny all;\n")

    def test_accepts_valid_ipv4_and_ipv6_rules_and_canonical_equivalence(self):
        cases = (
            (
                "192.0.2.10",
                "# managed\nallow 192.0.2.10/32; # operator\nallow 198.51.100.0/24;\ndeny all;\n",
            ),
            (
                "2001:0db8:0:0::10",
                "allow 2001:db8::10/128;\nallow 2001:db8:1::/64;\ndeny all;\n",
            ),
        )
        for expected, text in cases:
            with self.subTest(expected=expected):
                self.assertTrue(self.verify(expected, text))


if __name__ == "__main__":
    unittest.main()
