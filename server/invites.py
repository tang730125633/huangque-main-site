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
VALID_SOURCES = {"web_link", "web_manual", "miniprogram", "miniprogram_card", "admin"}
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
REWARD_CLAIM_TTL_SECONDS = 7 * 24 * 3600
RISK_HASH_RETENTION = 30 * 24 * 3600


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
    invite_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_invites)").fetchall()}
    if "invalid_reason" not in invite_cols:
        conn.execute("ALTER TABLE user_invites ADD COLUMN invalid_reason TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_inviter_time ON user_invites(inviter_user_id, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_campaign_status ON user_invites(campaign_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_ip_time ON user_invites(ip_hash, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_device_time ON user_invites(device_hash, bound_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_bound_at ON user_invites(bound_at)")
    conn.execute(
        "UPDATE user_invites SET ip_hash=NULL,device_hash=NULL WHERE bound_at<? AND (ip_hash IS NOT NULL OR device_hash IS NOT NULL)",
        (now - RISK_HASH_RETENTION,),
    )
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
    upgrade_cols = {row["name"] for row in conn.execute("PRAGMA table_info(membership_upgrade_records)").fetchall()}
    if "event_type" not in upgrade_cols:
        conn.execute("ALTER TABLE membership_upgrade_records ADD COLUMN event_type TEXT NOT NULL DEFAULT 'upgrade'")
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
        event_type TEXT NOT NULL DEFAULT 'upgrade',
        reward_points INTEGER NOT NULL,
        reward_total_after INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'recorded',
        created_at INTEGER NOT NULL,
        voided_at INTEGER,
        void_reason TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invite_rewards_inviter ON invite_reward_point_records(inviter_user_id,id DESC)")
    reward_cols = {row["name"] for row in conn.execute("PRAGMA table_info(invite_reward_point_records)").fetchall()}
    if "event_type" not in reward_cols:
        conn.execute("ALTER TABLE invite_reward_point_records ADD COLUMN event_type TEXT NOT NULL DEFAULT 'upgrade'")
    conn.execute("DROP INDEX IF EXISTS idx_invite_rewards_relation_level")
    conn.execute("DROP INDEX IF EXISTS idx_invite_rewards_upgrade_relation_level")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_rewards_upgrade_relation_level
                    ON invite_reward_point_records(invite_relation_id,invitee_level)
                    WHERE event_type='upgrade' AND status IN ('recorded','pending_review')""")
    if "voided_by" not in reward_cols:
        conn.execute("ALTER TABLE invite_reward_point_records ADD COLUMN voided_by TEXT")
    if "claim_id" not in reward_cols:
        conn.execute("ALTER TABLE invite_reward_point_records ADD COLUMN claim_id INTEGER")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_rewards_claim
                    ON invite_reward_point_records(claim_id) WHERE claim_id IS NOT NULL""")
    conn.execute("""CREATE TABLE IF NOT EXISTS invite_reward_claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        upgrade_record_id INTEGER NOT NULL UNIQUE,
        source_order_id TEXT,
        invite_relation_id INTEGER NOT NULL,
        direct_inviter_user_id INTEGER NOT NULL,
        invitee_user_id INTEGER NOT NULL,
        target_level TEXT NOT NULL,
        event_type TEXT NOT NULL DEFAULT 'upgrade',
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        recipient_user_id INTEGER,
        recipient_level_snapshot TEXT,
        reward_points INTEGER NOT NULL DEFAULT 0,
        transfer_depth INTEGER NOT NULL DEFAULT 0,
        settled_at INTEGER,
        voided_at INTEGER,
        reason TEXT,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_invite_claims_owner_status
                    ON invite_reward_claims(direct_inviter_user_id,status,expires_at)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_invite_claims_expiry
                    ON invite_reward_claims(status,expires_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS invite_reward_notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        claim_id INTEGER,
        notice_type TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL DEFAULT '{}',
        last_shown_day TEXT,
        read_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_invite_reward_notices_user
                    ON invite_reward_notifications(user_id,notice_type,read_at,id)""")
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


def inviter_eligibility(conn, user_id, now=None, enforce_membership=True):
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
    has_membership = tier in INVITER_MEMBERSHIP_TIERS and expires_at > now
    eligible = account_active and (has_membership or not enforce_membership)
    if not row:
        reason = "邀请用户不存在"
    elif not account_active:
        reason = "账号状态异常，暂时不能邀请新用户"
    elif enforce_membership and not has_membership:
        reason = "只有有效会员可以邀请新用户"
    else:
        reason = ""
    return {
        "eligible": eligible,
        "membership_tier": tier if has_membership else "",
        "membership_expires_at": expires_at if has_membership else 0,
        "reason": reason,
    }


def require_inviter_eligibility(conn, user_id, now=None, public=False, enforce_membership=True):
    eligibility = inviter_eligibility(conn, user_id, now, enforce_membership)
    if eligibility["eligible"]:
        return eligibility
    if public:
        raise InviteError("inviter_ineligible", "该邀请码当前不可用，请更换邀请码", 409)
    raise InviteError("membership_required", eligibility["reason"], 403)


def invited_membership_limit(conn, user_id, target_tier):
    """保留旧调用合同；邀请关系不再限制被邀请人的会员等级。"""
    target_tier = str(target_tier or "")
    relation = conn.execute(
        """SELECT ui.id,ui.inviter_user_id,inviter.username,inviter.display_name,
                  inviter.membership_tier
             FROM user_invites ui
             JOIN users inviter ON inviter.id=ui.inviter_user_id
            WHERE ui.invitee_user_id=? AND ui.status='bound' AND ui.risk_status<>'blocked'""",
        (int(user_id),),
    ).fetchone()
    if not relation or not target_tier:
        return {"allowed": True, "relation": dict(relation) if relation else None}
    return {
        "allowed": True,
        "relation": dict(relation),
        "inviter_tier": str(relation["membership_tier"] or ""),
    }


