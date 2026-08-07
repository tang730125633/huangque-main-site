import http.cookiejar
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer


class PointTransferTests(unittest.TestCase):
    PASSWORD = "secret123"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.ITER = 1000
        self.auth.init_db()
        salt = "11" * 16
        password_hash = self.auth.hash_pw(self.PASSWORD, salt)
        c = self.auth.db()
        try:
            c.execute(
                """INSERT INTO users(
                       username,pw_hash,pw_salt,display_name,points,role,must_change,
                       card_initial_password,account_id,account_status,membership_tier,
                       membership_started_at,membership_expires_at
                   ) VALUES(?,?,?,?,?,'member',0,0,?,'active','partner',1,4102444800)""",
                ("sender", password_hash, salt, "发送者", 200000, "HQAAAAAA"),
            )
            c.execute(
                """INSERT INTO users(
                       username,pw_hash,pw_salt,display_name,points,role,must_change,
                       card_initial_password,account_id,account_status
                   ) VALUES(?,?,?,?,?,'member',0,0,?,'active')""",
                ("recipient", password_hash, salt, "接收者", 100, "HQBBBBBB"),
            )
            c.commit()
        finally:
            c.close()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _transfer(self, amount=100, request_id="transfer-request-0001", **overrides):
        values = {
            "sender_username": "sender",
            "recipient_account_id": "HQBBBBBB",
            "amount": amount,
            "password": self.PASSWORD,
            "request_id": request_id,
            "note": "测试赠送",
            "now": 1785945600,
        }
        values.update(overrides)
        return self.auth.transfer_points(**values)

    def _error_code(self, code, **kwargs):
        with self.assertRaises(self.auth.PointTransferError) as ctx:
            self._transfer(**kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_schema_has_transfer_ledger_and_sender_request_uniqueness(self):
        c = self.auth.db()
        try:
            columns = {row["name"] for row in c.execute("PRAGMA table_info(point_transfers)")}
            indexes = {row["name"] for row in c.execute("PRAGMA index_list(point_transfers)")}
        finally:
            c.close()
        self.assertIn("transfer_id", columns)
        self.assertIn("sender_before", columns)
        self.assertIn("recipient_after", columns)
        self.assertTrue(any(name.startswith("sqlite_autoindex_point_transfers") for name in indexes))

    def test_only_active_partner_and_initiator_can_send_points(self):
        c = self.auth.db()
        try:
            c.execute(
                "UPDATE users SET membership_tier='experience',membership_expires_at=4102444800 "
                "WHERE username='sender'"
            )
            c.commit()
        finally:
            c.close()
        self._error_code("point_transfer_not_allowed")
        with self.assertRaises(self.auth.PointTransferError) as lookup:
            self.auth.point_transfer_recipient("sender", "HQBBBBBB")
        self.assertEqual(lookup.exception.code, "point_transfer_not_allowed")

        c = self.auth.db()
        try:
            c.execute(
                "UPDATE users SET membership_tier='partner',membership_expires_at=1 "
                "WHERE username='sender'"
            )
            c.commit()
        finally:
            c.close()
        self._error_code("point_transfer_not_allowed")

        c = self.auth.db()
        try:
            c.execute(
                "UPDATE users SET membership_tier='initiator',membership_expires_at=4102444800 "
                "WHERE username='sender'"
            )
            c.commit()
        finally:
            c.close()
        result = self._transfer(amount=1, request_id="transfer-initiator-0001")
        self.assertEqual(result["points"], 199999)

    def test_success_is_atomic_and_writes_paired_audit_rows(self):
        result = self._transfer(amount=123)
        self.assertEqual(result["points"], 199877)
        self.assertEqual(result["daily_used"], 123)
        self.assertEqual(result["daily_remaining"], 99877)
        self.assertEqual(result["counterpart"]["name"], "接收者")
        self.assertNotIn("username", result["counterpart"])

        c = self.auth.db()
        try:
            balances = dict(c.execute("SELECT username,points FROM users").fetchall())
            transfer = c.execute("SELECT * FROM point_transfers").fetchone()
            audits = c.execute(
                "SELECT username,delta,before_points,after_points,who_admin FROM points_audit ORDER BY id"
            ).fetchall()
        finally:
            c.close()
        self.assertEqual(balances, {"sender": 199877, "recipient": 223})
        self.assertEqual(transfer["sender_before"], 200000)
        self.assertEqual(transfer["recipient_after"], 223)
        self.assertEqual([(row["username"], row["delta"]) for row in audits], [
            ("sender", -123), ("recipient", 123),
        ])
        self.assertTrue(all(row["who_admin"] == self.auth.POINT_TRANSFER_ACTOR for row in audits))

    def test_idempotent_replay_does_not_move_points_twice(self):
        first = self._transfer(amount=500)
        second = self._transfer(amount=500)
        self.assertEqual(first["transfer_id"], second["transfer_id"])
        c = self.auth.db()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM point_transfers").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM points_audit").fetchone()[0], 2)
            balances = dict(c.execute("SELECT username,points FROM users").fetchall())
        finally:
            c.close()
        self.assertEqual(balances, {"sender": 199500, "recipient": 600})

    def test_request_id_reuse_with_different_payload_is_rejected(self):
        self._transfer(amount=100)
        self._error_code("transaction_conflict", amount=101)

    def test_password_self_inactive_and_insufficient_guards_leave_balances_unchanged(self):
        self._error_code("password_invalid", password="wrong-password")
        self._error_code("self_transfer", recipient_account_id="HQAAAAAA")
        c = self.auth.db()
        try:
            c.execute("UPDATE users SET account_status='disabled' WHERE username='recipient'")
            c.execute("UPDATE users SET points=5 WHERE username='sender'")
            c.commit()
        finally:
            c.close()
        self._error_code("recipient_not_found")
        c = self.auth.db()
        try:
            c.execute("UPDATE users SET account_status='active' WHERE username='recipient'")
            c.commit()
        finally:
            c.close()
        self._error_code("insufficient_points", amount=6)
        c = self.auth.db()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM point_transfers").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM points_audit").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT points FROM users WHERE username='sender'").fetchone()[0], 5)
        finally:
            c.close()

    def test_daily_limit_allows_exactly_100000_then_blocks_more(self):
        self._transfer(amount=60000, request_id="transfer-daily-0001")
        exact = self._transfer(amount=40000, request_id="transfer-daily-0002")
        self.assertEqual(exact["daily_remaining"], 0)
        error = self._error_code(
            "daily_limit_exceeded", amount=1, request_id="transfer-daily-0003",
        )
        self.assertEqual(error.extra["daily_used"], 100000)
        self.assertEqual(error.extra["daily_remaining"], 0)

    def test_concurrent_transfers_cannot_bypass_daily_limit(self):
        results = []
        lock = threading.Lock()

        def worker(index):
            try:
                self._transfer(amount=10000, request_id="transfer-concurrent-%04d" % index)
                value = "ok"
            except self.auth.PointTransferError as exc:
                value = exc.code
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 10)
        self.assertEqual(results.count("daily_limit_exceeded"), 10)
        c = self.auth.db()
        try:
            self.assertEqual(c.execute("SELECT points FROM users WHERE username='sender'").fetchone()[0], 100000)
            self.assertEqual(c.execute("SELECT points FROM users WHERE username='recipient'").fetchone()[0], 100100)
        finally:
            c.close()

    def test_invitation_reward_ledger_is_not_transferable_balance(self):
        c = self.auth.db()
        try:
            sender_id = c.execute("SELECT id FROM users WHERE username='sender'").fetchone()[0]
            recipient_id = c.execute("SELECT id FROM users WHERE username='recipient'").fetchone()[0]
            c.execute("UPDATE users SET points=5 WHERE id=?", (sender_id,))
            c.execute(
                """INSERT INTO invite_reward_point_records(
                       invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
                       inviter_level_snapshot,invitee_level,reward_points,reward_total_after,status,created_at
                   ) VALUES(1,1,?,?, 'experience','experience',999999,999999,'recorded',?)""",
                (sender_id, recipient_id, 1785945600),
            )
            c.commit()
        finally:
            c.close()
        self._error_code("insufficient_points", amount=6)

    def test_history_has_directions_pagination_and_no_login_identifier(self):
        self._transfer(amount=100, request_id="transfer-history-0001")
        sender = self.auth.list_point_transfers("sender", limit=1, offset=0)
        recipient = self.auth.list_point_transfers("recipient", limit=1, offset=0)
        self.assertEqual(sender["items"][0]["direction"], "sent")
        self.assertEqual(recipient["items"][0]["direction"], "received")
        self.assertEqual(sender["total"], 1)
        self.assertNotIn("username", json.dumps(sender, ensure_ascii=False))

    def test_recipient_lookup_uses_safe_name_fallback(self):
        c = self.auth.db()
        try:
            c.execute("UPDATE users SET display_name=username WHERE username='recipient'")
            c.commit()
        finally:
            c.close()
        recipient = self.auth.point_transfer_recipient("sender", "hqbbbbbb")
        self.assertEqual(recipient, {
            "account_id": "HQBBBBBB", "name": "黄雀用户", "avatar": "",
        })

    def test_http_endpoints_require_login_and_support_transfer_history(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                urllib.request.urlopen(base + "/api/auth/points/transfers", timeout=3)
            self.assertEqual(unauthenticated.exception.code, 401)

            token = self.auth.issue_token("sender")
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            lookup = urllib.request.Request(
                base + "/api/auth/points/transfer/recipient?account_id=HQBBBBBB",
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(lookup, timeout=3) as response:
                recipient = json.loads(response.read())["recipient"]
            self.assertEqual(recipient["account_id"], "HQBBBBBB")

            request = urllib.request.Request(
                base + "/api/auth/points/transfer",
                data=json.dumps({
                    "recipient_account_id": "HQBBBBBB",
                    "amount": 50,
                    "password": self.PASSWORD,
                    "request_id": "transfer-http-0001",
                    "note": "HTTP",
                }).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                transferred = json.loads(response.read())
            self.assertEqual(transferred["points"], 199950)
            history = urllib.request.Request(
                base + "/api/auth/points/transfers?limit=20&offset=0",
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(history, timeout=3) as response:
                listed = json.loads(response.read())
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["items"][0]["amount"], 50)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_password_recheck_is_rate_limited(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        token = self.auth.issue_token("sender")

        def request(password, request_id):
            return urllib.request.urlopen(urllib.request.Request(
                base + "/api/auth/points/transfer",
                data=json.dumps({
                    "recipient_account_id": "HQBBBBBB", "amount": 1,
                    "password": password, "request_id": request_id,
                }).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            ), timeout=3)

        try:
            for index in range(self.auth.LOGIN_FAIL_MAX):
                with self.assertRaises(urllib.error.HTTPError) as wrong:
                    request("wrong-password", "transfer-rate-wrong-%02d" % index)
                self.assertEqual(wrong.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as limited:
                request(self.PASSWORD, "transfer-rate-correct")
            self.assertEqual(limited.exception.code, 429)
            self.assertEqual(self.auth.get_points_row("sender")["points"], 200000)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
