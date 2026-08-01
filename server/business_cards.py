"""个人名片和邀请关系树；只使用调用方传入的 SQLite 连接。"""
import base64
import hashlib
import hmac
import io
import json
import secrets
import time

try:
    from .content_domains import miniprogram_security
except ImportError:
    try:
        from content_domains import miniprogram_security
    except ImportError:
        miniprogram_security = None


TEXT_FIELDS = ("headline", "company", "bio", "phone", "email", "address")
JSON_FIELDS = ("tags", "works", "links")
MEDIA_FIELDS = ("avatar", "wechat_qr")
SENSITIVE_FIELDS = ("phone", "email", "address", "wechat_qr")
MAX_JSON_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 16_000_000


class CardError(Exception):
    def __init__(self, code, detail="请求无效", status=400):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def attribution_token(code, public_id, owner_user_id, secret, now=None):
    if not secret:
        return ""
    now = int(now or time.time())
    payload = {"code": str(code), "card_public_id": str(public_id), "owner_user_id": int(owner_user_id),
               "validated_at": now, "exp": now + 7 * 24 * 3600}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return _b64(raw) + "." + _b64(hmac.new(secret.encode(), raw, hashlib.sha256).digest())


def verify_attribution(token, secret, now=None):
    if not secret or not isinstance(token, str) or len(token) > 1024 or "." not in token:
        raise CardError("invalid_invite_attribution", "邀请归因已失效", 409)
    try:
        raw64, sig64 = token.split(".", 1)
        raw = base64.urlsafe_b64decode(raw64 + "=" * (-len(raw64) % 4))
        sig = base64.urlsafe_b64decode(sig64 + "=" * (-len(sig64) % 4))
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        payload = json.loads(raw)
    except Exception as exc:
        raise CardError("invalid_invite_attribution", "邀请归因已失效", 409) from exc
    if not hmac.compare_digest(sig, expected) or int(payload.get("exp") or 0) <= int(now or time.time()):
        raise CardError("invalid_invite_attribution", "邀请归因已失效", 409)
    if not all(payload.get(key) for key in ("code", "card_public_id", "owner_user_id")):
        raise CardError("invalid_invite_attribution", "邀请归因已失效", 409)
    return payload


def owner(conn, public_id):
    row = conn.execute("SELECT user_id FROM business_cards WHERE public_id=?", (str(public_id),)).fetchone()
    return int(row["user_id"]) if row else 0


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS business_cards(
        user_id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
        headline TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]', works_json TEXT NOT NULL DEFAULT '[]', links_json TEXT NOT NULL DEFAULT '[]',
        avatar_key TEXT NOT NULL DEFAULT '', wechat_qr_key TEXT NOT NULL DEFAULT '',
        phone_public INTEGER NOT NULL DEFAULT 0, email_public INTEGER NOT NULL DEFAULT 0,
        address_public INTEGER NOT NULL DEFAULT 0, wechat_qr_public INTEGER NOT NULL DEFAULT 0,
        discoverable_in_network INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'draft', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        published_at INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_public ON business_cards(public_id,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_network ON business_cards(user_id,status,discoverable_in_network)")


def _public_id(conn):
    for _ in range(64):
        value = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        if not conn.execute("SELECT 1 FROM business_cards WHERE public_id=?", (value,)).fetchone():
            return value
    raise RuntimeError("business card id exhausted")


def create_draft(conn, user_id, payload=None, now=None):
    now = int(now or time.time())
    row = conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone()
    if row:
        return dict(row)
    conn.execute("INSERT INTO business_cards(user_id,public_id,created_at,updated_at) VALUES(?,?,?,?)",
                 (int(user_id), _public_id(conn), now, now))
    if payload:
        update(conn, user_id, payload, now)
    return dict(conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone())


def _json(value, field):
    if value is None:
        return None
    if not isinstance(value, (list, dict)):
        raise CardError("invalid_" + field)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode()) > MAX_JSON_BYTES:
        raise CardError("card_too_large")
    return raw


def _text(value, field):
    if not isinstance(value, str):
        raise CardError("invalid_" + field)
    value = value.strip()
    if len(value) > (1000 if field == "bio" else 160):
        raise CardError("card_too_large")
    return value