def minimum_reward_points(target_level):
    target_level = str(target_level or "")
    return int(INVITE_REWARD_TOTALS.get(target_level, {}).get(target_level, 0))


def _create_pending_reward_claim(conn, relation, upgrade_id, to_level, event_type,
                                 source_order_id, now):
    existing = conn.execute(
        "SELECT * FROM invite_reward_claims WHERE upgrade_record_id=?", (int(upgrade_id),),
    ).fetchone()
    if existing:
        return dict(existing)
    points = minimum_reward_points(to_level)
    if points <= 0:
        return None
    cur = conn.execute(
        """INSERT INTO invite_reward_claims(
               upgrade_record_id,source_order_id,invite_relation_id,direct_inviter_user_id,
               invitee_user_id,target_level,event_type,status,created_at,expires_at,
               reward_points,updated_at
           ) VALUES(?,?,?,?,?,?,?,'pending_upgrade',?,?,?,?)""",
        (int(upgrade_id), str(source_order_id or "") or None, int(relation["id"]),
         int(relation["inviter_user_id"]), int(relation["invitee_user_id"]),
         str(to_level), str(event_type), int(now), int(now) + REWARD_CLAIM_TTL_SECONDS,
         points, int(now)),
    )
    claim = dict(conn.execute(
        "SELECT * FROM invite_reward_claims WHERE id=?", (cur.lastrowid,),
    ).fetchone())
    conn.execute(
        """INSERT OR IGNORE INTO invite_reward_notifications(
               user_id,claim_id,notice_type,operation_key,payload_json,created_at,updated_at
           ) VALUES(?,?,'pending_upgrade',?,?,?,?)""",
        (claim["direct_inviter_user_id"], claim["id"], "pending:%d" % claim["id"],
         json.dumps({
             "required_tier": claim["target_level"],
             "expires_at": claim["expires_at"],
             "reward_points": claim["reward_points"],
         }, ensure_ascii=False, separators=(",", ":")), int(now), int(now)),
    )
    return claim


