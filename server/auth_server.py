#!/usr/bin/env python3
# 黄雀 AI · 独立认证服务（零依赖，标准库）
# 端口 127.0.0.1:8095，nginx 把 /api/auth/ 路由过来。与 leadgen(8090) 完全隔离。
import sqlite3, hashlib, secrets, json, os, sys, time, urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 微信支付客户端(仅用系统已装 cryptography)。缺 wxpay.py/cryptography 时置 None,
# 支付路由回 503,不拖垮整个认证服务。
try:
    import wxpay
except Exception:
    wxpay = None

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
PORT = 8095
ITER = 200000
TOKEN_TTL = int(os.environ.get("HQ_AUTH_TOKEN_TTL", str(30 * 24 * 3600)))
AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")
AUTH_COOKIE_SECURE = os.environ.get("HQ_AUTH_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
LOGIN_FAIL_WINDOW = int(os.environ.get("HQ_AUTH_FAIL_WINDOW", "300"))
LOGIN_FAIL_MAX = int(os.environ.get("HQ_AUTH_FAIL_MAX", "5"))
REGISTER_WINDOW = int(os.environ.get("HQ_AUTH_REGISTER_WINDOW", "300"))
REGISTER_MAX = int(os.environ.get("HQ_AUTH_REGISTER_MAX", "5"))
NEW_USER_TRIAL_POINTS = int(os.environ.get("HQ_AUTH_TRIAL_POINTS", "16"))  # 新注册赠送试用点数(约2次标准清晰度作图)
# 充值定价：客户端只传金额(元)，点数一律服务端算，绝不信客户端传的点数——
# 否则用户能花 1 元买百万点。与 recharge.html / 小程序 recharge.js 保持一致。
# 固定档含赠送(略高于 10 点/元)；自定义严格 10 点/元、限 10~5000 元整。
RECHARGE_TIERS = {99: 1000, 199: 2000, 499: 5000}   # 金额(元) -> 点数(含赠送)
RECHARGE_RATE = 10                                   # 自定义:每元 10 点
RECHARGE_CUSTOM_MIN = 10
RECHARGE_CUSTOM_MAX = 5000

def recharge_points_for(amount):
    """金额(元) -> 点数。固定档用赠送价；其余按 10 点/元(限 10~5000 元整数)。非法返回 None。
    金额只接受整数元(避免分位歧义与非整点数)；拒绝 NaN/Infinity/超大数/非数字(否则 int/float 抛错致 500)。"""
    try:
        yuan = int(amount)
        if yuan != float(amount):    # 拒绝非整数元(如 15.5);float() 对超大整数/inf 会抛 OverflowError
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if yuan in RECHARGE_TIERS:
        return RECHARGE_TIERS[yuan]
    if RECHARGE_CUSTOM_MIN <= yuan <= RECHARGE_CUSTOM_MAX:
        return yuan * RECHARGE_RATE
    return None
LOGIN_FAILS = {}
REGISTER_HITS = {}
REVOKED_TOKENS = set()
ACCOUNT_ID_LENGTH = 8
ACCOUNT_ID_PREFIX = "HQ"
ACCOUNT_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CANVAS_NAME_MAX = 48
CANVAS_DATA_MAX_BYTES = int(os.environ.get("HQ_CANVAS_DATA_MAX_BYTES", str(6 * 1024 * 1024)))
CANVAS_ROLES = {"viewer", "editor"}
CANVAS_OPS_MAX_PER_BATCH = 200
CANVAS_OPS_MAX_BYTES = int(os.environ.get("HQ_CANVAS_OPS_MAX_BYTES", str(1024 * 1024)))
CANVAS_SYNC_MAX_BATCHES = int(os.environ.get("HQ_CANVAS_SYNC_MAX_BATCHES", "100"))
CANVAS_SYNC_MAX_OPS_BYTES = int(os.environ.get("HQ_CANVAS_SYNC_MAX_OPS_BYTES", str(2 * 1024 * 1024)))
CANVAS_PRESENCE_MAX_BYTES = 4096
CANVAS_NODE_TYPES = {"text", "image", "reverse", "gen", "video"}
CANVAS_OPS_RETAINED_BATCHES = 1000
CANVAS_PRESENCE_WINDOW_SECONDS = 30

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        pw_salt TEXT NOT NULL,
        display_name TEXT,
        points INTEGER DEFAULT 0,
        role TEXT DEFAULT 'member',
        must_change INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "account_id" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN account_id TEXT")
    _ensure_all_account_ids(c)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_account_id ON users(account_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS tokens(
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        expires_at INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS points_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        who_admin TEXT NOT NULL,
        username TEXT NOT NULL,
        delta INTEGER NOT NULL,
        before_points INTEGER NOT NULL,
        after_points INTEGER NOT NULL,
        reason TEXT,
        created_at INTEGER NOT NULL
    )""")
    # 任务扣点/退点接入审计后，这张表按任务量增长（原来只有人工加减点，几乎不涨）。
    # 按用户查流水是后台最常用的路径，没索引会随表全扫。
    c.execute("CREATE INDEX IF NOT EXISTS idx_points_audit_user ON points_audit(username, id DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS friendships(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        friend_username TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(username, friend_username)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_friendships_user ON friendships(username, id DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS friend_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_username TEXT NOT NULL,
        to_username TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        reviewed_at INTEGER,
        UNIQUE(from_username, to_username, status)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_friend_requests_to ON friend_requests(to_username, status, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_friend_requests_from ON friend_requests(from_username, status, id DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS canvas_boards(
        id TEXT PRIMARY KEY,
        owner_username TEXT NOT NULL,
        name TEXT NOT NULL,
        data_json TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_canvas_boards_owner ON canvas_boards(owner_username, updated_at DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS canvas_members(
        board_id TEXT NOT NULL,
        username TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        invited_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY(board_id, username)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_canvas_members_user ON canvas_members(username, board_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS canvas_ops(
        board_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        op_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        username TEXT NOT NULL,
        ops_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY(board_id, version),
        UNIQUE(board_id, op_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_canvas_ops_board_version ON canvas_ops(board_id, version)")
    c.execute("""CREATE TABLE IF NOT EXISTS canvas_presence(
        board_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        username TEXT NOT NULL,
        last_seen INTEGER NOT NULL,
        PRIMARY KEY(board_id, client_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_canvas_presence_board_seen ON canvas_presence(board_id, last_seen)")
    c.execute("""CREATE TABLE IF NOT EXISTS recharge_orders(
        order_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        amount REAL NOT NULL,
        points INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT,
        created_at INTEGER NOT NULL,
        reviewed_by TEXT,
        reviewed_at INTEGER,
        review_note TEXT
    )""")
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tokens)").fetchall()}
    if "expires_at" not in cols:
        c.execute("ALTER TABLE tokens ADD COLUMN expires_at INTEGER")
    rcols = {r["name"] for r in c.execute("PRAGMA table_info(recharge_orders)").fetchall()}
    if "transaction_id" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN transaction_id TEXT")  # 微信支付流水号
    if "pay_channel" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN pay_channel TEXT")     # wxpay_native / wxpay_jsapi / manual
    c.commit(); c.close()

def generate_account_id():
    n = ACCOUNT_ID_LENGTH - len(ACCOUNT_ID_PREFIX)
    return ACCOUNT_ID_PREFIX + "".join(secrets.choice(ACCOUNT_ID_ALPHABET) for _ in range(n))

def _new_unique_account_id(c):
    for _ in range(64):
        account_id = generate_account_id()
        row = c.execute("SELECT 1 FROM users WHERE account_id=?", (account_id,)).fetchone()
        if not row:
            return account_id
    raise RuntimeError("account_id exhausted")

def _ensure_all_account_ids(c):
    rows = c.execute("SELECT username FROM users WHERE account_id IS NULL OR account_id=''").fetchall()
    for row in rows:
        c.execute("UPDATE users SET account_id=? WHERE username=?", (_new_unique_account_id(c), row["username"]))

def ensure_account_id(username, c=None):
    own = c is None
    if own: c = db()
    try:
        row = c.execute("SELECT account_id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        account_id = row["account_id"]
        if account_id:
            return account_id
        account_id = _new_unique_account_id(c)
        c.execute("UPDATE users SET account_id=? WHERE username=?", (account_id, username))
        if own: c.commit()
        return account_id
    finally:
        if own: c.close()

def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITER).hex()

def create_user(username, password, points=0, role='member'):
    init_db()
    salt = secrets.token_hex(16)
    c = db()
    c.execute("""INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)
                 VALUES(?,?,?,?,?,?,1,?)
                 ON CONFLICT(username) DO UPDATE SET
                   pw_hash=excluded.pw_hash, pw_salt=excluded.pw_salt,
                   points=excluded.points, role=excluded.role, must_change=1""",
              (username, hash_pw(password, salt), salt, username, points, role, _new_unique_account_id(c)))
    ensure_account_id(username, c)
    c.commit(); c.close()
    print("OK user:", username)

def public_user(username, display_name=None, points=0, role='member', must_change=False, account_id=None):
    return {
        "username": username,
        "name": display_name or username,
        "points": points,
        "role": role,
        "must_change": bool(must_change),
        "account_id": account_id or ""
    }

def public_points(row):
    return {
        "username": row["username"],
        "user_id": row["id"],
        "points": row["points"],
    }

def public_friend(row):
    return {
        "username": row["username"],
        "name": row["display_name"] or row["username"],
        "account_id": row["account_id"] or "",
        "role": row["role"],
        "created_at": row["friend_created_at"],
    }

def list_friends(username):
    c = db()
    try:
        rows = c.execute("""SELECT u.username, u.display_name, u.account_id, u.role, f.created_at AS friend_created_at
                            FROM friendships f
                            JOIN users u ON u.username=f.friend_username
                            WHERE f.username=?
                            ORDER BY f.id DESC""", (username,)).fetchall()
        return [public_friend(r) for r in rows]
    finally:
        c.close()

def add_friend_by_account_id(username, account_id):
    account_id = (account_id or "").strip().upper()
    if not account_id:
        return None, "missing"
    c = db()
    try:
        me = c.execute("SELECT username, account_id FROM users WHERE username=?", (username,)).fetchone()
        if not me:
            return None, "not_found"
        friend = c.execute("""SELECT username, display_name, account_id, role
                              FROM users WHERE account_id=?""", (account_id,)).fetchone()
        if not friend:
            return None, "not_found"
        if friend["username"] == username:
            return None, "self"
        try:
            c.execute("""INSERT INTO friendships(username, friend_username, created_at)
                         VALUES(?,?,?)""", (username, friend["username"], int(time.time())))
            c.commit()
        except sqlite3.IntegrityError:
            return public_friend({
                "username": friend["username"],
                "display_name": friend["display_name"],
                "account_id": friend["account_id"],
                "role": friend["role"],
                "friend_created_at": int(time.time()),
            }), "exists"
        return list_friends(username), None
    finally:
        c.close()

def public_friend_request(row):
    return {
        "id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
        "from_user": {
            "username": row["from_username"],
            "name": row["from_display_name"] or row["from_username"],
            "account_id": row["from_account_id"] or "",
        },
        "to_user": {
            "username": row["to_username"],
            "name": row["to_display_name"] or row["to_username"],
            "account_id": row["to_account_id"] or "",
        },
    }

def list_friend_requests(username):
    c = db()
    try:
        rows = c.execute("""SELECT r.id, r.from_username, r.to_username, r.status, r.created_at, r.reviewed_at,
                                   fu.display_name AS from_display_name, fu.account_id AS from_account_id,
                                   tu.display_name AS to_display_name, tu.account_id AS to_account_id
                            FROM friend_requests r
                            JOIN users fu ON fu.username=r.from_username
                            JOIN users tu ON tu.username=r.to_username
                            WHERE (r.from_username=? OR r.to_username=?) AND r.status='pending'
                            ORDER BY r.id DESC""", (username, username)).fetchall()
        incoming, outgoing = [], []
        for row in rows:
            item = public_friend_request(row)
            if row["to_username"] == username:
                incoming.append(item)
            else:
                outgoing.append(item)
        return {"incoming": incoming, "outgoing": outgoing}
    finally:
        c.close()

def are_friends(c, username, friend_username):
    row = c.execute("""SELECT 1 FROM friendships
                       WHERE username=? AND friend_username=?""", (username, friend_username)).fetchone()
    return bool(row)

def create_friend_request(username, account_id):
    account_id = (account_id or "").strip().upper()
    if not account_id:
        return None, "missing"
    c = db()
    try:
        me = c.execute("SELECT username, account_id FROM users WHERE username=?", (username,)).fetchone()
        if not me:
            return None, "not_found"
        friend = c.execute("""SELECT username, display_name, account_id, role
                              FROM users WHERE account_id=?""", (account_id,)).fetchone()
        if not friend:
            return None, "not_found"
        if friend["username"] == username:
            return None, "self"
        if are_friends(c, username, friend["username"]):
            return None, "already_friends"
        reverse = c.execute("""SELECT id FROM friend_requests
                               WHERE from_username=? AND to_username=? AND status='pending'""",
                            (friend["username"], username)).fetchone()
        if reverse:
            return None, "incoming_pending"
        try:
            c.execute("""INSERT INTO friend_requests(from_username, to_username, status, created_at)
                         VALUES(?,?,?,?)""", (username, friend["username"], "pending", int(time.time())))
            c.commit()
        except sqlite3.IntegrityError:
            return None, "pending"
        return list_friend_requests(username), None
    finally:
        c.close()

def respond_friend_request(username, request_id, action):
    action = (action or "").strip().lower()
    if action not in ("accept", "reject"):
        return None, "bad_action"
    try:
        request_id = int(request_id)
    except Exception:
        return None, "missing"
    c = db()
    try:
        row = c.execute("""SELECT * FROM friend_requests
                           WHERE id=? AND to_username=? AND status='pending'""",
                        (request_id, username)).fetchone()
        if not row:
            return None, "not_found"
        now = int(time.time())
        status = "accepted" if action == "accept" else "rejected"
        c.execute("""UPDATE friend_requests SET status=?, reviewed_at=?
                     WHERE id=? AND to_username=? AND status='pending'""",
                  (status, now, request_id, username))
        if action == "accept":
            c.execute("""INSERT OR IGNORE INTO friendships(username, friend_username, created_at)
                         VALUES(?,?,?)""", (row["from_username"], row["to_username"], now))
            c.execute("""INSERT OR IGNORE INTO friendships(username, friend_username, created_at)
                         VALUES(?,?,?)""", (row["to_username"], row["from_username"], now))
        c.commit()
        return {"friends": list_friends(username), "requests": list_friend_requests(username)}, None
    finally:
        c.close()

def normalize_canvas_name(name):
    name = " ".join(str(name or "").strip().split())
    if not name:
        return "未命名协作画布", None
    if any(ord(ch) < 32 for ch in name):
        return None, "bad_name"
    return name[:CANVAS_NAME_MAX], None

def normalize_canvas_role(role):
    role = str(role or "viewer").strip().lower()
    if role not in CANVAS_ROLES:
        return None
    return role

def validate_canvas_data(data):
    if not isinstance(data, dict):
        return "bad_data"
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return "bad_data"
    node_ids = set()
    for node in nodes:
        if (not isinstance(node, dict) or not valid_canvas_node_id(node.get("id"))
                or node.get("type") not in CANVAS_NODE_TYPES or node["id"] in node_ids):
            return "bad_data"
        node_ids.add(node["id"])
    for edge in edges:
        if canvas_edge_key(edge) is None:
            return "bad_data"
        if edge["from"]["node"] not in node_ids or edge["to"]["node"] not in node_ids:
            return "bad_data"
    return None

def pack_canvas_data(data):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, "bad_data"
    validation_error = validate_canvas_data(data)
    if validation_error:
        return None, validation_error
    try:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None, "bad_data"
    if len(raw.encode("utf-8")) > CANVAS_DATA_MAX_BYTES:
        return None, "too_large"
    return raw, None

def make_canvas_id(c):
    for _ in range(64):
        board_id = "cb_" + secrets.token_urlsafe(12).replace("-", "_")
        row = c.execute("SELECT 1 FROM canvas_boards WHERE id=?", (board_id,)).fetchone()
        if not row:
            return board_id
    raise RuntimeError("canvas id exhausted")

def row_get(row, key, default=None):
    try:
        return row[key]
    except Exception:
        return default

def public_canvas_member(row):
    return {
        "username": row["username"],
        "name": row["display_name"] or row["username"],
        "account_id": row["account_id"] or "",
        "role": row["member_role"],
        "created_at": row["member_created_at"],
    }

def list_canvas_members(c, board_id):
    rows = c.execute("""SELECT m.username, m.role AS member_role, m.created_at AS member_created_at,
                               u.display_name, u.account_id
                        FROM canvas_members m
                        JOIN users u ON u.username=m.username
                        WHERE m.board_id=?
                        ORDER BY m.created_at DESC, m.username ASC""", (board_id,)).fetchall()
    return [public_canvas_member(row) for row in rows]

def canvas_member_count(c, board_id):
    row = c.execute("SELECT COUNT(*) AS n FROM canvas_members WHERE board_id=?", (board_id,)).fetchone()
    return int(row["n"] or 0) if row else 0

def public_canvas_board(row, role, include_data=False, members_count=None):
    board = {
        "id": row["id"],
        "name": row["name"],
        "owner_username": row["owner_username"],
        "role": role,
        "version": int(row["version"] or 1),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "members_count": int(members_count if members_count is not None else row_get(row, "members_count", 0) or 0),
    }
    if include_data:
        try:
            board["data"] = json.loads(row["data_json"] or "{}")
        except Exception:
            board["data"] = {}
    return board

def canvas_role_and_board(c, username, board_id):
    row = c.execute("SELECT * FROM canvas_boards WHERE id=?", (board_id,)).fetchone()
    if not row:
        return None, None
    if row["owner_username"] == username:
        return "owner", row
    member = c.execute("SELECT role FROM canvas_members WHERE board_id=? AND username=?",
                       (board_id, username)).fetchone()
    if not member:
        return None, row
    return member["role"], row

def list_canvas_boards(username):
    c = db()
    try:
        items = []
        owned = c.execute("""SELECT b.*, (SELECT COUNT(*) FROM canvas_members m WHERE m.board_id=b.id) AS members_count
                             FROM canvas_boards b
                             WHERE b.owner_username=?""", (username,)).fetchall()
        shared = c.execute("""SELECT b.*, m.role AS access_role,
                                     (SELECT COUNT(*) FROM canvas_members cm WHERE cm.board_id=b.id) AS members_count
                              FROM canvas_members m
                              JOIN canvas_boards b ON b.id=m.board_id
                              WHERE m.username=? AND b.owner_username<>?""", (username, username)).fetchall()
        for row in owned:
            items.append(public_canvas_board(row, "owner", members_count=row["members_count"]))
        for row in shared:
            items.append(public_canvas_board(row, row["access_role"], members_count=row["members_count"]))
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items, None
    finally:
        c.close()

def create_canvas_board(username, payload):
    name, err = normalize_canvas_name((payload or {}).get("name"))
    if err:
        return None, err
    data_json, err = pack_canvas_data((payload or {}).get("data") or {})
    if err:
        return None, err
    c = db()
    try:
        now = int(time.time())
        board_id = make_canvas_id(c)
        c.execute("""INSERT INTO canvas_boards(id, owner_username, name, data_json, version, created_at, updated_at)
                     VALUES(?,?,?,?,?,?,?)""", (board_id, username, name, data_json, 1, now, now))
        row = c.execute("SELECT * FROM canvas_boards WHERE id=?", (board_id,)).fetchone()
        c.commit()
        return public_canvas_board(row, "owner", include_data=True), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def get_canvas_board(username, board_id):
    c = db()
    try:
        role, row = canvas_role_and_board(c, username, board_id)
        if not role:
            return None, "not_found"
        board = public_canvas_board(row, role, include_data=True, members_count=canvas_member_count(c, board_id))
        board["members"] = list_canvas_members(c, board_id)
        return board, None
    finally:
        c.close()

def save_canvas_board(username, board_id, payload):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        role, row = canvas_role_and_board(c, username, board_id)
        if not role:
            c.rollback()
            return None, "not_found"
        if role not in ("owner", "editor"):
            c.rollback()
            return None, "forbidden"
        try:
            expected_version = int((payload or {}).get("version"))
        except Exception:
            c.rollback()
            return None, "bad_version"
        current_version = int(row["version"] or 1)
        if expected_version != current_version:
            board = public_canvas_board(row, role, include_data=True, members_count=canvas_member_count(c, board_id))
            board["members"] = list_canvas_members(c, board_id)
            c.rollback()
            return board, "conflict"
        data_json, err = pack_canvas_data((payload or {}).get("data") or {})
        if err:
            c.rollback()
            return None, err
        name = row["name"]
        if "name" in (payload or {}):
            name, err = normalize_canvas_name(payload.get("name"))
            if err:
                c.rollback()
                return None, err
        now = int(time.time())
        c.execute("""UPDATE canvas_boards
                     SET name=?, data_json=?, version=version+1, updated_at=?
                     WHERE id=?""", (name, data_json, now, board_id))
        fresh = c.execute("SELECT * FROM canvas_boards WHERE id=?", (board_id,)).fetchone()
        board = public_canvas_board(fresh, role, include_data=True, members_count=canvas_member_count(c, board_id))
        board["members"] = list_canvas_members(c, board_id)
        c.commit()
        return board, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def valid_canvas_node_id(value):
    return (isinstance(value, str) and 1 <= len(value) <= 64
            and value[0].isascii() and value[0].isalnum()
            and all(char.isascii() and (char.isalnum() or char in "_-") for char in value))

def canvas_edge_key(edge):
    if not isinstance(edge, dict):
        return None
    start = edge.get("from")
    end = edge.get("to")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    values = (start.get("node"), start.get("port"), end.get("node"), end.get("port"))
    if (not valid_canvas_node_id(values[0]) or not valid_canvas_node_id(values[2])
            or any(not isinstance(value, str) or not value for value in (values[1], values[3]))):
        return None
    return "%s:%s->%s:%s" % values

def normalize_canvas_ops_payload(payload):
    payload = payload or {}
    if not isinstance(payload, dict):
        return None, "bad_ops"
    op_id = payload.get("op_id")
    client_id = payload.get("client_id")
    ops = payload.get("ops")
    if not isinstance(op_id, str) or not op_id.strip() or len(op_id) > 128:
        return None, "bad_op_id"
    if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 128:
        return None, "bad_client_id"
    try:
        base_version = int(payload.get("base_version"))
    except Exception:
        return None, "bad_base_version"
    if base_version < 1:
        return None, "bad_base_version"
    if not isinstance(ops, list) or not ops:
        return None, "bad_ops"
    if len(ops) > CANVAS_OPS_MAX_PER_BATCH:
        return None, "too_many_ops"
    normalized = []
    for op in ops:
        if not isinstance(op, dict):
            return None, "bad_op"
        kind = op.get("type")
        if kind == "node.create":
            node = op.get("node")
            if (not isinstance(node, dict) or not valid_canvas_node_id(node.get("id"))
                    or node.get("type") not in CANVAS_NODE_TYPES):
                return None, "bad_op"
            normalized.append({"type": kind, "node": node})
        elif kind == "node.patch":
            fields = op.get("fields")
            if (not valid_canvas_node_id(op.get("id")) or not isinstance(fields, dict)
                    or not fields or "id" in fields or "type" in fields):
                return None, "bad_op"
            normalized.append({"type": kind, "id": op["id"], "fields": fields})
        elif kind == "node.delete":
            if not valid_canvas_node_id(op.get("id")):
                return None, "bad_op"
            normalized.append({"type": kind, "id": op["id"]})
        elif kind == "edge.create":
            edge = op.get("edge")
            if canvas_edge_key(edge) is None:
                return None, "bad_op"
            normalized.append({"type": kind, "edge": edge})
        elif kind == "edge.patch":
            fields = op.get("fields")
            if (not isinstance(op.get("id"), str) or not op["id"] or not isinstance(fields, dict)
                    or not fields or "from" in fields or "to" in fields):
                return None, "bad_op"
            normalized.append({"type": kind, "id": op["id"], "fields": fields})
        elif kind == "edge.delete":
            if not isinstance(op.get("id"), str) or not op["id"]:
                return None, "bad_op"
            normalized.append({"type": kind, "id": op["id"]})
        elif kind == "board.rename":
            name, err = normalize_canvas_name(op.get("name"))
            if err:
                return None, err
            normalized.append({"type": kind, "name": name})
        else:
            return None, "bad_op"
    return {"op_id": op_id.strip(), "client_id": client_id.strip(), "base_version": base_version, "ops": normalized}, None

def apply_json_merge_patch(target, patch):
    if not isinstance(patch, dict):
        return json.loads(json.dumps(patch, ensure_ascii=False))
    result = json.loads(json.dumps(target, ensure_ascii=False)) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = apply_json_merge_patch(result.get(key), value)
        else:
            result[key] = json.loads(json.dumps(value, ensure_ascii=False))
    return result

def apply_canvas_ops_to_snapshot(data, name, ops):
    snapshot = dict(data) if isinstance(data, dict) else {}
    nodes = [dict(item) for item in snapshot.get("nodes", []) if isinstance(item, dict)]
    edges = [dict(item) for item in snapshot.get("edges", []) if isinstance(item, dict)]
    node_index = {item.get("id"): item for item in nodes if isinstance(item.get("id"), str) and item["id"]}
    edge_index = {canvas_edge_key(item): item for item in edges if canvas_edge_key(item) is not None}
    for op in ops:
        kind = op["type"]
        if kind == "node.create":
            node = op["node"]
            if node["id"] not in node_index:
                created = dict(node)
                nodes.append(created)
                node_index[node["id"]] = created
        elif kind == "node.patch":
            node = node_index.get(op["id"])
            if node is not None:
                patched = apply_json_merge_patch(node, op["fields"])
                node.clear()
                node.update(patched)
        elif kind == "node.delete":
            deleted_id = op["id"]
            node_index.pop(deleted_id, None)
            nodes = [item for item in nodes if item.get("id") != deleted_id]
            edges = [item for item in edges if not (
                isinstance(item.get("from"), dict) and item["from"].get("node") == deleted_id
            ) and not (
                isinstance(item.get("to"), dict) and item["to"].get("node") == deleted_id
            )]
            edge_index = {canvas_edge_key(item): item for item in edges if canvas_edge_key(item) is not None}
        elif kind == "edge.create":
            edge = op["edge"]
            edge_key = canvas_edge_key(edge)
            if edge_key not in edge_index:
                created = dict(edge)
                edges.append(created)
                edge_index[edge_key] = created
        elif kind == "edge.patch":
            edge = edge_index.get(op["id"])
            if edge is not None:
                patched = apply_json_merge_patch(edge, op["fields"])
                edge.clear()
                edge.update(patched)
        elif kind == "edge.delete":
            deleted_key = op["id"]
            edge_index.pop(deleted_key, None)
            edges = [item for item in edges if canvas_edge_key(item) != deleted_key]
        elif kind == "board.rename":
            name = op["name"]
    snapshot["nodes"] = nodes
    snapshot["edges"] = edges
    return snapshot, name

def public_canvas_batch(row):
    try:
        ops = json.loads(row["ops_json"])
    except Exception:
        ops = []
    return {
        "version": int(row["version"]),
        "op_id": row["op_id"],
        "client_id": row["client_id"],
        "username": row["username"],
        "ops": ops,
    }

def canvas_online_count(c, board_id, now=None):
    now = int(time.time()) if now is None else int(now)
    cutoff = now - CANVAS_PRESENCE_WINDOW_SECONDS
    c.execute("DELETE FROM canvas_presence WHERE board_id=? AND last_seen<?", (board_id, cutoff))
    row = c.execute("""SELECT COUNT(*) AS n
                       FROM canvas_presence p
                       JOIN canvas_boards b ON b.id=p.board_id
                       LEFT JOIN canvas_members m ON m.board_id=p.board_id AND m.username=p.username
                       WHERE p.board_id=? AND p.last_seen>=?
                         AND (p.username=b.owner_username OR m.role='editor')""", (board_id, cutoff)).fetchone()
    return int(row["n"] or 0) if row else 0

def apply_canvas_ops(username, board_id, payload):
    normalized, err = normalize_canvas_ops_payload(payload)
    if err:
        return None, err
    try:
        ops_json = json.dumps(normalized["ops"], ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return None, "bad_ops"
    if len(ops_json.encode("utf-8")) > CANVAS_OPS_MAX_BYTES:
        return None, "too_large"
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        role, row = canvas_role_and_board(c, username, board_id)
        if not role:
            c.rollback()
            return None, "not_found"
        if role not in ("owner", "editor"):
            c.rollback()
            return None, "forbidden"
        existing = c.execute("SELECT * FROM canvas_ops WHERE board_id=? AND op_id=?",
                             (board_id, normalized["op_id"])).fetchone()
        if existing:
            board = public_canvas_board(row, role, include_data=True, members_count=canvas_member_count(c, board_id))
            board["members"] = list_canvas_members(c, board_id)
            result = {
                "version": int(row["version"]),
                "batch": public_canvas_batch(existing),
                "board": board,
            }
            c.rollback()
            return result, None
        try:
            data = json.loads(row["data_json"] or "{}")
        except Exception:
            data = {}
        data, name = apply_canvas_ops_to_snapshot(data, row["name"], normalized["ops"])
        data_json, err = pack_canvas_data(data)
        if err:
            c.rollback()
            return None, err
        version = int(row["version"] or 1) + 1
        now = int(time.time())
        c.execute("""UPDATE canvas_boards SET name=?, data_json=?, version=?, updated_at=? WHERE id=?""",
                  (name, data_json, version, now, board_id))
        c.execute("""INSERT INTO canvas_ops(board_id, version, op_id, client_id, username, ops_json, created_at)
                     VALUES(?,?,?,?,?,?,?)""",
                  (board_id, version, normalized["op_id"], normalized["client_id"], username, ops_json, now))
        c.execute("""DELETE FROM canvas_ops WHERE rowid IN (
                     SELECT rowid FROM canvas_ops WHERE board_id=?
                     ORDER BY version DESC LIMIT -1 OFFSET ?
                   )""", (board_id, CANVAS_OPS_RETAINED_BATCHES))
        batch = {
            "version": version,
            "op_id": normalized["op_id"],
            "client_id": normalized["client_id"],
            "username": username,
            "ops": normalized["ops"],
        }
        fresh = c.execute("SELECT * FROM canvas_boards WHERE id=?", (board_id,)).fetchone()
        board = public_canvas_board(fresh, role, include_data=True, members_count=canvas_member_count(c, board_id))
        board["members"] = list_canvas_members(c, board_id)
        c.commit()
        return {"version": version, "batch": batch, "board": board}, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def sync_canvas_ops(username, board_id, since_version):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        role, row = canvas_role_and_board(c, username, board_id)
        if not role:
            c.rollback()
            return None, "not_found"
        current_version = int(row["version"] or 1)
        oldest = c.execute("SELECT MIN(version) AS version FROM canvas_ops WHERE board_id=?", (board_id,)).fetchone()
        oldest_version = int(oldest["version"]) if oldest and oldest["version"] is not None else None
        reset = since_version > current_version or (
            oldest_version is not None and since_version < oldest_version - 1
        )
        online_count = canvas_online_count(c, board_id)
        batches = []
        if not reset:
            cursor = c.execute("SELECT * FROM canvas_ops WHERE board_id=? AND version>? ORDER BY version ASC LIMIT ?",
                               (board_id, since_version, CANVAS_SYNC_MAX_BATCHES + 1))
            expected_version = since_version + 1
            response_bytes = 0
            batch_count = 0
            while True:
                item = cursor.fetchone()
                if item is None:
                    break
                batch_count += 1
                if batch_count > CANVAS_SYNC_MAX_BATCHES:
                    reset = True
                    batches = []
                    break
                if int(item["version"]) != expected_version:
                    reset = True
                    break
                response_bytes += len((item["ops_json"] or "").encode("utf-8"))
                if response_bytes > CANVAS_SYNC_MAX_OPS_BYTES:
                    reset = True
                    batches = []
                    break
                batches.append(public_canvas_batch(item))
                expected_version += 1
            if expected_version != current_version + 1:
                reset = True
                batches = []
        result = {"version": current_version, "role": role, "batches": batches, "reset": reset, "online_count": online_count}
        if reset:
            board = public_canvas_board(row, role, include_data=True, members_count=canvas_member_count(c, board_id))
            board["members"] = list_canvas_members(c, board_id)
            result["board"] = board
        c.commit()
        return result, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def record_canvas_presence(username, board_id, payload):
    client_id = (payload or {}).get("client_id") if isinstance(payload, dict) else None
    if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 128:
        return None, "bad_client_id"
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        role, row = canvas_role_and_board(c, username, board_id)
        if not role:
            c.rollback()
            return None, "not_found"
        now = int(time.time())
        c.execute("""INSERT INTO canvas_presence(board_id, client_id, username, last_seen)
                     VALUES(?,?,?,?) ON CONFLICT(board_id, client_id) DO UPDATE SET
                       username=excluded.username, last_seen=excluded.last_seen""",
                  (board_id, client_id.strip(), username, now))
        result = {"online_count": canvas_online_count(c, board_id, now)}
        c.commit()
        return result, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def add_canvas_member(username, board_id, payload):
    account_id = str((payload or {}).get("account_id") or "").strip().upper()
    role = normalize_canvas_role((payload or {}).get("role") or "viewer")
    if not account_id:
        return None, "missing"
    if not role:
        return None, "bad_role"
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        access, board = canvas_role_and_board(c, username, board_id)
        if not access:
            c.rollback()
            return None, "not_found"
        if access != "owner":
            c.rollback()
            return None, "forbidden"
        target = c.execute("SELECT username FROM users WHERE account_id=?", (account_id,)).fetchone()
        if not target:
            c.rollback()
            return None, "user_not_found"
        target_username = target["username"]
        if target_username == username:
            c.rollback()
            return None, "self"
        if not are_friends(c, username, target_username):
            c.rollback()
            return None, "not_friend"
        now = int(time.time())
        c.execute("""INSERT INTO canvas_members(board_id, username, role, invited_by, created_at)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(board_id, username) DO UPDATE SET
                       role=excluded.role, invited_by=excluded.invited_by""",
                  (board_id, target_username, role, username, now))
        members = list_canvas_members(c, board_id)
        c.commit()
        return members, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def remove_canvas_member(username, board_id, member_username):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        access, board = canvas_role_and_board(c, username, board_id)
        if not access:
            c.rollback()
            return None, "not_found"
        if access != "owner":
            c.rollback()
            return None, "forbidden"
        c.execute("DELETE FROM canvas_members WHERE board_id=? AND username=?", (board_id, member_username))
        members = list_canvas_members(c, board_id)
        c.commit()
        return members, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def delete_canvas_board(username, board_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        access, board = canvas_role_and_board(c, username, board_id)
        if not access:
            c.rollback()
            return False, "not_found"
        if access != "owner":
            c.rollback()
            return False, "forbidden"
        c.execute("DELETE FROM canvas_members WHERE board_id=?", (board_id,))
        c.execute("DELETE FROM canvas_ops WHERE board_id=?", (board_id,))
        c.execute("DELETE FROM canvas_presence WHERE board_id=?", (board_id,))
        c.execute("DELETE FROM canvas_boards WHERE id=?", (board_id,))
        c.commit()
        return True, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def get_points_row(username, c=None):
    own = c is None
    if own: c = db()
    row = c.execute("SELECT id, username, points FROM users WHERE username=?", (username,)).fetchone()
    if own: c.close()
    return row

SYSTEM_ACTOR = "system"   # points_audit.who_admin：非管理员操作（任务扣点/退点）用它，与人工加减点区分


def _write_audit(c, who_admin, username, delta, before, after, reason):
    """在【同一个事务里】写审计流水。分开写会出现「扣了点但审计没记」或反过来。"""
    c.execute(
        "INSERT INTO points_audit(who_admin, username, delta, before_points, after_points, reason, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (who_admin, username, delta, before, after, (reason or "")[:120], int(time.time())))


def deduct_points(username, amount, reason=""):
    """任务提交时预扣点。reason 形如 'job:collect#1354'，由调用方传入。

    在补上审计之前，points_audit 只记录管理员加减点和充值审批 —— 任务扣点/退点完全隐形，
    对账时无法追溯「这个用户的点数为什么少了」。那 21 条「既退点又出结果」的僵尸记录
    (280 点)也因此没法核。
    """
    amount = int(amount or 0)
    if amount < 0:
        raise ValueError("amount must be >= 0")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        before_row = get_points_row(username, c)
        if not before_row:
            c.rollback()
            return None, "not_found"
        before = int(before_row["points"] or 0)
        if amount:
            cur = c.execute(
                "UPDATE users SET points = points - ? WHERE username=? AND points >= ?",
                (amount, username, amount),
            )
            if cur.rowcount != 1:
                c.rollback()
                return None, "insufficient"
        row = get_points_row(username, c)
        if not row:
            c.rollback()
            return None, "not_found"
        if amount:
            _write_audit(c, SYSTEM_ACTOR, username, -amount, before, int(row["points"] or 0), reason)
        c.commit()
        return public_points(row), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def refund_points(username, amount, reason=""):
    """任务失败/超时后退点。reason 同 deduct_points。"""
    amount = int(amount or 0)
    if amount < 0:
        raise ValueError("amount must be >= 0")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        before_row = get_points_row(username, c)
        if not before_row:
            c.rollback()
            return None, "not_found"
        before = int(before_row["points"] or 0)
        if amount:
            cur = c.execute("UPDATE users SET points = points + ? WHERE username=?", (amount, username))
            if cur.rowcount != 1:
                c.rollback()
                return None, "not_found"
        row = get_points_row(username, c)
        if not row:
            c.rollback()
            return None, "not_found"
        if amount:
            _write_audit(c, SYSTEM_ACTOR, username, amount, before, int(row["points"] or 0), reason)
        c.commit()
        return public_points(row), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def public_admin_user(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "points": row["points"],
        "role": row["role"],
        "must_change": bool(row["must_change"]),
        "created_at": row["created_at"],
    }

def list_admin_users(query="", sort="created_at", direction="desc", limit=100):
    allowed_sort = {"username", "display_name", "points", "role", "must_change", "created_at"}
    sort = sort if sort in allowed_sort else "created_at"
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    limit = max(1, min(300, int(limit or 100)))
    query = (query or "").strip()
    sql = "SELECT id, username, display_name, points, role, must_change, created_at FROM users"
    args = []
    if query:
        sql += " WHERE username LIKE ? OR display_name LIKE ?"
        like = "%" + query + "%"
        args.extend([like, like])
    sql += " ORDER BY %s %s LIMIT ?" % (sort, direction)
    args.append(limit)
    c = db()
    try:
        rows = c.execute(sql, args).fetchall()
        total = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        return {"items": [public_admin_user(r) for r in rows], "total": total}
    finally:
        c.close()

def adjust_points_admin(who_admin, username, delta, reason=""):
    username = (username or "").strip()
    delta = int(delta or 0)
    reason = (reason or "").strip()[:300]
    if not username:
        return None, "missing_username"
    if delta == 0:
        return None, "zero_delta"
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT id, username, points FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            c.rollback()
            return None, "not_found"
        before = int(row["points"] or 0)
        after = before + delta
        if after < 0:
            c.rollback()
            return {"before": before, "after": before}, "insufficient"
        c.execute("UPDATE users SET points=? WHERE username=?", (after, username))
        now = int(time.time())
        c.execute(
            """INSERT INTO points_audit(who_admin, username, delta, before_points, after_points, reason, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (who_admin, username, delta, before, after, reason, now),
        )
        c.commit()
        return {
            "username": username,
            "points": after,
            "before": before,
            "after": after,
            "delta": delta,
            "reason": reason,
            "at": now,
        }, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def list_points_audit(username="", limit=100, actor=""):
    """actor='admin' 只看人工加减点/充值审批，'system' 只看任务扣退点，''(默认) 全看。

    任务流水接入后，条数远多于人工操作，不过滤的话后台第一页会被任务刷屏。
    """
    limit = max(1, min(300, int(limit or 100)))
    username = (username or "").strip()
    sql = """SELECT id, who_admin, username, delta, before_points, after_points, reason, created_at
             FROM points_audit"""
    where, args = [], []
    if username:
        where.append("username=?")
        args.append(username)
    if actor == "admin":
        where.append("who_admin<>?")
        args.append(SYSTEM_ACTOR)
    elif actor == "system":
        where.append("who_admin=?")
        args.append(SYSTEM_ACTOR)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    c = db()
    try:
        rows = c.execute(sql, args).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        c.close()

def public_recharge_order(row):
    return {
        "order_id": row["order_id"],
        "username": row["username"],
        "amount": row["amount"],
        "points": row["points"],
        "status": row["status"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "reviewed_by": row["reviewed_by"] or "",
        "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"] or "",
    }

def create_recharge_order(username, amount, points, note=""):
    username = (username or "").strip()
    amount = float(amount or 0)
    points = int(points or 0)
    note = (note or "").strip()[:300]
    if not username:
        return None, "missing_username"
    if amount <= 0:
        return None, "amount_invalid"
    if points <= 0:
        return None, "points_invalid"
    now = int(time.time())
    order_id = "R%d%s" % (now, secrets.token_hex(3).upper())
    c = db()
    try:
        c.execute(
            """INSERT INTO recharge_orders(order_id, username, amount, points, status, note, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (order_id, username, amount, points, "pending", note, now),
        )
        c.commit()
        row = c.execute("SELECT * FROM recharge_orders WHERE order_id=?", (order_id,)).fetchone()
        return public_recharge_order(row), None
    finally:
        c.close()

def list_recharge_orders(username="", status="", limit=100):
    username = (username or "").strip()
    status = (status or "").strip()
    limit = max(1, min(300, int(limit or 100)))
    sql = "SELECT * FROM recharge_orders"
    args = []
    where = []
    if username:
        where.append("username=?")
        args.append(username)
    if status:
        where.append("status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, order_id DESC LIMIT ?"
    args.append(limit)
    c = db()
    try:
        rows = c.execute(sql, args).fetchall()
        return {"items": [public_recharge_order(r) for r in rows]}
    finally:
        c.close()

def review_recharge_order(who_admin, order_id, action, reason=""):
    who_admin = (who_admin or "").strip()
    order_id = (order_id or "").strip()
    action = (action or "").strip().lower()
    reason = (reason or "").strip()[:300]
    if action not in {"approve", "reject"}:
        return None, "bad_action"
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        order = c.execute("SELECT * FROM recharge_orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            c.rollback()
            return None, "not_found"
        if order["status"] != "pending":
            c.rollback()
            return public_recharge_order(order), "already_reviewed"
        now = int(time.time())
        if action == "approve":
            user = c.execute("SELECT id, username, points FROM users WHERE username=?", (order["username"],)).fetchone()
            if not user:
                c.rollback()
                return None, "user_not_found"
            before = int(user["points"] or 0)
            delta = int(order["points"] or 0)
            after = before + delta
            c.execute("UPDATE users SET points=? WHERE username=?", (after, order["username"]))
            c.execute(
                """INSERT INTO points_audit(who_admin, username, delta, before_points, after_points, reason, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (who_admin, order["username"], delta, before, after, "充值审批: %s %s" % (order_id, reason), now),
            )
            status = "approved"
        else:
            status = "rejected"
        c.execute(
            """UPDATE recharge_orders SET status=?, reviewed_by=?, reviewed_at=?, review_note=?
               WHERE order_id=?""",
            (status, who_admin, now, reason, order_id),
        )
        row = c.execute("SELECT * FROM recharge_orders WHERE order_id=?", (order_id,)).fetchone()
        c.commit()
        return public_recharge_order(row), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def get_recharge_order(order_id):
    order_id = (order_id or "").strip()
    if not order_id:
        return None
    c = db()
    try:
        return c.execute("SELECT * FROM recharge_orders WHERE order_id=?", (order_id,)).fetchone()
    finally:
        c.close()

def set_recharge_transaction(order_id, transaction_id, pay_channel):
    """回填微信流水号/支付方式。与加点分开——重复回调也可安全补写，不影响幂等。"""
    c = db()
    try:
        c.execute("UPDATE recharge_orders SET transaction_id=?, pay_channel=? WHERE order_id=?",
                  ((transaction_id or "").strip(), (pay_channel or "").strip(), (order_id or "").strip()))
        c.commit()
    finally:
        c.close()

def cleanup_expired_tokens(c=None):
    own = c is None
    if own: c = db()
    c.execute("DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at <= ?", (int(time.time()),))
    if own:
        c.commit(); c.close()

def issue_token(username, c=None):
    own = c is None
    if own: c = db()
    cleanup_expired_tokens(c)
    tok = secrets.token_urlsafe(32)
    c.execute("INSERT INTO tokens(token,username,expires_at) VALUES(?,?,?)",
              (tok, username, int(time.time()) + TOKEN_TTL))
    if own:
        c.commit(); c.close()
    return tok

def bearer_token(auth):
    parts = (auth or "").strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()

def cookie_token(header):
    try:
        jar = cookies.SimpleCookie()
        jar.load(header or "")
        morsel = jar.get(AUTH_COOKIE_NAME)
        return morsel.value.strip() if morsel and morsel.value else ""
    except Exception:
        return ""

def request_token(headers):
    token = bearer_token(headers.get("Authorization"))
    if token and token != "__cookie__":
        return token
    return cookie_token(headers.get("Cookie"))

def auth_cookie_header(token):
    parts = [f"{AUTH_COOKIE_NAME}={token}", "Path=/", f"Max-Age={TOKEN_TTL}", "HttpOnly", "SameSite=Lax"]
    if AUTH_COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)

def clear_auth_cookie_header():
    parts = [f"{AUTH_COOKIE_NAME}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
    if AUTH_COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _body(self):
        self._json_error = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json_error = True
            return {}
    def _content_length_exceeds(self, limit):
        try:
            return int(self.headers.get("Content-Length") or 0) > int(limit)
        except Exception:
            return False
    def _bad_json(self):
        return getattr(self, "_json_error", False)
    def _client_ip(self):
        # 限流真实 IP：只信 nginx 下发的 X-Real-IP(=$remote_addr，客户端发的会被 nginx 覆盖)。
        # 绝不取 X-Forwarded-For 首段——那是客户端可控的，伪造+轮换即可让限流 key 每次都变、
        # 对任意账号无限撞密码/批量刷号(#189)。auth 只监听 127.0.0.1、外部必经 nginx，故 X-Real-IP 可信。
        xr = (self.headers.get("X-Real-IP") or "").strip()
        if xr:
            return xr
        # 回退：XFF 最后一跳(=nginx 追加的 $remote_addr，仍不取客户端可控的首段)
        parts = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        if parts:
            return parts[-1]
        return self.client_address[0] if self.client_address else ""
    def _rate_key(self, username):
        return self._client_ip() + "|" + (username or "")
    def _login_limited(self, username):
        now = time.time()
        key = self._rate_key(username)
        LOGIN_FAILS[key] = [t for t in LOGIN_FAILS.get(key, []) if now - t < LOGIN_FAIL_WINDOW]
        return len(LOGIN_FAILS[key]) >= LOGIN_FAIL_MAX
    def _record_login_failure(self, username):
        now = time.time()
        key = self._rate_key(username)
        LOGIN_FAILS[key] = [t for t in LOGIN_FAILS.get(key, []) if now - t < LOGIN_FAIL_WINDOW]
        LOGIN_FAILS[key].append(now)
    def _clear_login_failures(self, username):
        LOGIN_FAILS.pop(self._rate_key(username), None)
    def _register_limited(self):
        now = time.time()
        key = self._client_ip()
        REGISTER_HITS[key] = [t for t in REGISTER_HITS.get(key, []) if now - t < REGISTER_WINDOW]
        return len(REGISTER_HITS[key]) >= REGISTER_MAX
    def _record_register_hit(self):
        now = time.time()
        key = self._client_ip()
        REGISTER_HITS[key] = [t for t in REGISTER_HITS.get(key, []) if now - t < REGISTER_WINDOW]
        REGISTER_HITS[key].append(now)
    def _user(self):
        tok = request_token(self.headers)
        if not tok:
            return None
        c = db()
        r = c.execute("""SELECT u.* FROM tokens t JOIN users u ON u.username=t.username
                         WHERE t.token=? AND (t.expires_at IS NULL OR t.expires_at > ?)""",
                      (tok, int(time.time()))).fetchone()
        c.close()
        return r
    def _internal_auth(self):
        if not INTERNAL_TOKEN:
            return False
        token = self.headers.get("X-HQ-Internal-Token") or ""
        return secrets.compare_digest(token, INTERNAL_TOKEN)

    def _require_internal(self):
        if self._internal_auth():
            return True
        self._send(403, {"detail": "forbidden"})
        return False

    def _require_admin_user(self):
        row = self._user()
        if not row:
            self._send(401, {"detail": "未登录或登录已过期"})
            return None
        if row["role"] != "admin":
            self._send(403, {"detail": "需要管理员权限"})
            return None
        return row

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/auth/admin/points/adjust":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            username = (d.get("username") or "").strip()
            reason = (d.get("reason") or "").strip()
            try:
                delta = int(d.get("delta") or 0)
            except Exception:
                return self._send(400, {"detail": "delta must be an integer"})
            if not username:
                return self._send(400, {"detail": "missing username"})
            if delta == 0:
                return self._send(400, {"detail": "delta cannot be 0"})
            try:
                result, err = adjust_points_admin(admin["username"], username, delta, reason)
                if err == "not_found":
                    return self._send(404, {"detail": "user not found"})
                if err == "insufficient":
                    return self._send(400, {"detail": "点数不能扣成负数", "user": result})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "adjustment": result})
            except Exception:
                return self._send(500, {"detail": "points adjust failed"})
        if p == "/api/auth/admin/recharge/review":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                order, err = review_recharge_order(
                    admin["username"],
                    d.get("order_id"),
                    d.get("action"),
                    d.get("reason") or d.get("review_note") or "",
                )
                if err == "not_found":
                    return self._send(404, {"detail": "order not found"})
                if err == "user_not_found":
                    return self._send(404, {"detail": "user not found"})
                if err == "already_reviewed":
                    return self._send(409, {"detail": "订单已审批", "order": order})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "order": order})
            except Exception:
                return self._send(500, {"detail": "recharge review failed"})
        if p == "/api/auth/recharge/order":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            amount = d.get("amount")
            points = recharge_points_for(amount)   # 点数服务端算,绝不信客户端(与 wxpay 路由一致)
            if points is None:
                return self._send(400, {"detail": "无效的充值金额(固定档 99/199/499，或自定义 10~5000 元整数)"})
            amount = int(amount)
            try:
                order, err = create_recharge_order(row["username"], amount, points, d.get("note") or "")
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "order": order})
            except Exception:
                return self._send(500, {"detail": "充值申请提交失败"})
        if p == "/api/auth/wxpay/native":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"detail": "微信支付未配置"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            amount = d.get("amount")                 # 客户端只传金额(元)
            points = recharge_points_for(amount)     # 点数服务端算,不信客户端
            if points is None:
                return self._send(400, {"detail": "无效的充值金额(固定档 99/199/499，或自定义 10~5000 元整数)"})
            amount = int(amount)
            try:
                order, err = create_recharge_order(row["username"], amount, points, "微信扫码充值")
                if err:
                    return self._send(400, {"detail": err})
                code_url = wxpay.create_native(
                    order["order_id"], "黄雀点数充值 %d点" % points, amount * 100)
                return self._send(200, {"ok": True, "order": order, "code_url": code_url})
            except Exception as e:
                # 下单失败:订单停留在 pending(等同一个没人审的人工申请),无害
                return self._send(502, {"detail": "微信下单失败", "error": str(e)[:200]})
        if p == "/api/auth/wxpay/jsapi":
            # 小程序内充值:需登录态(定位黄雀账号) + wx.login 的 js_code(换微信 openid)
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"detail": "微信支付未配置"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            js_code = (d.get("js_code") or "").strip()
            if not js_code:
                return self._send(400, {"detail": "缺少 js_code"})
            amount = d.get("amount")                 # 客户端只传金额(元)
            points = recharge_points_for(amount)     # 点数服务端算,不信客户端
            if points is None:
                return self._send(400, {"detail": "无效的充值金额(固定档 99/199/499，或自定义 10~5000 元整数)"})
            amount = int(amount)
            try:
                openid = wxpay.jscode2session(js_code)
                order, err = create_recharge_order(row["username"], amount, points, "微信小程序充值")
                if err:
                    return self._send(400, {"detail": err})
                prepay_id = wxpay.create_jsapi(
                    order["order_id"], "黄雀点数充值 %d点" % points, amount * 100, openid)
                pay = wxpay.jsapi_pay_params(prepay_id)   # 客户端 wx.requestPayment 参数
                return self._send(200, {"ok": True, "order": order, "pay": pay})
            except Exception as e:
                return self._send(502, {"detail": "微信下单失败", "error": str(e)[:200]})
        if p == "/api/auth/wxpay/notify":
            # 微信服务器回调:不带登录态/内部 token,靠 V3 签名验真。必须读原始字节验签。
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"code": "FAIL", "message": "not configured"})
            if not wxpay.verify_notify(self.headers, raw):
                return self._send(401, {"code": "FAIL", "message": "签名验证失败"})
            try:
                resource = wxpay.decrypt_resource(json.loads(raw or b"{}")["resource"])
            except Exception:
                return self._send(400, {"code": "FAIL", "message": "解密失败"})
            if resource.get("trade_state") != "SUCCESS":
                return self._send(200, {"code": "SUCCESS"})   # 非成功态,确认收到即可,不加点
            order_id = (resource.get("out_trade_no") or "").strip()
            txn_id = (resource.get("transaction_id") or "").strip()
            paid_total = (resource.get("amount") or {}).get("total")
            order_row = get_recharge_order(order_id)
            if not order_row:
                return self._send(200, {"code": "SUCCESS"})   # 未知订单,回200止重推,不加点
            # 金额核对(防篡改):实付分数须等于订单金额*100
            if paid_total != int(round(float(order_row["amount"]) * 100)):
                return self._send(200, {"code": "SUCCESS"})   # 金额不符,不加点
            try:
                # review_recharge_order 自带幂等:重复回调因 status 已 approved 返回 already_reviewed,不重复加点
                review_recharge_order("wxpay", order_id, "approve", "wxpay txn=%s" % txn_id)
                set_recharge_transaction(order_id, txn_id, "wxpay")
                return self._send(200, {"code": "SUCCESS"})
            except Exception:
                return self._send(500, {"code": "FAIL", "message": "处理失败"})   # 抛错让微信重推
        if p in {"/api/auth/points/deduct", "/api/auth/points/refund"}:
            if not self._require_internal():
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            username = (d.get("username") or "").strip()
            try:
                amount = int(d.get("amount") or 0)
            except Exception:
                return self._send(400, {"detail": "amount must be an integer"})
            if not username:
                return self._send(400, {"detail": "missing username"})
            if amount < 0:
                return self._send(400, {"detail": "amount must be >= 0"})
            reason = str(d.get("reason") or "")   # 形如 job:collect#1354；老调用方不传就留空
            try:
                if p.endswith("/deduct"):
                    points, err = deduct_points(username, amount, reason)
                    if err == "insufficient":
                        return self._send(402, {"detail": "点数不足", "need": amount})
                else:
                    points, err = refund_points(username, amount, reason)
                if err == "not_found":
                    return self._send(404, {"detail": "user not found"})
                return self._send(200, {"ok": True, "points": points["points"], "user": points})
            except Exception:
                return self._send(500, {"detail": "points update failed"})
        if p == "/api/auth/register":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            name = (d.get("display_name") or u).strip() or u
            if self._register_limited():
                return self._send(429, {"detail": "注册次数过多，请稍后再试"})
            if not u or not pw:
                return self._send(400, {"detail": "请填写账号和密码"})
            if len(u) > 64:
                return self._send(400, {"detail": "账号最多 64 位"})
            if len(name) > 32:
                return self._send(400, {"detail": "昵称最多 32 个字符"})
            if any(ch.isspace() for ch in u):
                return self._send(400, {"detail": "账号不能包含空白字符"})
            if len(pw) < 6:
                return self._send(400, {"detail": "密码至少 6 位"})
            salt = secrets.token_hex(16)
            c = db()
            try:
                account_id = _new_unique_account_id(c)
                c.execute("""INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)
                             VALUES(?,?,?,?,?,?,0,?)""",
                          (u, hash_pw(pw, salt), salt, name, NEW_USER_TRIAL_POINTS, "member", account_id))
                tok = issue_token(u, c)
                c.commit()
                self._record_register_hit()
            except sqlite3.IntegrityError:
                c.rollback()
                return self._send(409, {"detail": "账号已存在"})
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "注册失败"})
            finally:
                c.close()
            return self._send(200, {"user": public_user(u, name, NEW_USER_TRIAL_POINTS, account_id=account_id)}, {"Set-Cookie": auth_cookie_header(tok)})
        if p == "/api/auth/login":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            if self._login_limited(u):
                return self._send(429, {"detail": "登录失败次数过多，请稍后再试"})
            c = db(); row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); c.close()
            if not row or hash_pw(pw, row["pw_salt"]) != row["pw_hash"]:
                self._record_login_failure(u)
                return self._send(401, {"detail": "账号或密码错误"})
            self._clear_login_failures(u)
            account_id = row["account_id"] or ensure_account_id(u)
            tok = issue_token(u)
            return self._send(200, {"user": {
                "username": u, "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"]),
                "account_id": account_id}}, {"Set-Cookie": auth_cookie_header(tok)})
        if p == "/api/auth/miniprogram-login":
            # 小程序 wx.request 不像浏览器那样自动带 httpOnly cookie，专供小程序客户端：
            # token 放响应体让小程序自己存起来，后续走 Authorization: Bearer 请求头（网站登录/注册不受影响，仍只走 cookie）。
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            if self._login_limited(u):
                return self._send(429, {"detail": "登录失败次数过多，请稍后再试"})
            c = db(); row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); c.close()
            if not row or hash_pw(pw, row["pw_salt"]) != row["pw_hash"]:
                self._record_login_failure(u)
                return self._send(401, {"detail": "账号或密码错误"})
            self._clear_login_failures(u)
            account_id = row["account_id"] or ensure_account_id(u)
            tok = issue_token(u)
            return self._send(200, {"token": tok, "user": {
                "username": u, "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"]),
                "account_id": account_id}})
        if p == "/api/auth/miniprogram-register":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            name = (d.get("display_name") or u).strip() or u
            if self._register_limited():
                return self._send(429, {"detail": "注册次数过多，请稍后再试"})
            if not u or not pw:
                return self._send(400, {"detail": "请填写账号和密码"})
            if len(u) > 64:
                return self._send(400, {"detail": "账号最多 64 位"})
            if len(name) > 32:
                return self._send(400, {"detail": "昵称最多 32 个字符"})
            if any(ch.isspace() for ch in u):
                return self._send(400, {"detail": "账号不能包含空白字符"})
            if len(pw) < 6:
                return self._send(400, {"detail": "密码至少 6 位"})
            salt = secrets.token_hex(16)
            c = db()
            try:
                account_id = _new_unique_account_id(c)
                c.execute("""INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)
                             VALUES(?,?,?,?,?,?,0,?)""",
                          (u, hash_pw(pw, salt), salt, name, NEW_USER_TRIAL_POINTS, "member", account_id))
                tok = issue_token(u, c)
                c.commit()
                self._record_register_hit()
            except sqlite3.IntegrityError:
                c.rollback()
                return self._send(409, {"detail": "账号已存在"})
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "注册失败"})
            finally:
                c.close()
            return self._send(200, {"token": tok, "user": public_user(u, name, NEW_USER_TRIAL_POINTS, account_id=account_id)})
        if p == "/api/auth/logout":
            clear_cookie = {"Set-Cookie": clear_auth_cookie_header()}
            tok = request_token(self.headers)
            if not tok:
                return self._send(200, {"ok": True}, clear_cookie)
            if not tok:
                return self._send(401, {"detail": "未登录"})
            if tok in REVOKED_TOKENS:
                return self._send(200, {"ok": True}, clear_cookie)
            c = db()
            cur = c.execute("DELETE FROM tokens WHERE token=?", (tok,))
            c.commit(); c.close()
            if cur.rowcount < 1:
                return self._send(200, {"ok": True}, clear_cookie)
            if cur.rowcount < 1:
                return self._send(401, {"detail": "未登录"})
            REVOKED_TOKENS.add(tok)
            return self._send(200, {"ok": True}, clear_cookie)
        if p == "/api/auth/change_password":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            oldp = d.get("old_password") or ""
            newp = d.get("new_password") or ""
            if not oldp: return self._send(400, {"detail": "请填写当前密码"})
            if len(newp) < 6: return self._send(400, {"detail": "新密码至少 6 位"})
            salt = secrets.token_hex(16)
            c = db()
            try:
                fresh = c.execute("SELECT pw_hash,pw_salt FROM users WHERE username=?", (row["username"],)).fetchone()
                if not fresh or hash_pw(oldp, fresh["pw_salt"]) != fresh["pw_hash"]:
                    return self._send(400, {"detail": "当前密码不正确"})
                c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change=0 WHERE username=?",
                          (hash_pw(newp, salt), salt, row["username"]))
                c.execute("DELETE FROM tokens WHERE username=?", (row["username"],))
                c.commit()
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "修改密码失败"})
            finally:
                c.close()
            return self._send(200, {"ok": True, "reauth": True})
        if p == "/api/auth/profile":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            name = d.get("display_name")
            if not isinstance(name, str):
                return self._send(400, {"detail": "请填写昵称"})
            name = name.strip()
            if not name:
                return self._send(400, {"detail": "昵称不能为空"})
            if len(name) > 32:
                return self._send(400, {"detail": "昵称最多 32 个字符"})
            if any(ord(ch) < 32 for ch in name):
                return self._send(400, {"detail": "昵称不能包含控制字符"})
            c = db()
            try:
                c.execute("UPDATE users SET display_name=? WHERE username=?", (name, row["username"]))
                fresh = c.execute("SELECT * FROM users WHERE username=?", (row["username"],)).fetchone()
                c.commit()
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "保存昵称失败"})
            finally:
                c.close()
            return self._send(200, {"ok": True, "user": public_user(
                fresh["username"], fresh["display_name"], fresh["points"], fresh["role"], fresh["must_change"],
                fresh["account_id"] or ensure_account_id(fresh["username"])
            )})
        if p == "/api/auth/canvas/boards":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "request body must be valid JSON"})
            try:
                board, err = create_canvas_board(row["username"], d)
                if err == "bad_name":
                    return self._send(400, {"detail": "画布名称不可包含控制字符"})
                if err == "bad_data":
                    return self._send(400, {"detail": "画布数据格式不正确"})
                if err == "too_large":
                    return self._send(413, {"detail": "画布数据过大"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "board": board})
            except Exception:
                return self._send(500, {"detail": "canvas create failed"})
        canvas_prefix = "/api/auth/canvas/boards/"
        if p.startswith(canvas_prefix) and p.endswith("/save"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):-len("/save")])
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "request body must be valid JSON"})
            try:
                board, err = save_canvas_board(row["username"], board_id, d)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err == "forbidden":
                    return self._send(403, {"detail": "没有编辑权限"})
                if err == "bad_version":
                    return self._send(400, {"detail": "缺少画布版本号"})
                if err == "conflict":
                    return self._send(409, {"detail": "画布已被其他成员更新", "board": board})
                if err == "bad_name":
                    return self._send(400, {"detail": "画布名称不可包含控制字符"})
                if err == "bad_data":
                    return self._send(400, {"detail": "画布数据格式不正确"})
                if err == "too_large":
                    return self._send(413, {"detail": "画布数据过大"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "board": board})
            except Exception:
                return self._send(500, {"detail": "canvas save failed"})
        if p.startswith(canvas_prefix) and p.endswith("/ops"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):-len("/ops")])
            if self._content_length_exceeds(CANVAS_OPS_MAX_BYTES):
                return self._send(413, {"detail": "画布操作数据过大"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "request body must be valid JSON"})
            try:
                result, err = apply_canvas_ops(row["username"], board_id, d)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err == "forbidden":
                    return self._send(403, {"detail": "没有编辑权限"})
                if err in {"too_many_ops", "too_large"}:
                    return self._send(413, {"detail": "画布操作数据过大"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, **result})
            except Exception:
                return self._send(500, {"detail": "canvas ops failed"})
        if p.startswith(canvas_prefix) and p.endswith("/presence"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):-len("/presence")])
            if self._content_length_exceeds(CANVAS_PRESENCE_MAX_BYTES):
                return self._send(413, {"detail": "在线状态数据过大"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "request body must be valid JSON"})
            try:
                result, err = record_canvas_presence(row["username"], board_id, d)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, **result})
            except Exception:
                return self._send(500, {"detail": "canvas presence failed"})
        if p.startswith(canvas_prefix) and p.endswith("/members"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):-len("/members")])
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "request body must be valid JSON"})
            try:
                members, err = add_canvas_member(row["username"], board_id, d)
                if err == "missing":
                    return self._send(400, {"detail": "请填写账号 ID"})
                if err == "bad_role":
                    return self._send(400, {"detail": "协作权限不正确"})
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err == "forbidden":
                    return self._send(403, {"detail": "只有画布创建者可以邀请成员"})
                if err == "user_not_found":
                    return self._send(404, {"detail": "账号 ID 不存在"})
                if err == "self":
                    return self._send(400, {"detail": "不能邀请自己"})
                if err == "not_friend":
                    return self._send(403, {"detail": "只能邀请已添加的好友"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "members": members})
            except Exception:
                return self._send(500, {"detail": "canvas member update failed"})
        if p in {"/api/auth/friends/request", "/api/auth/friends/add"}:
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            requests, err = create_friend_request(row["username"], d.get("account_id"))
            if err == "missing":
                return self._send(400, {"detail": "请填写账号 ID"})
            if err == "not_found":
                return self._send(404, {"detail": "账号 ID 不存在"})
            if err == "self":
                return self._send(400, {"detail": "不能添加自己"})
            if err == "already_friends":
                return self._send(409, {"detail": "已经是好友"})
            if err == "pending":
                return self._send(409, {"detail": "好友申请已发送"})
            if err == "incoming_pending":
                return self._send(409, {"detail": "对方已发来好友申请，请先处理"})
            return self._send(200, {"ok": True, "requests": requests})
        if p == "/api/auth/friend-requests/respond":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            data, err = respond_friend_request(row["username"], d.get("request_id"), d.get("action"))
            if err == "missing":
                return self._send(400, {"detail": "缺少申请 ID"})
            if err == "bad_action":
                return self._send(400, {"detail": "操作必须是 accept 或 reject"})
            if err == "not_found":
                return self._send(404, {"detail": "好友申请不存在"})
            return self._send(200, {"ok": True, **data})
        self._send(404, {"detail": "not found"})

    def do_DELETE(self):
        p = self.path.split("?")[0]
        canvas_prefix = "/api/auth/canvas/boards/"
        row = self._user()
        if not row:
            return self._send(401, {"detail": "未登录"})
        if p.startswith(canvas_prefix) and "/members/" in p[len(canvas_prefix):]:
            rest = p[len(canvas_prefix):]
            board_id, member_username = rest.split("/members/", 1)
            board_id = urllib.parse.unquote(board_id)
            member_username = urllib.parse.unquote(member_username)
            try:
                members, err = remove_canvas_member(row["username"], board_id, member_username)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err == "forbidden":
                    return self._send(403, {"detail": "只有画布创建者可以移除成员"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "members": members})
            except Exception:
                return self._send(500, {"detail": "canvas member delete failed"})
        if p.startswith(canvas_prefix):
            board_id = urllib.parse.unquote(p[len(canvas_prefix):])
            try:
                ok, err = delete_canvas_board(row["username"], board_id)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err == "forbidden":
                    return self._send(403, {"detail": "只有画布创建者可以删除"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": bool(ok)})
            except Exception:
                return self._send(500, {"detail": "canvas delete failed"})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/auth/admin/users":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = list_admin_users(
                    query=(q.get("q") or [""])[0],
                    sort=(q.get("sort") or ["created_at"])[0],
                    direction=(q.get("dir") or ["desc"])[0],
                    limit=(q.get("limit") or ["100"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except Exception:
                return self._send(500, {"detail": "users query failed"})
        if p == "/api/auth/admin/points/audit":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = list_points_audit(
                    username=(q.get("username") or [""])[0],
                    limit=(q.get("limit") or ["100"])[0],
                    actor=(q.get("actor") or [""])[0],
                )
                return self._send(200, {"ok": True, **data})
            except Exception:
                return self._send(500, {"detail": "audit query failed"})
        if p == "/api/auth/admin/recharge/orders":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = list_recharge_orders(
                    username=(q.get("username") or [""])[0],
                    status=(q.get("status") or [""])[0],
                    limit=(q.get("limit") or ["100"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except Exception:
                return self._send(500, {"detail": "recharge orders query failed"})
        if p == "/api/auth/recharge/orders":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = list_recharge_orders(
                    username=row["username"],
                    status=(q.get("status") or [""])[0],
                    limit=(q.get("limit") or ["50"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except Exception:
                return self._send(500, {"detail": "充值申请查询失败"})
        if p == "/api/auth/points":
            if not self._require_internal():
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            username = (q.get("username", [""])[0] or "").strip()
            if not username:
                return self._send(400, {"detail": "missing username"})
            row = get_points_row(username)
            if not row:
                return self._send(404, {"detail": "user not found"})
            return self._send(200, {"ok": True, "points": row["points"], "user": public_points(row)})
        if p == "/api/auth/me":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            account_id = row["account_id"] or ensure_account_id(row["username"])
            return self._send(200, {"user": {
                "username": row["username"], "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"]),
                "account_id": account_id}})
        if p == "/api/auth/friends":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            return self._send(200, {"ok": True, "friends": list_friends(row["username"])})
        if p == "/api/auth/friend-requests":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            return self._send(200, {"ok": True, **list_friend_requests(row["username"])})
        if p == "/api/auth/canvas/boards":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            try:
                boards, err = list_canvas_boards(row["username"])
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "boards": boards})
            except Exception:
                return self._send(500, {"detail": "canvas list failed"})
        canvas_prefix = "/api/auth/canvas/boards/"
        if p.startswith(canvas_prefix) and p.endswith("/sync"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):-len("/sync")])
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                since_version = int((query.get("since") or [""])[0])
            except Exception:
                return self._send(400, {"detail": "bad_since"})
            if since_version < 1:
                return self._send(400, {"detail": "bad_since"})
            try:
                result, err = sync_canvas_ops(row["username"], board_id, since_version)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, **result})
            except Exception:
                return self._send(500, {"detail": "canvas sync failed"})
        if p.startswith(canvas_prefix):
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):])
            try:
                board, err = get_canvas_board(row["username"], board_id)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "board": board})
            except Exception:
                return self._send(500, {"detail": "canvas read failed"})
        if p == "/api/auth/health":
            return self._send(200, {"ok": True, "service": "huangque-auth"})
        self._send(404, {"detail": "not found"})

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "create-user":
        pts = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        role = sys.argv[5] if len(sys.argv) > 5 else 'member'
        create_user(sys.argv[2], sys.argv[3], pts, role)
        sys.exit(0)
    init_db()
    print("huangque-auth on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
