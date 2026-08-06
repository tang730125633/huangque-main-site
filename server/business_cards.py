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


TEXT_FIELDS = ("name", "headline", "company", "bio", "phone", "email", "address")
JSON_FIELDS = ("tags", "works", "links")
MEDIA_FIELDS = ("avatar", "wechat_qr")
WORK_IMAGE_FIELDS = ("work_image_1", "work_image_2", "work_image_3")
WORK_VIDEO_FIELDS = ("work_video_1", "work_video_2", "work_video_3")
SENSITIVE_FIELDS = ("phone", "email", "address", "wechat_qr")
MAX_JSON_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16_000_000
ANONYMOUS_JOURNEY_RETENTION = 30 * 24 * 3600
CONVERTED_JOURNEY_RETENTION = 365 * 24 * 3600


class CardError(Exception):
    def __init__(self, code, detail="请求无效", status=400):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def attribution_token(code, public_id, owner_user_id, secret, now=None, journey_id=""):
    if not secret:
        return ""
    now = int(now or time.time())
    payload = {"code": str(code), "card_public_id": str(public_id), "owner_user_id": int(owner_user_id),
               "validated_at": now, "exp": now + 7 * 24 * 3600}
    if journey_id:
        payload["journey_id"] = str(journey_id)
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


def public_owner(conn, public_id):
    row = conn.execute(
        """SELECT c.user_id FROM business_cards c JOIN users u ON u.id=c.user_id
             WHERE c.public_id=? AND c.status='published' AND u.account_status='active'""",
        (str(public_id),),
    ).fetchone()
    return int(row["user_id"]) if row else 0


def cleanup_referral_journeys(conn, now=None):
    now = int(now or time.time())
    conn.execute(
        "DELETE FROM card_referral_journeys WHERE registered_user_id IS NULL AND visited_at<?",
        (now - ANONYMOUS_JOURNEY_RETENTION,),
    )
    conn.execute(
        "DELETE FROM card_referral_journeys WHERE registered_user_id IS NOT NULL AND registered_at<?",
        (now - CONVERTED_JOURNEY_RETENTION,),
    )


def _referral_journey(conn, attribution, now=None):
    journey_id = str((attribution or {}).get("journey_id") or "")
    if not journey_id:
        return None
    row = conn.execute(
        "SELECT * FROM card_referral_journeys WHERE journey_id=?", (journey_id,),
    ).fetchone()
    now = int(now or time.time())
    if (
        not row or int(row["expires_at"] or 0) <= now
        or row["card_public_id"] != str(attribution.get("card_public_id") or "")
        or int(row["inviter_user_id"]) != int(attribution.get("owner_user_id") or 0)
    ):
        raise CardError("invalid_invite_attribution", "邀请归因已失效", 409)
    return row


def start_referral_journey(conn, attribution, campaign_id, now=None):
    journey_id = str((attribution or {}).get("journey_id") or "")
    if not journey_id:
        return None
    now = int(now or time.time())
    cleanup_referral_journeys(conn, now)
    conn.execute(
        """INSERT OR IGNORE INTO card_referral_journeys(
               journey_id,campaign_id,card_public_id,inviter_user_id,source,
               visited_at,card_started_at,expires_at
           ) VALUES(?,?,?,?,'miniprogram_card',?,?,?)""",
        (journey_id, int(campaign_id), str(attribution["card_public_id"]),
         int(attribution["owner_user_id"]), min(now, int(attribution["validated_at"])),
         now, int(attribution["exp"])),
    )
    row = _referral_journey(conn, attribution, now)
    conn.execute(
        "UPDATE card_referral_journeys SET card_started_at=COALESCE(card_started_at,?) WHERE journey_id=?",
        (now, row["journey_id"]),
    )
    return dict(conn.execute(
        "SELECT * FROM card_referral_journeys WHERE journey_id=?", (row["journey_id"],),
    ).fetchone())