def reward_upgrade_preview(conn, user_id, target_tier, now=None):
    """预览会员升级将产生的一级邀请奖励积分，不写数据库。"""
    now = int(now or time.time())
    invited_membership_limit(conn, user_id, target_tier)
    relation = conn.execute(
        """SELECT ui.id,ui.inviter_user_id,inviter.username,inviter.display_name,
                  inviter.membership_tier,inviter.membership_expires_at,inviter.account_status
             FROM user_invites ui
             JOIN users inviter ON inviter.id=ui.inviter_user_id
            WHERE ui.invitee_user_id=? AND ui.status='bound' AND ui.risk_status<>'blocked'""",
        (int(user_id),),
    ).fetchone()
    if not relation:
        return {"has_inviter": False, "reward_points": 0}
    inviter_tier = str(relation["membership_tier"] or "")
    inviter_active = (
        str(relation["account_status"] or "active") == "active"
        and inviter_tier in INVITE_REWARD_TOTALS
        and int(relation["membership_expires_at"] or 0) > now
    )
    target_total = INVITE_REWARD_TOTALS.get(inviter_tier, {}).get(str(target_tier or ""), 0) if inviter_active else 0
    current_total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE invite_relation_id=? AND status IN ('recorded','pending_review')
              AND event_type='upgrade'""",
        (relation["id"],),
    ).fetchone()[0]
    duplicate = conn.execute(
        """SELECT 1 FROM invite_reward_point_records
            WHERE invite_relation_id=? AND invitee_level=?
              AND status IN ('recorded','pending_review') AND event_type='upgrade'""",
        (relation["id"], str(target_tier or "")),
    ).fetchone()
    delta = 0 if duplicate else max(0, int(target_total or 0) - int(current_total or 0))
    return {
        "has_inviter": True,
        "inviter_username": relation["username"],
        "inviter_name": relation["display_name"] or relation["username"],
        "inviter_tier": inviter_tier,
        "inviter_tier_name": MEMBERSHIP_NAMES.get(inviter_tier, "非会员"),
        "reward_points": int(delta),
        "reward_total_after": int(current_total or 0) + int(delta),
    }


def record_membership_upgrade(conn, user_id, from_level, to_level, source,
                              source_order_id=None, operator="", now=None, event_type="upgrade"):
    """记录会员升级，并为一级邀请人生成不叠加的奖励积分差额。"""
    now = int(now or time.time())
    user_id = int(user_id)
    from_level = str(from_level or "")
    to_level = str(to_level or "")
    if event_type not in ("upgrade", "renewal") or to_level not in MEMBERSHIP_LEVEL_ORDER or not to_level:
        return {"upgrade_record_id": None, "reward": None}
    if event_type == "upgrade" and MEMBERSHIP_LEVEL_ORDER.get(to_level, 0) <= MEMBERSHIP_LEVEL_ORDER.get(from_level, 0):
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
            claim = conn.execute(
                "SELECT * FROM invite_reward_claims WHERE upgrade_record_id=?",
                (existing["id"],),
            ).fetchone()
            return {
                "upgrade_record_id": existing["id"],
                "reward": dict(reward) if reward else None,
                "claim": dict(claim) if claim else None,
            }
    cur = conn.execute(
        """INSERT INTO membership_upgrade_records(
               user_id,from_level,to_level,source,source_order_id,operator,status,created_at,event_type
           ) VALUES(?,?,?,?,?,?, 'effective',?,?)""",
        (user_id, from_level, to_level, str(source or "admin"),
         str(source_order_id or "") or None, str(operator or ""), now, event_type),
    )
    upgrade_id = int(cur.lastrowid)
    relation = conn.execute(
        """SELECT ui.id,ui.inviter_user_id,ui.invitee_user_id,ui.risk_status,
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
    if event_type == "renewal":
        target_total = {"experience": 200, "partner": 240, "initiator": 280}.get(inviter_tier, 0) if inviter_active else 0
    if target_total <= 0:
        claim = _create_pending_reward_claim(
            conn, relation, upgrade_id, to_level, event_type, source_order_id, now,
        )
        return {"upgrade_record_id": upgrade_id, "reward": None, "claim": claim}
    duplicate = conn.execute(
        """SELECT * FROM invite_reward_point_records
            WHERE invite_relation_id=? AND invitee_level=? AND event_type='upgrade'
              AND status IN ('recorded','pending_review')""",
        (relation["id"], to_level),
    ).fetchone()
    if event_type == "upgrade" and duplicate:
        return {"upgrade_record_id": upgrade_id, "reward": dict(duplicate)}
    upgrade_total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE invite_relation_id=? AND status IN ('recorded','pending_review')
              AND event_type='upgrade'""",
        (relation["id"],),
    ).fetchone()[0]
    all_total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE invite_relation_id=? AND status IN ('recorded','pending_review')""",
        (relation["id"],),
    ).fetchone()[0]
    delta = int(target_total) if event_type == "renewal" else max(0, int(target_total) - int(upgrade_total or 0))
    if delta <= 0:
        return {"upgrade_record_id": upgrade_id, "reward": None}
    reward_status = "pending_review" if relation["risk_status"] == "review" else "recorded"
    reward_cur = conn.execute(
        """INSERT INTO invite_reward_point_records(
               invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
               inviter_level_snapshot,invitee_level,reward_points,reward_total_after,status,created_at,event_type
           ) VALUES(?,?,?,?,?,?,?,?, ?,?,?)""",
        (relation["id"], upgrade_id, relation["inviter_user_id"], relation["invitee_user_id"],
         inviter_tier, to_level, delta, int(all_total or 0) + delta, reward_status, now, event_type),
    )
    reward = conn.execute(
        "SELECT * FROM invite_reward_point_records WHERE id=?", (reward_cur.lastrowid,),
    ).fetchone()
    claim_cur = conn.execute(
        """INSERT INTO invite_reward_claims(
               upgrade_record_id,source_order_id,invite_relation_id,direct_inviter_user_id,
               invitee_user_id,target_level,event_type,status,created_at,expires_at,
               recipient_user_id,recipient_level_snapshot,reward_points,transfer_depth,
               settled_at,reason,updated_at
           ) VALUES(?,?,?,?,?,?,?,'credited',?,?,?,?,?,0,?,'membership_qualified',?)""",
        (upgrade_id, str(source_order_id or "") or None, relation["id"],
         relation["inviter_user_id"], relation["invitee_user_id"], to_level, event_type,
         now, now + REWARD_CLAIM_TTL_SECONDS, relation["inviter_user_id"], inviter_tier,
         delta, now, now),
    )
    claim = conn.execute(
        "SELECT * FROM invite_reward_claims WHERE id=?", (claim_cur.lastrowid,),
    ).fetchone()
    conn.execute(
        "UPDATE invite_reward_point_records SET claim_id=? WHERE id=?",
        (claim["id"], reward["id"]),
    )
    reward = conn.execute(
        "SELECT * FROM invite_reward_point_records WHERE id=?", (reward["id"],),
    ).fetchone()
    return {"upgrade_record_id": upgrade_id, "reward": dict(reward), "claim": dict(claim)}


def _active_membership_row(conn, user_id, required_level, now):
    row = conn.execute(
        """SELECT id,membership_tier,membership_expires_at,account_status
             FROM users WHERE id=?""",
        (int(user_id),),
    ).fetchone()
    if not row or str(row["account_status"] or "active") != "active":
        return None
    tier = str(row["membership_tier"] or "")
    if int(row["membership_expires_at"] or 0) <= int(now):
        return None
    if MEMBERSHIP_LEVEL_ORDER.get(tier, 0) < MEMBERSHIP_LEVEL_ORDER.get(str(required_level or ""), 0):
        return None
    return row


