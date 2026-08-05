import http.cookiejar
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


class AuthProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("profile_user", "secret123", 19, "member")
        self.auth.create_user("friend_user", "secret123", 7, "member")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self._post("/api/auth/login", {"username": "profile_user", "password": "secret123"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _post(self, path, payload, client=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with (client or self.client).open(req, timeout=3) as response:
            return json.loads(response.read())

    def _get(self, path, client=None):
        with (client or self.client).open(self.base + path, timeout=3) as response:
            return json.loads(response.read())

    def _delete(self, path, client=None):
        req = urllib.request.Request(self.base + path, method="DELETE")
        with (client or self.client).open(req, timeout=3) as response:
            return json.loads(response.read())

    def _login_client(self, username):
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self._post("/api/auth/login", {"username": username, "password": "secret123"}, client)
        return client

    def test_profile_updates_display_name_only(self):
        data = self._post("/api/auth/profile", {
            "display_name": "  新昵称  ", "role": "admin", "points": 999999,
        })
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["name"], "新昵称")
        self.assertEqual(data["user"]["role"], "member")
        self.assertEqual(data["user"]["points"], 19)
        c = sqlite3.connect(self.auth.DB)
        try:
            row = c.execute(
                "SELECT display_name,role,points FROM users WHERE username='profile_user'"
            ).fetchone()
        finally:
            c.close()
        self.assertEqual(row, ("新昵称", "member", 19))

    def test_me_returns_stable_fixed_account_id(self):
        first = self._get("/api/auth/me")["user"]["account_id"]
        second = self._get("/api/auth/me")["user"]["account_id"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), self.auth.ACCOUNT_ID_LENGTH)
        self.assertTrue(first.startswith(self.auth.ACCOUNT_ID_PREFIX))

    def test_me_returns_card_avatar_without_creating_a_card(self):
        self.assertEqual(self._get("/api/auth/me")["user"]["avatar"], "")
        c = self.auth.db()
        try:
            user_id = c.execute("SELECT id FROM users WHERE username='profile_user'").fetchone()[0]
            self.assertIsNone(c.execute("SELECT 1 FROM business_cards WHERE user_id=?", (user_id,)).fetchone())
            self.auth.business_cards.create_draft(c, user_id)
            c.execute("UPDATE business_cards SET avatar_key='cards/avatar.jpg' WHERE user_id=?", (user_id,))
            c.commit()
        finally:
            c.close()
        original = self.auth.business_cards._media_url
        self.auth.business_cards._media_url = lambda key: "/media/" + key
        try:
            self.assertEqual(self._get("/api/auth/me")["user"]["avatar"], "/media/cards/avatar.jpg")
        finally:
            self.auth.business_cards._media_url = original

    def test_profile_cannot_modify_account_id(self):
        before = self._get("/api/auth/me")["user"]["account_id"]
        data = self._post("/api/auth/profile", {
            "display_name": "Visible Name",
            "account_id": "HQAAAAAA",
        })
        self.assertEqual(data["user"]["account_id"], before)
        after = self._get("/api/auth/me")["user"]["account_id"]
        self.assertEqual(after, before)

    def test_init_db_backfills_missing_account_ids(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute("UPDATE users SET account_id=NULL WHERE username='profile_user'")
            c.commit()
        finally:
            c.close()
        self.auth.init_db()
        data = self._get("/api/auth/me")
        self.assertEqual(len(data["user"]["account_id"]), self.auth.ACCOUNT_ID_LENGTH)

    def test_friend_request_requires_acceptance(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            account_id = c.execute(
                "SELECT account_id FROM users WHERE username='friend_user'"
            ).fetchone()[0]
        finally:
            c.close()
        data = self._post("/api/auth/friends/request", {"account_id": account_id.lower()})
        self.assertTrue(data["ok"])
        self.assertEqual(data["requests"]["outgoing"][0]["to_user"]["username"], "friend_user")
        listed = self._get("/api/auth/friends")
        self.assertEqual(listed["friends"], [])

        friend_client = self._login_client("friend_user")
        incoming = self._get("/api/auth/friend-requests", friend_client)
        self.assertEqual(incoming["incoming"][0]["from_user"]["username"], "profile_user")
        request_id = incoming["incoming"][0]["id"]
        accepted = self._post(
            "/api/auth/friend-requests/respond",
            {"request_id": request_id, "action": "accept"},
            friend_client,
        )
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["friends"][0]["username"], "profile_user")

        listed = self._get("/api/auth/friends")
        self.assertEqual(listed["friends"][0]["account_id"], account_id)

    def test_friend_request_rejects_self_duplicate_and_existing_friend(self):
        me = self._get("/api/auth/me")["user"]["account_id"]
        with self.assertRaises(urllib.error.HTTPError) as self_ctx:
            self._post("/api/auth/friends/request", {"account_id": me})
        self.assertEqual(self_ctx.exception.code, 400)
        c = sqlite3.connect(self.auth.DB)
        try:
            account_id = c.execute(
                "SELECT account_id FROM users WHERE username='friend_user'"
            ).fetchone()[0]
        finally:
            c.close()
        self._post("/api/auth/friends/request", {"account_id": account_id})
        with self.assertRaises(urllib.error.HTTPError) as dup_ctx:
            self._post("/api/auth/friends/request", {"account_id": account_id})
        self.assertEqual(dup_ctx.exception.code, 409)

        friend_client = self._login_client("friend_user")
        request_id = self._get("/api/auth/friend-requests", friend_client)["incoming"][0]["id"]
        self._post(
            "/api/auth/friend-requests/respond",
            {"request_id": request_id, "action": "accept"},
            friend_client,
        )
        with self.assertRaises(urllib.error.HTTPError) as existing_ctx:
            self._post("/api/auth/friends/request", {"account_id": account_id})
        self.assertEqual(existing_ctx.exception.code, 409)

    def test_delete_friend_removes_both_sides_and_allows_new_request(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            account_id = c.execute(
                "SELECT account_id FROM users WHERE username='friend_user'"
            ).fetchone()[0]
        finally:
            c.close()

        self._post("/api/auth/friends/request", {"account_id": account_id})
        friend_client = self._login_client("friend_user")
        request_id = self._get("/api/auth/friend-requests", friend_client)["incoming"][0]["id"]
        self._post(
            "/api/auth/friend-requests/respond",
            {"request_id": request_id, "action": "accept"},
            friend_client,
        )

        deleted = self._delete("/api/auth/friends/friend_user")
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["friends"], [])
        self.assertEqual(self._get("/api/auth/friends", friend_client)["friends"], [])
        c = sqlite3.connect(self.auth.DB)
        try:
            archived_statuses = [
                row[0] for row in c.execute(
                    "SELECT status FROM friend_requests WHERE from_username='profile_user' AND to_username='friend_user'"
                ).fetchall()
            ]
        finally:
            c.close()
        self.assertTrue(any(status.startswith("removed:") for status in archived_statuses))

        requested_again = self._post("/api/auth/friends/request", {"account_id": account_id})
        self.assertTrue(requested_again["ok"])
        self.assertEqual(requested_again["requests"]["outgoing"][0]["to_user"]["username"], "friend_user")
        second_request_id = self._get(
            "/api/auth/friend-requests", friend_client
        )["incoming"][0]["id"]
        accepted_again = self._post(
            "/api/auth/friend-requests/respond",
            {"request_id": second_request_id, "action": "accept"},
            friend_client,
        )
        self.assertTrue(accepted_again["ok"])
        self.assertEqual(accepted_again["friends"][0]["username"], "profile_user")
        self.assertEqual(self._get("/api/auth/friends")["friends"][0]["username"], "friend_user")

    def test_delete_friend_rejects_non_friend(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._delete("/api/auth/friends/friend_user")
        self.assertEqual(ctx.exception.code, 404)

    def test_profile_rejects_empty_and_long_names(self):
        for name in ("   ", "a" * 33):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/auth/profile", {"display_name": name})
            self.assertEqual(ctx.exception.code, 400)

    def test_profile_requires_login(self):
        anonymous = urllib.request.build_opener()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/auth/profile", {"display_name": "访客"}, anonymous)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
