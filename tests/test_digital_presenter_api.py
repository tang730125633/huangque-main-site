# -*- coding: utf-8 -*-
import json
import importlib
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from server import content_api  # noqa: E402
from server.content_domains.canvas_access import CanvasAccess  # noqa: E402

core = content_api.core
digital_presenter = content_api.digital_presenter
feature_flags = digital_presenter.feature_flags


class CanvasAccessResolverTests(unittest.TestCase):
    def test_resolver_builds_frozen_server_side_capabilities(self):
        canvas_access = importlib.import_module("content_domains.canvas_access")
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "board_id": "board-a",
                    "board_owner_username": "owner",
                    "role": "editor",
                }).encode("utf-8")

        def open_request(request, timeout):
            calls.append((request, timeout))
            return Response()

        access = canvas_access.resolve_canvas_access(
            "editor",
            "board-a",
            auth_base="http://auth.test",
            internal_token="trusted-token",
            opener=open_request,
        )

        self.assertEqual("editor", access.actor_username)
        self.assertEqual("owner", access.board_owner_username)
        self.assertEqual("editor", access.role)
        self.assertTrue(access.can_read)
        self.assertTrue(access.can_write)
        self.assertFalse(access.can_charge)
        self.assertEqual(
            {"username": "editor", "board_id": "board-a"},
            json.loads(calls[0][0].data),
        )
        self.assertEqual("trusted-token", calls[0][0].get_header("X-hq-internal-token"))
        with self.assertRaises(Exception):
            access.role = "owner"

    def test_resolver_fails_closed_for_missing_token_or_untrusted_response(self):
        canvas_access = importlib.import_module("content_domains.canvas_access")
        self.assertIsNone(canvas_access.resolve_canvas_access("owner", "board-a", internal_token=""))

        class WrongBoardResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "board_id": "board-b",
                    "board_owner_username": "owner",
                    "role": "owner",
                }).encode("utf-8")

        self.assertIsNone(canvas_access.resolve_canvas_access(
            "owner", "board-a", internal_token="trusted-token",
            opener=lambda *_args, **_kwargs: WrongBoardResponse(),
        ))


