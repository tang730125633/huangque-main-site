import io
import json
import unittest
from contextlib import redirect_stdout

from server.content_domains import error_contract


class ErrorContractTests(unittest.TestCase):
    def test_idempotent_request_id_is_stable_without_exposing_key(self):
        headers = {"Idempotency-Key": "customer-operation-secret"}
        first = error_contract.request_id(headers)
        self.assertEqual(first, error_contract.request_id(headers))
        self.assertNotIn("customer-operation-secret", first)

    def test_catalog_is_unique_and_stable(self):
        items = error_contract.public_catalog()
        self.assertEqual(len(items), len({item["code"] for item in items}))
        self.assertTrue(all(item["code"].startswith("HQ-") for item in items))
        self.assertTrue(all(item["message"] and isinstance(item["retryable"], bool) for item in items))

    def test_upstream_detail_is_not_exposed(self):
        payload, code = error_contract.normalize(
            502,
            {"detail": 'provider rejected api_key="secret-value" raw body', "code": "invalid_upstream_response"},
            "req_public_1",
        )
        self.assertEqual(code, "HQ-UPSTREAM-001")
        self.assertEqual(payload["detail"], "生成渠道暂时不可用，请稍后再试")
        self.assertNotIn("secret-value", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["error"]["request_id"], "req_public_1")

    def test_client_detail_stays_compatible_but_secrets_are_redacted(self):
        payload, code = error_contract.normalize(
            400, {"detail": '参数错误 token=private-token'}, "req_public_2"
        )
        self.assertEqual(code, "HQ-REQUEST-001")
        self.assertEqual(payload["code"], "invalid_request")
        self.assertIn("token=***", payload["detail"])
        self.assertNotIn("private-token", payload["detail"])

    def test_audit_contains_only_redacted_summary(self):
        out = io.StringIO()
        with redirect_stdout(out):
            error_contract.audit(503, {"detail": "Authorization: Bearer abc.def"}, "req_3", "HQ-UPSTREAM-002")
        line = out.getvalue()
        self.assertIn("HQ-UPSTREAM-002", line)
        self.assertIn("Authorization: ***", line)
        self.assertNotIn("abc.def", line)


if __name__ == "__main__":
    unittest.main()
