#!/usr/bin/env python3
# 黄雀 AI · 独立认证服务（零依赖，标准库）
# 端口 127.0.0.1:8095，nginx 把 /api/auth/ 路由过来。与 leadgen(8090) 完全隔离。
import sqlite3, hashlib, secrets, json, os, sys, time, urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
LOGIN_FAILS = {}
REGISTER_HITS = {}
REVOKED_TOKENS = set()

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
    c.commit(); c.close()

def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITER).hex()

def create_user(username, password, points=0, role='member'):
    init_db()
    salt = secrets.token_hex(16)
    c = db()
    c.execute("""INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change)
                 VALUES(?,?,?,?,?,?,1)
                 ON CONFLICT(username) DO UPDATE SET
                   pw_hash=excluded.pw_hash, pw_salt=excluded.pw_salt,
                   points=excluded.points, role=excluded.role, must_change=1""",
              (username, hash_pw(password, salt), salt, username, points, role))
    c.commit(); c.close()
    print("OK user:", username)

def public_user(username, display_name=None, points=0, role='member', must_change=False):
    return {
        "username": username,
        "name": display_name or username,
        "points": points,
        "role": role,
        "must_change": bool(must_change)
    }

def public_points(row):
    return {
        "username": row["username"],
        "user_id": row["id"],
        "points": row["points"],
    }

def get_points_row(username, c=None):
    own = c is None
    if own: c = db()
    row = c.execute("SELECT id, username, points FROM users WHERE username=?", (username,)).fetchone()
    if own: c.close()
    return row

def deduct_points(username, amount):
    amount = int(amount or 0)
    if amount < 0:
        raise ValueError("amount must be >= 0")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if amount:
            cur = c.execute(
                "UPDATE users SET points = points - ? WHERE username=? AND points >= ?",
                (amount, username, amount),
            )
            if cur.rowcount != 1:
                row = get_points_row(username, c)
                c.rollback()
                if row:
                    return None, "insufficient"
                return None, "not_found"
        row = get_points_row(username, c)
        if not row:
            c.rollback()
            return None, "not_found"
        c.commit()
        return public_points(row), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def refund_points(username, amount):
    amount = int(amount or 0)
    if amount < 0:
        raise ValueError("amount must be >= 0")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if amount:
            cur = c.execute("UPDATE users SET points = points + ? WHERE username=?", (amount, username))
            if cur.rowcount != 1:
                c.rollback()
                return None, "not_found"
        row = get_points_row(username, c)
        if not row:
            c.rollback()
            return None, "not_found"
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

def list_points_audit(username="", limit=100):
    limit = max(1, min(300, int(limit or 100)))
    username = (username or "").strip()
    sql = """SELECT id, who_admin, username, delta, before_points, after_points, reason, created_at
             FROM points_audit"""
    args = []
    if username:
        sql += " WHERE username=?"
        args.append(username)
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
            try:
                order, err = create_recharge_order(row["username"], d.get("amount"), d.get("points"), d.get("note") or "")
                if err == "amount_invalid":
                    return self._send(400, {"detail": "充值金额必须大于 0"})
                if err == "points_invalid":
                    return self._send(400, {"detail": "充值点数必须大于 0"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "order": order})
            except Exception:
                return self._send(500, {"detail": "充值申请提交失败"})
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
            try:
                if p.endswith("/deduct"):
                    points, err = deduct_points(username, amount)
                    if err == "insufficient":
                        return self._send(402, {"detail": "点数不足", "need": amount})
                else:
                    points, err = refund_points(username, amount)
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
                c.execute("""INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change)
                             VALUES(?,?,?,?,?,?,0)""",
                          (u, hash_pw(pw, salt), salt, name, 0, "member"))
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
            return self._send(200, {"token": tok, "user": public_user(u, name)}, {"Set-Cookie": auth_cookie_header(tok)})
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
            tok = issue_token(u)
            return self._send(200, {"token": tok, "user": {
                "username": u, "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"])}}, {"Set-Cookie": auth_cookie_header(tok)})
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
            return self._send(200, {"user": {
                "username": row["username"], "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"])}})
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