def update(conn, user_id, payload, now=None):
    if not isinstance(payload, dict):
        raise CardError("invalid_card")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_JSON_BYTES:
        raise CardError("card_too_large")
    row = conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone()
    if not row:
        row = create_draft(conn, user_id, now=now)
    allowed = set(TEXT_FIELDS + JSON_FIELDS + MEDIA_FIELDS + SENSITIVE_FIELDS + ("discoverable_in_network",))
    values = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key in TEXT_FIELDS:
            values[key] = _text(value, key)
        elif key in JSON_FIELDS:
            values[key + "_json"] = _json(value, key)
        elif key in MEDIA_FIELDS:
            if not isinstance(value, str) or len(value) > 512:
                raise CardError("invalid_" + key)
            values[key + "_key"] = value.strip()
        else:
            values[key] = 1 if value is True else 0
    if values:
        values["updated_at"] = int(now or time.time())
        fields = ",".join(key + "=?" for key in values)
        conn.execute("UPDATE business_cards SET " + fields + " WHERE user_id=?", (*values.values(), int(user_id)))
    return dict(conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone())


def _decode(row, owner=False):
    result = {
        "public_id": row["public_id"], "name": row["display_name"] or "黄雀用户",
        "headline": row["headline"], "company": row["company"], "bio": row["bio"],
        "tags": json.loads(row["tags_json"] or "[]"), "works": json.loads(row["works_json"] or "[]"),
        "links": json.loads(row["links_json"] or "[]"), "avatar": row["avatar_key"] or "",
    }
    if owner:
        result.update({field: row[field] for field in TEXT_FIELDS[3:]})
        result.update({field: row[field + "_key"] or "" for field in ("avatar", "wechat_qr")})
        result.update({field + "_public": bool(row[field + "_public"]) for field in SENSITIVE_FIELDS})
        result.update({"discoverable_in_network": bool(row["discoverable_in_network"]), "status": row["status"]})
    else:
        for field in TEXT_FIELDS[3:]:
            if row[field + "_public"]:
                result[field] = row[field]
        if row["wechat_qr_public"]:
            result["wechat_qr"] = row["wechat_qr_key"] or ""
    return result


def mine(conn, user_id):
    row = conn.execute("SELECT c.*,u.display_name FROM business_cards c JOIN users u ON u.id=c.user_id WHERE c.user_id=?", (int(user_id),)).fetchone()
    return _decode(row, True) if row else None


def publish(conn, user_id, status, now=None):
    row = create_draft(conn, user_id, now=now)
    if status not in ("published", "unpublished"):
        raise CardError("invalid_status")
    now = int(now or time.time())
    conn.execute("UPDATE business_cards SET status=?,published_at=?,updated_at=? WHERE user_id=?",
                 (status, now if status == "published" else None, now, int(user_id)))
    return mine(conn, user_id)


def public(conn, public_id):
    row = conn.execute("""SELECT c.*,u.display_name,u.account_status FROM business_cards c
                          JOIN users u ON u.id=c.user_id WHERE c.public_id=?""", (str(public_id),)).fetchone()
    if not row or row["status"] != "published" or row["account_status"] != "active":
        raise CardError("not_found", "not found", 404)
    return _decode(row)


def media_key(conn, public_id, field, owner_id=None):
    if field not in MEDIA_FIELDS:
        raise CardError("not_found", "not found", 404)
    row = conn.execute("""SELECT c.*,u.account_status FROM business_cards c JOIN users u ON u.id=c.user_id
                          WHERE c.public_id=?""", (str(public_id),)).fetchone()
    if not row or (owner_id is None and (row["status"] != "published" or row["account_status"] != "active")):
        raise CardError("not_found", "not found", 404)
    if owner_id is not None and int(owner_id) == int(row["user_id"]):
        return row[field + "_key"] or ""
    if not row[field + "_public"] and field == "wechat_qr":
        raise CardError("not_found", "not found", 404)
    return row[field + "_key"] or ""


