"""Digital presenter project domain and centralized HTTP policy."""

import urllib.parse

from . import feature_flags
from .canvas_access import resolve_canvas_access

from .digital_presenter_store import (
    IdempotencyConflict,
    PermissionDenied,
    RevisionConflict,
    create_project,
    delete_project,
    get_project,
    init_db,
    update_project,
)


class UnregisteredWriteRoute(RuntimeError):
    pass


ROUTE_POLICIES = {
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

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PREFIX = "/api/gen/digital-presenter/"


def validate_route_policies():
    allowed = {"read", "write", "paid", "delete"}
    for (method, path), policy in ROUTE_POLICIES.items():
        if method not in {"GET", "POST", "PUT", "DELETE"} or not path.startswith(_PREFIX):
            raise ValueError("invalid digital presenter route registration")
        if policy not in allowed:
            raise ValueError("invalid digital presenter route policy")
        if method == "GET" and policy != "read":
            raise ValueError("GET route must be read-only")
        if method in _MUTATING_METHODS and policy == "read":
            raise ValueError("mutating route cannot be read-only")
    return True


def route_policy(method, path):
    method = str(method or "").upper()
    path = str(path or "").split("?", 1)[0]
    policy = ROUTE_POLICIES.get((method, path))
    if policy:
        return policy
    if path.startswith(_PREFIX) and method in _MUTATING_METHODS:
        raise UnregisteredWriteRoute("digital presenter write route has no policy")
    return None


validate_route_policies()


def _send_error(handler, error):
    if isinstance(error, LookupError):
        handler._send(404, {"detail": str(error)[:220], "code": "not_found"})
    elif isinstance(error, PermissionDenied):
        handler._send(403, {"detail": str(error)[:220], "code": "forbidden"})
    elif isinstance(error, RevisionConflict):
        handler._send(409, {"detail": str(error)[:220], "code": "revision_conflict"})
    elif isinstance(error, IdempotencyConflict):
        handler._send(409, {"detail": str(error)[:220], "code": "idempotency_conflict"})
    else:
        handler._send(400, {"detail": str(error)[:220], "code": "invalid_request"})


def _request_object(handler):
    body = handler._json_body_strict()
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return body


def _single_query(handler, name, detail):
    values = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query).get(name) or []
    if len(values) != 1 or not str(values[0]).strip():
        raise ValueError(detail)
    return str(values[0]).strip()


def _request_access(handler, username):
    board_id = str(handler.headers.get("X-Canvas-Board-Id") or "").strip()
    if not board_id:
        raise ValueError("缺少画布 ID")
    access = resolve_canvas_access(username, board_id)
    if access is None:
        raise LookupError("协作画布不存在")
    return access


def _require_policy_access(access, policy):
    if policy == "read":
        access.require_read()
    elif policy == "write":
        access.require_write()
    elif policy == "paid":
        access.require_charge()
    elif policy == "delete":
        access.require_delete()


def dispatch_http(handler, method, db_factory, verify_token):
    """Handle registered digital-presenter routes; return whether a route matched."""
    path = handler.path.split("?", 1)[0]
    try:
        policy = route_policy(method, path)
    except UnregisteredWriteRoute as error:
        handler._send(500, {"detail": str(error), "code": "route_policy_missing"})
        return True
    if policy is None:
        return False

    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录", "code": "unauthorized"})
        return True
    if user.get("must_change"):
        handler._send(403, {"detail": "请先修改初始密码", "code": "must_change"})
        return True
    if path.endswith("/capability"):
        handler._send(200, {"enabled": bool(feature_flags.is_enabled("digital_presenter"))})
        return True
    if policy in {"write", "paid", "delete"} and not feature_flags.is_enabled(
        "digital_presenter"
    ):
        handler._send(404, {"detail": "功能暂未开放", "code": "feature_disabled"})
        return True

    try:
        access = _request_access(handler, user["username"])
        _require_policy_access(access, policy)
        if method == "POST" and path.endswith("/projects"):
            handler._send(200, create_project(
                db_factory,
                access,
                _request_object(handler),
                handler.headers.get("Idempotency-Key"),
            ))
        elif method == "GET" and path.endswith("/project"):
            project_id = _single_query(handler, "id", "缺少项目 ID")
            handler._send(200, get_project(db_factory, access, project_id))
        elif method == "PUT" and path.endswith("/project"):
            body = _request_object(handler)
            project_id = body.pop("project_id", None)
            revision = body.pop("revision", None)
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValueError("缺少项目 ID")
            handler._send(200, update_project(
                db_factory, access, project_id.strip(), revision, body
            ))
        elif method == "DELETE" and path.endswith("/project"):
            project_id = _single_query(handler, "id", "缺少项目 ID")
            revision_text = _single_query(handler, "revision", "缺少项目版本")
            try:
                revision = int(revision_text)
            except (TypeError, ValueError):
                raise ValueError("项目版本无效")
            handler._send(200, delete_project(db_factory, access, project_id, revision))
        else:
            handler._send(404, {"detail": "功能尚未实现", "code": "not_implemented"})
    except (
        LookupError,
        PermissionDenied,
        RevisionConflict,
        IdempotencyConflict,
        ValueError,
    ) as error:
        _send_error(handler, error)
    return True


def make_handler(base_handler, core_module):
    class DigitalPresenterHandler(base_handler):
        def do_GET(self):
            if dispatch_http(self, "GET", core_module.jdb, core_module.verify):
                return
            return super().do_GET()

        def do_POST(self):
            if dispatch_http(self, "POST", core_module.jdb, core_module.verify):
                return
            return super().do_POST()

        def do_PUT(self):
            if dispatch_http(self, "PUT", core_module.jdb, core_module.verify):
                return
            return super().do_PUT()

        def do_PATCH(self):
            if dispatch_http(self, "PATCH", core_module.jdb, core_module.verify):
                return
            return super().do_PATCH()

        def do_DELETE(self):
            if dispatch_http(self, "DELETE", core_module.jdb, core_module.verify):
                return
            return super().do_DELETE()

    DigitalPresenterHandler.__name__ = "DigitalPresenterHandler"
    return DigitalPresenterHandler


__all__ = [
    "IdempotencyConflict",
    "PermissionDenied",
    "RevisionConflict",
    "init_db",
    "create_project",
    "get_project",
    "update_project",
    "delete_project",
    "ROUTE_POLICIES",
    "UnregisteredWriteRoute",
    "validate_route_policies",
    "route_policy",
    "dispatch_http",
    "make_handler",
]