def settle_claim(conn, claim_id, recipient_user_id, now=None, transferred=False, depth=0):
    now = int(now or time.time())
    claim = conn.execute(
        "SELECT * FROM invite_reward_claims WHERE id=?", (int(claim_id),),
    ).fetchone()
    if not claim or claim["status"] != "pending_upgrade":
        return dict(claim) if claim else None
    recipient = _active_membership_row(
        conn, recipient_user_id, claim["target_level"], now,
    )
    if not recipient:
        return None
    recipient_tier = str(recipient["membership_tier"] or "")
    target_total = int(
        INVITE_REWARD_TOTALS.get(recipient_tier, {}).get(claim["target_level"], 0)
    )
    if target_total <= 0:
        return None
    existing_ledger = conn.execute(
        "SELECT * FROM invite_reward_point_records WHERE claim_id=?", (claim["id"],),
    ).fetchone()
    if existing_ledger:
        return dict(claim)
    upgrade_total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE invite_relation_id=? AND status IN ('recorded','pending_review')
              AND event_type='upgrade'""",
        (claim["invite_relation_id"],),
    ).fetchone()[0]
    all_total = conn.execute(
        """SELECT COALESCE(SUM(reward_points),0) FROM invite_reward_point_records
            WHERE invite_relation_id=? AND status IN ('recorded','pending_review')""",
        (claim["invite_relation_id"],),
    ).fetchone()[0]
    points = target_total if claim["event_type"] == "renewal" else max(
        0, target_total - int(upgrade_total or 0)
    )
    final_status = "transferred" if transferred else "credited"
    if points > 0:
        relation = conn.execute(
            "SELECT risk_status FROM user_invites WHERE id=?",
            (claim["invite_relation_id"],),
        ).fetchone()
        ledger_status = "pending_review" if relation and relation["risk_status"] == "review" else "recorded"
        conn.execute(
            """INSERT INTO invite_reward_point_records(
                   invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
                   inviter_level_snapshot,invitee_level,reward_points,reward_total_after,
                   status,created_at,event_type,claim_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (claim["invite_relation_id"], claim["upgrade_record_id"], int(recipient_user_id),
             claim["invitee_user_id"], recipient_tier, claim["target_level"], points,
             int(all_total or 0) + points, ledger_status, now, claim["event_type"], claim["id"]),
        )
    conn.execute(
        """UPDATE invite_reward_claims
              SET status=?,recipient_user_id=?,recipient_level_snapshot=?,reward_points=?,
                  transfer_depth=?,settled_at=?,reason=?,updated_at=?
            WHERE id=? AND status='pending_upgrade'""",
        (final_status, int(recipient_user_id), recipient_tier, points, int(depth or 0),
         now, "upward_transfer" if transferred else "membership_qualified", now, claim["id"]),
    )
    return dict(conn.execute(
        "SELECT * FROM invite_reward_claims WHERE id=?", (claim["id"],),
    ).fetchone())


def settle_pending_for_user(conn, user_id, now=None):
    now = int(now or time.time())
    rows = conn.execute(
        """SELECT id FROM invite_reward_claims
            WHERE direct_inviter_user_id=? AND status='pending_upgrade' AND expires_at>=?
            ORDER BY id""",
        (int(user_id), now),
    ).fetchall()
    settled = []
    for row in rows:
        result = settle_claim(conn, row["id"], user_id, now=now)
        if result and result["status"] == "credited":
            settled.append(result)
    summary = {
        "count": len(settled),
        "total_points": sum(int(row["reward_points"] or 0) for row in settled),
        "claim_ids": [int(row["id"]) for row in settled],
    }
    if settled:
        operation_key = "unlock:%d:%s" % (
            int(user_id), ",".join(str(row["id"]) for row in settled),
        )
        conn.execute(
            """INSERT OR IGNORE INTO invite_reward_notifications(
                   user_id,notice_type,operation_key,payload_json,created_at,updated_at
               ) VALUES(?,'reward_unlocked',?,?,?,?)""",
            (int(user_id), operation_key, json.dumps({
                "claim_count": summary["count"],
                "total_points": summary["total_points"],
                "claim_ids": summary["claim_ids"],
            }, ensure_ascii=False, separators=(",", ":")),
             now, now),
        )
    return summary


def _eligible_ancestor(conn, claim, now, max_depth=100):
    current = int(claim["direct_inviter_user_id"])
    seen = {current, int(claim["invitee_user_id"])}
    for depth in range(1, max(1, min(int(max_depth or 100), 100)) + 1):
        relation = conn.execute(
            """SELECT inviter_user_id FROM user_invites
                WHERE invitee_user_id=? AND status='bound' AND risk_status='normal'""",
            (current,),
        ).fetchone()
        if not relation:
            return None, 0
        candidate = int(relation["inviter_user_id"])
        if candidate in seen:
            return None, 0
        seen.add(candidate)
        if _active_membership_row(conn, candidate, claim["target_level"], now):
            return candidate, depth
        current = candidate
    return None, 0


def expire_pending_claims(conn, now=None, limit=100):
    now = int(now or time.time())
    limit = max(1, min(int(limit or 100), 500))
    rows = conn.execute(
        """SELECT * FROM invite_reward_claims
            WHERE status='pending_upgrade' AND expires_at<? ORDER BY id LIMIT ?""",
        (now, limit),
    ).fetchall()
    summary = {"processed": 0, "transferred": 0, "no_recipient": 0}
    for claim in rows:
        recipient_id, depth = _eligible_ancestor(conn, claim, now)
        if recipient_id:
            result = settle_claim(
                conn, claim["id"], recipient_id, now=now, transferred=True, depth=depth,
            )
            if result and result["status"] == "transferred":
                summary["processed"] += 1
                summary["transferred"] += 1
                continue
        changed = conn.execute(
            """UPDATE invite_reward_claims
                  SET status='no_recipient',reason='no_eligible_ancestor',updated_at=?
                WHERE id=? AND status='pending_upgrade'""",
            (now, claim["id"]),
        ).rowcount
        if changed:
            summary["processed"] += 1
            summary["no_recipient"] += 1
    return summary


def void_claims_for_upgrade(conn, upgrade_record_id, reason, now=None):
    now = int(now or time.time())
    return conn.execute(
        """UPDATE invite_reward_claims
              SET status='voided',voided_at=?,reason=?,updated_at=?
            WHERE upgrade_record_id=? AND status='pending_upgrade'""",
        (now, str(reason or "membership_voided"), now, int(upgrade_record_id)),
    ).rowcount


def void_pending_claims_for_invitee(conn, invitee_user_id, reason, now=None):
    now = int(now or time.time())
    return conn.execute(
        """UPDATE invite_reward_claims
              SET status='voided',voided_at=?,reason=?,updated_at=?
            WHERE invitee_user_id=? AND status='pending_upgrade'""",
        (now, str(reason or "membership_revoked"), now, int(invitee_user_id)),
    ).rowcount


