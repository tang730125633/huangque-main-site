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


class AuthCanvasCollabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("owner", "secret123", 20, "member")
        self.auth.create_user("editor", "secret123", 20, "member")
        self.auth.create_user("viewer", "secret123", 20, "member")
        self.auth.create_user("stranger", "secret123", 20, "member")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.client = self._login_client("owner")

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
        req = urllib.request.Request(
            self.base + "/api/auth/login",
            data=json.dumps({"username": username, "password": "secret123"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with client.open(req, timeout=3) as response:
            json.loads(response.read())
        return client

    def _account_id(self, username):
        c = sqlite3.connect(self.auth.DB)
        try:
            return c.execute("SELECT account_id FROM users WHERE username=?", (username,)).fetchone()[0]
        finally:
            c.close()

    def _make_friends(self, left, right):
        c = sqlite3.connect(self.auth.DB)
        now = 1760000000
        try:
            c.execute(
                "INSERT OR IGNORE INTO friendships(username, friend_username, created_at) VALUES(?,?,?)",
                (left, right, now),
            )
            c.execute(
                "INSERT OR IGNORE INTO friendships(username, friend_username, created_at) VALUES(?,?,?)",
                (right, left, now),
            )
            c.commit()
        finally:
            c.close()

    def _create_board(self):
        return self._post(
            "/api/auth/canvas/boards",
            {"name": "Launch Flow", "data": {"nodes": [{"id": "n1"}], "edges": []}},
        )["board"]

    def test_owner_creates_lists_and_reads_canvas_board(self):
        board = self._create_board()
        self.assertEqual(board["name"], "Launch Flow")
        self.assertEqual(board["role"], "owner")
        self.assertEqual(board["version"], 1)
        self.assertEqual(board["data"]["nodes"][0]["id"], "n1")

        listed = self._get("/api/auth/canvas/boards")["boards"]
        self.assertEqual([item["id"] for item in listed], [board["id"]])

        read = self._get("/api/auth/canvas/boards/%s" % board["id"])
        self.assertEqual(read["board"]["data"]["nodes"][0]["id"], "n1")

    def test_non_member_cannot_read_canvas_board(self):
        board = self._create_board()
        stranger_client = self._login_client("stranger")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/auth/canvas/boards/%s" % board["id"], stranger_client)

        self.assertEqual(ctx.exception.code, 404)

    def test_owner_invites_friend_and_editor_saves_with_version_guard(self):
        board = self._create_board()
        self._make_friends("owner", "editor")

        with self.assertRaises(urllib.error.HTTPError) as stranger_ctx:
            self._post(
                "/api/auth/canvas/boards/%s/members" % board["id"],
                {"account_id": self._account_id("stranger"), "role": "editor"},
            )
        self.assertEqual(stranger_ctx.exception.code, 403)

        added = self._post(
            "/api/auth/canvas/boards/%s/members" % board["id"],
            {"account_id": self._account_id("editor"), "role": "editor"},
        )
        self.assertTrue(added["ok"])
        self.assertEqual(added["members"][0]["username"], "editor")

        editor_client = self._login_client("editor")
        read = self._get("/api/auth/canvas/boards/%s" % board["id"], editor_client)
        self.assertEqual(read["board"]["role"], "editor")

        saved = self._post(
            "/api/auth/canvas/boards/%s/save" % board["id"],
            {"version": 1, "data": {"nodes": [{"id": "n2"}], "edges": []}},
            editor_client,
        )
        self.assertEqual(saved["board"]["version"], 2)
        self.assertEqual(saved["board"]["data"]["nodes"][0]["id"], "n2")

        with self.assertRaises(urllib.error.HTTPError) as stale_ctx:
            self._post(
                "/api/auth/canvas/boards/%s/save" % board["id"],
                {"version": 1, "data": {"nodes": []}},
                editor_client,
            )
        self.assertEqual(stale_ctx.exception.code, 409)

    def test_viewer_member_can_read_but_cannot_save(self):
        board = self._create_board()
        self._make_friends("owner", "viewer")
        self._post(
            "/api/auth/canvas/boards/%s/members" % board["id"],
            {"account_id": self._account_id("viewer"), "role": "viewer"},
        )

        viewer_client = self._login_client("viewer")
        read = self._get("/api/auth/canvas/boards/%s" % board["id"], viewer_client)
        self.assertEqual(read["board"]["role"], "viewer")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/auth/canvas/boards/%s/save" % board["id"],
                {"version": 1, "data": {"nodes": [{"id": "blocked"}]}},
                viewer_client,
            )

        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
