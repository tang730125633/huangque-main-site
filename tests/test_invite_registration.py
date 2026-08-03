import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch


class InviteRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_enforcement = os.environ.get("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED")
        os.environ.pop("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED", None)
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.INVITE_HASH_SECRET = "test-invite-secret"
        self.auth.INVITE_PUBLIC_BASE_URL = "https://fang.example.test"
        self.auth.REGISTER_MAX = 10
        self.auth.REGISTER_WINDOW = 120
        self.auth.REGISTER_IP_MAX = 20
        self.auth.REGISTER_IP_WINDOW = 60
        self.auth.REGISTER_HITS.clear()
        self.auth.init_db()
        self.auth.create_user("inviter", "secret123", 10)
        now = int(time.time())
        conn = sqlite3.connect(self.auth.DB)
        conn.execute(
            """UPDATE users
                  SET membership_tier='experience',
                      membership_started_at=?,
                      membership_expires_at=?
                WHERE username='inviter'""",
            (now, now + self.auth.MEMBERSHIP_YEAR_SECONDS),
        )
        conn.commit()
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        if self.old_enforcement is None:
            os.environ.pop("HQ_MEMBERSHIP_ENFORCEMENT_ENABLED", None)
        else:
            os.environ["HQ_MEMBERSHIP_ENFORCEMENT_ENABLED"] = self.old_enforcement
        self.tmp.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.auth.DB)
        conn.row_factory = sqlite3.Row
        return conn

    def _user_id(self, username):
        conn = self._connect()
        try:
            return conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        finally:
            conn.close()

    def _invite_code(self, username="inviter"):
        user_id = self._user_id(username)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self.auth.invites.ensure_user_code(conn, user_id)
            conn.commit()
            return row["code"]
        finally:
            conn.close()

    def _request(self, path, payload=None, headers=None):
        request_headers = dict(headers or {})
        data = None
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(
            self.base + path, data=data, headers=request_headers, method=method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read()), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), exc.headers

    def test_full_membership_schema_is_created_and_invite_requirement_is_configurable(self):
        conn = self._connect()
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({
                "invite_campaigns", "invite_codes", "user_invites",
                "invite_admin_audit", "membership_upgrade_records",
                "invite_reward_point_records",
            } <= tables)
            conn.execute("UPDATE invite_campaigns SET code_required=1")
            conn.commit()
        finally:
            conn.close()

        result, err = self.auth.register_account("legacy", "secret123")
        self.assertIsNone(result)
        self.assertEqual(err["code"], "code_required")
        conn = self._connect()
        conn.execute("UPDATE invite_campaigns SET code_required=0")
        conn.commit()
        conn.close()
        result, err = self.auth.register_account("legacy", "secret123")
        self.assertIsNone(err)
        self.assertEqual(result["user"]["points"], 16)
        conn = self._connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0], 0,
            )
        finally:
            conn.close()

    def test_website_registration_keeps_legacy_response_and_binds_direct_inviter(self):
        code = self._invite_code()
        status, body, headers = self._request(
            "/api/auth/register",
            {
                "username": "web_new",
                "password": "secret123",
                "invite_code": code,
                "invite_source": "web_link",
                "device_id": "web-device",
            },
            {"X-Real-IP": "203.0.113.10"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"user", "invite_bound"})
        self.assertTrue(body["invite_bound"])
        self.assertEqual(body["user"]["points"], 16)
        self.assertNotIn("invite", body["user"])
        self.assertIn("HttpOnly", headers.get("Set-Cookie") or "")

        conn = self._connect()
        try:
            relation = conn.execute(
                "SELECT * FROM user_invites WHERE invitee_user_id=?",
                (self._user_id("web_new"),),
            ).fetchone()
            self.assertEqual(relation["inviter_user_id"], self._user_id("inviter"))
            self.assertEqual(relation["source"], "web_link")
            self.assertEqual(len(relation["ip_hash"]), 64)
            self.assertEqual(len(relation["device_hash"]), 64)
            self.assertNotEqual(relation["ip_hash"], "203.0.113.10")
            self.assertNotEqual(relation["device_hash"], "web-device")
        finally:
            conn.close()

    def test_registration_rejects_oversized_credentials_before_hashing(self):
        for username, password, code in (
            ("u" * 65, "secret123", "username_too_long"),
            ("user", "p" * 129, "password_too_long"),
        ):
            with self.subTest(code=code), patch.object(self.auth, "hash_pw") as hash_pw:
                result, err = self.auth.register_account(username, password)
                self.assertIsNone(result)
                self.assertEqual(err["code"], code)
                hash_pw.assert_not_called()

    def test_miniprogram_registration_returns_only_token_and_user(self):
        code = self._invite_code()
        status, body, _ = self._request(
            "/api/auth/miniprogram-register",
            {
                "username": "mp_new",
                "password": "secret123",
                "invite_code": code,
                "device_id": "mp-device",
            },
            {"X-Real-IP": "203.0.113.11"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"token", "user", "invite_bound"})
        self.assertTrue(body["invite_bound"])
        self.assertEqual(body["user"]["points"], 16)
        self.assertNotIn("invite", body["user"])
        conn = self._connect()
        try:
            source = conn.execute(
                "SELECT source FROM user_invites WHERE invitee_user_id=?",
                (self._user_id("mp_new"),),
            ).fetchone()[0]
            self.assertEqual(source, "miniprogram")
        finally:
            conn.close()

    def test_free_users_can_keep_using_invite_codes_when_membership_gate_is_enabled(self):
        code = self._invite_code()
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE users SET membership_tier='',
                                    membership_started_at=NULL,
                                    membership_expires_at=NULL
                   WHERE username='inviter'"""
            )
            conn.commit()
        finally:
            conn.close()

        status, _, _ = self._request("/api/auth/invite/validate?code=" + code)
        self.assertEqual(status, 200)
        status, body, _ = self._request(
            "/api/auth/register",
            {"username": "switch_off_user", "password": "secret123", "invite_code": code},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["invite_bound"])

        os.environ["HQ_MEMBERSHIP_ENFORCEMENT_ENABLED"] = "1"
        status, body, _ = self._request("/api/auth/invite/validate?code=" + code)
        self.assertEqual(status, 200)

    def test_invalid_code_rolls_back_user_relation_and_token(self):
        result, err = self.auth.register_account(
            "rolled_back", "secret123", invite_code="ABCDEF",
        )
        self.assertIsNone(result)
        self.assertEqual(err["code"], "invalid_code")
        conn = self._connect()
        try:
            self.assertFalse(
                conn.execute("SELECT 1 FROM users WHERE username='rolled_back'").fetchone()
            )
            self.assertFalse(
                conn.execute("SELECT 1 FROM tokens WHERE username='rolled_back'").fetchone()
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0], 0,
            )
        finally:
            conn.close()

    def test_concurrent_same_username_creates_one_complete_registration(self):
        code = self._invite_code()
        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()

        def register():
            barrier.wait()
            item = self.auth.register_account(
                "same_user", "secret123", invite_code=code,
            )
            with result_lock:
                results.append(item)

        threads = [threading.Thread(target=register) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(bool(result) for result, _ in results), 1)
        self.assertEqual(
            sum(bool(err and err["code"] == "username_exists") for _, err in results), 1,
        )
        conn = self._connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users WHERE username='same_user'").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("""SELECT COUNT(*) FROM user_invites ui
                                JOIN users u ON u.id=ui.invitee_user_id
                                WHERE u.username='same_user'""").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tokens WHERE username='same_user'").fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_failed_web_and_miniprogram_attempts_share_limit_without_renewing_block(self):
        self.auth.REGISTER_MAX = 2
        ip_header = {"X-Real-IP": "203.0.113.20"}
        self.assertEqual(
            self._request("/api/auth/register", {}, ip_header)[0], 400,
        )
        self.assertEqual(
            self._request(
                "/api/auth/miniprogram-register",
                {"username": "bad", "password": "x"},
                ip_header,
            )[0],
            400,
        )
        key = "203.0.113.20|missing-device"
        before_block = list(self.auth.REGISTER_HITS[key])
        status, _, _ = self._request(
            "/api/auth/register",
            {"username": "blocked", "password": "secret123"},
            ip_header,
        )
        self.assertEqual(status, 429)
        self.assertEqual(self.auth.REGISTER_HITS[key], before_block)
        conn = self._connect()
        try:
            self.assertFalse(
                conn.execute("SELECT 1 FROM users WHERE username='blocked'").fetchone()
            )
        finally:
            conn.close()

    def test_register_limit_check_and_consume_is_atomic(self):
        self.auth.REGISTER_MAX = 1
        barrier = threading.Barrier(6)
        statuses = []
        status_lock = threading.Lock()

        def register(index):
            barrier.wait()
            path = (
                "/api/auth/register" if index % 2 == 0
                else "/api/auth/miniprogram-register"
            )
            status, _, _ = self._request(
                path,
                {"username": "limited%d" % index, "password": "secret123"},
                {"X-Real-IP": "203.0.113.21"},
            )
            with status_lock:
                statuses.append(status)

        threads = [threading.Thread(target=register, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(429), 5)

    def test_same_ip_different_devices_have_independent_registration_limits(self):
        self.auth.REGISTER_MAX = 1
        headers = {"X-Real-IP": "203.0.113.22"}

        first = self._request(
            "/api/auth/register",
            {"username": "device_a_1", "password": "secret123", "device_id": "device-a"},
            headers,
        )
        blocked = self._request(
            "/api/auth/miniprogram-register",
            {"username": "device_a_2", "password": "secret123", "device_id": "device-a"},
            headers,
        )
        other_device = self._request(
            "/api/auth/register",
            {"username": "device_b_1", "password": "secret123", "device_id": "device-b"},
            headers,
        )

        self.assertEqual(first[0], 200)
        self.assertEqual(blocked[0], 429)
        self.assertEqual(other_device[0], 200)

    def test_same_ip_all_devices_share_twenty_attempts_per_minute(self):
        self.auth.REGISTER_MAX = 100
        self.auth.REGISTER_IP_MAX = 2
        headers = {"X-Real-IP": "203.0.113.23"}

        first = self._request(
            "/api/auth/register",
            {"username": "shared_ip_1", "password": "secret123", "device_id": "device-1"},
            headers,
        )
        second = self._request(
            "/api/auth/register",
            {"username": "shared_ip_2", "password": "secret123", "device_id": "device-2"},
            headers,
        )
        blocked = self._request(
            "/api/auth/register",
            {"username": "shared_ip_3", "password": "secret123", "device_id": "device-3"},
            headers,
        )

        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(blocked[0], 429)

    def test_registration_rate_limit_defaults_to_ten_attempts_per_two_minutes(self):
        self.assertEqual(self.auth.REGISTER_MAX, 10)
        self.assertEqual(self.auth.REGISTER_WINDOW, 120)
        self.assertEqual(self.auth.REGISTER_IP_MAX, 20)
        self.assertEqual(self.auth.REGISTER_IP_WINDOW, 60)

    def test_code_endpoint_requires_login_and_first_issue_is_concurrently_stable(self):
        self.assertEqual(self._request("/api/auth/invite/code")[0], 401)
        token = self.auth.issue_token("inviter")
        headers = {"Authorization": "Bearer " + token}
        self.assertEqual(self._request("/api/invite/code", headers=headers)[0], 200)
        barrier = threading.Barrier(5)
        responses = []
        response_lock = threading.Lock()

        def get_code():
            barrier.wait()
            status, body, _ = self._request("/api/auth/invite/code", headers=headers)
            with response_lock:
                responses.append((status, body))

        threads = [threading.Thread(target=get_code) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual({status for status, _ in responses}, {200})
        codes = {body["code"] for _, body in responses}
        self.assertEqual(len(codes), 1)
        for _, body in responses:
            self.assertEqual(set(body), {"ok", "code", "invite_link"})
            self.assertEqual(
                body["invite_link"],
                "https://fang.example.test/register?invite=" + body["code"],
            )
        conn = self._connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM invite_codes").fetchone()[0], 1,
            )
        finally:
            conn.close()

    def test_member_clients_hide_partner_and_initiator_reward_ledger(self):
        conn = self._connect()
        try:
            now = int(time.time())
            conn.execute(
                """UPDATE users
                      SET membership_tier='partner',
                          membership_started_at=?,
                          membership_expires_at=?
                    WHERE username='inviter'""",
                (now, now + self.auth.MEMBERSHIP_YEAR_SECONDS),
            )
            conn.commit()
        finally:
            conn.close()

        result, err = self.auth.register_account(
            "reward-invitee", "secret123", invite_code=self._invite_code(),
        )
        self.assertIsNone(err)
        upgraded, err = self.auth.set_membership_admin(
            "admin", "reward-invitee", "experience", "测试邀请奖励",
        )
        self.assertIsNone(err)
        self.assertEqual(upgraded["membership_tier"], "experience")
        conn = self._connect()
        try:
            ledger = self.auth.invites.reward_points(
                conn, self._user_id("inviter"),
            )
            self.assertEqual(ledger["total_reward_points"], 240)
            self.assertEqual(ledger["total"], 1)
        finally:
            conn.close()

        token = self.auth.issue_token("inviter")
        headers = {"Authorization": "Bearer " + token}
        for tier in ("partner", "initiator"):
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE users SET membership_tier=? WHERE username='inviter'",
                    (tier,),
                )
                conn.commit()
            finally:
                conn.close()

            with self.subTest(tier=tier):
                status, body, _ = self._request(
                    "/api/auth/invite/reward-points?limit=20&offset=0",
                    headers=headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["total_reward_points"], 0)
                self.assertEqual(body["total"], 0)
                self.assertEqual(body["records"], [])

                website_status, website_body, _ = self._request(
                    "/api/invite/reward-points?limit=20&offset=0",
                    headers=headers,
                )
                self.assertEqual(website_status, 200)
                self.assertEqual(website_body["total_reward_points"], 0)
                self.assertEqual(website_body["total"], 0)
                self.assertEqual(website_body["records"], [])

        conn = self._connect()
        try:
            conn.execute(
                "UPDATE users SET membership_tier='experience' WHERE username='inviter'",
            )
            conn.commit()
        finally:
            conn.close()
        status, body, _ = self._request(
            "/api/auth/invite/reward-points?limit=20&offset=0",
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["total_reward_points"], 240)
        self.assertEqual(body["total"], 1)
        website_status, website_body, _ = self._request(
            "/api/invite/reward-points?limit=20&offset=0",
            headers=headers,
        )
        self.assertEqual(website_status, 200)
        self.assertEqual(website_body["total_reward_points"], 240)
        self.assertEqual(website_body["total"], 1)

    def test_empty_hash_secret_stores_no_ip_or_device_identifier(self):
        self.auth.INVITE_HASH_SECRET = ""
        result, err = self.auth.register_account(
            "no_hash", "secret123", invite_code=self._invite_code(),
            client_ip="203.0.113.30", device_id="raw-device",
        )
        self.assertIsNone(err)
        self.assertIsNotNone(result)
        conn = self._connect()
        try:
            relation = conn.execute(
                "SELECT ip_hash,device_hash FROM user_invites WHERE invitee_user_id=?",
                (self._user_id("no_hash"),),
            ).fetchone()
            self.assertIsNone(relation["ip_hash"])
            self.assertIsNone(relation["device_hash"])
        finally:
            conn.close()

    def test_risk_hashes_expire_and_unknown_sources_are_rejected(self):
        code = self._invite_code()
        result, err = self.auth.register_account(
            "old_hash", "secret123", invite_code=code,
            client_ip="203.0.113.40", device_id="old-device",
        )
        self.assertIsNone(err)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE user_invites SET bound_at=? WHERE invitee_user_id=?",
                (int(time.time()) - self.auth.invites.RISK_HASH_RETENTION - 1, self._user_id("old_hash")),
            )
            conn.commit()
        finally:
            conn.close()
        result, err = self.auth.register_account(
            "new_hash", "secret123", invite_code=code,
            client_ip="203.0.113.41", device_id="new-device",
        )
        self.assertIsNone(err)
        self.auth.create_user("bad_source", "secret123")
        conn = self._connect()
        try:
            old = conn.execute(
                "SELECT ip_hash,device_hash FROM user_invites WHERE invitee_user_id=?",
                (self._user_id("old_hash"),),
            ).fetchone()
            self.assertIsNone(old["ip_hash"])
            self.assertIsNone(old["device_hash"])
            with self.assertRaises(self.auth.invites.InviteError) as raised:
                self.auth.invites.bind_registration(
                    conn, self._user_id("bad_source"), code, "unknown",
                    hash_secret=self.auth.INVITE_HASH_SECRET,
                )
            self.assertEqual(raised.exception.code, "invalid_source")
        finally:
            conn.close()

    def test_password_hash_finishes_before_registration_write_lock(self):
        events = []
        original_hash = self.auth.hash_pw
        original_db = self.auth.db

        class TracedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if sql == "BEGIN IMMEDIATE":
                    events.append("begin")
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def traced_hash(password, salt):
            events.append("hash")
            return original_hash(password, salt)

        with patch.object(self.auth, "hash_pw", traced_hash), patch.object(
            self.auth, "db", lambda: TracedConnection(original_db())
        ):
            result, err = self.auth.register_account("lock_order", "secret123")
        self.assertIsNone(err)
        self.assertIsNotNone(result)
        self.assertLess(events.index("hash"), events.index("begin"))

    def test_daily_limit_failure_rolls_back_second_account(self):
        code = self._invite_code()
        conn = self._connect()
        try:
            conn.execute("UPDATE invite_campaigns SET daily_invite_limit=1")
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(
            self.auth.register_account("first", "secret123", invite_code=code)[1]
        )
        result, err = self.auth.register_account("second", "secret123", invite_code=code)
        self.assertIsNone(result)
        self.assertEqual(err["code"], "daily_limit")
        conn = self._connect()
        try:
            self.assertFalse(
                conn.execute("SELECT 1 FROM users WHERE username='second'").fetchone()
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
