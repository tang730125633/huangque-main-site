import http.cookiejar
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer


class InviteRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.INVITE_HASH_SECRET = "test-invite-secret"
        self.auth.INVITE_PUBLIC_BASE_URL = "https://fang.example.test"
        self.auth.REGISTER_HITS.clear()
        self.auth.init_db()
        self.auth.create_user("inviter", "secret123", 10)
        self._activate_member("inviter")

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _connect(self):
        c = sqlite3.connect(self.auth.DB)
        c.row_factory = sqlite3.Row
        return c

    def _user_id(self, username):
        c = self._connect()
        try:
            return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        finally:
            c.close()

    def _activate_member(self, username, tier="experience", expires_at=None):
        c = self._connect()
        try:
            now = int(time.time())
            c.execute(
                """UPDATE users SET membership_tier=?,membership_started_at=?,membership_expires_at=?
                     WHERE username=?""",
                (tier, now, int(expires_at or now + self.auth.MEMBERSHIP_YEAR_SECONDS), username),
            )
            c.commit()
        finally:
            c.close()

    def _invite_code(self, username="inviter"):
        c = self._connect()
        try:
            row = self.auth.invites.ensure_user_code(c, self._user_id(username))
            c.commit()
            return row["code"]
        finally:
            c.close()

    def test_schema_and_default_campaign_are_created(self):
        c = self._connect()
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({
                "invite_campaigns", "invite_codes", "user_invites", "invite_admin_audit",
            }.issubset(tables))
            config = self.auth.invites.campaign_config(c)
            self.assertTrue(config["enabled"])
            self.assertFalse(config["code_required"])
        finally:
            c.close()

    def test_invite_code_is_stable_six_character_code(self):
        first = self._invite_code()
        second = self._invite_code()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 6)
        self.assertTrue(set(first).issubset(set(self.auth.invites.CODE_ALPHABET)))

    def test_only_active_members_can_create_or_use_invite_codes(self):
        self.auth.create_user("nonmember", "secret123", 10)
        c = self._connect()
        try:
            with self.assertRaises(self.auth.invites.InviteError) as raised:
                self.auth.invites.ensure_user_code(c, self._user_id("nonmember"))
            self.assertEqual(raised.exception.code, "membership_required")

            code = self._invite_code()
            c.execute(
                "UPDATE users SET membership_expires_at=? WHERE username='inviter'",
                (int(time.time()) - 1,),
            )
            c.commit()
            with self.assertRaises(self.auth.invites.InviteError) as expired:
                self.auth.invites.validate_code(c, code)
            self.assertEqual(expired.exception.code, "inviter_ineligible")
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM invite_codes WHERE code=?", (code,)).fetchone()[0], 1,
            )
            c.close()
            c = None
            result, err = self.auth.register_account("expired_invite", "secret123", invite_code=code)
            self.assertIsNone(result)
            self.assertEqual(err["code"], "inviter_ineligible")
            c = self._connect()
            self.assertFalse(c.execute(
                "SELECT 1 FROM users WHERE username='expired_invite'"
            ).fetchone())

            renewed_until = int(time.time()) + self.auth.MEMBERSHIP_YEAR_SECONDS
            c.execute(
                "UPDATE users SET membership_expires_at=? WHERE username='inviter'",
                (renewed_until,),
            )
            c.commit()
            self.assertEqual(self.auth.invites.validate_code(c, code)["code"], code)
        finally:
            if c:
                c.close()

    def test_registration_and_binding_commit_together(self):
        code = self._invite_code()
        result, err = self.auth.register_account(
            "new_user", "secret123", "新用户", code, "web_link",
            client_ip="203.0.113.9", device_id="device-one",
        )
        self.assertIsNone(err)
        self.assertTrue(result["invite_bound"])
        c = self._connect()
        try:
            relation = c.execute("""SELECT ui.*,u.username AS inviter
                                    FROM user_invites ui JOIN users u ON u.id=ui.inviter_user_id
                                    WHERE ui.invitee_user_id=?""", (self._user_id("new_user"),)).fetchone()
            self.assertEqual(relation["inviter"], "inviter")
            self.assertEqual(relation["source"], "web_link")
            self.assertNotEqual(relation["ip_hash"], "203.0.113.9")
            self.assertNotEqual(relation["device_hash"], "device-one")
        finally:
            c.close()

    def test_required_or_invalid_code_rolls_back_user_creation(self):
        c = self._connect()
        c.execute("UPDATE invite_campaigns SET code_required=1")
        c.commit(); c.close()

        result, err = self.auth.register_account("missing_code", "secret123")
        self.assertIsNone(result)
        self.assertEqual(err["code"], "code_required")
        result, err = self.auth.register_account("bad_code", "secret123", invite_code="ABCDEF")
        self.assertIsNone(result)
        self.assertEqual(err["code"], "invalid_code")
        c = self._connect()
        try:
            count = c.execute(
                "SELECT COUNT(*) FROM users WHERE username IN ('missing_code','bad_code')"
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            c.close()

    def test_daily_limit_rejects_binding_and_rolls_back_registration(self):
        code = self._invite_code()
        c = self._connect()
        c.execute("UPDATE invite_campaigns SET daily_invite_limit=1")
        c.commit(); c.close()
        first, first_err = self.auth.register_account("first", "secret123", invite_code=code)
        self.assertIsNone(first_err)
        self.assertTrue(first["invite_bound"])

        second, second_err = self.auth.register_account("second", "secret123", invite_code=code)
        self.assertIsNone(second)
        self.assertEqual(second_err["code"], "daily_limit")
        c = self._connect()
        try:
            self.assertFalse(c.execute("SELECT 1 FROM users WHERE username='second'").fetchone())
        finally:
            c.close()

    def test_concurrent_registration_creates_one_user_and_one_relation(self):
        code = self._invite_code()
        results = []
        lock = threading.Lock()

        def register():
            item = self.auth.register_account("same_user", "secret123", invite_code=code)
            with lock:
                results.append(item)

        threads = [threading.Thread(target=register) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(1 for result, err in results if result and not err), 1)
        self.assertEqual(sum(1 for result, err in results if err and err["code"] == "username_exists"), 1)
        c = self._connect()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE username='same_user'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0], 1)
        finally:
            c.close()

    def test_disabled_campaign_keeps_plain_registration_but_rejects_invite_code(self):
        code = self._invite_code()
        c = self._connect()
        c.execute("UPDATE invite_campaigns SET status='disabled'")
        c.commit(); c.close()

        plain, plain_err = self.auth.register_account("plain_user", "secret123")
        self.assertIsNone(plain_err)
        self.assertFalse(plain["invite_bound"])
        invited, invited_err = self.auth.register_account("invited_user", "secret123", invite_code=code)
        self.assertIsNone(invited)
        self.assertEqual(invited_err["code"], "campaign_inactive")
        c = self._connect()
        try:
            self.assertFalse(c.execute("SELECT 1 FROM users WHERE username='invited_user'").fetchone())
        finally:
            c.close()

    def test_dashboard_counts_direct_and_indirect_invites(self):
        inviter_code = self._invite_code()
        first, err = self.auth.register_account("first", "secret123", invite_code=inviter_code)
        self.assertIsNone(err)
        self._activate_member("first")
        first_code = self._invite_code("first")
        second, err = self.auth.register_account("second", "secret123", invite_code=first_code)
        self.assertIsNone(err)
        c = self._connect()
        try:
            data = self.auth.invites.dashboard(c, self._user_id("inviter"))
            self.assertEqual(data["direct_invites"], 1)
            self.assertEqual(data["indirect_invites"], 1)
        finally:
            c.close()

    def test_invited_users_support_two_levels_pagination_and_recharge_total(self):
        inviter_code = self._invite_code()
        for index in range(11):
            result, err = self.auth.register_account(
                "direct_%02d" % index, "secret123", invite_code=inviter_code,
            )
            self.assertIsNone(err)
        self._activate_member("direct_00")
        self._activate_member("direct_01", expires_at=int(time.time()) - 1)
        child_code = self._invite_code("direct_00")
        child, err = self.auth.register_account("second_user", "secret123", invite_code=child_code)
        self.assertIsNone(err)
        now = 1700000000
        c = self._connect()
        try:
            c.execute("""INSERT INTO recharge_orders(order_id,username,amount,points,status,created_at)
                         VALUES('approved-one','direct_00',99,1000,'approved',?)""", (now,))
            c.execute("""INSERT INTO recharge_orders(order_id,username,amount,points,status,created_at)
                         VALUES('pending-one','direct_00',999,9990,'pending',?)""", (now,))
            c.execute("""INSERT INTO virtual_pay_orders(
                order_id,username,openid,package_id,product_id,amount_fen,points,env,status,created_at
            ) VALUES('virtual-one','second_user','openid-test','pkg','product',1250,125,0,'credited',?)""", (now,))
            c.commit()
            first_page = self.auth.invites.invited_users(c, self._user_id("inviter"), level=1, limit=10, offset=0)
            second_page = self.auth.invites.invited_users(c, self._user_id("inviter"), level=1, limit=10, offset=10)
            self.assertEqual(first_page["total"], 11)
            self.assertEqual(len(first_page["users"]), 10)
            self.assertEqual(len(second_page["users"]), 1)
            direct = next(user for user in first_page["users"] + second_page["users"] if user["username"] == "direct_00")
            self.assertEqual(direct["recharge_total"], 99.0)
            self.assertTrue(direct["membership_active"])
            self.assertEqual(direct["membership_name"], "体验官")
            expired = next(user for user in first_page["users"] + second_page["users"] if user["username"] == "direct_01")
            self.assertEqual(expired["membership_status"], "expired")
            nonmember = next(user for user in first_page["users"] + second_page["users"] if user["username"] == "direct_02")
            self.assertEqual(nonmember["membership_status"], "none")
            indirect = self.auth.invites.invited_users(c, self._user_id("inviter"), level=2, limit=10, offset=0)
            self.assertEqual(indirect["total"], 1)
            self.assertEqual(indirect["users"][0]["username"], "second_user")
            self.assertEqual(indirect["users"][0]["parent_username"], "direct_00")
            self.assertEqual(indirect["users"][0]["recharge_total"], 12.5)
        finally:
            c.close()

    def test_http_endpoints_validate_register_and_return_referrer(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        inviter_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        invitee_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            login = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "inviter", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            inviter_client.open(login, timeout=3).read()
            code_data = json.loads(inviter_client.open(base + "/api/invite/code", timeout=3).read())
            code = code_data["code"]
            self.assertEqual(code_data["invite_link"], "https://fang.example.test/register?invite=" + code)
            validate = json.loads(urllib.request.urlopen(
                base + "/api/invite/validate?code=" + urllib.parse.quote(code), timeout=3,
            ).read())
            self.assertEqual(validate["inviter"]["name"], "inviter")

            register = urllib.request.Request(
                base + "/api/auth/register",
                data=json.dumps({
                    "username": "web_new", "password": "secret123",
                    "invite_code": code, "invite_source": "web_link", "device_id": "web-device",
                }).encode(),
                headers={"Content-Type": "application/json", "X-Real-IP": "203.0.113.10"},
                method="POST",
            )
            registered = json.loads(invitee_client.open(register, timeout=3).read())
            self.assertTrue(registered["invite_bound"])
            referrer = json.loads(invitee_client.open(base + "/api/invite/referrer", timeout=3).read())
            self.assertEqual(referrer["referrer"]["name"], "inviter")
            users = json.loads(inviter_client.open(base + "/api/invite/users", timeout=3).read())
            self.assertEqual(users["users"][0]["username"], "web_new")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_miniprogram_registration_uses_shared_transaction(self):
        code = self._invite_code()
        result, err = self.auth.register_account(
            "mp_new", "secret123", invite_code=code,
            invite_source="miniprogram", device_id="mini-device",
        )
        self.assertIsNone(err)
        self.assertIn("token", result)
        c = self._connect()
        try:
            source = c.execute(
                "SELECT source FROM user_invites WHERE invitee_user_id=?",
                (self._user_id("mp_new"),),
            ).fetchone()[0]
            self.assertEqual(source, "miniprogram")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
