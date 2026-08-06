"""Authenticated invite-network views with privacy-safe traversal grants."""
import base64
import hashlib
import hmac
import json
import time

try:
    from . import business_cards
    from .invites import MEMBERSHIP_LEVEL_ORDER, MEMBERSHIP_NAMES
except ImportError:
    import business_cards
    from invites import MEMBERSHIP_LEVEL_ORDER, MEMBERSHIP_NAMES


GRANT_TTL_SECONDS = 600


class NetworkError(Exception):
    def __init__(self, code, detail, status=400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def issue_node_grant(viewer_id, target_user_id, secret, now=None):
    if not secret:
        return ""
    now = int(now or time.time())
    payload = {
        "viewer": int(viewer_id),
        "target": int(target_user_id),
        "exp": now + GRANT_TTL_SECONDS,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(str(secret).encode("utf-8"), raw, hashlib.sha256).digest()
    return _b64(raw) + "." + _b64(signature)


def verify_node_grant(token, viewer_id, secret, now=None):
    if not secret or not isinstance(token, str) or len(token) > 1024 or "." not in token:
        return None
    try:
        raw64, signature64 = token.split(".", 1)
        raw = base64.urlsafe_b64decode(raw64 + "=" * (-len(raw64) % 4))
        signature = base64.urlsafe_b64decode(signature64 + "=" * (-len(signature64) % 4))
        expected = hmac.new(str(secret).encode("utf-8"), raw, hashlib.sha256).digest()
        payload = json.loads(raw.decode("utf-8"))
        now = int(now or time.time())
        if not hmac.compare_digest(signature, expected):
            return None
        if int(payload.get("viewer") or 0) != int(viewer_id):
            return None
        if int(payload.get("exp") or 0) <= now:
            return None
        target = int(payload.get("target") or 0)
        return target if target > 0 else None
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def _membership(conn, user_id, now):
    row = conn.execute(
        """SELECT id,username,membership_tier,membership_expires_at,account_status
             FROM users WHERE id=?""",
        (int(user_id),),
    ).fetchone()
    if not row:
        return None, ""
    tier = str(row["membership_tier"] or "")
    active = (
        str(row["account_status"] or "active") == "active"
        and tier in MEMBERSHIP_LEVEL_ORDER
        and tier != ""
        and int(row["membership_expires_at"] or 0) > int(now)
    )
    return row, tier if active else ""


def _reward_display(conn, relation_id, hidden):
    if hidden:
        return 0, "", 0
    claim = conn.execute(
        """SELECT status,reward_points,expires_at FROM invite_reward_claims
            WHERE invite_relation_id=? ORDER BY id DESC LIMIT 1""",
        (int(relation_id),),
    ).fetchone()
    if claim:
        return int(claim["reward_points"] or 0), str(claim["status"] or ""), int(claim["expires_at"] or 0)
    reward = conn.execute(
        """SELECT status,reward_points FROM invite_reward_point_records
            WHERE invite_relation_id=? ORDER BY id DESC LIMIT 1""",
        (int(relation_id),),
    ).fetchone()
    if reward:
        return int(reward["reward_points"] or 0), str(reward["status"] or ""), 0
    return 0, "", 0


def _person(conn, user_id, viewer_id, secret, relation, can_browse, now,
            relation_id=None, hide_rewards=False):
    row, tier = _membership(conn, user_id, now)
    if not row:
        raise NetworkError("not_found", "用户不存在", 404)
    card = conn.execute(
        """SELECT public_id,status,discoverable_in_network
            FROM business_cards WHERE user_id=?""",
        (int(user_id),),
    ).fetchone()
    card_public_id = (
        card["public_id"]
        if card and card["status"] == "published" and card["discoverable_in_network"]
        else ""
    )
    public_card = {}
    if card_public_id:
        try:
            public_card = business_cards.public(conn, card_public_id)
        except business_cards.CardError:
            card_public_id = ""
    points, reward_status, expires_at = (0, "", 0)
    if relation_id is not None:
        points, reward_status, expires_at = _reward_display(conn, relation_id, hide_rewards)
    return {
        "username": row["username"],
        "membership_tier": tier,
        "membership_name": MEMBERSHIP_NAMES.get(tier, "非会员"),
        "relation": relation,
        "card_available": bool(card_public_id),
        "card_public_id": card_public_id,
        "name": public_card.get("name", ""),
        "title": public_card.get("title", ""),
        "avatar": public_card.get("avatar", ""),
        "node_grant": issue_node_grant(viewer_id, user_id, secret, now) if can_browse else "",
        "reward_points": int(points),
        "reward_status": reward_status,
        "reward_expires_at": int(expires_at),
    }


def _children(conn, parent_id, viewer_id, secret, can_browse, cursor, limit, now,
              hide_rewards):
    cursor = max(0, int(cursor or 0))
    limit = max(1, min(int(limit or 20), 50))
    rows = conn.execute(
        """SELECT id,invitee_user_id FROM user_invites
            WHERE inviter_user_id=? AND id>? AND status='bound' AND risk_status='normal'
            ORDER BY id LIMIT ?""",
        (int(parent_id), cursor, limit + 1),
    ).fetchall()
    page = rows[:limit]
    items = [
        _person(
            conn, row["invitee_user_id"], viewer_id, secret, "child", can_browse, now,
            relation_id=row["id"], hide_rewards=hide_rewards,
        )
        for row in page
    ]
    return items, int(page[-1]["id"]) if len(rows) > limit and page else 0


def downlines_page(conn, viewer_id, secret, cursor=0, limit=20, now=None):
    now = int(now or time.time())
    viewer, viewer_tier = _membership(conn, viewer_id, now)
    if not viewer or str(viewer["account_status"] or "active") != "active":
        raise NetworkError("account_inactive", "账号状态异常", 403)
    can_browse = bool(viewer_tier)
    hidden = viewer_tier in ("partner", "initiator")
    items, next_cursor = _children(
        conn, viewer_id, viewer_id, secret, can_browse, cursor, limit, now, hidden,
    )
    total = 0 if hidden else int(conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE inviter_user_id=? AND status='recorded'""",
        (int(viewer_id),),
    ).fetchone()[0] or 0)
    parent_relation = conn.execute(
        """SELECT id,inviter_user_id FROM user_invites
            WHERE invitee_user_id=? AND status='bound' AND risk_status='normal'""",
        (int(viewer_id),),
    ).fetchone()
    parent = None
    if parent_relation:
        parent = _person(
            conn, parent_relation["inviter_user_id"], viewer_id, secret, "parent",
            can_browse, now, hide_rewards=True,
        )
    return {
        "can_browse_network": can_browse,
        "membership_tier": viewer_tier,
        "total_reward_points": total,
        "parent": parent,
        "items": items,
        "next_cursor": next_cursor,
        "server_time": now,
    }


def network_page(conn, viewer_id, grant, secret, cursor=0, limit=20, now=None):
    now = int(now or time.time())
    viewer, viewer_tier = _membership(conn, viewer_id, now)
    if not viewer_tier:
        raise NetworkError("membership_required", "开通会员后可查看其他用户的上下线", 403)
    target_id = verify_node_grant(grant, viewer_id, secret, now)
    if not target_id:
        raise NetworkError("invalid_network_grant", "查看凭证已失效", 403)
    node = _person(conn, target_id, viewer_id, secret, "self", True, now, hide_rewards=True)
    parent_relation = conn.execute(
        """SELECT inviter_user_id FROM user_invites
            WHERE invitee_user_id=? AND status='bound' AND risk_status='normal'""",
        (target_id,),
    ).fetchone()
    parent = _person(
        conn, parent_relation["inviter_user_id"], viewer_id, secret, "parent", True, now,
        hide_rewards=True,
    ) if parent_relation else None
    items, next_cursor = _children(
        conn, target_id, viewer_id, secret, True, cursor, limit, now, True,
    )
    return {
        "node": node,
        "parent": parent,
        "items": items,
        "next_cursor": next_cursor,
        "server_time": now,
    }