def convert_referral_journey(conn, attribution, registered_user_id, relation_id, now=None):
    row = _referral_journey(conn, attribution, now)
    if not row:
        return None
    registered_user_id = int(registered_user_id)
    if row["registered_user_id"] is not None and int(row["registered_user_id"]) != registered_user_id:
        raise CardError("invite_journey_used", "本次邀请归因已被使用，请重新打开邀请名片", 409)
    now = int(now or time.time())
    conn.execute(
        """UPDATE card_referral_journeys
              SET registered_user_id=?,invite_relation_id=?,registered_at=COALESCE(registered_at,?)
            WHERE journey_id=?""",
        (registered_user_id, int(relation_id), now, row["journey_id"]),
    )
    return dict(conn.execute(
        "SELECT * FROM card_referral_journeys WHERE journey_id=?", (row["journey_id"],),
    ).fetchone())


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS business_cards(
        user_id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
        miniprogram_openid TEXT,
        name TEXT NOT NULL DEFAULT '', headline TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]', works_json TEXT NOT NULL DEFAULT '[]', links_json TEXT NOT NULL DEFAULT '[]',
        avatar_key TEXT NOT NULL DEFAULT '', wechat_qr_key TEXT NOT NULL DEFAULT '',
        phone_public INTEGER NOT NULL DEFAULT 0, email_public INTEGER NOT NULL DEFAULT 0,
        address_public INTEGER NOT NULL DEFAULT 0, wechat_qr_public INTEGER NOT NULL DEFAULT 0,
        discoverable_in_network INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'draft', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        published_at INTEGER
    )""")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(business_cards)").fetchall()}
    if "name" not in columns:
        conn.execute("ALTER TABLE business_cards ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    if "miniprogram_openid" not in columns:
        conn.execute("ALTER TABLE business_cards ADD COLUMN miniprogram_openid TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_public ON business_cards(public_id,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_network ON business_cards(user_id,status,discoverable_in_network)")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_miniprogram_openid
                    ON business_cards(miniprogram_openid)
                    WHERE miniprogram_openid IS NOT NULL""")
    conn.execute("""CREATE TABLE IF NOT EXISTS network_node_ids(
        user_id INTEGER PRIMARY KEY, node_id TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS card_referral_journeys(
        journey_id TEXT PRIMARY KEY,
        campaign_id INTEGER NOT NULL,
        card_public_id TEXT NOT NULL,
        inviter_user_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        visited_at INTEGER NOT NULL,
        card_started_at INTEGER,
        registered_user_id INTEGER,
        invite_relation_id INTEGER,
        registered_at INTEGER,
        published_at INTEGER,
        expires_at INTEGER NOT NULL
    )""")
    journey_columns = {row["name"] for row in conn.execute("PRAGMA table_info(card_referral_journeys)").fetchall()}
    if "published_at" not in journey_columns:
        conn.execute("ALTER TABLE card_referral_journeys ADD COLUMN published_at INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_referral_inviter_time ON card_referral_journeys(inviter_user_id,visited_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_referral_expiry ON card_referral_journeys(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_referral_anonymous_time ON card_referral_journeys(visited_at) WHERE registered_user_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_referral_registered_time ON card_referral_journeys(registered_at) WHERE registered_user_id IS NOT NULL")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_card_referral_registered_user
                    ON card_referral_journeys(registered_user_id)
                    WHERE registered_user_id IS NOT NULL""")
    cleanup_referral_journeys(conn)


def node_id(conn, user_id):
    row = conn.execute("SELECT node_id FROM network_node_ids WHERE user_id=?", (int(user_id),)).fetchone()
    if row:
        return row["node_id"]
    value = secrets.token_urlsafe(18)
    conn.execute("INSERT OR IGNORE INTO network_node_ids(user_id,node_id,created_at) VALUES(?,?,?)", (int(user_id), value, int(time.time())))
    return conn.execute("SELECT node_id FROM network_node_ids WHERE user_id=?", (int(user_id),)).fetchone()["node_id"]


def node_user_id(conn, value):
    row = conn.execute("SELECT user_id FROM network_node_ids WHERE node_id=?", (str(value or ""),)).fetchone()
    return int(row["user_id"]) if row else 0


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


def openid_owner(conn, openid):
    row = conn.execute(
        "SELECT user_id FROM business_cards WHERE miniprogram_openid=?",
        (str(openid or "").strip(),),
    ).fetchone()
    return int(row["user_id"]) if row else 0


def bind_miniprogram_openid(conn, user_id, openid, now=None):
    """Bind one WeChat Mini Program identity to one card in the caller's transaction."""
    openid = str(openid or "").strip()
    if not openid or len(openid) > 256:
        raise CardError("invalid_openid", "微信登录态无效", 400)
    user_id = int(user_id)
    create_draft(conn, user_id, now=now)
    existing_owner = openid_owner(conn, openid)
    if existing_owner and existing_owner != user_id:
        raise CardError("openid_in_use", "该微信已绑定其他名片", 409)
    conn.execute(
        "UPDATE business_cards SET miniprogram_openid=?,updated_at=? WHERE user_id=?",
        (openid, int(now or time.time()), user_id),
    )
    return dict(conn.execute("SELECT * FROM business_cards WHERE user_id=?", (user_id,)).fetchone())


