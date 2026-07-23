"""Trusted canvas membership resolution for content domains."""

from dataclasses import dataclass
import json
import os
import urllib.request


class PermissionDenied(Exception):
    pass


@dataclass(frozen=True)
class CanvasAccess:
    board_id: str
    actor_username: str
    board_owner_username: str
    role: str

    @property
    def can_read(self):
        return self.role in {"owner", "editor", "viewer"}

    @property
    def can_write(self):
        return self.role in {"owner", "editor"}

    @property
    def can_charge(self):
        return self.role == "owner"

    @property
    def can_delete(self):
        return self.role == "owner"

    def require_read(self):
        if not self.can_read:
            raise PermissionDenied("没有查看权限")

    def require_write(self):
        if not self.can_write:
            raise PermissionDenied("没有编辑权限")

    def require_charge(self):
        if not self.can_charge:
            raise PermissionDenied("只有画布所有者可发起付费操作")

    def require_delete(self):
        if not self.can_delete:
            raise PermissionDenied("只有画布所有者可删除项目")


def resolve_canvas_access(username, board_id, *, auth_base=None, internal_token=None,
                          opener=None, timeout=6):
    """Resolve membership through the auth service; any uncertainty denies access."""
    username = str(username or "").strip()
    board_id = str(board_id or "").strip()
    if internal_token is None:
        internal_token = os.environ.get("HQ_INTERNAL_TOKEN", "")
    internal_token = str(internal_token or "").strip()
    if not username or not board_id or not internal_token:
        return None
    auth_base = str(auth_base or os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")).rstrip("/")
    open_request = opener or urllib.request.urlopen
    request = urllib.request.Request(
        auth_base + "/api/auth/internal/canvas/access",
        data=json.dumps({"username": username, "board_id": board_id}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-HQ-Internal-Token": internal_token,
        },
        method="POST",
    )
    try:
        with open_request(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("board_id") != board_id:
        return None
    owner = payload.get("board_owner_username")
    role = payload.get("role")
    if not isinstance(owner, str) or not owner.strip() or role not in {"owner", "editor", "viewer"}:
        return None
    return CanvasAccess(
        board_id=board_id,
        actor_username=username,
        board_owner_username=owner.strip(),
        role=role,
    )