def _notice_payload(row):
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    return {
        "id": int(row["id"]),
        "notice_type": row["notice_type"],
        **payload,
    }


def next_reward_notice(conn, user_id, now=None):
    now = int(now or time.time())
    unlocked = conn.execute(
        """SELECT * FROM invite_reward_notifications
            WHERE user_id=? AND notice_type='reward_unlocked' AND read_at IS NULL
            ORDER BY id LIMIT 1""",
        (int(user_id),),
    ).fetchone()
    if unlocked:
        return _notice_payload(unlocked)
    today = datetime.datetime.fromtimestamp(now, SHANGHAI).strftime("%Y-%m-%d")
    pending = conn.execute(
        """SELECT n.* FROM invite_reward_notifications n
            JOIN invite_reward_claims c ON c.id=n.claim_id
            WHERE n.user_id=? AND n.notice_type='pending_upgrade'
              AND c.status='pending_upgrade' AND c.expires_at>=?
              AND COALESCE(n.last_shown_day,'')<>?
            ORDER BY n.id LIMIT 1""",
        (int(user_id), now, today),
    ).fetchone()
    return _notice_payload(pending) if pending else None


def ack_reward_notice(conn, user_id, notice_id, now=None):
    now = int(now or time.time())
    row = conn.execute(
        "SELECT * FROM invite_reward_notifications WHERE id=? AND user_id=?",
        (int(notice_id), int(user_id)),
    ).fetchone()
    if not row:
        return False
    if row["notice_type"] == "pending_upgrade":
        today = datetime.datetime.fromtimestamp(now, SHANGHAI).strftime("%Y-%m-%d")
        conn.execute(
            "UPDATE invite_reward_notifications SET last_shown_day=?,updated_at=? WHERE id=?",
            (today, now, row["id"]),
        )
    else:
        conn.execute(
            "UPDATE invite_reward_notifications SET read_at=?,updated_at=? WHERE id=? AND read_at IS NULL",
            (now, now, row["id"]),
        )
    return True


def reward_points(conn, inviter_user_id, limit=20, offset=0, hidden=False):
    """返回独立邀请奖励积分汇总；绝不读取或修改 users.points。"""
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    if hidden:
        return {
            "total_reward_points": 0,
            "records": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
        }
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


def admin_reward_points(conn, filters=None, limit=100, offset=0):
    filters = filters or {}
    limit = max(1, min(int(limit or 100), 300))
    offset = max(0, int(offset or 0))
    where, args = ["1=1"], []
    if filters.get("inviter_user_id") is not None:
        where.append("rr.inviter_user_id=?")
        args.append(int(filters["inviter_user_id"]))
    if filters.get("inviter"):
        where.append("(ir.username LIKE ? OR ir.display_name LIKE ?)")
        value = "%" + str(filters["inviter"]).strip() + "%"
        args.extend([value, value])
    if filters.get("invitee"):
        where.append("(ie.username LIKE ? OR ie.display_name LIKE ?)")
        value = "%" + str(filters["invitee"]).strip() + "%"
        args.extend([value, value])
    if filters.get("status") in ("recorded", "pending_review", "voided"):
        where.append("rr.status=?")
        args.append(filters["status"])
    clause = " AND ".join(where)
    base = """ FROM invite_reward_point_records rr
        JOIN users ir ON ir.id=rr.inviter_user_id
        JOIN users ie ON ie.id=rr.invitee_user_id
        WHERE %s""" % clause
    total = conn.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
    sums = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN rr.status='recorded' THEN rr.reward_points ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN rr.status='voided' THEN rr.reward_points ELSE 0 END),0)" + base,
        args,
    ).fetchone()
    rows = conn.execute(
        """SELECT rr.*,ir.username AS inviter_username,ir.display_name AS inviter_name,
                  ie.username AS invitee_username,ie.display_name AS invitee_name""" + base +
        " ORDER BY rr.id DESC LIMIT ? OFFSET ?", args + [limit, offset],
    ).fetchall()
    return {
        "items": [{**dict(row),
                   "inviter_level_name": MEMBERSHIP_NAMES.get(row["inviter_level_snapshot"], ""),
                   "invitee_level_name": MEMBERSHIP_NAMES.get(row["invitee_level"], "")}
                  for row in rows],
        "total": int(total), "recorded_points": int(sums[0]), "voided_points": int(sums[1]),
        "limit": limit, "offset": offset,
    }


