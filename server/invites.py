#!/usr/bin/env python3
"""邀请注册领域逻辑。

该模块只操作调用方传入的 SQLite 连接，不自行提交事务。注册账号、绑定邀请
关系和签发登录令牌因此可以由 auth_server 放在同一个事务中完成。
"""
import datetime
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import zipfile
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6
VALID_SOURCES = {"web_link", "web_manual", "miniprogram", "admin"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DAILY_LIMIT = int(os.environ.get("HQ_INVITE_DAILY_LIMIT", "50"))
IP_REVIEW_THRESHOLD = int(os.environ.get("HQ_INVITE_IP_REVIEW_THRESHOLD", "3"))
INVITER_MEMBERSHIP_TIERS = {"experience", "partner", "initiator"}
MEMBERSHIP_NAMES = {
    "experience": "体验官",
    "partner": "合伙人",
    "initiator": "发起人",
}
INVITE_REWARD_TOTALS = {
    "experience": {"experience": 200},
    "partner": {"experience": 240, "partner": 1500},
    "initiator": {"experience": 280, "partner": 2500, "initiator": 15000},
}
MEMBERSHIP_LEVEL_ORDER = {"": 0, "experience": 1, "partner": 2, "initiator": 3}


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
        short_slug TEXT,
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
        invalid_reason TEXT,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_inviter_time ON user_invites(inviter_user_id, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_campaign_status ON user_invites(campaign_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_ip_time ON user_invites(ip_hash, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_device_time ON user_invites(device_hash, bound_at DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS invite_admin_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operator_user_id INTEGER NOT NULL,
        invite_relation_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        before_json TEXT,
        after_json TEXT,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS membership_upgrade_records(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        from_level TEXT NOT NULL DEFAULT '',
        to_level TEXT NOT NULL,
        source TEXT NOT NULL,
        source_order_id TEXT,
        operator TEXT,
        status TEXT NOT NULL DEFAULT 'effective',
        created_at INTEGER NOT NULL,
        voided_at INTEGER,
        void_reason TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_membership_upgrades_user ON membership_upgrade_records(user_id,id DESC)")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_membership_upgrades_source
                    ON membership_upgrade_records(source,source_order_id)
                    WHERE source_order_id IS NOT NULL AND source_order_id<>''""")
    conn.execute("""CREATE TABLE IF NOT EXISTS invite_reward_point_records(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invite_relation_id INTEGER NOT NULL,
        upgrade_record_id INTEGER NOT NULL UNIQUE,
        inviter_user_id INTEGER NOT NULL,
        invitee_user_id INTEGER NOT NULL,
        inviter_level_snapshot TEXT NOT NULL,
        invitee_level TEXT NOT NULL,
        reward_points INTEGER NOT NULL,
        reward_total_after INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'recorded',
        created_at INTEGER NOT NULL,
        voided_at INTEGER,
        void_reason TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invite_rewards_inviter ON invite_reward_point_records(inviter_user_id,id DESC)")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_rewards_relation_level
                    ON invite_reward_point_records(invite_relation_id,invitee_level)""")
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
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _campaign_row(conn):
    return conn.execute("SELECT * FROM invite_campaigns ORDER BY id DESC LIMIT 1").fetchone()


def campaign_config(conn, now=None):
    now = int(now or time.time())
    row = _campaign_row(conn)
    if not row:
        return {
            "enabled": False, "code_required": False, "daily_invite_limit": 0,
            "start_at": None, "end_at": None,
        }
    active = (
        row["status"] == "enabled"
        and (row["start_at"] is None or int(row["start_at"]) <= now)
        and (row["end_at"] is None or int(row["end_at"]) >= now)
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": active,
        "code_required": bool(row["code_required"]) if active else False,
        "daily_invite_limit": int(row["daily_invite_limit"] or 0),
        "start_at": row["start_at"],
        "end_at": row["end_at"],
    }


def _active_campaign(conn, now=None):
    config = campaign_config(conn, now)
    if not config.get("enabled"):
        return None
    return conn.execute("SELECT * FROM invite_campaigns WHERE id=?", (config["id"],)).fetchone()


def _new_code(conn):
    for _ in range(128):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not conn.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone():
            return code
    raise RuntimeError("invite code exhausted")


def inviter_eligibility(conn, user_id, now=None):
    """返回用户当前是否具备新增邀请关系的资格。"""
    now = int(now or time.time())
    row = conn.execute(
        """SELECT membership_tier,membership_expires_at,account_status
             FROM users WHERE id=?""",
        (int(user_id),),
    ).fetchone()
    tier = str(row["membership_tier"] or "") if row else ""
    expires_at = int(row["membership_expires_at"] or 0) if row else 0
    account_active = bool(row and str(row["account_status"] or "active") == "active")
    eligible = account_active and tier in INVITER_MEMBERSHIP_TIERS and expires_at > now
    if not row:
        reason = "邀请用户不存在"
    elif not account_active:
        reason = "账号状态异常，暂时不能邀请新用户"
    elif tier not in INVITER_MEMBERSHIP_TIERS or expires_at <= now:
        reason = "只有有效会员可以邀请新用户"
    else:
        reason = ""
    return {
        "eligible": eligible,
        "membership_tier": tier if eligible else "",
        "membership_expires_at": expires_at if eligible else 0,
        "reason": reason,
    }


def require_inviter_eligibility(conn, user_id, now=None, public=False):
    eligibility = inviter_eligibility(conn, user_id, now)
    if eligibility["eligible"]:
        return eligibility
    if public:
        raise InviteError("inviter_ineligible", "该邀请码当前不可用，请更换邀请码", 409)
    raise InviteError("membership_required", eligibility["reason"], 403)


def record_membership_upgrade(conn, user_id, from_level, to_level, source,
                              source_order_id=None, operator="", now=None):
    """记录会员升级，并为一级邀请人生成不叠加的奖励积分差额。"""
    now = int(now or time.time())
    user_id = int(user_id)
    from_level = str(from_level or "")
    to_level = str(to_level or "")
    if to_level not in MEMBERSHIP_LEVEL_ORDER or not to_level:
        return {"upgrade_record_id": None, "reward": None}
    if MEMBERSHIP_LEVEL_ORDER.get(to_level, 0) <= MEMBERSHIP_LEVEL_ORDER.get(from_level, 0):
        return {"upgrade_record_id": None, "reward": None}
    if source_order_id:
        existing = conn.execute(
            "SELECT id FROM membership_upgrade_records WHERE source=? AND source_order_id=?",
            (str(source or "admin"), str(source_order_id)),
        ).fetchone()
        if existing:
            reward = conn.execute(
                "SELECT * FROM invite_reward_point_records WHERE upgrade_record_id=?",
                (existing["id"],),
            ).fetchone()
            return {"upgrade_record_id": existing["id"], "reward": dict(reward) if reward else None}
    cur = conn.execute(
        """INSERT INTO membership_upgrade_records(
               user_id,from_level,to_level,source,source_order_id,operator,status,created_at
           ) VALUES(?,?,?,?,?,?, 'effective',?)""",
        (user_id, from_level, to_level, str(source or "admin"),
         str(source_order_id or "") or None, str(operator or ""), now),
    )
    upgrade_id = int(cur.lastrowid)
    relation = conn.execute(
        """SELECT ui.id,ui.inviter_user_id,ui.invitee_user_id,
                  inviter.membership_tier,inviter.membership_expires_at,inviter.account_status
             FROM user_invites ui
             JOIN users inviter ON inviter.id=ui.inviter_user_id
            WHERE ui.invitee_user_id=? AND ui.status='bound' AND ui.risk_status<>'blocked'""",
        (user_id,),
    ).fetchone()
    if not relation:
        return {"upgrade_record_id": upgrade_id, "reward": None}
    inviter_tier = str(relation["membership_tier"] or "")
    inviter_active = (
        str(relation["account_status"] or "active") == "active"
        and inviter_tier in INVITE_REWARD_TOTALS
        and int(relation["membership_expires_at"] or 0) > now
    )
    target_total = INVITE_REWARD_TOTALS.get(inviter_tier, {}).get(to_level, 0) if inviter_active else 0
    if target_total <= 0:
        return {"upgrade_record_id": upgrade_id, "reward": None}
    duplicate = conn.execute(
        """SELECT * FROM invite_reward_point_records
            WHERE invite_relation_id=? AND invitee_level=?""",
        (relation["id"], to_level),
    ).fetchone()
    if duplicate:
        return {"upgrade_record_id": upgrade_id, "reward": dict(duplicate)}
    current_total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE invite_relation_id=? AND status='recorded'""",
        (relation["id"],),
    ).fetchone()[0]
    delta = max(0, int(target_total) - int(current_total or 0))
    if delta <= 0:
        return {"upgrade_record_id": upgrade_id, "reward": None}
    reward_cur = conn.execute(
        """INSERT INTO invite_reward_point_records(
               invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
               inviter_level_snapshot,invitee_level,reward_points,reward_total_after,status,created_at
           ) VALUES(?,?,?,?,?,?,?,?, 'recorded',?)""",
        (relation["id"], upgrade_id, relation["inviter_user_id"], relation["invitee_user_id"],
         inviter_tier, to_level, delta, int(current_total or 0) + delta, now),
    )
    reward = conn.execute(
        "SELECT * FROM invite_reward_point_records WHERE id=?", (reward_cur.lastrowid,),
    ).fetchone()
    return {"upgrade_record_id": upgrade_id, "reward": dict(reward)}


def reward_points(conn, inviter_user_id, limit=20, offset=0):
    """返回独立邀请奖励积分汇总；绝不读取或修改 users.points。"""
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    inviter_user_id = int(inviter_user_id)
    total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE inviter_user_id=? AND status='recorded'""",
        (inviter_user_id,),
    ).fetchone()[0]
    count = conn.execute(
        """SELECT COUNT(*) FROM invite_reward_point_records
            WHERE inviter_user_id=? AND status='recorded'""",
        (inviter_user_id,),
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT rr.id,rr.inviter_level_snapshot,rr.invitee_level,rr.reward_points,
                  rr.reward_total_after,rr.created_at,u.username,u.display_name,u.account_id
             FROM invite_reward_point_records rr
             JOIN users u ON u.id=rr.invitee_user_id
            WHERE rr.inviter_user_id=? AND rr.status='recorded'
            ORDER BY rr.id DESC LIMIT ? OFFSET ?""",
        (inviter_user_id, limit, offset),
    ).fetchall()
    return {
        "total_reward_points": int(total or 0),
        "records": [{
            "id": row["id"],
            "invitee_username": row["username"],
            "invitee_name": row["display_name"] or row["username"],
            "invitee_account_id": row["account_id"] or "",
            "inviter_level": row["inviter_level_snapshot"],
            "inviter_level_name": MEMBERSHIP_NAMES.get(row["inviter_level_snapshot"], ""),
            "invitee_level": row["invitee_level"],
            "invitee_level_name": MEMBERSHIP_NAMES.get(row["invitee_level"], ""),
            "reward_points": int(row["reward_points"]),
            "reward_total_after": int(row["reward_total_after"]),
            "created_at": int(row["created_at"]),
        } for row in rows],
        "total": int(count),
        "limit": limit,
        "offset": offset,
    }


def ensure_user_code(conn, user_id, now=None):
    now = int(now or time.time())
    require_inviter_eligibility(conn, user_id, now)
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


def rotate_user_code(conn, user_id, now=None):
    now = int(now or time.time())
    campaign = _active_campaign(conn, now)
    if not campaign:
        raise InviteError("campaign_inactive", "邀请活动当前未开启", 409)
    conn.execute(
        "UPDATE invite_codes SET status='disabled' WHERE campaign_id=? AND inviter_user_id=? AND status='active'",
        (campaign["id"], int(user_id)),
    )
    return ensure_user_code(conn, user_id, now)


def _masked_account(account_id):
    value = str(account_id or "")
    if len(value) <= 4:
        return value[:1] + "***" if value else ""
    return value[:2] + "****" + value[-2:]


def validate_code(conn, code, now=None):
    code = normalize_code(code)
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        raise InviteError("invalid_code", "邀请码无效", 404)
    now = int(now or time.time())
    row = conn.execute("""SELECT ic.*,c.name AS campaign_name,c.status AS campaign_status,
                                  c.start_at,c.end_at,u.display_name,u.account_id
                           FROM invite_codes ic
                           JOIN invite_campaigns c ON c.id=ic.campaign_id
                           JOIN users u ON u.id=ic.inviter_user_id
                           WHERE ic.code=? AND ic.status='active'
                             AND COALESCE(u.account_status,'active')='active'""", (code,)).fetchone()
    if not row:
        raise InviteError("invalid_code", "邀请码无效", 404)
    if row["campaign_status"] != "enabled":
        raise InviteError("campaign_inactive", "邀请活动当前未开启", 409)
    if row["start_at"] is not None and int(row["start_at"]) > now:
        raise InviteError("campaign_not_started", "邀请活动尚未开始", 409)
    if row["end_at"] is not None and int(row["end_at"]) < now:
        raise InviteError("campaign_ended", "邀请活动已结束", 409)
    require_inviter_eligibility(conn, row["inviter_user_id"], now, public=True)
    return row


def public_inviter(row):
    return {
        "name": row["display_name"] or "黄雀用户",
        "account_id": _masked_account(row["account_id"]),
    }


def _privacy_hash(raw_value, secret):
    value = str(raw_value or "").strip()
    if not value or not secret:
        return None
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def bind_registration(conn, invitee_user_id, invite_code, source, client_ip="", device_id="", hash_secret="", now=None):
    now = int(now or time.time())
    config = campaign_config(conn, now)
    code = normalize_code(invite_code)
    if not config.get("enabled"):
        if code:
            raise InviteError("campaign_inactive", "邀请活动当前未开启", 409)
        return None
    if not code:
        if config.get("code_required"):
            raise InviteError("code_required", "请输入邀请码", 400)
        return None
    invite = validate_code(conn, code, now)
    invitee_user_id = int(invitee_user_id)
    if int(invite["inviter_user_id"]) == invitee_user_id:
        raise InviteError("self_invite", "不能使用自己的邀请码", 409)
    if conn.execute("SELECT 1 FROM user_invites WHERE invitee_user_id=?", (invitee_user_id,)).fetchone():
        raise InviteError("already_bound", "该账号已经绑定邀请人", 409)
    today = day_start(now)
    used_today = conn.execute("""SELECT COUNT(*) FROM user_invites
                                 WHERE inviter_user_id=? AND status='bound'
                                   AND risk_status<>'blocked' AND bound_at>=?""",
                              (invite["inviter_user_id"], today)).fetchone()[0]
    limit = int(config.get("daily_invite_limit") or 0)
    if limit > 0 and int(used_today) >= limit:
        raise InviteError("daily_limit", "该邀请码今日邀请人数已达上限", 409)
    source = source if source in VALID_SOURCES else "web_manual"
    ip_hash = _privacy_hash(client_ip, hash_secret)
    device_hash = _privacy_hash(device_id, hash_secret)
    risk_status = "normal"
    if device_hash and conn.execute(
        "SELECT 1 FROM user_invites WHERE device_hash=? AND invitee_user_id<>? LIMIT 1",
        (device_hash, invitee_user_id),
    ).fetchone():
        risk_status = "review"
    if ip_hash:
        same_ip_today = conn.execute(
            "SELECT COUNT(*) FROM user_invites WHERE ip_hash=? AND bound_at>=?",
            (ip_hash, today),
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
    return conn.execute("SELECT * FROM user_invites WHERE invitee_user_id=?", (invitee_user_id,)).fetchone()


def dashboard(conn, inviter_user_id, now=None):
    now = int(now or time.time())
    user_id = int(inviter_user_id)
    total = conn.execute("SELECT COUNT(*) FROM user_invites WHERE inviter_user_id=?", (user_id,)).fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM user_invites WHERE inviter_user_id=? AND bound_at>=?",
        (user_id, day_start(now)),
    ).fetchone()[0]
    valid = conn.execute("""SELECT COUNT(*) FROM user_invites
                            WHERE inviter_user_id=? AND status='bound' AND risk_status<>'blocked'""",
                         (user_id,)).fetchone()[0]
    second = conn.execute("""SELECT COUNT(*) FROM user_invites child
                             JOIN user_invites direct ON direct.invitee_user_id=child.inviter_user_id
                             WHERE direct.inviter_user_id=?
                               AND direct.status='bound' AND direct.risk_status<>'blocked'
                               AND child.status='bound' AND child.risk_status<>'blocked'""",
                          (user_id,)).fetchone()[0]
    return {
        "total_bound": int(total),
        "today_new": int(today),
        "valid_invites": int(valid),
        "direct_invites": int(valid),
        "indirect_invites": int(second),
    }


def invited_users(conn, inviter_user_id, level=1, limit=10, offset=0, now=None):
    now = int(now or time.time())
    level = int(level or 1)
    if level not in (1, 2):
        raise ValueError("level must be 1 or 2")
    limit = max(1, min(int(limit or 10), 100))
    offset = max(0, int(offset or 0))
    recharge_sql = """COALESCE((SELECT SUM(ro.amount) FROM recharge_orders ro
                                  WHERE ro.username=u.username AND ro.status='approved'),0)
                      + COALESCE((SELECT SUM(vp.amount_fen) / 100.0 FROM virtual_pay_orders vp
                                  WHERE vp.username=u.username AND vp.status='credited'),0)"""
    if level == 1:
        joins = """FROM user_invites ui JOIN users u ON u.id=ui.invitee_user_id
                   LEFT JOIN users parent ON parent.id=ui.inviter_user_id
                   WHERE ui.inviter_user_id=?"""
    else:
        joins = """FROM user_invites ui
                   JOIN user_invites direct ON direct.invitee_user_id=ui.inviter_user_id
                   JOIN users u ON u.id=ui.invitee_user_id
                   LEFT JOIN users parent ON parent.id=ui.inviter_user_id
                   WHERE direct.inviter_user_id=?
                     AND direct.status='bound' AND direct.risk_status<>'blocked'"""
    total = conn.execute("SELECT COUNT(*) " + joins, (int(inviter_user_id),)).fetchone()[0]
    rows = conn.execute("""SELECT ui.id,ui.status,ui.risk_status,ui.bound_at,ui.source,
                                  u.username,u.display_name,u.account_id,u.created_at,
                                  u.membership_tier,u.membership_expires_at,
                                  parent.username AS parent_username,parent.display_name AS parent_name,
                                  """ + recharge_sql + " AS recharge_total " + joins +
                        " ORDER BY ui.id DESC LIMIT ? OFFSET ?",
                        (int(inviter_user_id), limit, offset)).fetchall()
    users = []
    for row in rows:
        tier = str(row["membership_tier"] or "")
        expires_at = int(row["membership_expires_at"] or 0)
        known_tier = tier in MEMBERSHIP_NAMES
        membership_active = known_tier and expires_at > now
        users.append({
            "id": row["id"],
            "username": row["username"],
            "name": row["display_name"] or row["username"],
            "account_id": row["account_id"] or "",
            "registered_at": row["created_at"],
            "bound_at": row["bound_at"],
            "status": row["status"],
            "risk_status": row["risk_status"],
            "source": row["source"],
            "level": level,
            "parent_username": row["parent_username"] or "",
            "parent_name": row["parent_name"] or row["parent_username"] or "",
            "recharge_total": round(float(row["recharge_total"] or 0), 2),
            "membership_tier": tier if known_tier else "",
            "membership_name": MEMBERSHIP_NAMES.get(tier, "非会员"),
            "membership_active": membership_active,
            "membership_status": "active" if membership_active else ("expired" if known_tier else "none"),
            "membership_expires_at": expires_at if known_tier else 0,
        })
    return {"users": users, "total": int(total), "level": level, "limit": limit, "offset": offset}


def referrer(conn, invitee_user_id):
    row = conn.execute("""SELECT ui.status,ui.risk_status,ui.bound_at,u.display_name,u.account_id
                          FROM user_invites ui JOIN users u ON u.id=ui.inviter_user_id
                          WHERE ui.invitee_user_id=?""", (int(invitee_user_id),)).fetchone()
    if not row:
        return None
    return {
        "name": row["display_name"] or "黄雀用户",
        "account_id": row["account_id"] or "",
        "status": row["status"],
        "risk_status": row["risk_status"],
        "bound_at": row["bound_at"],
    }


ADMIN_RELATION_ACTIONS = {"invalidate", "unbind", "restore", "ban", "unban"}


def admin_update_config(conn, payload, operator_user_id=None, now=None):
    """Update the current invite campaign with a small, validated allow-list."""
    now = int(now or time.time())
    row = _campaign_row(conn)
    if not row:
        raise InviteError("campaign_missing", "邀请活动不存在", 404)
    name = str(payload.get("name", row["name"]) or "").strip()
    status = str(payload.get("status", row["status"]) or "").strip()
    if not name or len(name) > 80:
        raise InviteError("invalid_name", "活动名称应为 1-80 个字符")
    if status not in {"enabled", "disabled"}:
        raise InviteError("invalid_status", "活动状态不正确")

    def optional_ts(key):
        value = payload.get(key, row[key])
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise InviteError("invalid_time", "活动时间格式不正确")

    start_at = optional_ts("start_at")
    end_at = optional_ts("end_at")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise InviteError("invalid_time_range", "结束时间必须晚于开始时间")
    try:
        daily_limit = int(payload.get("daily_invite_limit", row["daily_invite_limit"]))
    except (TypeError, ValueError):
        raise InviteError("invalid_daily_limit", "每日邀请上限必须是整数")
    if daily_limit < 1 or daily_limit > 100000:
        raise InviteError("invalid_daily_limit", "每日邀请上限应为 1-100000")
    required = 1 if bool(payload.get("code_required", row["code_required"])) else 0
    before = dict(row)
    conn.execute("""UPDATE invite_campaigns
                    SET name=?,status=?,start_at=?,end_at=?,code_required=?,
                        daily_invite_limit=?,updated_at=? WHERE id=?""", (
        name, status, start_at, end_at, required, daily_limit, now, row["id"],
    ))
    after = dict(conn.execute("SELECT * FROM invite_campaigns WHERE id=?", (row["id"],)).fetchone())
    if operator_user_id is not None:
        conn.execute("""INSERT INTO invite_admin_audit(
            operator_user_id,invite_relation_id,action,reason,before_json,after_json,created_at
        ) VALUES(?,0,'config_update','',?,?,?)""", (
            int(operator_user_id), json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False), now,
        ))
    return after


def admin_stats(conn, days=30, now=None):
    now = int(now or time.time())
    try:
        days = max(1, min(int(days or 30), 180))
    except (TypeError, ValueError):
        days = 30
    since = day_start(now) - (days - 1) * 86400
    totals = conn.execute("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN status='bound' THEN 1 ELSE 0 END) AS bound,
        SUM(CASE WHEN status='invalid' THEN 1 ELSE 0 END) AS invalid,
        SUM(CASE WHEN status='unbound' THEN 1 ELSE 0 END) AS unbound,
        SUM(CASE WHEN risk_status='review' THEN 1 ELSE 0 END) AS review,
        SUM(CASE WHEN risk_status='blocked' THEN 1 ELSE 0 END) AS blocked,
        COUNT(DISTINCT inviter_user_id) AS inviters
        FROM user_invites""").fetchone()
    today = conn.execute("SELECT COUNT(*) FROM user_invites WHERE bound_at>=?", (day_start(now),)).fetchone()[0]
    rows = conn.execute("""SELECT strftime('%Y-%m-%d',bound_at,'unixepoch','localtime') AS day,COUNT(*) AS count
                           FROM user_invites WHERE bound_at>=? GROUP BY day ORDER BY day""", (since,)).fetchall()
    series_map = {row["day"]: int(row["count"]) for row in rows}
    series = []
    start_date = datetime.datetime.fromtimestamp(since, SHANGHAI).date()
    for offset in range(days):
        label = (start_date + datetime.timedelta(days=offset)).isoformat()
        series.append({"date": label, "count": series_map.get(label, 0)})
    return {
        "total": int(totals["total"] or 0), "today": int(today or 0),
        "bound": int(totals["bound"] or 0), "invalid": int(totals["invalid"] or 0),
        "unbound": int(totals["unbound"] or 0), "review": int(totals["review"] or 0),
        "blocked": int(totals["blocked"] or 0), "inviters": int(totals["inviters"] or 0),
        "days": days, "series": series,
    }


def _admin_relation_where(filters):
    clauses, params = [], []
    joins = """ FROM user_invites ui
        JOIN users inviter ON inviter.id=ui.inviter_user_id
        JOIN users invitee ON invitee.id=ui.invitee_user_id
        JOIN invite_campaigns c ON c.id=ui.campaign_id """
    for key, alias in (("inviter", "inviter"), ("invitee", "invitee")):
        value = str(filters.get(key) or "").strip()
        if value:
            clauses.append(f"({alias}.username LIKE ? OR {alias}.display_name LIKE ? OR {alias}.account_id LIKE ?)")
            params.extend(["%" + value + "%"] * 3)
    code = normalize_code(filters.get("code"))
    if code:
        clauses.append("ui.invite_code=?"); params.append(code)
    status = str(filters.get("status") or "").strip()
    if status:
        if status not in {"bound", "invalid", "unbound"}:
            raise InviteError("invalid_status", "关系状态筛选不正确")
        clauses.append("ui.status=?"); params.append(status)
    risk = str(filters.get("risk_status") or "").strip()
    if risk:
        if risk not in {"normal", "review", "blocked"}:
            raise InviteError("invalid_risk_status", "风控状态筛选不正确")
        clauses.append("ui.risk_status=?"); params.append(risk)
    for key, op in (("start_at", ">="), ("end_at", "<=")):
        value = filters.get(key)
        if value not in (None, ""):
            try:
                clauses.append("ui.bound_at" + op + "?"); params.append(int(value))
            except (TypeError, ValueError):
                raise InviteError("invalid_time", "时间筛选格式不正确")
    return joins + ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params


def admin_relations(conn, filters=None, limit=50, offset=0):
    filters = filters or {}
    try:
        limit = max(1, min(int(limit or 50), 200)); offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise InviteError("invalid_pagination", "分页参数不正确")
    joins, params = _admin_relation_where(filters)
    total = conn.execute("SELECT COUNT(*)" + joins, params).fetchone()[0]
    rows = conn.execute("""SELECT ui.*,c.name AS campaign_name,
        inviter.username AS inviter_username,inviter.display_name AS inviter_name,inviter.account_id AS inviter_account_id,
        invitee.username AS invitee_username,invitee.display_name AS invitee_name,invitee.account_id AS invitee_account_id,
        COALESCE(invitee.account_status,'active') AS invitee_account_status
        """ + joins + " ORDER BY ui.id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    return {"total": int(total), "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}


def admin_relation_action(conn, relation_id, action, reason, operator_user_id, now=None):
    now = int(now or time.time())
    action = str(action or "").strip()
    reason = str(reason or "").strip()
    if action not in ADMIN_RELATION_ACTIONS:
        raise InviteError("invalid_action", "不支持的处理动作")
    if action != "restore" and not reason:
        raise InviteError("reason_required", "请填写处理原因")
    row = conn.execute("SELECT * FROM user_invites WHERE id=?", (int(relation_id),)).fetchone()
    if not row:
        raise InviteError("relation_not_found", "邀请关系不存在", 404)
    before = dict(row)
    if action == "invalidate":
        conn.execute("UPDATE user_invites SET status='invalid',invalid_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))
    elif action == "unbind":
        conn.execute("UPDATE user_invites SET status='unbound',invalid_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))
    elif action == "restore":
        conn.execute("UPDATE user_invites SET status='bound',risk_status='normal',invalid_reason=NULL,updated_at=? WHERE id=?", (now, row["id"]))
    elif action == "ban":
        conn.execute("UPDATE user_invites SET risk_status='blocked',invalid_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))
        conn.execute("UPDATE users SET account_status='banned' WHERE id=?", (row["invitee_user_id"],))
        conn.execute("DELETE FROM tokens WHERE username=(SELECT username FROM users WHERE id=?)", (row["invitee_user_id"],))
    elif action == "unban":
        conn.execute("UPDATE user_invites SET risk_status='normal',invalid_reason=NULL,updated_at=? WHERE id=?", (now, row["id"]))
        conn.execute("UPDATE users SET account_status='active' WHERE id=?", (row["invitee_user_id"],))
    after = dict(conn.execute("SELECT * FROM user_invites WHERE id=?", (row["id"],)).fetchone())
    conn.execute("""INSERT INTO invite_admin_audit(
        operator_user_id,invite_relation_id,action,reason,before_json,after_json,created_at
    ) VALUES(?,?,?,?,?,?,?)""", (
        int(operator_user_id), row["id"], action, reason,
        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), now,
    ))
    return after


def admin_audit(conn, limit=100):
    try:
        limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    rows = conn.execute("""SELECT a.id,a.invite_relation_id,a.action,a.reason,a.created_at,
        u.username AS operator_username,u.display_name AS operator_name
        FROM invite_admin_audit a LEFT JOIN users u ON u.id=a.operator_user_id
        ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _xlsx_col(index):
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def export_relations_xlsx(conn, filters=None):
    """Create a dependency-free XLSX report with inline UTF-8 strings."""
    data = admin_relations(conn, filters or {}, limit=200, offset=0)
    items = data["items"]
    # Export must not silently truncate. Fetch all matching rows after a bounded count query.
    if data["total"] > 200:
        joins, params = _admin_relation_where(filters or {})
        rows = conn.execute("""SELECT ui.*,c.name AS campaign_name,
            inviter.username AS inviter_username,inviter.display_name AS inviter_name,inviter.account_id AS inviter_account_id,
            invitee.username AS invitee_username,invitee.display_name AS invitee_name,invitee.account_id AS invitee_account_id,
            COALESCE(invitee.account_status,'active') AS invitee_account_status
            """ + joins + " ORDER BY ui.id DESC", params).fetchall()
        items = [dict(row) for row in rows]
    headers = ["关系ID", "邀请人账号", "邀请人昵称", "邀请人账号ID", "被邀请人账号", "被邀请人昵称",
               "被邀请人账号ID", "邀请码", "来源", "关系状态", "风控状态", "账号状态", "绑定时间", "处理原因"]
    rows = [headers]
    for item in items:
        bound = datetime.datetime.fromtimestamp(int(item["bound_at"]), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        rows.append([item["id"], item["inviter_username"], item["inviter_name"] or "", item["inviter_account_id"] or "",
                     item["invitee_username"], item["invitee_name"] or "", item["invitee_account_id"] or "",
                     item["invite_code"], item["source"], item["status"], item["risk_status"],
                     item["invitee_account_status"], bound, item["invalid_reason"] or ""])
    sheet_rows = []
    for r_idx, values in enumerate(rows, 1):
        cells = []
        for c_idx, value in enumerate(values, 1):
            ref = _xlsx_col(c_idx) + str(r_idx)
            style = ' s="1"' if r_idx == 1 else ''
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}"{style} t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        row_style = ' ht="28" customHeight="1"' if r_idx == 1 else ' ht="22" customHeight="1"'
        sheet_rows.append(f'<row r="{r_idx}"{row_style}>' + "".join(cells) + "</row>")
    widths = [10, 18, 18, 16, 18, 18, 16, 12, 14, 12, 12, 12, 22, 28]
    cols = "".join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths, 1))
    last_row = max(1, len(rows))
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' \
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' \
        '<sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' \
        '<cols>' + cols + '</cols><sheetData>' + "".join(sheet_rows) + \
        f'</sheetData><autoFilter ref="A1:N{last_row}"/></worksheet>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>' \
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' \
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' \
            '<Default Extension="xml" ContentType="application/xml"/>' \
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' \
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' \
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' \
            '</Types>')
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>' \
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' \
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' \
            '</Relationships>')
        zf.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?>' \
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' \
            '<sheets><sheet name="邀请关系" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?>' \
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' \
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' \
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' \
            '</Relationships>')
        zf.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8"?>' \
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' \
            '<fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font>' \
            '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts>' \
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>' \
            '<fill><patternFill patternType="solid"><fgColor rgb="FF176B5B"/><bgColor indexed="64"/></patternFill></fill></fills>' \
            '<borders count="2"><border/><border><bottom style="thin"><color rgb="FFDDE5E1"/></bottom></border></borders>' \
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' \
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"><alignment vertical="center"/></xf>' \
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs>' \
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()