def _json(value, field):
    if value is None:
        raise CardError("invalid_" + field)
    if not isinstance(value, (str, list, dict)):
        raise CardError("invalid_" + field)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode()) > MAX_JSON_BYTES:
        raise CardError("card_too_large")
    return raw


def _work_media_key(user_id, media_type, slot, key):
    extension = {"image": ".jpg", "video": ".mp4"}.get(media_type)
    prefix = "cards/%s/work_%s_%s/" % (int(user_id), media_type, int(slot))
    return bool(extension and isinstance(key, str) and key.startswith(prefix) and key.endswith(extension) and len(key) <= 512)


def _work_image_key(user_id, slot, key):
    return _work_media_key(user_id, "image", slot, key)


def _work_slots(works):
    groups, other = {"image": {}, "video": {}}, []
    for item in works if isinstance(works, list) else []:
        media_type = item.get("type") if isinstance(item, dict) else ""
        if media_type not in groups:
            other.append(item)
            continue
        try:
            slot = int(item.get("slot") or 0)
        except (TypeError, ValueError):
            slot = 0
        slots = groups[media_type]
        if slot not in (1, 2, 3) or slot in slots:
            slot = next((value for value in (1, 2, 3) if value not in slots), 0)
        if slot:
            slots[slot] = {**item, "type": media_type, "slot": slot}
    return groups["image"], groups["video"], other


def _works(value, user_id):
    if not isinstance(value, list):
        raise CardError("invalid_works")
    images, videos, other = _work_slots(value)
    for media_type, slots in (("image", images), ("video", videos)):
        for slot, item in slots.items():
            key = item.get("key")
            if key and not _work_media_key(user_id, media_type, slot, key):
                raise CardError("invalid_work_" + media_type, "作品媒体无效")
            item.pop("url", None)
    return _json(
        [images[slot] for slot in sorted(images)] + [videos[slot] for slot in sorted(videos)] + other,
        "works",
    )


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
    payload = dict(payload)
    if "title" in payload and "headline" not in payload:
        payload["headline"] = payload["title"]
    privacy = payload.pop("privacy", None)
    if privacy is not None:
        if not isinstance(privacy, dict):
            raise CardError("invalid_privacy")
        for field in SENSITIVE_FIELDS:
            if field in privacy:
                payload[field + "_public"] = privacy[field]
    row = conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone()
    if not row:
        row = create_draft(conn, user_id, now=now)
    public_fields = tuple(field + "_public" for field in SENSITIVE_FIELDS)
    allowed = set(TEXT_FIELDS + JSON_FIELDS + public_fields + ("discoverable_in_network",))
    values = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key in TEXT_FIELDS:
            values[key] = _text(value, key)
        elif key in JSON_FIELDS:
            values[key + "_json"] = _works(value, user_id) if key == "works" else _json(value, key)
        else:
            if not isinstance(value, bool):
                raise CardError("invalid_" + key)
            values[key] = 1 if value else 0
    if values:
        values["updated_at"] = int(now or time.time())
        fields = ",".join(key + "=?" for key in values)
        conn.execute("UPDATE business_cards SET " + fields + " WHERE user_id=?", (*values.values(), int(user_id)))
    return dict(conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone())