def admin_reward_claims(conn, filters=None, limit=100, offset=0):
    filters = filters or {}
    limit = max(1, min(int(limit or 100), 300))
    offset = max(0, int(offset or 0))
    where, args = ["1=1"], []
    if filters.get("status") in (
        "pending_upgrade", "credited", "transferred", "voided", "no_recipient",
    ):
        where.append("c.status=?")
        args.append(filters["status"])
    if filters.get("inviter"):
        where.append("di.username LIKE ?")
        args.append("%" + str(filters["inviter"]).strip() + "%")
    if filters.get("invitee"):
        where.append("ie.username LIKE ?")
        args.append("%" + str(filters["invitee"]).strip() + "%")
    clause = " AND ".join(where)
    base = """ FROM invite_reward_claims c
        JOIN users di ON di.id=c.direct_inviter_user_id
        JOIN users ie ON ie.id=c.invitee_user_id
        LEFT JOIN users recipient ON recipient.id=c.recipient_user_id
        WHERE %s""" % clause
    total = conn.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
    rows = conn.execute(
        """SELECT c.*,di.username AS direct_inviter_username,
                  ie.username AS invitee_username,recipient.username AS recipient_username""" +
        base + " ORDER BY c.id DESC LIMIT ? OFFSET ?", args + [limit, offset],
    ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        for key in ("direct_inviter_username", "invitee_username", "recipient_username"):
            item[key] = masked_admin_account(item.get(key))
    return {
        "items": items,
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


def admin_user_relations(conn, user_id, limit=20, now=None):
    user_id = int(user_id)
    limit = max(1, min(int(limit or 20), 100))
    now = int(now or time.time())
    referrer_row = conn.execute(
        """SELECT ui.id AS relation_id,ui.status,ui.risk_status,ui.bound_at,ui.source,
                  u.id AS user_id,u.username,u.display_name,
                  u.membership_tier,u.membership_expires_at
             FROM user_invites ui
             JOIN users u ON u.id=ui.inviter_user_id
            WHERE ui.invitee_user_id=?""",
        (user_id,),
    ).fetchone()
    total = conn.execute(
        "SELECT COUNT(*) FROM user_invites WHERE inviter_user_id=?",
        (user_id,),
    ).fetchone()[0]
    invitee_rows = conn.execute(
        """SELECT ui.id AS relation_id,ui.status,ui.risk_status,ui.bound_at,ui.source,
                  u.id AS user_id,u.username,u.display_name,
                  u.membership_tier,u.membership_expires_at
             FROM user_invites ui
             JOIN users u ON u.id=ui.invitee_user_id
            WHERE ui.inviter_user_id=?
            ORDER BY ui.id DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()

    def relation_user(row):
        if not row:
            return None
        tier = str(row["membership_tier"] or "")
        expires_at = int(row["membership_expires_at"] or 0)
        known_tier = tier in MEMBERSHIP_NAMES
        active = known_tier and expires_at > now
        return {
            "relation_id": int(row["relation_id"]),
            "user_id": int(row["user_id"]),
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
            "membership_tier": tier if known_tier else "",
            "membership_name": MEMBERSHIP_NAMES.get(tier, "非会员"),
            "membership_active": active,
            "membership_status": "active" if active else ("expired" if known_tier else "none"),
            "membership_expires_at": expires_at if known_tier else 0,
            "status": row["status"],
            "risk_status": row["risk_status"],
            "bound_at": int(row["bound_at"] or 0),
            "source": row["source"] or "",
        }

    return {
        "referrer": relation_user(referrer_row),
        "invitees": {
            "items": [relation_user(row) for row in invitee_rows],
            "total": int(total),
            "limit": limit,
        },
    }


def admin_reward_action(conn, reward_id, action, reason, operator, now=None):
    reward_id = int(reward_id)
    action = str(action or "").strip()
    reason = str(reason or "").strip()[:300]
    if action not in ("void", "restore"):
        raise InviteError("invalid_action", "奖励台账操作无效", 400)
    if not reason:
        raise InviteError("reason_required", "必须填写操作原因", 400)
    row = conn.execute("SELECT * FROM invite_reward_point_records WHERE id=?", (reward_id,)).fetchone()
    if not row:
        raise InviteError("reward_not_found", "奖励记录不存在", 404)
    target = "voided" if action == "void" else "recorded"
    if row["status"] == target:
        return dict(row)
    now = int(now or time.time())
    if action == "void":
        conn.execute("""UPDATE invite_reward_point_records
                        SET status='voided',voided_at=?,void_reason=?,voided_by=? WHERE id=?""",
                     (now, reason, operator, reward_id))
    else:
        if row["void_reason"] == "membership_refund":
            raise InviteError("refunded_reward_not_restorable", "退款订单的奖励不能恢复", 409)
        if row["event_type"] == "upgrade":
            records = conn.execute(
                """SELECT inviter_level_snapshot,invitee_level,reward_points,status
                     FROM invite_reward_point_records
                    WHERE invite_relation_id=? AND event_type='upgrade'""",
                (row["invite_relation_id"],),
            ).fetchall()
            cap = max([
                INVITE_REWARD_TOTALS.get(item["inviter_level_snapshot"], {}).get(item["invitee_level"], 0)
                for item in records
            ] or [0])
            current = sum(
                int(item["reward_points"] or 0)
                for item in records
                if item["status"] in ("recorded", "pending_review")
            )
            if current + int(row["reward_points"] or 0) > cap:
                raise InviteError("reward_cap_exceeded", "恢复后将超过该关系的升级奖励上限", 409)
        conn.execute("""UPDATE invite_reward_point_records
                        SET status='recorded',voided_at=NULL,void_reason=NULL,voided_by=? WHERE id=?""",
                     (operator, reward_id))
    return dict(conn.execute("SELECT * FROM invite_reward_point_records WHERE id=?", (reward_id,)).fetchone())


def ensure_user_code(conn, user_id, now=None, enforce_membership=True):
    now = int(now or time.time())
    require_inviter_eligibility(conn, user_id, now, enforce_membership=enforce_membership)
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


def rotate_user_code(conn, user_id, now=None, enforce_membership=True):
    now = int(now or time.time())
    campaign = _active_campaign(conn, now)
    if not campaign:
        raise InviteError("campaign_inactive", "邀请活动当前未开启", 409)
    conn.execute(
        "UPDATE invite_codes SET status='disabled' WHERE campaign_id=? AND inviter_user_id=? AND status='active'",
        (campaign["id"], int(user_id)),
    )
    return ensure_user_code(conn, user_id, now, enforce_membership)


def _masked_account(account_id):
    value = str(account_id or "")
    if len(value) <= 4:
        return value[:1] + "***" if value else ""
    return value[:2] + "****" + value[-2:]


def validate_code(conn, code, now=None, enforce_membership=True):
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
    require_inviter_eligibility(
        conn, row["inviter_user_id"], now, public=True,
        enforce_membership=enforce_membership,
    )
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


def bind_registration(conn, invitee_user_id, invite_code, source, client_ip="", device_id="",
                      hash_secret="", now=None, enforce_membership=True):
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
    invite = validate_code(conn, code, now, enforce_membership)
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
    if source not in VALID_SOURCES:
        raise InviteError("invalid_source", "邀请来源无效", 400)
    conn.execute(
        "UPDATE user_invites SET ip_hash=NULL,device_hash=NULL WHERE bound_at<? AND (ip_hash IS NOT NULL OR device_hash IS NOT NULL)",
        (now - RISK_HASH_RETENTION,),
    )
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


def admin_referral_journeys(conn, filters=None, days=30, limit=100, offset=0, now=None):
    filters = filters or {}
    now = int(now or time.time())
    try:
        days = max(1, min(int(days or 30), 365))
        limit = max(1, min(int(limit or 100), 300))
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise InviteError("invalid_pagination", "轨迹查询参数不正确")
    where = ["j.visited_at>=?"]
    args = [day_start(now) - (days - 1) * 86400]
    user = str(filters.get("user") or "").strip()
    if user:
        value = "%" + user + "%"
        where.append("(inviter.username LIKE ? OR inviter.display_name LIKE ? OR inviter.account_id LIKE ? "
                     "OR invitee.username LIKE ? OR invitee.display_name LIKE ? OR invitee.account_id LIKE ?)")
        args.extend([value] * 6)
    status = str(filters.get("status") or "").strip()
    if status == "visited":
        where.append("j.card_started_at IS NULL")
    elif status == "started":
        where.append("j.card_started_at IS NOT NULL AND j.registered_user_id IS NULL")
    elif status == "registered":
        where.append("j.registered_user_id IS NOT NULL")
    elif status == "rewarded":
        where.append("pa.id IS NOT NULL")
    elif status == "member":
        where.append("EXISTS(SELECT 1 FROM membership_upgrade_records mu WHERE mu.user_id=j.registered_user_id AND mu.status='effective')")
    elif status:
        raise InviteError("invalid_status", "轨迹状态筛选不正确")
    joins = """ FROM card_referral_journeys j
        JOIN users inviter ON inviter.id=j.inviter_user_id
        LEFT JOIN users invitee ON invitee.id=j.registered_user_id
        LEFT JOIN user_invites ui ON ui.id=j.invite_relation_id
        LEFT JOIN points_audit pa ON pa.transaction_key=('card-referral:' || j.journey_id) """
    clause = " WHERE " + " AND ".join(where)
    summary = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN j.card_started_at IS NOT NULL THEN 1 ELSE 0 END),0) AS started,
                  COALESCE(SUM(CASE WHEN j.registered_user_id IS NOT NULL THEN 1 ELSE 0 END),0) AS registered,
                  COALESCE(SUM(CASE WHEN j.invite_relation_id IS NOT NULL THEN 1 ELSE 0 END),0) AS bound,
                  COALESCE(SUM(CASE WHEN pa.id IS NOT NULL THEN 1 ELSE 0 END),0) AS trial_rewarded,
                  COALESCE(SUM(CASE WHEN j.published_at IS NOT NULL THEN 1 ELSE 0 END),0) AS published,
                  COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM membership_upgrade_records mu
                    WHERE mu.user_id=j.registered_user_id AND mu.status='effective') THEN 1 ELSE 0 END),0) AS members
           """ + joins + clause,
        args,
    ).fetchone()
    rows = conn.execute(
        """SELECT j.*,inviter.username AS inviter_username,inviter.display_name AS inviter_name,
                  inviter.account_id AS inviter_account_id,
                  invitee.username AS invitee_username,invitee.display_name AS invitee_name,
                  invitee.account_id AS invitee_account_id,
                  ui.status AS relation_status,ui.risk_status,ui.bound_at,
                  j.published_at,pa.created_at AS benefit_granted_at,
                  (SELECT MAX(mu.created_at) FROM membership_upgrade_records mu
                    WHERE mu.user_id=j.registered_user_id AND mu.status='effective') AS membership_at,
                  (SELECT COALESCE(SUM(rr.reward_points),0) FROM invite_reward_point_records rr
                    WHERE rr.invite_relation_id=j.invite_relation_id AND rr.status='recorded') AS membership_reward_points
           """ + joins + clause + " ORDER BY j.visited_at DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    total = int(summary["total"] or 0)
    registered = int(summary["registered"] or 0)
    items = []
    for row in rows:
        item = dict(row)
        for prefix in ("inviter", "invitee"):
            username = item.pop(prefix + "_username", "") or ""
            item[prefix + "_account"] = masked_admin_account(username)
            item[prefix + "_name"] = masked_admin_account(item.get(prefix + "_name") or "")
        items.append(item)
    return {
        "items": items,
        "total": total,
        "days": days,
        "limit": limit,
        "offset": offset,
        "summary": {
            "visited": total,
            "started": int(summary["started"] or 0),
            "registered": registered,
            "bound": int(summary["bound"] or 0),
            "trial_rewarded": int(summary["trial_rewarded"] or 0),
            "published": int(summary["published"] or 0),
            "members": int(summary["members"] or 0),
            "registration_rate": round(registered / total, 4) if total else 0,
        },
    }


def masked_admin_account(value):
    value = str(value or "")
    if len(value) == 11 and value.startswith("1") and value.isdigit():
        return value[:3] + "****" + value[-4:]
    return value


def admin_relation_view(item):
    view = dict(item)
    for prefix in ("inviter", "invitee"):
        username = view.pop(prefix + "_username", "") or ""
        view[prefix + "_account"] = masked_admin_account(username)
        view[prefix + "_name"] = masked_admin_account(view.get(prefix + "_name") or "")
    code = str(view.get("invite_code") or "")
    view["invite_code"] = code[:2] + "••••" if code else ""
    return view


def admin_reward_view(item):
    view = dict(item)
    for prefix in ("inviter", "invitee"):
        username = view.pop(prefix + "_username", "") or ""
        view[prefix + "_account"] = masked_admin_account(username)
        view[prefix + "_name"] = masked_admin_account(view.get(prefix + "_name") or "")
    return view


def admin_user_relation_view(item):
    if not item:
        return None
    view = dict(item)
    username = view.pop("username", "") or ""
    view["account"] = masked_admin_account(username)
    view["display_name"] = masked_admin_account(view.get("display_name") or "")
    return view


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
    rows = conn.execute("""SELECT ui.id,ui.campaign_id,ui.inviter_user_id,ui.invitee_user_id,
        ui.invite_code,ui.source,ui.status,ui.risk_status,ui.bound_at,ui.invalid_reason,ui.updated_at,c.name AS campaign_name,
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
    audit_fields = ("id", "status", "risk_status", "invalid_reason", "updated_at")
    before = {key: row[key] for key in audit_fields}
    if action == "invalidate":
        conn.execute("UPDATE user_invites SET status='invalid',invalid_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))
        conn.execute("""UPDATE invite_reward_point_records
                        SET status='voided',voided_at=?,void_reason='relation_invalid',voided_by='system'
                        WHERE invite_relation_id=? AND status<>'voided'""", (now, row["id"]))
    elif action == "unbind":
        conn.execute("UPDATE user_invites SET status='unbound',invalid_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))
        conn.execute("""UPDATE invite_reward_point_records
                        SET status='voided',voided_at=?,void_reason='relation_unbound',voided_by='system'
                        WHERE invite_relation_id=? AND status<>'voided'""", (now, row["id"]))
    elif action == "restore":
        conn.execute("UPDATE user_invites SET status='bound',risk_status='normal',invalid_reason=NULL,updated_at=? WHERE id=?", (now, row["id"]))
        conn.execute("UPDATE invite_reward_point_records SET status='recorded' WHERE invite_relation_id=? AND status='pending_review'", (row["id"],))
    elif action == "ban":
        conn.execute("UPDATE user_invites SET risk_status='blocked',invalid_reason=?,updated_at=? WHERE id=?", (reason, now, row["id"]))
        conn.execute("""UPDATE invite_reward_point_records
                        SET status='voided',voided_at=?,void_reason='relation_blocked',voided_by='system'
                        WHERE invite_relation_id=? AND status<>'voided'""", (now, row["id"]))
        conn.execute("UPDATE users SET account_status='banned' WHERE id=?", (row["invitee_user_id"],))
        conn.execute("DELETE FROM tokens WHERE username=(SELECT username FROM users WHERE id=?)", (row["invitee_user_id"],))
    elif action == "unban":
        conn.execute("UPDATE user_invites SET risk_status='normal',invalid_reason=NULL,updated_at=? WHERE id=?", (now, row["id"]))
        conn.execute("UPDATE users SET account_status='active' WHERE id=?", (row["invitee_user_id"],))
        conn.execute("UPDATE invite_reward_point_records SET status='recorded' WHERE invite_relation_id=? AND status='pending_review'", (row["id"],))
    fresh = conn.execute("SELECT * FROM user_invites WHERE id=?", (row["id"],)).fetchone()
    after = {key: fresh[key] for key in audit_fields}
    conn.execute("""INSERT INTO invite_admin_audit(
        operator_user_id,invite_relation_id,action,reason,before_json,after_json,created_at
    ) VALUES(?,?,?,?,?,?,?)""", (
        int(operator_user_id), row["id"], action, reason,
        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), now,
    ))
    return dict(fresh)


def admin_audit(conn, limit=100):
    try:
        limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    rows = conn.execute("""SELECT a.id,a.invite_relation_id,a.action,a.reason,a.created_at,
        u.username AS operator_username,u.display_name AS operator_name
        FROM invite_admin_audit a LEFT JOIN users u ON u.id=a.operator_user_id
        ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["operator_account"] = masked_admin_account(item.pop("operator_username", ""))
        item["operator_name"] = masked_admin_account(item.get("operator_name") or "")
        items.append(item)
    return items


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
    headers = ["关系ID", "邀请人昵称", "邀请人账号ID", "被邀请人昵称", "被邀请人账号ID",
               "来源", "关系状态", "风控状态", "账号状态", "绑定时间", "处理原因"]
    rows = [headers]
    for item in items:
        bound = datetime.datetime.fromtimestamp(int(item["bound_at"]), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        inviter_name = "" if item["inviter_name"] == item["inviter_username"] else item["inviter_name"] or ""
        invitee_name = "" if item["invitee_name"] == item["invitee_username"] else item["invitee_name"] or ""
        rows.append([item["id"], inviter_name, item["inviter_account_id"] or "",
                     invitee_name, item["invitee_account_id"] or "",
                     item["source"], item["status"], item["risk_status"],
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
    widths = [10, 18, 16, 18, 16, 16, 12, 12, 12, 22, 28]
    cols = "".join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths, 1))
    last_row = max(1, len(rows))
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' \
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' \
        '<sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' \
        '<cols>' + cols + '</cols><sheetData>' + "".join(sheet_rows) + \
        f'</sheetData><autoFilter ref="A1:K{last_row}"/></worksheet>'
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
