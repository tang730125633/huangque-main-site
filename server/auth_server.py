#!/usr/bin/env python3
# 黄雀 AI · 独立认证服务（零依赖，标准库）
# 端口 127.0.0.1:8095，nginx 把 /api/auth/ 路由过来。与 leadgen(8090) 完全隔离。
import sqlite3, hashlib, secrets, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
PORT = 8095
ITER = 200000
TOKEN_TTL = int(os.environ.get("HQ_AUTH_TOKEN_TTL", str(30 * 24 * 3600)))
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

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
        xf = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xf or (self.client_address[0] if self.client_address else "")
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
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:].strip()
        c = db()
        r = c.execute("""SELECT u.* FROM tokens t JOIN users u ON u.username=t.username
                         WHERE t.token=? AND (t.expires_at IS NULL OR t.expires_at > ?)""",
                      (tok, int(time.time()))).fetchone()
        c.close()
        return r

    def do_POST(self):
        p = self.path.split("?")[0]
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
            return self._send(200, {"token": tok, "user": public_user(u, name)})
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
                "role": row["role"], "must_change": bool(row["must_change"])}})
        if p == "/api/auth/logout":
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                return self._send(401, {"detail": "未登录"})
            tok = auth[7:].strip()
            if not tok:
                return self._send(401, {"detail": "未登录"})
            if tok in REVOKED_TOKENS:
                return self._send(200, {"ok": True})
            c = db()
            cur = c.execute("DELETE FROM tokens WHERE token=?", (tok,))
            c.commit(); c.close()
            if cur.rowcount < 1:
                return self._send(401, {"detail": "未登录"})
            REVOKED_TOKENS.add(tok)
            return self._send(200, {"ok": True})
        if p == "/api/auth/change_password":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            d = self._body(); newp = d.get("new_password") or ""
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            if len(newp) < 6: return self._send(400, {"detail": "新密码至少 6 位"})
            salt = secrets.token_hex(16)
            c = db(); c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change=0 WHERE username=?",
                                (hash_pw(newp, salt), salt, row["username"])); c.commit(); c.close()
            return self._send(200, {"ok": True})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
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
