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

    def _internal_post(self, path, payload, token="test-internal-token"):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-HQ-Internal-Token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.loads(response.read())

    def _get(self, path, client=None):
        with (client or self.client).open(self.base + path, timeout=3) as response:
            return json.loads(response.read())

    def _delete(self, path, client=None):
        req = urllib.request.Request(self.base + path, method="DELETE")
        with (client or self.client).open(req, timeout=3) as response:
            return json.loads(response.read())

    def _ops(self, board_id, op_id, ops, client_id="owner-tab", client=None, base_version=1):
        return self._post(
            "/api/auth/canvas/boards/%s/ops" % board_id,
            {
                "op_id": op_id,
                "client_id": client_id,
                "base_version": base_version,
                "ops": ops,
            },
            client,
        )

    def _invite(self, board, username, role):
        self._make_friends("owner", username)
        self._post(
            "/api/auth/canvas/boards/%s/members" % board["id"],
            {"account_id": self._account_id(username), "role": role},
        )

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
            {"name": "Launch Flow", "data": {"nodes": [{"id": "n1", "type": "text"}], "edges": []}},
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
            {"version": 1, "data": {"nodes": [{"id": "n2", "type": "text"}], "edges": []}},
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
                {"version": 1, "data": {"nodes": [{"id": "blocked", "type": "text"}], "edges": []}},
                viewer_client,
            )

        self.assertEqual(ctx.exception.code, 403)

    def test_internal_canvas_access_reports_only_trusted_membership(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        self._invite(board, "viewer", "viewer")
        self.auth.INTERNAL_TOKEN = "test-internal-token"

        expected = {
            "owner": "owner",
            "editor": "editor",
            "viewer": "viewer",
        }
        for username, role in expected.items():
            with self.subTest(username=username):
                resolved = self._internal_post(
                    "/api/auth/internal/canvas/access",
                    {"username": username, "board_id": board["id"]},
                )
                self.assertEqual(
                    {
                        "board_id": board["id"],
                        "board_owner_username": "owner",
                        "role": role,
                    },
                    resolved,
                )

        with self.assertRaises(urllib.error.HTTPError) as missing:
            self._internal_post(
                "/api/auth/internal/canvas/access",
                {"username": "stranger", "board_id": board["id"]},
            )
        self.assertEqual(404, missing.exception.code)

        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self._internal_post(
                "/api/auth/internal/canvas/access",
                {"username": "owner", "board_id": board["id"]},
                token="wrong-token",
            )
        self.assertEqual(403, forbidden.exception.code)

    def test_ops_merge_fields_and_keep_edge_without_snapshot_id(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")

        owner_batch = self._ops(
            board["id"],
            "owner-create-edge",
            [
                {"type": "node.create", "node": {"id": "n2", "type": "text", "title": "Second", "x": 2}},
                {
                    "type": "edge.create",
                    "edge": {
                        "from": {"node": "n1", "port": "out"},
                        "to": {"node": "n2", "port": "in"},
                        "label": "draft",
                    },
                },
            ],
        )
        self.assertEqual(owner_batch["version"], 2)
        self.assertEqual(owner_batch["batch"]["version"], 2)
        self.assertEqual(owner_batch["batch"]["op_id"], "owner-create-edge")
        self.assertEqual(owner_batch["batch"]["client_id"], "owner-tab")
        self.assertEqual(owner_batch["batch"]["username"], "owner")

        editor_batch = self._ops(
            board["id"],
            "editor-patch-node",
            [{"type": "node.patch", "id": "n2", "fields": {"title": "Edited"}}],
            client_id="editor-tab",
            client=editor_client,
            base_version=2,
        )
        self.assertEqual(editor_batch["version"], 3)

        edge_key = "n1:out->n2:in"
        patched = self._ops(
            board["id"],
            "owner-patch-edge",
            [
                {"type": "node.patch", "id": "n2", "fields": {"x": 99}},
                {"type": "edge.patch", "id": edge_key, "fields": {"label": "approved"}},
                {"type": "board.rename", "name": "Renamed Flow"},
            ],
            base_version=3,
        )
        self.assertEqual(patched["version"], 4)

        current = self._get("/api/auth/canvas/boards/%s" % board["id"])["board"]
        node = next(item for item in current["data"]["nodes"] if item["id"] == "n2")
        self.assertEqual(node["title"], "Edited")
        self.assertEqual(node["x"], 99)
        self.assertEqual(current["name"], "Renamed Flow")
        self.assertNotIn("id", current["data"]["edges"][0])
        self.assertEqual(current["data"]["edges"][0]["label"], "approved")

    def test_nested_merge_patch_preserves_siblings_and_null_deletes(self):
        board = self._create_board()
        edge_key = "n1:out->n2:in"
        self._ops(
            board["id"],
            "seed-nested",
            [
                {
                    "type": "node.patch",
                    "id": "n1",
                    "fields": {
                        "params": {"title": "Draft", "text": "Prompt"},
                        "outputs": {"image": "old.png", "video": "old.mp4"},
                    },
                },
                {"type": "node.create", "node": {"id": "n2", "type": "text"}},
                {
                    "type": "edge.create",
                    "edge": {
                        "from": {"node": "n1", "port": "out"},
                        "to": {"node": "n2", "port": "in"},
                        "style": {"color": "red", "width": 1},
                    },
                },
            ],
        )
        self._ops(
            board["id"],
            "patch-title-color",
            [
                {
                    "type": "node.patch",
                    "id": "n1",
                    "fields": {"params": {"title": "Final"}, "outputs": {"image": "new.png"}},
                },
                {"type": "edge.patch", "id": edge_key, "fields": {"style": {"color": "blue"}}},
            ],
            base_version=2,
        )
        self._ops(
            board["id"],
            "patch-text-width",
            [
                {
                    "type": "node.patch",
                    "id": "n1",
                    "fields": {"params": {"text": "Revised"}, "outputs": {"video": "new.mp4"}},
                },
                {"type": "edge.patch", "id": edge_key, "fields": {"style": {"width": 2}}},
            ],
            base_version=3,
        )
        self._ops(
            board["id"],
            "delete-output-field",
            [{"type": "node.patch", "id": "n1", "fields": {"outputs": {"image": None}}}],
            base_version=4,
        )

        current = self._get("/api/auth/canvas/boards/%s" % board["id"])["board"]["data"]
        node = next(item for item in current["nodes"] if item["id"] == "n1")
        self.assertEqual(node["params"], {"title": "Final", "text": "Revised"})
        self.assertEqual(node["outputs"], {"video": "new.mp4"})
        self.assertEqual(current["edges"][0]["style"], {"color": "blue", "width": 2})

    def test_ops_are_idempotent_and_sync_ordered_batches(self):
        board = self._create_board()
        first = self._ops(
            board["id"],
            "same-batch",
            [{"type": "node.patch", "id": "n1", "fields": {"title": "once"}}],
        )
        duplicate = self._ops(
            board["id"],
            "same-batch",
            [{"type": "node.patch", "id": "n1", "fields": {"title": "once"}}],
        )
        with self.assertRaises(urllib.error.HTTPError) as reused:
            self._ops(
                board["id"],
                "same-batch",
                [{"type": "node.patch", "id": "n1", "fields": {"title": "twice"}}],
            )
        self.assertEqual(409, reused.exception.code)
        self.assertEqual("idempotency_conflict", json.loads(reused.exception.read())["code"])
        second = self._ops(
            board["id"],
            "next-batch",
            [{"type": "node.patch", "id": "n1", "fields": {"x": 7}}],
            base_version=2,
        )

        self.assertEqual(duplicate, first)
        self.assertEqual(second["version"], 3)
        synced = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"])
        self.assertFalse(synced["reset"])
        self.assertEqual(synced["version"], 3)
        self.assertEqual(synced["online_count"], 0)
        self.assertEqual([item["version"] for item in synced["batches"]], [2, 3])
        self.assertEqual(synced["batches"][0]["op_id"], "same-batch")
        self.assertEqual(synced["batches"][1]["op_id"], "next-batch")

    def test_ops_return_current_board_for_stale_and_duplicate_batches(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")

        self._ops(
            board["id"],
            "owner-v2",
            [{"type": "node.patch", "id": "n1", "fields": {"owner_title": "owner"}}],
        )
        # 新契约: 过期 base_version → 409 并回当前版本, 不再静默应用
        with self.assertRaises(urllib.error.HTTPError) as stale:
            self._ops(
                board["id"],
                "editor-stale-v1",
                [{"type": "node.patch", "id": "n1", "fields": {"editor_x": 42}}],
                client_id="editor-tab",
                client=editor_client,
                base_version=1,
            )
        self.assertEqual(stale.exception.code, 409)
        stale_body = json.loads(stale.exception.read())
        self.assertEqual(stale_body["version"], 2)

        # 409 后先 sync 再带新版本重试 → 成功
        synced = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"], editor_client)
        self.assertFalse(synced["reset"])
        editor_result = self._ops(
            board["id"],
            "editor-v3",
            [{"type": "node.patch", "id": "n1", "fields": {"editor_x": 42}}],
            client_id="editor-tab",
            client=editor_client,
            base_version=synced["version"],
        )
        self.assertEqual(editor_result["version"], 3)
        self.assertEqual(editor_result["board"]["version"], 3)
        node = editor_result["board"]["data"]["nodes"][0]
        self.assertEqual(node["owner_title"], "owner")
        self.assertEqual(node["editor_x"], 42)

        self._ops(
            board["id"],
            "owner-v4",
            [{"type": "node.create", "node": {"id": "n2", "type": "text"}}],
            base_version=3,
        )
        # 旧 op_id 重放(即使 base_version 过期) → 幂等 200, 版本不再涨
        duplicate = self._ops(
            board["id"],
            "editor-v3",
            [{"type": "node.patch", "id": "n1", "fields": {"editor_x": 42}}],
            client_id="editor-tab",
            client=editor_client,
            base_version=1,
        )
        self.assertEqual(duplicate["version"], 4)
        self.assertEqual(duplicate["batch"]["version"], 3)
        self.assertEqual(duplicate["board"]["version"], 4)
        self.assertEqual([item["id"] for item in duplicate["board"]["data"]["nodes"]], ["n1", "n2"])

    def test_sync_checkpoint_after_save_avoids_reset(self):
        board = self._create_board()
        self._post(
            "/api/auth/canvas/boards/%s/save" % board["id"],
            {"version": 1, "data": {"nodes": [{"id": "saved", "type": "text"}], "edges": []}},
        )

        # 新契约: /save 写入 board.snapshot 检查点批次, sync 走增量不再 reset
        synced = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"])
        self.assertFalse(synced["reset"])
        self.assertEqual(synced["version"], 2)
        self.assertEqual(len(synced["batches"]), 1)
        snapshot = synced["batches"][0]["ops"][0]
        self.assertEqual(snapshot["type"], "board.snapshot")
        self.assertEqual(snapshot["data"]["nodes"][0]["id"], "saved")

    def test_stale_patches_after_delete_rejected_with_conflict(self):
        board = self._create_board()
        self._ops(
            board["id"],
            "create",
            [
                {"type": "node.create", "node": {"id": "n2", "type": "text", "x": 1}},
                {
                    "type": "edge.create",
                    "edge": {
                        "from": {"node": "n1", "port": "out"},
                        "to": {"node": "n2", "port": "in"},
                    },
                },
            ],
        )
        edge_key = "n1:out->n2:in"
        self._ops(
            board["id"],
            "delete",
            [
                {"type": "node.delete", "id": "n2"},
                {"type": "edge.delete", "id": edge_key},
            ],
            base_version=2,
        )
        # 新契约: 基于删除前版本的补丁批次 → 409, 删除结果保持不变
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self._ops(
                board["id"],
                "stale-patches",
                [
                    {"type": "node.patch", "id": "n2", "fields": {"x": 100}},
                    {"type": "edge.patch", "id": edge_key, "fields": {"label": "revived"}},
                ],
                base_version=2,
            )
        self.assertEqual(conflict.exception.code, 409)

        current = self._get("/api/auth/canvas/boards/%s" % board["id"])["board"]
        self.assertEqual(current["version"], 3)
        self.assertEqual([item["id"] for item in current["data"]["nodes"]], ["n1"])
        self.assertEqual(current["data"]["edges"], [])

    def test_sync_resets_when_retention_has_dropped_requested_history(self):
        board = self._create_board()
        # 本用例只验证保留窗口裁剪, 放宽限频常量避免触发限频
        self.auth.CANVAS_OPS_RATE_MAX_PER_WINDOW = 10 ** 9
        for index in range(1001):
            result, err = self.auth.apply_canvas_ops(
                "owner",
                board["id"],
                {
                    "op_id": "retained-%d" % index,
                    "client_id": "retention-tab",
                    "base_version": index + 1,
                    "ops": [{"type": "board.rename", "name": "Board %d" % index}],
                },
            )
            self.assertIsNone(err)
            self.assertEqual(result["version"], index + 2)

        synced = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"])
        self.assertTrue(synced["reset"])
        self.assertEqual(synced["board"]["version"], 1002)
        c = sqlite3.connect(self.auth.DB)
        try:
            retained = c.execute("SELECT COUNT(*) FROM canvas_ops WHERE board_id=?", (board["id"],)).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(retained, 1000)

    def test_ops_and_sync_enforce_canvas_permissions_and_validation(self):
        board = self._create_board()
        viewer_client = self._login_client("viewer")
        stranger_client = self._login_client("stranger")
        self._invite(board, "viewer", "viewer")

        with self.assertRaises(urllib.error.HTTPError) as malformed:
            self._ops(board["id"], "bad", [{"type": "node.patch", "fields": {"x": 1}}])
        self.assertEqual(malformed.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as invalid_type:
            self._ops(board["id"], "bad-type", [{"type": "node.create", "node": {"id": "bad", "type": "script"}}])
        self.assertEqual(invalid_type.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as unsafe_id:
            self._ops(board["id"], "unsafe-id", [{"type": "node.create", "node": {"id": "__proto__", "type": "text"}}])
        self.assertEqual(unsafe_id.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as patched_type:
            self._ops(board["id"], "patch-type", [{"type": "node.patch", "id": "n1", "fields": {"type": "script"}}])
        self.assertEqual(patched_type.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as unsafe_full_save:
            self._post(
                "/api/auth/canvas/boards/%s/save" % board["id"],
                {"version": 1, "data": {"nodes": [{"id": "__proto__", "type": "text"}], "edges": []}},
            )
        self.assertEqual(unsafe_full_save.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as too_many:
            self._ops(board["id"], "too-many", [{"type": "board.rename", "name": "x"}] * 201)
        self.assertEqual(too_many.exception.code, 413)

        with self.assertRaises(urllib.error.HTTPError) as viewer_ops:
            self._ops(
                board["id"],
                "viewer-op",
                [{"type": "node.patch", "id": "n1", "fields": {"x": 1}}],
                client=viewer_client,
            )
        self.assertEqual(viewer_ops.exception.code, 403)

        self.assertFalse(self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"], viewer_client)["reset"])
        with self.assertRaises(urllib.error.HTTPError) as stranger_sync:
            self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"], stranger_client)
        self.assertEqual(stranger_sync.exception.code, 404)

    def test_sync_caps_history_and_reports_current_role(self):
        board = self._create_board()
        old_limit = self.auth.CANVAS_SYNC_MAX_BATCHES
        old_byte_limit = self.auth.CANVAS_SYNC_MAX_OPS_BYTES
        self.auth.CANVAS_SYNC_MAX_BATCHES = 2
        try:
            for index in range(3):
                self._ops(
                    board["id"],
                    "cap-%d" % index,
                    [{"type": "board.rename", "name": "Cap %d" % index}],
                    base_version=index + 1,
                )
            synced = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"])
            self.assertTrue(synced["reset"])
            self.assertEqual(synced["batches"], [])
            self.assertEqual(synced["role"], "owner")
            self.assertEqual(synced["board"]["version"], 4)

            self.auth.CANVAS_SYNC_MAX_BATCHES = 100
            self.auth.CANVAS_SYNC_MAX_OPS_BYTES = 8
            byte_capped = self._get("/api/auth/canvas/boards/%s/sync?since=3" % board["id"])
            self.assertTrue(byte_capped["reset"])
            self.assertEqual(byte_capped["batches"], [])
        finally:
            self.auth.CANVAS_SYNC_MAX_BATCHES = old_limit
            self.auth.CANVAS_SYNC_MAX_OPS_BYTES = old_byte_limit

    def test_sync_reports_member_role_downgrade_without_board_version_change(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")
        before = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"], editor_client)
        self.assertEqual(before["role"], "editor")
        self._post(
            "/api/auth/canvas/boards/%s/members" % board["id"],
            {"account_id": self._account_id("editor"), "role": "viewer"},
        )
        after = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"], editor_client)
        self.assertEqual(after["role"], "viewer")
        self.assertEqual(after["version"], 1)

    def test_ops_reject_oversized_serialized_log_without_committing(self):
        board = self._create_board()
        payload = {
            "op_id": "oversized-log",
            "client_id": "owner-tab",
            "base_version": 1,
            "ops": [
                {"type": "node.patch", "id": "n1", "fields": {"params": {"text": "x" * (1024 * 1024)}}}
            ],
        }

        with self.assertRaises(urllib.error.HTTPError) as oversized:
            self._post("/api/auth/canvas/boards/%s/ops" % board["id"], payload)
        self.assertEqual(oversized.exception.code, 413)

        current = self._get("/api/auth/canvas/boards/%s" % board["id"])["board"]
        self.assertEqual(current["version"], 1)
        self.assertNotIn("params", current["data"]["nodes"][0])

    def test_ops_reject_content_length_before_reading_body(self):
        class UnreadableBody:
            def read(self, _size=-1):
                raise AssertionError("oversized /ops body must not be read")

        sent = []
        handler = object.__new__(self.auth.H)
        handler.path = "/api/auth/canvas/boards/board-id/ops"
        handler.headers = {"Content-Length": str(self.auth.CANVAS_OPS_MAX_BYTES + 1)}
        handler.rfile = UnreadableBody()
        handler._user = lambda: {"username": "owner"}
        handler._send = lambda code, payload: sent.append((code, payload))

        self.auth.H.do_POST(handler)

        self.assertEqual(sent[0][0], 413)

    def test_presence_rejects_content_length_before_reading_body(self):
        class UnreadableBody:
            def read(self, _size=-1):
                raise AssertionError("oversized /presence body must not be read")

        sent = []
        handler = object.__new__(self.auth.H)
        handler.path = "/api/auth/canvas/boards/board-id/presence"
        handler.headers = {"Content-Length": str(self.auth.CANVAS_PRESENCE_MAX_BYTES + 1)}
        handler.rfile = UnreadableBody()
        handler._user = lambda: {"username": "owner"}
        handler._send = lambda code, payload: sent.append((code, payload))

        self.auth.H.do_POST(handler)

        self.assertEqual(sent[0][0], 413)

    def test_presence_counts_recent_editors_and_expires_old_heartbeats(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")

        owner_presence = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "owner-tab"},
        )
        self.assertEqual(owner_presence["online_count"], 1)
        editor_presence = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "editor-tab"},
            editor_client,
        )
        self.assertEqual(editor_presence["online_count"], 2)

        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "UPDATE canvas_presence SET last_seen=? WHERE board_id=? AND client_id=?",
                (int(self.auth.time.time()) - 31, board["id"], "owner-tab"),
            )
            c.commit()
        finally:
            c.close()
        refreshed = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "editor-tab"},
            editor_client,
        )
        self.assertEqual(refreshed["online_count"], 1)

    def test_ops_rate_limited_after_window_exceeded(self):
        board = self._create_board()
        for index in range(self.auth.CANVAS_OPS_RATE_MAX_PER_WINDOW):
            self._ops(
                board["id"],
                "rate-%d" % index,
                [{"type": "board.rename", "name": "Rate %d" % index}],
                base_version=index + 1,
            )
        with self.assertRaises(urllib.error.HTTPError) as limited:
            self._ops(
                board["id"],
                "rate-overflow",
                [{"type": "board.rename", "name": "Overflow"}],
                base_version=self.auth.CANVAS_OPS_RATE_MAX_PER_WINDOW + 1,
            )
        self.assertEqual(limited.exception.code, 429)
        body = json.loads(limited.exception.read())
        self.assertIn("retry_after", body)

    def test_presence_dedupes_rapid_heartbeats(self):
        board = self._create_board()
        first = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "owner-tab"},
        )
        self.assertEqual(first["online_count"], 1)
        self.assertNotIn("deduped", first)
        second = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "owner-tab"},
        )
        self.assertTrue(second["deduped"])
        self.assertEqual(second["online_count"], 1)

    def test_member_limit_blocks_new_invites_but_allows_role_change(self):
        board = self._create_board()
        self.auth.CANVAS_MAX_MEMBERS_PER_BOARD = 1
        self._invite(board, "editor", "editor")
        self._make_friends("owner", "viewer")
        with self.assertRaises(urllib.error.HTTPError) as limited:
            self._post(
                "/api/auth/canvas/boards/%s/members" % board["id"],
                {"account_id": self._account_id("viewer"), "role": "viewer"},
            )
        self.assertEqual(limited.exception.code, 429)
        changed = self._post(
            "/api/auth/canvas/boards/%s/members" % board["id"],
            {"account_id": self._account_id("editor"), "role": "viewer"},
        )
        self.assertTrue(changed["ok"])
        self.assertEqual(changed["members"][0]["role"], "viewer")

    def test_sync_long_poll_waits_for_changes_and_validates_wait(self):
        board = self._create_board()
        started = self.auth.time.time()
        waited = self._get("/api/auth/canvas/boards/%s/sync?since=1&wait=1" % board["id"])
        self.assertFalse(waited["reset"])
        self.assertEqual(waited["batches"], [])
        self.assertGreaterEqual(self.auth.time.time() - started, 1)

        outcome = {}
        def long_poll():
            outcome.update(self._get("/api/auth/canvas/boards/%s/sync?since=1&wait=5" % board["id"]))
        poller = threading.Thread(target=long_poll)
        poller.start()
        self.auth.time.sleep(0.3)
        self._ops(
            board["id"],
            "wake-poller",
            [{"type": "board.rename", "name": "Wake"}],
            base_version=1,
        )
        poller.join(timeout=6)
        self.assertFalse(outcome["reset"])
        self.assertEqual(len(outcome["batches"]), 1)
        self.assertEqual(outcome["batches"][0]["op_id"], "wake-poller")

        with self.assertRaises(urllib.error.HTTPError) as bad_wait:
            self._get("/api/auth/canvas/boards/%s/sync?since=1&wait=abc" % board["id"])
        self.assertEqual(bad_wait.exception.code, 400)

    def test_sync_wait_degrades_when_user_slots_full(self):
        board = self._create_board()
        self.auth.CANVAS_SYNC_WAIT_MAX_PER_USER = 0
        started = self.auth.time.time()
        degraded = self._get("/api/auth/canvas/boards/%s/sync?since=1&wait=5" % board["id"])
        self.assertLess(self.auth.time.time() - started, 5)
        self.assertTrue(degraded["wait_degraded"])
        self.assertFalse(degraded["reset"])
        self.assertEqual(degraded["batches"], [])

    def test_presence_cross_user_client_id_is_not_deduped(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")
        first = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "shared-tab"},
        )
        self.assertNotIn("deduped", first)
        # 另一成员在 3 秒内复用同 client_id: 必须写库而非误判为重复心跳
        second = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "shared-tab"},
            editor_client,
        )
        self.assertNotIn("deduped", second)
        c = sqlite3.connect(self.auth.DB)
        try:
            row = c.execute(
                "SELECT username FROM canvas_presence WHERE board_id=? AND client_id=?",
                (board["id"], "shared-tab"),
            ).fetchone()
        finally:
            c.close()
        self.assertEqual(row[0], "editor")
        self.assertEqual(second["online_count"], 1)

    def test_member_can_leave_board_but_owner_cannot(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")

        left = self._post("/api/auth/canvas/boards/%s/leave" % board["id"], {}, editor_client)
        self.assertTrue(left["ok"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/auth/canvas/boards/%s" % board["id"], editor_client)
        self.assertEqual(ctx.exception.code, 404)
        read = self._get("/api/auth/canvas/boards/%s" % board["id"])
        self.assertEqual(read["board"]["members"], [])

        with self.assertRaises(urllib.error.HTTPError) as owner_ctx:
            self._post("/api/auth/canvas/boards/%s/leave" % board["id"], {})
        self.assertEqual(owner_ctx.exception.code, 400)

    def test_presence_reports_online_users(self):
        board = self._create_board()
        self._invite(board, "editor", "editor")
        editor_client = self._login_client("editor")
        self._post("/api/auth/canvas/boards/%s/presence" % board["id"], {"client_id": "owner-tab"})
        resp = self._post(
            "/api/auth/canvas/boards/%s/presence" % board["id"],
            {"client_id": "editor-tab"},
            editor_client,
        )
        self.assertEqual(resp["online_count"], 2)
        names = [item["username"] for item in resp["online_users"]]
        self.assertEqual(names, ["editor", "owner"])
        synced = self._get("/api/auth/canvas/boards/%s/sync?since=1" % board["id"])
        self.assertEqual([item["username"] for item in synced["online_users"]], ["editor", "owner"])

    def test_boards_list_pagination(self):
        for _ in range(3):
            self._create_board()
        page = self._get("/api/auth/canvas/boards?limit=2")
        self.assertEqual(len(page["boards"]), 2)
        self.assertEqual(page["total"], 3)
        rest = self._get("/api/auth/canvas/boards?limit=2&offset=2")
        self.assertEqual(len(rest["boards"]), 1)
        self.assertEqual(rest["total"], 3)
        everything = self._get("/api/auth/canvas/boards")
        self.assertEqual(len(everything["boards"]), 3)
        self.assertEqual(everything["total"], 3)
        with self.assertRaises(urllib.error.HTTPError) as bad_limit:
            self._get("/api/auth/canvas/boards?limit=abc")
        self.assertEqual(bad_limit.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as bad_offset:
            self._get("/api/auth/canvas/boards?offset=-1")
        self.assertEqual(bad_offset.exception.code, 400)
        # 稳定排序：三块板同一秒创建（updated_at 相同），
        # 靠次键 id DESC 保证逐页拉取不重不漏，且与一次拉全的顺序一致
        paged_ids = []
        for off in range(3):
            one = self._get("/api/auth/canvas/boards?limit=1&offset=%d" % off)
            self.assertEqual(len(one["boards"]), 1)
            self.assertEqual(one["total"], 3)
            paged_ids.extend(b["id"] for b in one["boards"])
        self.assertEqual(len(set(paged_ids)), 3)
        self.assertEqual(paged_ids, [b["id"] for b in everything["boards"]])


if __name__ == "__main__":
    unittest.main()
