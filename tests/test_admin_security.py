import importlib
import http.client
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok": true}'


class AdminIngressValidationTests(unittest.TestCase):
    def setUp(self):
        self.old_max_delta = os.environ.get("HQ_ADMIN_POINTS_MAX_DELTA")
        os.environ["HQ_ADMIN_POINTS_MAX_DELTA"] = "1000"
        import server.admin_api as admin_api

        self.admin = importlib.reload(admin_api)

    def tearDown(self):
        if self.old_max_delta is None:
            os.environ.pop("HQ_ADMIN_POINTS_MAX_DELTA", None)
        else:
            os.environ["HQ_ADMIN_POINTS_MAX_DELTA"] = self.old_max_delta

    def test_reason_requires_trimmed_4_to_120_characters(self):
        for value in (None, "", " abc ", "x" * 121):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.admin.validate_admin_reason(value)
        self.assertEqual(self.admin.validate_admin_reason("  valid reason  "), "valid reason")
        self.assertEqual(len(self.admin.validate_admin_reason("x" * 120)), 120)

    def test_delta_requires_nonzero_bounded_integer(self):
        for value in (None, 0, True, 1.5, "1", 1001, -1001):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.admin.validate_points_delta(value)
        self.assertEqual(self.admin.validate_points_delta(1000), 1000)
        self.assertEqual(self.admin.validate_points_delta(-1000), -1000)

    def test_auth_admin_request_propagates_or_generates_safe_request_id(self):
        self.admin.AUTH_INTERNAL_TOKEN = "internal-secret"
        captured = []

        def fake_urlopen(req, timeout):
            captured.append(dict(req.header_items()))
            return _Response()

        with mock.patch.object(self.admin.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.admin.auth_admin_request("/test", "session-secret", request_id="client-123")
            self.admin.auth_admin_request("/test", "session-secret", request_id="bad\r\nid")

        self.assertEqual(captured[0]["X-request-id"], "client-123")
        generated = captured[1]["X-request-id"]
        self.assertRegex(generated, r"^[0-9a-f]{32}$")
        self.assertNotIn("session-secret", generated)
        self.assertNotIn("internal-secret", generated)

    def test_ingress_rejects_non_object_json_with_controlled_400(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.admin.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with mock.patch.object(
                self.admin, "verify", return_value={"username": "boss", "role": "admin"}
            ), mock.patch.object(self.admin, "auth_admin_request") as forwarded:
                for path in ("/api/admin/points/adjust", "/api/admin/recharge/review"):
                    for payload in (None, [], 7, "text"):
                        with self.subTest(path=path, payload=payload):
                            req = urllib.request.Request(
                                base + path,
                                data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
                                method="POST",
                            )
                            try:
                                urllib.request.urlopen(req, timeout=3)
                                status = 200
                            except urllib.error.HTTPError as exc:
                                status = exc.code
                            except http.client.RemoteDisconnected:
                                status = None
                            self.assertEqual(status, 400)
                forwarded.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class AdminUiSecurityTests(unittest.TestCase):
    def test_ui_exposes_reason_and_delta_limits_before_submission(self):
        html = (pathlib.Path(__file__).parents[1] / "site" / "admin" / "index.html").read_text(encoding="utf-8")
        self.assertIn("ADMIN_REASON_MIN=4", html)
        self.assertIn("ADMIN_REASON_MAX=120", html)
        self.assertIn("ADMIN_POINTS_MAX_DELTA=1000", html)
        self.assertIn("validateAdminReason", html)
        self.assertIn("amount>ADMIN_POINTS_MAX_DELTA", html)


class AuthAdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = {key: os.environ.get(key) for key in (
            "HQ_TEST_AUTH_DB", "HQ_ADMIN_POINTS_MAX_DELTA", "HQ_CSRF_SECRET", "HQ_ALLOWED_ORIGINS"
        )}
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        os.environ["HQ_ADMIN_POINTS_MAX_DELTA"] = "1000"
        os.environ["HQ_CSRF_SECRET"] = "admin-security-csrf"
        os.environ["HQ_ALLOWED_ORIGINS"] = "https://app.example.test"
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        self.auth.init_db()
        c = sqlite3.connect(self.auth.DB)
        try:
            c.executemany(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) VALUES(?,?,?,?,?,?,0)",
                [
                    ("boss", "h", "s", "boss", 10, "admin"),
                    ("fang", "h", "s", "fang", 10, "member"),
                    ("member", "h", "s", "member", 10, "member"),
                ],
            )
            c.commit()
        finally:
            c.close()
        self.admin_token = self.auth.issue_token("boss")
        self.member_token = self.auth.issue_token("member")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _post(self, path, payload, token=None, request_id=None):
        headers = {
            "Content-Type": "application/json",
            "X-HQ-Internal-Token": "test-internal-token",
            "Authorization": "Bearer " + (token or self.admin_token),
        }
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        except http.client.RemoteDisconnected:
            return None, {}

    def _snapshot(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            points = c.execute("SELECT points FROM users WHERE username='fang'").fetchone()[0]
            audits = c.execute("SELECT COUNT(*) FROM points_audit").fetchone()[0]
            return points, audits
        finally:
            c.close()

    def test_non_admin_cannot_adjust_points_or_review_recharge(self):
        order, err = self.auth.create_recharge_order("fang", 10, 100, "test")
        self.assertIsNone(err)
        before = self._snapshot()
        status, _ = self._post(
            "/api/auth/admin/points/adjust",
            {"username": "fang", "delta": 1, "reason": "valid reason"},
            token=self.member_token,
        )
        self.assertEqual(status, 403)
        status, _ = self._post(
            "/api/auth/admin/recharge/review",
            {"order_id": order["order_id"], "action": "approve", "reason": "valid reason"},
            token=self.member_token,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self._snapshot(), before)

    def test_final_boundary_rejects_bad_reason_and_delta_without_side_effects(self):
        cases = [
            ({"username": "fang", "delta": 1}, "reason"),
            ({"username": "fang", "delta": 1, "reason": "abc"}, "reason"),
            ({"username": "fang", "delta": 1, "reason": "x" * 121}, "reason"),
            ({"username": "fang", "delta": 1.5, "reason": "valid reason"}, "delta"),
            ({"username": "fang", "delta": 0, "reason": "valid reason"}, "delta"),
            ({"username": "fang", "delta": True, "reason": "valid reason"}, "delta"),
            ({"username": "fang", "delta": "1", "reason": "valid reason"}, "delta"),
            ({"username": "fang", "delta": 1001, "reason": "valid reason"}, "delta"),
            ({"username": "fang", "delta": -1001, "reason": "valid reason"}, "delta"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                before = self._snapshot()
                status, body = self._post("/api/auth/admin/points/adjust", payload)
                self.assertEqual(status, 400)
                self.assertIn(expected, body["detail"].lower())
                self.assertEqual(self._snapshot(), before)

    def test_final_boundaries_reject_non_object_json_without_side_effects(self):
        for path in ("/api/auth/admin/points/adjust", "/api/auth/admin/recharge/review"):
            for payload in (None, [], 7, "text"):
                with self.subTest(path=path, payload=payload):
                    before = self._snapshot()
                    status, body = self._post(path, payload)
                    self.assertEqual(status, 400)
                    self.assertIn("object", body.get("detail", "").lower())
                    self.assertEqual(self._snapshot(), before)

    def test_audit_decoder_preserves_legacy_json_looking_reason(self):
        legacy = '{"reason":"legacy reason","request_id":"legacy-id"}'
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "INSERT INTO points_audit(who_admin,username,delta,before_points,after_points,reason,created_at) "
                "VALUES('legacy','fang',1,10,11,?,1)",
                (legacy,),
            )
            c.commit()
        finally:
            c.close()
        item = self.auth.list_points_audit(actor="admin")["items"][0]
        self.assertEqual(item["reason"], legacy)
        self.assertNotIn("request_id", item)

    def test_audit_decoder_requires_exact_integer_envelope_version(self):
        legacy = '{"_hq_admin_audit":true,"reason":"legacy reason","request_id":"legacy-id"}'
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "INSERT INTO points_audit(who_admin,username,delta,before_points,after_points,reason,created_at) "
                "VALUES('legacy','fang',1,10,11,?,1)",
                (legacy,),
            )
            c.commit()
        finally:
            c.close()
        item = self.auth.list_points_audit(actor="admin")["items"][0]
        self.assertEqual(item["reason"], legacy)
        self.assertNotIn("request_id", item)

    def test_valid_adjustment_is_atomic_and_uses_request_id_json_envelope(self):
        status, body = self._post(
            "/api/auth/admin/points/adjust",
            {"username": "fang", "delta": 7, "reason": "  manual correction  "},
            request_id="req-42",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["adjustment"]["before"], 10)
        self.assertEqual(body["adjustment"]["after"], 17)
        c = sqlite3.connect(self.auth.DB)
        c.row_factory = sqlite3.Row
        try:
            row = c.execute("SELECT * FROM points_audit").fetchone()
        finally:
            c.close()
        self.assertEqual(row["who_admin"], "boss")
        self.assertEqual(row["username"], "fang")
        self.assertEqual(row["delta"], 7)
        self.assertEqual(row["before_points"], 10)
        self.assertEqual(row["after_points"], 17)
        self.assertEqual(
            row["reason"],
            '{"_hq_admin_audit":1,"reason":"manual correction","request_id":"req-42"}',
        )
        public = self.auth.list_points_audit(actor="admin")["items"][0]
        self.assertEqual(public["reason"], "manual correction")
        self.assertEqual(public["request_id"], "req-42")

    def test_recharge_reason_is_required_and_repeated_review_is_idempotent(self):
        for action in ("approve", "reject"):
            order, err = self.auth.create_recharge_order("fang", 10, 100, "test")
            self.assertIsNone(err)
            before = self._snapshot()
            status, _ = self._post(
                "/api/auth/admin/recharge/review",
                {"order_id": order["order_id"], "action": action, "reason": "abc"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(self._snapshot(), before)

        order, _ = self.auth.create_recharge_order("fang", 10, 100, "test")
        payload = {"order_id": order["order_id"], "action": "approve", "reason": "payment verified"}
        first_status, first = self._post(
            "/api/auth/admin/recharge/review", payload, request_id="recharge-9"
        )
        after_first = self._snapshot()
        second_status, second = self._post(
            "/api/auth/admin/recharge/review", payload, request_id="recharge-9"
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(second["order"], first["order"])
        self.assertEqual(self._snapshot(), after_first)

    def test_repeated_rejection_is_idempotent(self):
        order, _ = self.auth.create_recharge_order("fang", 10, 100, "test")
        payload = {"order_id": order["order_id"], "action": "reject", "reason": "payment not found"}
        first_status, first = self._post(
            "/api/auth/admin/recharge/review", payload, request_id="reject-9"
        )
        after_first = self._snapshot()
        second_status, second = self._post(
            "/api/auth/admin/recharge/review", payload, request_id="reject-9"
        )
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["order"]["status"], "rejected")
        self.assertEqual(second["order"], first["order"])
        self.assertEqual(self._snapshot(), after_first)


if __name__ == "__main__":
    unittest.main()