class DigitalPresenterDefaultGateTests(unittest.TestCase):
    def setUp(self):
        self.original_verify = core.verify
        core.verify = lambda token: (
            {"username": token, "must_change": token == "locked"} if token else None
        )
        self.flag_patch = patch.object(feature_flags, "is_enabled", return_value=False)
        self.flag_patch.start()
        handler = getattr(content_api, "H", core.H)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.flag_patch.stop()
        core.verify = self.original_verify

    def request(self, method, path, body=None, username="owner"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if username:
            headers["Authorization"] = "Bearer " + username
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_capability_is_authenticated_and_disabled_by_default(self):
        status, body = self.request("GET", "/api/gen/digital-presenter/capability")
        self.assertEqual(200, status)
        self.assertEqual({"enabled": False}, body)

        status, body = self.request(
            "GET", "/api/gen/digital-presenter/capability", username=None
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body.get("code"))

    def test_disabled_write_route_is_rejected_by_the_feature_gate(self):
        with patch.object(feature_flags, "is_enabled", return_value=False):
            status, body = self.request(
                "POST",
                "/api/gen/digital-presenter/projects",
                {"board_id": "board-a", "title": "测试项目"},
            )
        self.assertEqual(404, status)
        self.assertEqual("feature_disabled", body.get("code"))


class DigitalPresenterRoutePolicyTests(unittest.TestCase):
    def test_every_documented_route_has_an_explicit_policy(self):
        expected = {
            ("POST", "/api/gen/digital-presenter/projects"): "write",
            ("GET", "/api/gen/digital-presenter/project"): "read",
            ("PUT", "/api/gen/digital-presenter/project"): "write",
            ("DELETE", "/api/gen/digital-presenter/project"): "delete",
            ("GET", "/api/gen/digital-presenter/capability"): "read",
            ("PUT", "/api/gen/digital-presenter/assets/binding"): "write",
            ("DELETE", "/api/gen/digital-presenter/assets/binding"): "write",
            ("POST", "/api/gen/digital-presenter/quote"): "paid",
            ("POST", "/api/gen/digital-presenter/segments/plan"): "paid",
            ("PUT", "/api/gen/digital-presenter/segments"): "write",
            ("POST", "/api/gen/digital-presenter/assets/plan"): "paid",
            ("PUT", "/api/gen/digital-presenter/placements"): "write",
            ("POST", "/api/gen/digital-presenter/confirm-plan"): "write",
            ("POST", "/api/gen/digital-presenter/generate"): "paid",
            ("POST", "/api/gen/digital-presenter/generate/retry"): "paid",
            ("GET", "/api/gen/digital-presenter/jobs"): "read",
            ("PUT", "/api/gen/digital-presenter/timeline"): "write",
            ("POST", "/api/gen/digital-presenter/render"): "paid",
            ("POST", "/api/gen/digital-presenter/render/retry"): "paid",
            ("GET", "/api/gen/digital-presenter/render/status"): "read",
        }
        self.assertEqual(expected, digital_presenter.ROUTE_POLICIES)
        digital_presenter.validate_route_policies()

    def test_mutating_methods_cannot_be_registered_as_read(self):
        routes = (
            ("POST", "/api/gen/digital-presenter/projects"),
            ("PUT", "/api/gen/digital-presenter/project"),
            ("DELETE", "/api/gen/digital-presenter/project"),
        )
        for route in routes:
            with self.subTest(method=route[0]):
                with patch.dict(digital_presenter.ROUTE_POLICIES, {route: "read"}):
                    with self.assertRaises(ValueError):
                        digital_presenter.validate_route_policies()

    def test_unregistered_mutation_route_fails_closed(self):
        with self.assertRaises(digital_presenter.UnregisteredWriteRoute):
            digital_presenter.route_policy(
                "POST", "/api/gen/digital-presenter/future-write"
            )

    def test_policy_capability_matrix_covers_current_and_future_routes(self):
        expected = {
            "owner": {"read", "write", "paid", "delete"},
            "editor": {"read", "write"},
            "viewer": {"read"},
        }
        for role, allowed in expected.items():
            access = CanvasAccess("board-a", role, "owner", role)
            for policy in set(digital_presenter.ROUTE_POLICIES.values()):
                with self.subTest(role=role, policy=policy):
                    if policy in allowed:
                        digital_presenter._require_policy_access(access, policy)
                    else:
                        with self.assertRaises(digital_presenter.PermissionDenied):
                            digital_presenter._require_policy_access(access, policy)


class DigitalPresenterProjectApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_job_db = core.JOB_DB
        self.original_verify = core.verify
        core.JOB_DB = str(pathlib.Path(self.tmp.name) / "content.db")
        core.verify = lambda token: (
            {"username": token, "must_change": token == "locked"} if token else None
        )
        digital_presenter.init_db(core.jdb)
        self.enabled = True
        self.flag_patch = patch.object(
            feature_flags, "is_enabled", side_effect=lambda _feature: self.enabled
        )
        self.flag_patch.start()

        roles = {
            ("owner", "board-a"): ("owner", "owner"),
            ("editor", "board-a"): ("owner", "editor"),
            ("viewer", "board-a"): ("owner", "viewer"),
            ("otherowner", "board-b"): ("otherowner", "owner"),
            ("locked", "board-a"): ("owner", "editor"),
        }

        def resolve(username, board_id, **_kwargs):
            resolved = roles.get((username, board_id))
            if not resolved:
                return None
            owner, role = resolved
            return CanvasAccess(board_id, username, owner, role)

        self.access_patch = patch.object(
            digital_presenter, "resolve_canvas_access", side_effect=resolve
        )
        self.access_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), content_api.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.access_patch.stop()
        self.flag_patch.stop()
        core.JOB_DB = self.original_job_db
        core.verify = self.original_verify
        self.tmp.cleanup()

    def request(
        self,
        method,
        path,
        body=None,
        username="owner",
        board_id="board-a",
        idempotency_key=None,
    ):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if username:
            headers["Authorization"] = "Bearer " + username
        if board_id:
            headers["X-Canvas-Board-Id"] = board_id
        if method == "POST" and path == "/api/gen/digital-presenter/projects":
            if idempotency_key is None:
                idempotency_key = "test-create-" + uuid.uuid4().hex
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
        for attempt in range(2):
            request = urllib.request.Request(
                self.base + path, data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())
            except ConnectionAbortedError:
                if attempt:
                    raise

    def create(self, username="owner", board_id="board-a", **changes):
        payload = {
            "title": "门店资讯",
            "script_text": "今天介绍夏季护理方案。",
            "ratio": "9:16",
            "target_duration": 45,
        }
        payload.update(changes)
        status, project = self.request(
            "POST", "/api/gen/digital-presenter/projects", payload,
            username=username, board_id=board_id,
        )
        self.assertEqual(200, status, project)
        return project

    def test_authentication_password_gate_and_membership_are_required(self):
        status, body = self.request(
            "POST", "/api/gen/digital-presenter/projects", {}, username=None
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body.get("code"))

        status, body = self.request(
            "POST", "/api/gen/digital-presenter/projects", {}, username="locked"
        )
        self.assertEqual(403, status)
        self.assertEqual("must_change", body.get("code"))

        status, body = self.request(
            "POST", "/api/gen/digital-presenter/projects", {}, username="stranger"
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", body.get("code"))

    def test_owner_and_editor_create_and_edit_while_viewer_is_read_only(self):
        project = self.create(username="editor")
        self.assertEqual("owner", project["owner_username"])
        self.assertEqual("editor", project["created_by"])

        status, updated = self.request(
            "PUT", "/api/gen/digital-presenter/project",
            {"project_id": project["id"], "revision": 1, "title": "编辑版"},
            username="editor",
        )
        self.assertEqual(200, status)
        self.assertEqual(2, updated["revision"])

        status, read = self.request(
            "GET", "/api/gen/digital-presenter/project?id=" + project["id"],
            username="viewer",
        )
        self.assertEqual(200, status)
        self.assertEqual("编辑版", read["title"])

        status, body = self.request(
            "PUT", "/api/gen/digital-presenter/project",
            {"project_id": project["id"], "revision": 2, "title": "越权"},
            username="viewer",
        )
        self.assertEqual(403, status)
        self.assertEqual("forbidden", body.get("code"))

    def test_create_replays_after_committed_response_is_lost(self):
        payload = {
            "title": "response loss",
            "script_text": "same logical request",
            "ratio": "9:16",
            "target_duration": 45,
        }
        key = "test-response-loss"

        class Handler:
            path = "/api/gen/digital-presenter/projects"
            headers = {
                "X-Canvas-Board-Id": "board-a",
                "Idempotency-Key": key,
            }

            def __init__(self, drop=False):
                self.drop = drop
                self.sent = None

            def _token(self):
                return "owner"

            def _json_body_strict(self):
                return dict(payload)

            def _send(self, status, body):
                if self.drop:
                    raise ConnectionAbortedError("response lost after commit")
                self.sent = (status, body)

        with self.assertRaises(ConnectionAbortedError):
            digital_presenter.dispatch_http(Handler(drop=True), "POST", core.jdb, core.verify)
        replay = Handler()
        self.assertTrue(
            digital_presenter.dispatch_http(replay, "POST", core.jdb, core.verify)
        )
        self.assertEqual(200, replay.sent[0])
        with closing(sqlite3.connect(core.JOB_DB)) as connection:
            rows = connection.execute(
                "SELECT id FROM digital_presenter_projects"
            ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(rows[0][0], replay.sent[1]["id"])

    def test_create_rejects_idempotency_key_conflict_and_missing_key(self):
        key = "test-http-conflict"
        status, first = self.request(
            "POST",
            "/api/gen/digital-presenter/projects",
            {"title": "first"},
            idempotency_key=key,
        )
        self.assertEqual(200, status)
        status, body = self.request(
            "POST",
            "/api/gen/digital-presenter/projects",
            {"title": "second"},
            idempotency_key=key,
        )
        self.assertEqual(409, status)
        self.assertEqual("idempotency_conflict", body.get("code"))
        status, body = self.request(
            "POST",
            "/api/gen/digital-presenter/projects",
            {"title": "missing"},
            idempotency_key="",
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body.get("code"))
        with closing(sqlite3.connect(core.JOB_DB)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM digital_presenter_projects"
            ).fetchone()[0]
        self.assertEqual(1, count)
        self.assertTrue(first["id"].startswith("dp_"))

    def test_generic_project_create_api_rejects_asset_binding_fields(self):
        restricted = {
            "avatar_asset_id": "avatar-owned",
            "background_asset_id": "background-owned",
            "background_mode": "separate",
        }
        for field, value in restricted.items():
            with self.subTest(operation="create", field=field):
                status, body = self.request(
                    "POST", "/api/gen/digital-presenter/projects", {field: value}
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", body.get("code"))
        with closing(sqlite3.connect(core.JOB_DB)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM digital_presenter_projects"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_generic_project_update_api_rejects_asset_binding_fields(self):
        restricted = {
            "avatar_asset_id": "avatar-owned",
            "background_asset_id": "background-owned",
            "background_mode": "separate",
        }
        project = self.create()
        for field, value in restricted.items():
            with self.subTest(operation="update", field=field):
                status, body = self.request(
                    "PUT", "/api/gen/digital-presenter/project",
                    {"project_id": project["id"], "revision": 1, field: value},
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", body.get("code"))
        status, unchanged = self.request(
            "GET", "/api/gen/digital-presenter/project?id=" + project["id"]
        )
        self.assertEqual(200, status)
        self.assertEqual(1, unchanged["revision"])
        self.assertIsNone(unchanged["avatar_asset_id"])
        self.assertIsNone(unchanged["background_asset_id"])
        self.assertEqual("source", unchanged["background_mode"])

    def test_cross_board_invalid_field_stale_revision_and_delete_matrix(self):
        project = self.create()
        path = "/api/gen/digital-presenter/project?id=" + project["id"]
        status, _body = self.request(
            "GET", path, username="otherowner", board_id="board-b"
        )
        self.assertEqual(404, status)

        status, body = self.request(
            "PUT", "/api/gen/digital-presenter/project",
            {"project_id": project["id"], "revision": 1, "owner_username": "attacker"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body.get("code"))

        status, _updated = self.request(
            "PUT", "/api/gen/digital-presenter/project",
            {"project_id": project["id"], "revision": 1, "title": "新版"},
        )
        self.assertEqual(200, status)
        status, body = self.request(
            "PUT", "/api/gen/digital-presenter/project",
            {"project_id": project["id"], "revision": 1, "title": "旧版"},
        )
        self.assertEqual(409, status)
        self.assertEqual("revision_conflict", body.get("code"))

        status, body = self.request(
            "DELETE", path + "&revision=2", username="editor"
        )
        self.assertEqual(403, status)
        self.assertEqual("forbidden", body.get("code"))
        status, deleted = self.request("DELETE", path + "&revision=2")
        self.assertEqual(200, status)
        self.assertTrue(deleted["deleted"])

    def test_disabled_write_routes_do_not_touch_storage(self):
        with closing(sqlite3.connect(core.JOB_DB)) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM digital_presenter_projects"
            ).fetchone()[0]
        self.enabled = False
        write_routes = [
            (method, path)
            for (method, path), policy in digital_presenter.ROUTE_POLICIES.items()
            if policy != "read"
        ]
        for method, path in write_routes:
            with self.subTest(method=method, path=path):
                status, body = self.request(method, path, {})
                self.assertEqual(404, status)
                self.assertEqual("feature_disabled", body.get("code"))
        with closing(sqlite3.connect(core.JOB_DB)) as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM digital_presenter_projects"
            ).fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