def public_network_person(conn, user_id):
    row = conn.execute("""SELECT u.display_name,c.public_id,c.headline,c.company,c.avatar_key,c.status,c.discoverable_in_network
                          FROM users u LEFT JOIN business_cards c ON c.user_id=u.id WHERE u.id=?""", (int(user_id),)).fetchone()
    if row and row["status"] == "published" and row["discoverable_in_network"]:
        return {"public_id": row["public_id"], "name": row["display_name"] or "黄雀用户", "avatar": row["avatar_key"] or "", "headline": row["headline"] or "", "company": row["company"] or ""}
    return {"public_id": "", "name": "匿名用户", "avatar": "", "headline": "", "company": ""}


def ancestors(conn, user_id, limit=100):
    items, seen, current = [], {int(user_id)}, int(user_id)
    for _ in range(min(100, int(limit))):
        row = conn.execute("SELECT inviter_user_id FROM user_invites WHERE invitee_user_id=? AND status='bound' AND risk_status='normal'", (current,)).fetchone()
        if not row or int(row["inviter_user_id"]) in seen:
            break
        current = int(row["inviter_user_id"]); seen.add(current)
        items.append(public_network_person(conn, current))
    return items


def ancestor_ids(conn, user_id, limit=100):
    seen, current = {int(user_id)}, int(user_id)
    result = []
    for _ in range(min(100, int(limit))):
        row = conn.execute("SELECT inviter_user_id FROM user_invites WHERE invitee_user_id=? AND status='bound' AND risk_status='normal'", (current,)).fetchone()
        if not row or int(row["inviter_user_id"]) in seen:
            return result
        current = int(row["inviter_user_id"]); seen.add(current); result.append(current)
    return result


def children(conn, user_id, level=1, cursor=0, limit=20):
    level, cursor, limit = int(level or 1), int(cursor or 0), max(1, min(100, int(limit or 20)))
    if level < 1 or level > 100:
        raise CardError("invalid_level")
    current, seen = [int(user_id)], {int(user_id)}
    for _ in range(level):
        if not current:
            break
        placeholders = ",".join("?" for _ in current)
        rows = conn.execute("SELECT id,invitee_user_id FROM user_invites WHERE inviter_user_id IN (" + placeholders + ") AND status='bound' AND risk_status='normal' ORDER BY id", current).fetchall()
        current = [int(row["invitee_user_id"]) for row in rows if int(row["invitee_user_id"]) not in seen]
        seen.update(current)
    # At this point current is exactly the requested depth.  IDs are supplied by the prior level,
    # so keyset pagination is stable without exposing relations outside this subtree.
    pairs = [(row["id"], int(row["invitee_user_id"])) for row in conn.execute(
        "SELECT id,invitee_user_id FROM user_invites WHERE invitee_user_id IN (%s) ORDER BY id" % ",".join("?" for _ in current), current
    ).fetchall()] if current else []
    page = [(rid, uid) for rid, uid in pairs if rid > cursor][:limit]
    return {"level": level, "items": [public_network_person(conn, uid) for _, uid in page],
            "next_cursor": page[-1][0] if len(page) == limit else 0}


def upload_image(payload, field, prefix="cards"):
    if field not in MEDIA_FIELDS or not isinstance(payload, str) or not payload.startswith("data:image/"):
        raise CardError("invalid_image")
    try:
        header, encoded = payload.split(",", 1)
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise CardError("invalid_image") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise CardError("image_too_large")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_PIXELS or image.width < 1 or image.height < 1:
                raise CardError("image_too_large")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            out = io.BytesIO(); image.save(out, "JPEG", quality=90, optimize=True)
            raw = out.getvalue()
    except CardError:
        raise
    except Exception as exc:
        raise CardError("invalid_image") from exc
    # Security rejection/unavailability deliberately bubbles up; a card image must fail closed.
    if miniprogram_security is None:
        raise CardError("media_unavailable", "媒体服务暂不可用", 503)
    miniprogram_security.check_image(raw, field + ".jpg", "image/jpeg")
    try:
        from .content_domains import cos
    except ImportError:
        from content_domains import cos
    if not cos.enabled():
        raise CardError("media_unavailable", "媒体服务暂不可用", 503)
    key = "%s/%s/%s.jpg" % (prefix, field, secrets.token_urlsafe(16))
    cos.put_bytes(raw, key, "image/jpeg", private=True)
    return key