def set_media_key(conn, user_id, field, key, now=None):
    if field not in MEDIA_FIELDS or not isinstance(key, str) or not key.startswith("cards/") or len(key) > 512:
        raise CardError("invalid_image")
    create_draft(conn, user_id, now=now)
    conn.execute(
        "UPDATE business_cards SET %s=?,updated_at=? WHERE user_id=?" % (field + "_key"),
        (key, int(now or time.time()), int(user_id)),
    )
    return dict(conn.execute("SELECT * FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone())


def set_work_media_key(conn, user_id, media_type, slot, key, title=None, now=None):
    slot = int(slot)
    if slot not in (1, 2, 3) or not _work_media_key(user_id, media_type, slot, key):
        raise CardError("invalid_work_" + str(media_type), "作品媒体无效")
    if title is not None:
        title = _text(title, "title")
    row = create_draft(conn, user_id, now=now)
    images, videos, other = _work_slots(json.loads(row["works_json"] or "[]"))
    slots = images if media_type == "image" else videos
    item = slots.get(slot, {"type": media_type, "slot": slot, "title": ""})
    item.update({"type": media_type, "slot": slot, "key": key})
    item.pop("url", None)
    if title is not None:
        item["title"] = title
    slots[slot] = item
    conn.execute(
        "UPDATE business_cards SET works_json=?,updated_at=? WHERE user_id=?",
        (_json(
            [images[value] for value in sorted(images)] + [videos[value] for value in sorted(videos)] + other,
            "works",
        ), int(now or time.time()), int(user_id)),
    )
    return item


def set_work_image_key(conn, user_id, slot, key, title=None, now=None):
    return set_work_media_key(conn, user_id, "image", slot, key, title, now)


def _media_url(key):
    if not key:
        return ""
    try:
        from .content_domains import cos
    except ImportError:
        try:
            from content_domains import cos
        except ImportError:
            return ""
    try:
        return cos.object_url(key, private=True)
    except Exception:
        return ""


def _decode(row, owner=False):
    privacy = {field: bool(row[field + "_public"]) for field in SENSITIVE_FIELDS}
    works = json.loads(row["works_json"] or "[]")
    for item in works:
        if not isinstance(item, dict) or item.get("type") not in ("image", "video"):
            continue
        try:
            slot = int(item.get("slot") or 0)
        except (TypeError, ValueError):
            slot = 0
        key = item.get("key")
        if key and _work_media_key(row["user_id"], item["type"], slot, key):
            item["url"] = _media_url(key)
        else:
            item.pop("url", None)
        if not owner:
            item.pop("key", None)
    result = {
        "public_id": row["public_id"], "name": row["name"] or row["display_name"] or "黄雀用户",
        "title": row["headline"], "headline": row["headline"], "company": row["company"], "bio": row["bio"],
        "tags": json.loads(row["tags_json"] or "[]"), "works": works,
        "links": json.loads(row["links_json"] or "[]"), "avatar": _media_url(row["avatar_key"]),
    }
    if owner:
        result.update({field: row[field] for field in ("phone", "email", "address")})
        result["wechat_qr"] = _media_url(row["wechat_qr_key"])
        result.update({field + "_public": bool(row[field + "_public"]) for field in SENSITIVE_FIELDS})
        result.update({
            "privacy": privacy,
            "discoverable_in_network": bool(row["discoverable_in_network"]),
            "status": row["status"],
            "published": row["status"] == "published",
            "is_published": row["status"] == "published",
        })
    else:
        for field in ("phone", "email", "address"):
            if row[field + "_public"]:
                result[field] = row[field]
        if row["wechat_qr_public"]:
            result["wechat_qr"] = _media_url(row["wechat_qr_key"])
        result["privacy"] = privacy
    return result


def mine(conn, user_id):
    row = conn.execute("SELECT c.*,u.display_name FROM business_cards c JOIN users u ON u.id=c.user_id WHERE c.user_id=?", (int(user_id),)).fetchone()
    return _decode(row, True) if row else None


def publish(conn, user_id, status, now=None):
    create_draft(conn, user_id, now=now)
    if status not in ("published", "unpublished"):
        raise CardError("invalid_status")
    if status == "published":
        row = conn.execute("SELECT name,headline,company FROM business_cards WHERE user_id=?", (int(user_id),)).fetchone()
        if not row or not all(str(row[field] or "").strip() for field in ("name", "headline", "company")):
            raise CardError("card_incomplete", "请先填写姓名、职称和公司", 409)
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


def public_network_person(conn, user_id, admin=False):
    row = conn.execute("""SELECT u.display_name,u.account_status,c.public_id,c.name,c.headline,c.company,c.avatar_key,c.status,c.discoverable_in_network
                          FROM users u LEFT JOIN business_cards c ON c.user_id=u.id WHERE u.id=?""", (int(user_id),)).fetchone()
    count_sql = "SELECT COUNT(*) FROM user_invites WHERE inviter_user_id=?"
    if not admin:
        count_sql += " AND status='bound' AND risk_status='normal'"
    children_count = conn.execute(count_sql, (int(user_id),)).fetchone()[0]
    base = {"node_id": node_id(conn, user_id), "children_count": int(children_count or 0), "has_children": bool(children_count)}
    if row and row["account_status"] == "active" and row["status"] == "published" and row["discoverable_in_network"]:
        return {**base, "public_id": row["public_id"], "name": row["name"] or row["display_name"] or "黄雀用户", "avatar": _media_url(row["avatar_key"]), "headline": row["headline"] or "", "title": row["headline"] or "", "company": row["company"] or ""}
    return {**base, "public_id": "", "name": "匿名用户", "avatar": "", "headline": "", "title": "", "company": ""}


def _admin_relation_fields(conn, relation):
    user = conn.execute("SELECT id,username FROM users WHERE id=?", (relation["person_user_id"],)).fetchone()
    username = str(user["username"] or "")
    if len(username) == 11 and username.startswith("1") and username.isdigit():
        username = username[:3] + "****" + username[-4:]
    reward = conn.execute(
        "SELECT event_type,status,reward_points FROM invite_reward_point_records WHERE invite_relation_id=? ORDER BY id DESC LIMIT 1",
        (relation["id"],),
    ).fetchone()
    return {
        "user_id": int(user["id"]), "username": username,
        "relation_status": relation["status"], "risk_status": relation["risk_status"],
        "reward_event": ({"event_type": reward["event_type"], "status": reward["status"], "points": int(reward["reward_points"])} if reward else None),
    }


def ancestors(conn, user_id, limit=100, admin=False):
    items, seen, current = [], {int(user_id)}, int(user_id)
    for _ in range(min(100, int(limit))):
        sql = "SELECT id,inviter_user_id,status,risk_status FROM user_invites WHERE invitee_user_id=?"
        if not admin:
            sql += " AND status='bound' AND risk_status='normal'"
        row = conn.execute(sql, (current,)).fetchone()
        if not row or int(row["inviter_user_id"]) in seen:
            break
        current = int(row["inviter_user_id"]); seen.add(current)
        item = public_network_person(conn, current, admin=admin)
        if admin:
            item.update(_admin_relation_fields(conn, {
                "id": row["id"], "person_user_id": current,
                "status": row["status"], "risk_status": row["risk_status"],
            }))
        items.append(item)
    return list(reversed(items))


def ancestor_ids(conn, user_id, limit=100):
    seen, current = {int(user_id)}, int(user_id)
    result = []
    for _ in range(min(100, int(limit))):
        row = conn.execute("SELECT inviter_user_id FROM user_invites WHERE invitee_user_id=? AND status='bound' AND risk_status='normal'", (current,)).fetchone()
        if not row or int(row["inviter_user_id"]) in seen:
            return result
        current = int(row["inviter_user_id"]); seen.add(current); result.append(current)
    return result


def children(conn, user_id, cursor=0, limit=20, admin=False):
    cursor, limit = int(cursor or 0), max(1, min(100, int(limit or 20)))
    where = "WHERE inviter_user_id=? AND id>?"
    if not admin:
        where += " AND status='bound' AND risk_status='normal'"
    rows = conn.execute(
        "SELECT id,invitee_user_id,status,risk_status FROM user_invites " + where + " ORDER BY id LIMIT ?",
        (int(user_id), cursor, limit + 1),
    ).fetchall()
    page, items = rows[:limit], []
    for relation in page:
        item = public_network_person(conn, relation["invitee_user_id"], admin=admin)
        if admin:
            item.update(_admin_relation_fields(conn, {
                "id": relation["id"], "person_user_id": relation["invitee_user_id"],
                "status": relation["status"], "risk_status": relation["risk_status"],
            }))
        items.append(item)
    next_id = int(page[-1]["id"]) if len(rows) > limit else 0
    return {"items": items, "next_cursor": next_id, "next_before_id": next_id}


def upload_image(payload, field, prefix="cards"):
    if field not in MEDIA_FIELDS + WORK_IMAGE_FIELDS or not isinstance(payload, str) or not payload.startswith("data:image/"):
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
    if miniprogram_security is None:
        raise CardError("media_unavailable", "媒体服务暂不可用", 503)
    try:
        miniprogram_security.check_image(raw, field + ".jpg", "image/jpeg")
    except miniprogram_security.ContentRejected as exc:
        label = {"avatar": "头像", "wechat_qr": "微信二维码"}.get(field, "作品图片%s" % field[-1])
        raise CardError("content_rejected", "%s未通过微信安全检测：%s" % (label, exc), 400) from exc
    except miniprogram_security.SecurityUnavailable as exc:
        raise CardError("content_security_unavailable", str(exc), 503) from exc
    try:
        from .content_domains import cos
    except ImportError:
        from content_domains import cos
    if not cos.enabled():
        raise CardError("media_unavailable", "媒体服务暂不可用", 503)
    key = "%s/%s/%s.jpg" % (prefix, field, secrets.token_urlsafe(16))
    cos.put_bytes(raw, key, "image/jpeg", private=True)
    return key


def upload_video(payload, field, prefix="cards"):
    if field not in WORK_VIDEO_FIELDS or not isinstance(payload, str) or not payload.startswith("data:video/mp4;base64,"):
        raise CardError("invalid_video", "仅支持 MP4 视频")
    try:
        raw = base64.b64decode(payload.split(",", 1)[1], validate=True)
    except Exception as exc:
        raise CardError("invalid_video", "视频文件无效") from exc
    if len(raw) > MAX_VIDEO_BYTES:
        raise CardError("video_too_large", "请上传 20MB 以内的视频", 413)
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        raise CardError("invalid_video", "视频文件无效")
    try:
        from .content_domains import cos
    except ImportError:
        from content_domains import cos
    if not cos.enabled():
        raise CardError("media_unavailable", "媒体服务暂不可用", 503)
    key = "%s/%s/%s.mp4" % (prefix, field, secrets.token_urlsafe(16))
    cos.put_bytes(raw, key, "video/mp4", private=True)
    return key
