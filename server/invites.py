#!/usr/bin/env python3
"""邀请注册领域逻辑；事务由调用方统一提交。"""
import datetime
import hashlib
import hmac
import os
import secrets
import time
from zoneinfo import ZoneInfo


CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6
VALID_SOURCES = {"web_link", "web_manual", "miniprogram"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DAILY_LIMIT = int(os.environ.get("HQ_INVITE_DAILY_LIMIT", "50"))
IP_REVIEW_THRESHOLD = int(os.environ.get("HQ_INVITE_IP_REVIEW_THRESHOLD", "3"))


class InviteError(Exception):
    def __init__(self, code, detail, http_status=400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status


def init_schema(conn, now=None):
    now = int(now or time.time())
    conn.execute("""CREATE TABLE IF NOT EXISTS invite_campaigns(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'enabled',
        start_at INTEGER,
        end_at INTEGER,
        code_required INTEGER NOT NULL DEFAULT 0,
        daily_invite_limit INTEGER NOT NULL DEFAULT 50,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS invite_codes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        inviter_user_id INTEGER NOT NULL,
        code TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_codes_active_user
                    ON invite_codes(campaign_id, inviter_user_id) WHERE status='active'""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invite_codes_lookup ON invite_codes(code, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_invites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        inviter_user_id INTEGER NOT NULL,
        invitee_user_id INTEGER NOT NULL UNIQUE,
        invite_code TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'bound',
        risk_status TEXT NOT NULL DEFAULT 'normal',
        bound_at INTEGER NOT NULL,
        ip_hash TEXT,
        device_hash TEXT,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_inviter_time "
                 "ON user_invites(inviter_user_id, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_ip_time "
                 "ON user_invites(ip_hash, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_device_time "
                 "ON user_invites(device_hash, bound_at DESC)")
    if not conn.execute("SELECT 1 FROM invite_campaigns LIMIT 1").fetchone():
        conn.execute("""INSERT INTO invite_campaigns(
            name,status,start_at,end_at,code_required,daily_invite_limit,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)""", (
            "长期邀请活动", "enabled", None, None, 0, max(1, DEFAULT_DAILY_LIMIT), now, now,
        ))


def normalize_code(value):
    return str(value or "").strip().upper()


def day_start(timestamp=None):
    current = datetime.datetime.fromtimestamp(int(timestamp or time.time()), SHANGHAI)
    return int(current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _active_campaign(conn, now):
    return conn.execute("""SELECT * FROM invite_campaigns
                           WHERE status='enabled'
                             AND (start_at IS NULL OR start_at<=?)
                             AND (end_at IS NULL OR end_at>=?)
                           ORDER BY id DESC LIMIT 1""", (now, now)).fetchone()


def _new_code(conn):
    for _ in range(128):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not conn.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone():
            return code
    raise RuntimeError("invite code exhausted")


def ensure_user_code(conn, user_id, now=None):
    now = int(now or time.time())
    campaign = _active_campaign(conn, now)
    if not campaign:
        raise InviteError("campaign_inactive", "邀请活动当前未开启", 409)
    row = conn.execute(
        "SELECT * FROM invite_codes WHERE campaign_id=? AND inviter_user_id=? AND status='active'",
        (campaign["id"], int(user_id)),
    ).fetchone()
    if row:
        return row
    code = _new_code(conn)
    conn.execute("""INSERT INTO invite_codes(campaign_id,inviter_user_id,code,status,created_at)
                    VALUES(?,?,?,'active',?)""", (campaign["id"], int(user_id), code, now))
    return conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()


def validate_code(conn, code, now=None):
    code = normalize_code(code)
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        raise InviteError("invalid_code", "邀请码无效", 404)
    now = int(now or time.time())
    row = conn.execute("""SELECT ic.*,c.status AS campaign_status,c.start_at,c.end_at,
                                  c.daily_invite_limit
                           FROM invite_codes ic
                           JOIN invite_campaigns c ON c.id=ic.campaign_id
                           WHERE ic.code=? AND ic.status='active'""", (code,)).fetchone()
    if not row:
        raise InviteError("invalid_code", "邀请码无效", 404)
    if row["campaign_status"] != "enabled":
        raise InviteError("campaign_inactive", "邀请活动当前未开启", 409)
    if row["start_at"] is not None and int(row["start_at"]) > now:
        raise InviteError("campaign_not_started", "邀请活动尚未开始", 409)
    if row["end_at"] is not None and int(row["end_at"]) < now:
        raise InviteError("campaign_ended", "邀请活动已结束", 409)
    return row


def _privacy_hash(raw_value, secret):
    value = str(raw_value or "").strip()
    if not value or not secret:
        return None
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def bind_registration(conn, invitee_user_id, invite_code, source, client_ip="",
                      device_id="", hash_secret="", now=None):
    """可选地保存一条直接邀请关系；不计算或返回任何二级关系。"""
    code = normalize_code(invite_code)
    if not code:
        return None
    now = int(now or time.time())
    invite = validate_code(conn, code, now)
    invitee_user_id = int(invitee_user_id)
    if int(invite["inviter_user_id"]) == invitee_user_id:
        raise InviteError("self_invite", "不能使用自己的邀请码", 409)
    used_today = conn.execute("""SELECT COUNT(*) FROM user_invites
                                 WHERE inviter_user_id=? AND status='bound'
                                   AND risk_status<>'blocked' AND bound_at>=?""",
                              (invite["inviter_user_id"], day_start(now))).fetchone()[0]
    limit = int(invite["daily_invite_limit"] or 0)
    if limit > 0 and int(used_today) >= limit:
        raise InviteError("daily_limit", "该邀请码今日邀请人数已达上限", 409)

    source = source if source in VALID_SOURCES else "web_manual"
    ip_hash = _privacy_hash(client_ip, hash_secret)
    device_hash = _privacy_hash(device_id, hash_secret)
    risk_status = "normal"
    if device_hash and conn.execute(
        "SELECT 1 FROM user_invites WHERE device_hash=? LIMIT 1", (device_hash,)
    ).fetchone():
        risk_status = "review"
    if ip_hash:
        same_ip_today = conn.execute(
            "SELECT COUNT(*) FROM user_invites WHERE ip_hash=? AND bound_at>=?",
            (ip_hash, day_start(now)),
        ).fetchone()[0]
        if int(same_ip_today) >= max(1, IP_REVIEW_THRESHOLD):
            risk_status = "review"
    conn.execute("""INSERT INTO user_invites(
        campaign_id,inviter_user_id,invitee_user_id,invite_code,source,status,risk_status,
        bound_at,ip_hash,device_hash,updated_at
    ) VALUES(?,?,?,?,?,'bound',?,?,?,?,?)""", (
        invite["campaign_id"], invite["inviter_user_id"], invitee_user_id, code, source,
        risk_status, now, ip_hash, device_hash, now,
    ))
    return conn.execute(
        "SELECT * FROM user_invites WHERE invitee_user_id=?", (invitee_user_id,)
    ).fetchone()
