#!/usr/bin/env python3
# 黄雀 AI · 独立认证服务（零依赖，标准库）
# 端口 127.0.0.1:8095，nginx 把 /api/auth/ 路由过来。与 leadgen(8090) 完全隔离。
import datetime, sqlite3, hashlib, secrets, json, os, re, sys, time, urllib.parse, threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 微信支付客户端(仅用系统已装 cryptography)。缺 wxpay.py/cryptography 时置 None,
# 支付路由回 503,不拖垮整个认证服务。
try:
    import wxpay
except Exception:
    wxpay = None

try:
    from . import wechat_virtual_pay as wechat_vpay
except ImportError:  # 生产环境以脚本方式从 /home/ubuntu/auth-service 启动
    import wechat_virtual_pay as wechat_vpay

try:
    from . import wechat_subscribe
except ImportError:
    import wechat_subscribe

try:
    from . import invites
except ImportError:  # 生产环境以脚本方式从 /home/ubuntu/auth-service 启动
    import invites

try:
    from . import business_cards
except ImportError:
    import business_cards

try:
    from . import invite_network
except ImportError:
    import invite_network

try:
    from . import hq_cli_api
except ImportError:  # 生产环境以脚本方式从 /home/ubuntu/auth-service 启动
    import hq_cli_api

try:
    from .content_domains import pricing, error_contract
except ImportError:  # 生产环境以脚本方式从 /home/ubuntu/auth-service 启动
    from content_domains import pricing, error_contract

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
PORT = 8095
ITER = 200000
TOKEN_TTL = int(os.environ.get("HQ_AUTH_TOKEN_TTL", str(30 * 24 * 3600)))
AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")
AUTH_COOKIE_SECURE = os.environ.get("HQ_AUTH_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
INVITE_HASH_SECRET = os.environ.get("HQ_INVITE_HASH_SECRET", "")
INVITE_PUBLIC_BASE_URL = os.environ.get(
    "HQ_INVITE_PUBLIC_BASE_URL", "https://huangquechuanmei.com"
).strip().rstrip("/")
LOGIN_FAIL_WINDOW = int(os.environ.get("HQ_AUTH_FAIL_WINDOW", "300"))
LOGIN_FAIL_MAX = int(os.environ.get("HQ_AUTH_FAIL_MAX", "5"))
REGISTER_WINDOW = int(os.environ.get("HQ_AUTH_REGISTER_WINDOW", "120"))
REGISTER_MAX = int(os.environ.get("HQ_AUTH_REGISTER_MAX", "10"))
REGISTER_IP_WINDOW = int(os.environ.get("HQ_AUTH_REGISTER_IP_WINDOW", "60"))
REGISTER_IP_MAX = int(os.environ.get("HQ_AUTH_REGISTER_IP_MAX", "20"))
USERNAME_MAX_LENGTH = 64
PASSWORD_MAX_LENGTH = 128
NEW_USER_TRIAL_POINTS = int(os.environ.get("HQ_AUTH_TRIAL_POINTS", "16"))  # 暂时保留新用户注册赠送 16 点
# 充值定价：客户端只传金额(元)，点数一律服务端算，绝不信客户端传的点数——
# 否则用户能花 1 元买百万点。与 recharge.html / 小程序 recharge.js 保持一致。
# 固定档与自定义均按 10 点/元；自定义限 10~5000 元整。
RECHARGE_TIERS = {100: 1000, 200: 2000, 500: 5000}  # 金额(元) -> 点数
RECHARGE_RATE = 10                                   # 自定义:每元 10 点
RECHARGE_CUSTOM_MIN = 10
RECHARGE_CUSTOM_MAX = 5000
JSAPI_TEST_AMOUNT_YUAN = 0.1
JSAPI_TEST_POINTS = 1
MEMBERSHIP_YEAR_SECONDS = 365 * 24 * 3600
MEMBERSHIP_TIERS = {
    "experience": "体验官",
    "partner": "合伙人",
    "initiator": "发起人",
}
MEMBERSHIP_ORDER_TYPE = "membership_experience"
MEMBERSHIP_RENEWAL_ORDER_TYPE = "membership_experience_renewal"
MEMBERSHIP_DISCOUNT_BPS = {
    "experience": 10000,
    "partner": 7500,
    "initiator": 5500,
}
ANNOUNCEMENT_REQUEST_ID_MAX_LENGTH = 128
SHANGHAI_TZ = datetime.timezone(datetime.timedelta(hours=8))
MEMBERSHIP_ENFORCEMENT_ENV = "HQ_MEMBERSHIP_ENFORCEMENT_ENABLED"
VIRTUAL_PAY_RECONCILE_INTERVAL_SECONDS = 60
VIRTUAL_PAY_RECONCILE_BATCH = 100
VIRTUAL_PAY_RECONCILE_MIN_AGE_SECONDS = 10

def miniprogram_payments_enabled():
    """Operational kill switch for all mini-program payment order creation."""
    return os.environ.get("HQ_MINIPROGRAM_PAYMENTS_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )

def membership_enforcement_enabled():
    """会员强校验上线开关。

    默认关闭，允许先部署结构、核对并迁移存量名单，再显式开启。
    """
    return os.environ.get(MEMBERSHIP_ENFORCEMENT_ENV, "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def membership_discount_bps(tier):
    return MEMBERSHIP_DISCOUNT_BPS.get(str(tier or "").strip(), 10000)


def membership_discount_label(tier):
    return {
        10000: "原价",
        7500: "7.5折",
        5500: "5.5折",
    }.get(membership_discount_bps(tier), "原价")


def discounted_amount_fen(list_amount_yuan, tier):
    """按会员等级计算应付分数，使用整数计算避免浮点金额漂移。"""
    try:
        list_fen = int(round(float(list_amount_yuan) * 100))
    except (TypeError, ValueError, OverflowError):
        return None
    if list_fen <= 0:
        return None
    return (list_fen * membership_discount_bps(tier) + 5000) // 10000


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

def jsapi_recharge_quote(amount, membership_tier=""):
    """小程序 JSAPI 下单定价。

    保留 0.10 元 / 1 点的真机支付测试档，其余金额仍严格使用公开充值定价。
    """
    try:
        is_test_amount = int(round(float(amount) * 100)) == 10 and abs(float(amount) - 0.1) < 1e-9
    except (TypeError, ValueError, OverflowError):
        is_test_amount = False
    if is_test_amount:
        return JSAPI_TEST_AMOUNT_YUAN, JSAPI_TEST_POINTS
    points = recharge_points_for(amount)
    if points is None:
        return None
    amount_fen = discounted_amount_fen(int(amount), membership_tier)
    return amount_fen / 100.0, points


def purchase_quote(amount, product_type="points", jsapi=False, membership_tier=""):
    product_type = (product_type or "points").strip()
    if product_type in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
        membership_amount = pricing.get_price("membership.experience.price_yuan")
        try:
            if abs(float(amount) - membership_amount) > 1e-9:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        points = 0 if product_type == MEMBERSHIP_RENEWAL_ORDER_TYPE else pricing.get_price("membership.experience.bonus_points")
        return membership_amount, points, product_type
    if product_type != "points":
        return None
    quote = jsapi_recharge_quote(amount, membership_tier) if jsapi else None
    if jsapi:
        if quote is None:
            return None
        return quote[0], quote[1], "points"
    points = recharge_points_for(amount)
    if points is None:
        return None
    amount_fen = discounted_amount_fen(int(amount), membership_tier)
    return amount_fen / 100.0, points, "points"


def public_recharge_packages(membership_tier):
    discount_bps = membership_discount_bps(membership_tier)
    return {
        "membership_tier": str(membership_tier or ""),
        "discount_bps": discount_bps,
        "discount_label": membership_discount_label(membership_tier),
        "items": [
            {
                "list_amount": amount,
                "pay_amount_fen": discounted_amount_fen(amount, membership_tier),
                "pay_amount": discounted_amount_fen(amount, membership_tier) / 100.0,
                "points": points,
            }
            for amount, points in sorted(RECHARGE_TIERS.items())
        ],
    }
LOGIN_FAILS = {}
REGISTER_HITS = {}
REGISTER_HITS_LOCK = threading.Lock()
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
CANVAS_MAX_BOARDS_PER_USER = int(os.environ.get("HQ_CANVAS_MAX_BOARDS_PER_USER", "50"))
CANVAS_MAX_MEMBERS_PER_BOARD = int(os.environ.get("HQ_CANVAS_MAX_MEMBERS_PER_BOARD", "20"))
CANVAS_OPS_RATE_WINDOW_SECONDS = int(os.environ.get("HQ_CANVAS_OPS_RATE_WINDOW_SECONDS", "10"))
CANVAS_OPS_RATE_MAX_PER_WINDOW = int(os.environ.get("HQ_CANVAS_OPS_RATE_MAX_PER_WINDOW", "10"))
CANVAS_PRESENCE_MIN_INTERVAL_SECONDS = int(os.environ.get("HQ_CANVAS_PRESENCE_MIN_INTERVAL_SECONDS", "3"))
CANVAS_SYNC_MAX_WAIT_SECONDS = int(os.environ.get("HQ_CANVAS_SYNC_MAX_WAIT_SECONDS", "30"))
CANVAS_SYNC_WAIT_MAX_CONCURRENT = int(os.environ.get("HQ_CANVAS_SYNC_WAIT_MAX_CONCURRENT", "100"))
CANVAS_SYNC_WAIT_MAX_PER_USER = int(os.environ.get("HQ_CANVAS_SYNC_WAIT_MAX_PER_USER", "3"))
# 长轮询等待名额: 全局信号量限制同时在 hold 的请求数(防线程被耗尽),
# 每用户计数防单账号吃光全局额度; 超额时降级为立即返回(wait=0 行为)
_CANVAS_SYNC_WAIT_SEMAPHORE = threading.BoundedSemaphore(value=max(1, CANVAS_SYNC_WAIT_MAX_CONCURRENT))
_CANVAS_SYNC_WAIT_USERS = {}
_CANVAS_SYNC_WAIT_USERS_LOCK = threading.Lock()

def _canvas_sync_wait_acquire(username):
    if not _CANVAS_SYNC_WAIT_SEMAPHORE.acquire(blocking=False):
        return False
    with _CANVAS_SYNC_WAIT_USERS_LOCK:
        count = _CANVAS_SYNC_WAIT_USERS.get(username, 0)
        if count >= CANVAS_SYNC_WAIT_MAX_PER_USER:
            _CANVAS_SYNC_WAIT_SEMAPHORE.release()
            return False
        _CANVAS_SYNC_WAIT_USERS[username] = count + 1
    return True

def _canvas_sync_wait_release(username):
    with _CANVAS_SYNC_WAIT_USERS_LOCK:
        count = _CANVAS_SYNC_WAIT_USERS.get(username, 0)
        if count <= 1:
            _CANVAS_SYNC_WAIT_USERS.pop(username, None)
        else:
            _CANVAS_SYNC_WAIT_USERS[username] = count - 1
    _CANVAS_SYNC_WAIT_SEMAPHORE.release()

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
        card_initial_password INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "card_initial_password" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN card_initial_password INTEGER NOT NULL DEFAULT 0")
    if "account_id" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN account_id TEXT")
    if "account_status" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'")
    if "membership_tier" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN membership_tier TEXT NOT NULL DEFAULT ''")
    if "membership_started_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN membership_started_at INTEGER")
    if "membership_expires_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN membership_expires_at INTEGER")
    _ensure_all_account_ids(c)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_account_id ON users(account_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS tokens(
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        expires_at INTEGER,
        scope TEXT NOT NULL DEFAULT 'account'
    )""")
    token_cols = {r["name"] for r in c.execute("PRAGMA table_info(tokens)").fetchall()}
    if "scope" not in token_cols:
        c.execute("ALTER TABLE tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'account'")
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
    audit_cols = {r["name"] for r in c.execute("PRAGMA table_info(points_audit)").fetchall()}
    if "transaction_key" not in audit_cols:
        c.execute("ALTER TABLE points_audit ADD COLUMN transaction_key TEXT")
    # 任务扣点/退点接入审计后，这张表按任务量增长（原来只有人工加减点，几乎不涨）。
    # 按用户查流水是后台最常用的路径，没索引会随表全扫。
    c.execute("CREATE INDEX IF NOT EXISTS idx_points_audit_user ON points_audit(username, id DESC)")
    # 退款键直接落在现有资金流水上：Auth 已提交但响应丢失时，调用方用同一个键重试，
    # 只能命中原流水，不能再次加点。NULL 不参与冲突，老调用方保持原行为。
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_points_audit_transaction_key "
              "ON points_audit(transaction_key)")
    c.execute("""CREATE TABLE IF NOT EXISTS announcement_campaigns(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        detail TEXT NOT NULL,
        audience_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'published',
        recipient_count INTEGER NOT NULL DEFAULT 0,
        breakdown_json TEXT NOT NULL DEFAULT '{}',
        created_by TEXT NOT NULL,
        request_id TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        published_at INTEGER NOT NULL,
        recalled_at INTEGER,
        recalled_by TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_announcement_campaigns_created ON announcement_campaigns(id DESC)")
    campaign_cols = {r["name"] for r in c.execute("PRAGMA table_info(announcement_campaigns)").fetchall()}
    if "wechat_push_requested" not in campaign_cols:
        c.execute("ALTER TABLE announcement_campaigns ADD COLUMN wechat_push_requested INTEGER NOT NULL DEFAULT 0")
    if "wechat_recipient_count" not in campaign_cols:
        c.execute("ALTER TABLE announcement_campaigns ADD COLUMN wechat_recipient_count INTEGER NOT NULL DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS user_notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'system',
        title TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        campaign_id INTEGER,
        read_at INTEGER,
        popup_snoozed_until INTEGER
    )""")
    notification_cols = {r["name"] for r in c.execute("PRAGMA table_info(user_notifications)").fetchall()}
    if "campaign_id" not in notification_cols:
        c.execute("ALTER TABLE user_notifications ADD COLUMN campaign_id INTEGER")
    if "read_at" not in notification_cols:
        c.execute("ALTER TABLE user_notifications ADD COLUMN read_at INTEGER")
    if "popup_snoozed_until" not in notification_cols:
        c.execute("ALTER TABLE user_notifications ADD COLUMN popup_snoozed_until INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_notifications_user ON user_notifications(username,id DESC)")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_user_notifications_campaign_user
                 ON user_notifications(campaign_id,username) WHERE campaign_id IS NOT NULL""")
    c.execute("""CREATE TABLE IF NOT EXISTS wechat_subscription_grants(
        username TEXT NOT NULL,
        event_type TEXT NOT NULL,
        template_id TEXT NOT NULL,
        openid TEXT NOT NULL,
        remaining INTEGER NOT NULL DEFAULT 0,
        last_choice TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(username,event_type,template_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS wechat_subscription_outbox(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        event_type TEXT NOT NULL,
        business_id TEXT NOT NULL,
        job_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        template_id TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_until INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        last_error TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        sent_at INTEGER,
        UNIQUE(username,event_type,business_id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_wechat_sub_ready ON wechat_subscription_outbox(status,next_retry_at,id)")
    c.execute("""CREATE TABLE IF NOT EXISTS membership_recharge_records(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL,
        tier TEXT NOT NULL,
        before_expires_at INTEGER,
        after_expires_at INTEGER NOT NULL,
        operator TEXT NOT NULL,
        reason TEXT,
        created_at INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_membership_recharge_user ON membership_recharge_records(username,id DESC)")
    c.execute("""CREATE TABLE IF NOT EXISTS membership_voice_slot_entitlements(
        username TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_order_id TEXT,
        created_at INTEGER NOT NULL
    )""")
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
    c.execute("""CREATE TABLE IF NOT EXISTS virtual_pay_orders(
        order_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        openid TEXT NOT NULL,
        package_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        amount_fen INTEGER NOT NULL,
        points INTEGER NOT NULL,
        env INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'created',
        created_at INTEGER NOT NULL,
        paid_at INTEGER,
        credited_at INTEGER,
        delivered_at INTEGER,
        wx_order_id TEXT,
        wxpay_order_id TEXT,
        raw_order_json TEXT,
        last_error TEXT,
        order_type TEXT NOT NULL DEFAULT 'points'
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_virtual_pay_orders_user ON virtual_pay_orders(username, created_at DESC)")
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tokens)").fetchall()}
    if "expires_at" not in cols:
        c.execute("ALTER TABLE tokens ADD COLUMN expires_at INTEGER")
    rcols = {r["name"] for r in c.execute("PRAGMA table_info(recharge_orders)").fetchall()}
    if "transaction_id" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN transaction_id TEXT")  # 微信支付流水号
    if "pay_channel" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN pay_channel TEXT")     # wxpay_native / wxpay_jsapi / manual
    if "order_type" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'points'")
    if "list_amount" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN list_amount REAL")
    if "pricing_tier" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN pricing_tier TEXT NOT NULL DEFAULT ''")
    if "discount_bps" not in rcols:
        c.execute("ALTER TABLE recharge_orders ADD COLUMN discount_bps INTEGER NOT NULL DEFAULT 10000")
    vcols = {r["name"] for r in c.execute("PRAGMA table_info(virtual_pay_orders)").fetchall()}
    if "list_amount_fen" not in vcols:
        c.execute("ALTER TABLE virtual_pay_orders ADD COLUMN list_amount_fen INTEGER")
    if "pricing_tier" not in vcols:
        c.execute("ALTER TABLE virtual_pay_orders ADD COLUMN pricing_tier TEXT NOT NULL DEFAULT ''")
    if "discount_bps" not in vcols:
        c.execute("ALTER TABLE virtual_pay_orders ADD COLUMN discount_bps INTEGER NOT NULL DEFAULT 10000")
    if "order_type" not in vcols:
        c.execute("ALTER TABLE virtual_pay_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'points'")
    c.execute("""CREATE TABLE IF NOT EXISTS membership_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        before_tier TEXT NOT NULL DEFAULT '',
        after_tier TEXT NOT NULL DEFAULT '',
        before_expires_at INTEGER,
        after_expires_at INTEGER,
        operator TEXT NOT NULL,
        reason TEXT,
        created_at INTEGER NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_membership_audit_user ON membership_audit(username, id DESC)")
    user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "wx_openid" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN wx_openid TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wx_openid ON users(wx_openid) WHERE wx_openid IS NOT NULL")
    invites.init_schema(c)
    business_cards.init_schema(c)
    hq_cli_api.init_schema(c)
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


VIDEO_SUBSCRIPTION_KINDS = {"video", "tryon", "xiaole_video", "sora_video", "cinematic"}
def _video_subscription_title(kind):
    return {
        "video": "视频作品已完成",
        "tryon": "视频换装已完成",
        "xiaole_video": "视频作品已完成",
        "sora_video": "视频作品已完成",
        "cinematic": "剧情视频已完成",
    }.get(str(kind or "").strip().lower(), "视频作品已完成")


def subscription_status(username):
    configs = wechat_subscribe.public_configs()
    c = db()
    try:
        events = []
        for config in configs:
            row = c.execute(
                """SELECT remaining,last_choice FROM wechat_subscription_grants
                   WHERE username=? AND event_type=? AND template_id=?""",
                (username, config["event_type"], config["template_id"]),
            ).fetchone()
            events.append({
                "event_type": config["event_type"],
                "template_id": config["template_id"],
                "label": config["label"],
                "configured": config["configured"],
                "remaining": int(row["remaining"] or 0) if row else 0,
                "last_choice": row["last_choice"] if row else "",
            })
    finally:
        c.close()
    return {
        "configured": any(config["configured"] for config in configs),
        "events": events,
    }


def _subscription_openid(wx_code):
    if not str(wx_code or "").strip():
        return None, "missing_wx_code"
    session = wechat_vpay.code_to_session(str(wx_code).strip())
    openid = str(session.get("openid") or "").strip()
    if not openid:
        return None, "missing_openid"
    return openid, None


def record_subscription_choices(username, choices, wx_code):
    if not isinstance(choices, dict) or len(choices) != 1:
        return None, "bad_choices"
    event_type, raw_choice = next(iter(choices.items()))
    config = wechat_subscribe.config(str(event_type or "").strip())
    choice = str(raw_choice or "").strip().lower()
    if not config or not config["configured"]:
        return None, "not_configured"
    if choice not in {"accept", "reject", "ban", "filter"}:
        return None, "bad_choices"
    openid, err = _subscription_openid(wx_code)
    if err:
        return None, err
    now = int(time.time())
    template_id = config["template_id"]
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """INSERT INTO wechat_subscription_grants(
                   username,event_type,template_id,openid,remaining,last_choice,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(username,event_type,template_id) DO UPDATE SET
                   openid=excluded.openid,
                   remaining=wechat_subscription_grants.remaining+excluded.remaining,
                   last_choice=excluded.last_choice,updated_at=excluded.updated_at""",
            (username, config["event_type"], template_id, openid,
             1 if choice == "accept" else 0, choice, now),
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {"openid_bound": bool(openid), **subscription_status(username)}, None


def enqueue_video_subscription(username, job_id, kind):
    kind = str(kind or "").strip().lower()
    if kind not in VIDEO_SUBSCRIPTION_KINDS:
        return {"status": "ignored"}
    now = int(time.time())
    config = wechat_subscribe.public_config()
    business_id = "job:%s" % str(job_id)
    payload = {"title": _video_subscription_title(kind), "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "tip": "视频已生成，可前往资产库查看"}
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone() is None:
            c.rollback()
            return {"status": "unknown_user"}
        existing = c.execute(
            "SELECT status FROM wechat_subscription_outbox WHERE username=? AND event_type=? AND business_id=?",
            (username, wechat_subscribe.EVENT_TYPE, business_id),
        ).fetchone()
        if existing:
            c.rollback()
            return {"status": "duplicate", "delivery_status": existing["status"]}
        status = "pending" if config["configured"] else "dropped"
        c.execute(
            """INSERT INTO wechat_subscription_outbox(
                   username,event_type,business_id,job_id,kind,template_id,status,payload_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (username, wechat_subscribe.EVENT_TYPE, business_id, int(job_id), kind, config["template_id"], status,
             json.dumps(payload, ensure_ascii=False), now, now),
        )
        c.commit()
        return {"status": "accepted", "delivery_status": status}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _claim_video_subscription():
    now = int(time.time())
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        # A crashed sender already consumed one local grant. Restore it before
        # retrying the durable row; a rare duplicate is safer than a lost notice.
        stale = c.execute(
            """SELECT id,username,event_type,template_id
               FROM wechat_subscription_outbox
               WHERE status='sending' AND lease_until<?""",
            (now,),
        ).fetchall()
        for item in stale:
            c.execute(
                """UPDATE wechat_subscription_grants SET remaining=remaining+1,updated_at=?
                   WHERE username=? AND event_type=? AND template_id=?""",
                (now, item["username"], item["event_type"], item["template_id"]),
            )
            c.execute(
                """UPDATE wechat_subscription_outbox
                   SET status='failed',lease_until=0,next_retry_at=?,
                       last_error='sender lease expired',updated_at=?
                   WHERE id=? AND status='sending'""",
                (now, now, item["id"]),
            )
        c.execute("UPDATE wechat_subscription_outbox SET status='pending' WHERE status='failed' AND next_retry_at<=?", (now,))
        row = c.execute(
            """SELECT * FROM wechat_subscription_outbox
               WHERE status='pending' AND next_retry_at<=?
               ORDER BY id LIMIT 1""", (now,)
        ).fetchone()
        if not row:
            c.commit()
            return None
        config = wechat_subscribe.config(row["event_type"])
        if not config or not config["configured"]:
            c.execute("UPDATE wechat_subscription_outbox SET status='dropped',updated_at=? WHERE id=?", (now, row["id"]))
            c.commit()
            return None
        grant = c.execute(
            """SELECT remaining,openid FROM wechat_subscription_grants
               WHERE username=? AND event_type=? AND template_id=?""",
            (row["username"], row["event_type"], row["template_id"]),
        ).fetchone()
        if not grant or not grant["openid"] or int(grant["remaining"] or 0) < 1:
            c.execute("UPDATE wechat_subscription_outbox SET status='dropped',updated_at=? WHERE id=?", (now, row["id"]))
            c.commit()
            return None
        c.execute(
            """UPDATE wechat_subscription_grants SET remaining=remaining-1,updated_at=?
               WHERE username=? AND event_type=? AND template_id=? AND remaining>0""",
            (now, row["username"], row["event_type"], row["template_id"]),
        )
        c.execute(
            """UPDATE wechat_subscription_outbox
               SET status='sending',lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?""",
            (now + 60, now, row["id"]),
        )
        c.commit()
        result = {key: row[key] for key in row.keys()}
        result["openid"] = grant["openid"]
        result["attempts"] = int(result["attempts"] or 0) + 1
        return result
    finally:
        c.close()


def _finish_video_subscription(row, status, error="", restore_grant=False):
    now = int(time.time())
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        delay = min(3600, 2 ** min(int(row.get("attempts") or 0), 10))
        cur = c.execute(
            """UPDATE wechat_subscription_outbox SET status=?,lease_until=0,next_retry_at=?,
               last_error=?,updated_at=?,sent_at=? WHERE id=? AND status='sending'""",
            (status, now + delay if status == "failed" else 0, str(error)[:300], now,
             now if status == "sent" else None, row["id"]),
        )
        if cur.rowcount and restore_grant:
            c.execute(
                """UPDATE wechat_subscription_grants SET remaining=remaining+1,updated_at=?
                   WHERE username=? AND event_type=? AND template_id=?""",
                (now, row["username"], row["event_type"], row["template_id"]),
            )
        c.commit()
    finally:
        c.close()


def _video_subscription_worker():
    while True:
        row = _claim_video_subscription()
        if not row:
            time.sleep(5)
            continue
        try:
            payload = json.loads(row["payload_json"])
            wechat_subscribe.send(
                row["openid"], payload["title"], payload["time"], payload["tip"], row["template_id"],
                event_type=row["event_type"],
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "send_failed"))
            terminal = code in {"40003", "43101"}
            _finish_video_subscription(
                row, "dropped" if terminal else "failed", str(exc),
                restore_grant=not terminal,
            )
        else:
            _finish_video_subscription(row, "sent")

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

def credential_length_error(username, password):
    if len(username) > USERNAME_MAX_LENGTH:
        return {"code": "username_too_long", "detail": "账号最多 64 位"}
    if len(password) > PASSWORD_MAX_LENGTH:
        return {"code": "password_too_long", "detail": "密码最多 128 位"}
    return None

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

def register_account(username, password, display_name=None, invite_code="", invite_source="web_manual",
                     client_ip="", device_id="", card=None, invite_attribution_token=""):
    """在一个事务中创建账号、保存直接邀请关系并签发令牌。"""
    username = str(username or "").strip()
    password = str(password or "")
    display_name = str(display_name or username).strip() or username
    invite_code = invites.normalize_code(invite_code)
    invite_attribution_token = str(invite_attribution_token or "").strip()
    device_id = str(device_id or "").strip()
    if not username or not password:
        return None, {"status": 400, "code": "missing_credentials", "detail": "请填写账号和密码"}
    length_error = credential_length_error(username, password)
    if length_error:
        return None, {"status": 400, **length_error}
    if len(display_name) > 32:
        return None, {"status": 400, "code": "display_name_too_long", "detail": "昵称最多 32 个字符"}
    if any(ch.isspace() for ch in username):
        return None, {"status": 400, "code": "invalid_username", "detail": "账号不能包含空白字符"}
    if len(password) < 6:
        return None, {"status": 400, "code": "password_too_short", "detail": "密码至少 6 位"}
    if len(invite_code) > 32:
        return None, {"status": 400, "code": "invalid_code", "detail": "邀请码无效"}
    if len(device_id) > 256:
        return None, {"status": 400, "code": "device_id_too_long", "detail": "设备标识过长"}

    salt = secrets.token_hex(16)
    password_hash = hash_pw(password, salt)  # 慢哈希必须在取得 SQLite 写锁之前完成
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if invite_attribution_token:
            attribution = business_cards.verify_attribution(invite_attribution_token, INVITE_HASH_SECRET)
            if invite_code and invite_code != invites.normalize_code(attribution["code"]):
                raise business_cards.CardError("invalid_invite_attribution", "邀请归因不匹配", 409)
            if business_cards.public_owner(c, attribution["card_public_id"]) != int(attribution["owner_user_id"]):
                raise business_cards.CardError("invalid_invite_attribution", "邀请归因已失效", 409)
            invite_code = invites.normalize_code(attribution["code"])
        account_id = _new_unique_account_id(c)
        cur = c.execute("""INSERT INTO users(
            username,pw_hash,pw_salt,display_name,points,role,must_change,account_id
        ) VALUES(?,?,?,?,?,?,0,?)""", (
            username, password_hash, salt, display_name, NEW_USER_TRIAL_POINTS, "member", account_id,
        ))
        relation = invites.bind_registration(
            c, cur.lastrowid, invite_code, invite_source,
            client_ip=client_ip, device_id=device_id, hash_secret=INVITE_HASH_SECRET,
            enforce_membership=False,
        )
        if card is not None:
            business_cards.create_draft(c, cur.lastrowid, card)
        saved_card = business_cards.mine(c, cur.lastrowid) if card is not None else None
        token = issue_token(username, c)
        c.commit()
        return {
            "token": token,
            "user": public_user(
                username, display_name, NEW_USER_TRIAL_POINTS, account_id=account_id,
            ),
            "invite_bound": bool(relation),
            "card": saved_card,
        }, None
    except invites.InviteError as exc:
        c.rollback()
        return None, {"status": exc.http_status, "code": exc.code, "detail": exc.detail}
    except business_cards.CardError as exc:
        c.rollback()
        return None, {"status": exc.status, "code": exc.code, "detail": exc.detail}
    except sqlite3.IntegrityError:
        c.rollback()
        return None, {"status": 409, "code": "username_exists", "detail": "账号已存在"}
    except Exception:
        c.rollback()
        return None, {"status": 500, "code": "register_failed", "detail": "注册失败"}
    finally:
        c.close()


def miniprogram_openid(wx_code):
    if not str(wx_code or "").strip():
        return None, {"status": 400, "code": "missing_wx_code", "detail": "缺少微信登录凭证"}
    try:
        session = wechat_vpay.code_to_session(str(wx_code).strip())
    except wechat_vpay.VirtualPayError:
        return None, {"status": 400, "code": "wechat_auth_failed", "detail": "微信登录态获取失败"}
    except Exception:
        return None, {"status": 503, "code": "wechat_auth_unavailable", "detail": "微信登录暂不可用"}
    openid = str(session.get("openid") or "").strip()
    if not openid:
        return None, {"status": 400, "code": "wechat_auth_failed", "detail": "微信登录态获取失败"}
    return openid, None


def register_miniprogram_card(wx_code, phone, card, device_id, invite_code="", invite_attribution_token="", client_ip="", separate_sessions=False):
    """Create a phone account and bind its Mini Program identity in one SQLite transaction."""
    phone = str(phone or "").strip()
    device_id = str(device_id or "").strip()
    invite_code = invites.normalize_code(invite_code)
    invite_attribution_token = str(invite_attribution_token or "").strip()
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        return None, {"status": 400, "code": "invalid_phone", "detail": "请输入中国大陆 11 位手机号"}
    if not isinstance(card, dict):
        return None, {"status": 400, "code": "invalid_card", "detail": "名片信息无效"}
    required = (card.get("name"), card.get("title") or card.get("headline"), card.get("company"))
    if not all(isinstance(value, str) and value.strip() for value in required):
        return None, {"status": 400, "code": "incomplete_card", "detail": "请填写姓名、职称和公司"}
    card = dict(card)
    card["phone"] = phone
    if not device_id:
        return None, {"status": 400, "code": "missing_device_id", "detail": "缺少设备标识"}
    if len(device_id) > 256:
        return None, {"status": 400, "code": "device_id_too_long", "detail": "设备标识过长"}
    if len(invite_code) > 32:
        return None, {"status": 400, "code": "invalid_code", "detail": "邀请码无效"}
    openid, err = miniprogram_openid(wx_code)
    if err:
        return None, err
    salt = secrets.token_hex(16)
    password_hash = hash_pw(phone, salt)
    attribution = None
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        owner_id = business_cards.openid_owner(c, openid)
        if owner_id:
            owner = c.execute("SELECT * FROM users WHERE id=? AND account_status='active'", (owner_id,)).fetchone()
            if not owner:
                return None, {"status": 404, "code": "card_unbound", "detail": "微信尚未绑定名片"}
            token_key = "card_token" if separate_sessions else "token"
            token = issue_token(owner["username"], c, scope="card" if separate_sessions else "account")
            response = {
                "user": public_user(owner["username"], owner["display_name"], owner["points"], owner["role"], owner["must_change"], owner["account_id"], owner["membership_tier"], owner["membership_started_at"], owner["membership_expires_at"]),
                "card": card_for_owner(c, owner_id),
                "invite_bound": bool(c.execute(
                    "SELECT 1 FROM user_invites WHERE invitee_user_id=?", (owner_id,),
                ).fetchone()),
                "created": False,
                "invite_rewarded": False,
                "ai_account": owner["username"],
                "initial_password": initial_password_change_required(owner),
            }
            response[token_key] = token
            c.commit()
            return response, None
        if c.execute("SELECT 1 FROM users WHERE username=?", (phone,)).fetchone():
            return None, {"status": 409, "code": "account_exists", "detail": "该手机号已有账号，请使用账号登录"}
        initial_points = 0
        if invite_attribution_token:
            attribution = business_cards.verify_attribution(invite_attribution_token, INVITE_HASH_SECRET)
            if invite_code and invite_code != invites.normalize_code(attribution["code"]):
                raise business_cards.CardError("invalid_invite_attribution", "邀请归因不匹配", 409)
            if business_cards.public_owner(c, attribution["card_public_id"]) != int(attribution["owner_user_id"]):
                raise business_cards.CardError("invalid_invite_attribution", "邀请归因已失效", 409)
            invite_code = invites.normalize_code(attribution["code"])
        account_id = _new_unique_account_id(c)
        cur = c.execute("""INSERT INTO users(
            username,pw_hash,pw_salt,display_name,points,role,must_change,card_initial_password,account_id
        ) VALUES(?,?,?,?,?,?,0,1,?)""", (
            phone, password_hash, salt, phone, initial_points, "member", account_id,
        ))
        business_cards.create_draft(c, cur.lastrowid, card)
        business_cards.bind_miniprogram_openid(c, cur.lastrowid, openid)
        relation = invites.bind_registration(
            c, cur.lastrowid, invite_code, "miniprogram_card",
            client_ip=client_ip, device_id=device_id, hash_secret=INVITE_HASH_SECRET,
            enforce_membership=False,
        )
        if relation and attribution and attribution.get("journey_id"):
            business_cards.start_referral_journey(c, attribution, relation["campaign_id"])
            journey = business_cards.convert_referral_journey(
                c, attribution, cur.lastrowid, relation["id"],
            )
            if journey:
                initial_points = pricing.get_price("invite.card_trial_reward")
                c.execute("UPDATE users SET points=? WHERE id=?", (initial_points, cur.lastrowid))
        if relation and initial_points > 0:
            transaction_key = (
                "card-referral:" + str(attribution.get("journey_id"))
                if attribution and attribution.get("journey_id")
                else "card-referral-relation:" + str(relation["id"])
            )
            c.execute(
                """INSERT INTO points_audit(
                       who_admin,username,delta,before_points,after_points,reason,created_at,transaction_key
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (SYSTEM_ACTOR, phone, initial_points, 0, initial_points,
                 "名片邀请注册奖励", int(time.time()), transaction_key),
            )
        token_key = "card_token" if separate_sessions else "token"
        token = issue_token(phone, c, scope="card" if separate_sessions else "account")
        saved_card = card_for_owner(c, cur.lastrowid)
        c.commit()
        response = {
            "user": public_user(phone, phone, initial_points, "member", False, account_id),
            "card": saved_card,
            "invite_bound": bool(relation),
            "created": True,
            "invite_rewarded": bool(relation and initial_points > 0),
            "invite_reward_points": initial_points if relation else 0,
            "ai_account": phone,
            "initial_password": True,
        }
        response[token_key] = token
        return response, None
    except invites.InviteError as exc:
        c.rollback()
        return None, {"status": exc.http_status, "code": exc.code, "detail": exc.detail}
    except business_cards.CardError as exc:
        c.rollback()
        return None, {"status": exc.status, "code": exc.code, "detail": exc.detail}
    except sqlite3.IntegrityError:
        c.rollback()
        return None, {"status": 409, "code": "account_exists", "detail": "该手机号已有账号，请使用账号登录"}
    except Exception:
        c.rollback()
        return None, {"status": 500, "code": "register_failed", "detail": "注册失败"}
    finally:
        c.close()


def card_for_owner(conn, user_id):
    card = business_cards.mine(conn, user_id)
    if not card:
        return None
    user = conn.execute(
        "SELECT username,must_change,card_initial_password FROM users WHERE id=?",
        (int(user_id),),
    ).fetchone()
    card["ai_account"] = user["username"] if user else ""
    card["initial_password"] = initial_password_change_required(user)
    card["wechat_bound"] = bool(conn.execute(
        "SELECT 1 FROM business_cards WHERE user_id=? AND miniprogram_openid IS NOT NULL",
        (int(user_id),),
    ).fetchone())
    try:
        card["invite_code"] = invites.ensure_user_code(
            conn, user_id, enforce_membership=False,
        )["code"]
    except invites.InviteError:
        card["invite_code"] = ""
    return card


def initial_password_change_required(row):
    keys = set(row.keys()) if row is not None and hasattr(row, "keys") else set()
    return bool(row and (("must_change" in keys and row["must_change"]) or
                         ("card_initial_password" in keys and row["card_initial_password"])))

def membership_public(tier="", started_at=None, expires_at=None, now=None):
    tier = str(tier or "").strip()
    expires_at = int(expires_at or 0)
    started_at = int(started_at or 0)
    known_tier = tier in MEMBERSHIP_TIERS
    active = known_tier and expires_at > int(now or time.time())
    return {
        "membership_tier": tier if active else "",
        "membership_name": MEMBERSHIP_TIERS.get(tier, "") if active else "",
        "membership_active": active,
        "membership_started_at": started_at if active else 0,
        "membership_expires_at": expires_at if active else 0,
        "membership_status": "active" if active else ("expired" if known_tier else "none"),
        "membership_last_tier": tier if known_tier else "",
        "membership_last_name": MEMBERSHIP_TIERS.get(tier, "") if known_tier else "",
        "membership_last_expires_at": expires_at if known_tier else 0,
        "points_purchase_discount_bps": membership_discount_bps(tier) if active else 10000,
        "points_purchase_discount_label": membership_discount_label(tier) if active else "",
    }


def membership_for_row(row, now=None):
    if not row:
        return membership_public(now=now)
    keys = set(row.keys()) if hasattr(row, "keys") else set(row)
    return membership_public(
        row["membership_tier"] if "membership_tier" in keys else "",
        row["membership_started_at"] if "membership_started_at" in keys else None,
        row["membership_expires_at"] if "membership_expires_at" in keys else None,
        now,
    )


def membership_purchase_error(row, order_type, now=None, require_active_renewal=True):
    """统一会员商品资格；下单时续费须有效，已付款履约时只防会员类型漂移。"""
    if order_type == MEMBERSHIP_ORDER_TYPE:
        return "membership_already_owned" if row and str(row["membership_tier"] or "") else None
    if order_type == MEMBERSHIP_RENEWAL_ORDER_TYPE:
        if not row or str(row["membership_tier"] or "") != "experience":
            return "membership_renewal_not_eligible"
        if require_active_renewal and not membership_for_row(row, now)["membership_active"]:
            return "membership_renewal_not_eligible"
    return None


def user_has_active_membership(username, conn=None, now=None):
    own = conn is None
    c = conn or db()
    try:
        row = c.execute(
            "SELECT membership_tier,membership_started_at,membership_expires_at FROM users WHERE username=?",
            ((username or "").strip(),),
        ).fetchone()
        return bool(membership_for_row(row, now)["membership_active"])
    finally:
        if own:
            c.close()


def public_user(username, display_name=None, points=0, role='member', must_change=False, account_id=None,
                membership_tier="", membership_started_at=None, membership_expires_at=None):
    data = {
        "username": username,
        "name": display_name or username,
        "points": points,
        "role": role,
        "must_change": bool(must_change),
        "account_id": account_id or ""
    }
    data.update(membership_public(membership_tier, membership_started_at, membership_expires_at))
    return data

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

def remove_friend(username, friend_username):
    friend_username = (friend_username or "").strip()
    if not friend_username or friend_username == username:
        return None, "not_found"
    c = db()
    try:
        if not are_friends(c, username, friend_username):
            return None, "not_found"
        c.execute("""DELETE FROM friendships
                     WHERE (username=? AND friend_username=?)
                        OR (username=? AND friend_username=?)""",
                  (username, friend_username, friend_username, username))
        c.execute("""UPDATE friend_requests SET status='removed:' || id
                     WHERE status='accepted'
                       AND ((from_username=? AND to_username=?)
                         OR (from_username=? AND to_username=?))""",
                  (username, friend_username, friend_username, username))
        c.commit()
        return list_friends(username), None
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
    # +1：创建者本人也算成员
    return (int(row["n"] or 0) if row else 0) + 1

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

def list_canvas_boards(username, limit=None, offset=0):
    c = db()
    try:
        # 分页下推 SQL：UNION ALL 后在数据库内排序切片，避免全量拉取
        # 次键 id DESC 保证 updated_at 相同时顺序确定，跨页不重不漏
        offset = max(int(offset or 0), 0)
        params = [username, username, username]
        if limit is not None:
            limit = max(int(limit), 0)
            page_clause = " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            # SQLite 的 OFFSET 必须搭配 LIMIT；-1 表示不限行数
            page_clause = " LIMIT -1 OFFSET ?"
            params.append(offset)
        else:
            page_clause = ""
        rows = c.execute("""SELECT * FROM (
                                SELECT b.*, 'owner' AS access_role
                                FROM canvas_boards b
                                WHERE b.owner_username=?
                                UNION ALL
                                SELECT b.*, m.role AS access_role
                                FROM canvas_members m
                                JOIN canvas_boards b ON b.id=m.board_id
                                WHERE m.username=? AND b.owner_username<>?
                            ) ORDER BY updated_at DESC, id DESC""" + page_clause,
                         params).fetchall()
        # 成员数只对当前页逐行算（主键 COUNT），避免全量子查询
        items = [public_canvas_board(row, row["access_role"],
                                     members_count=canvas_member_count(c, row["id"]))
                 for row in rows]
        total_row = c.execute("""SELECT (SELECT COUNT(*) FROM canvas_boards WHERE owner_username=?)
                                      + (SELECT COUNT(*) FROM canvas_members m
                                         JOIN canvas_boards b ON b.id=m.board_id
                                         WHERE m.username=? AND b.owner_username<>?) AS n""",
                              (username, username, username)).fetchone()
        total = int(total_row["n"] or 0) if total_row else 0
        return items, total, None
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
        owned = c.execute("SELECT COUNT(*) AS n FROM canvas_boards WHERE owner_username=?",
                          (username,)).fetchone()
        if owned and int(owned["n"] or 0) >= CANVAS_MAX_BOARDS_PER_USER:
            return None, "too_many_boards"
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
        # 写入检查点批次: 其他协作者 sync 时拿到 board.snapshot 直接覆盖本地,
        # 不再因版本空洞触发 reset 全量重下
        snapshot_ops = json.dumps(
            [{"type": "board.snapshot", "name": name, "data": json.loads(data_json)}],
            ensure_ascii=False, separators=(",", ":"))
        c.execute("""INSERT INTO canvas_ops(board_id, version, op_id, client_id, username, ops_json, created_at)
                     VALUES(?,?,?,?,?,?,?)""",
                  (board_id, current_version + 1, "save-" + secrets.token_hex(8), "server", username, snapshot_ops, now))
        c.execute("""DELETE FROM canvas_ops WHERE rowid IN (
                     SELECT rowid FROM canvas_ops WHERE board_id=?
                     ORDER BY version DESC LIMIT -1 OFFSET ?
                   )""", (board_id, CANVAS_OPS_RETAINED_BATCHES))
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
        elif kind == "board.snapshot":
            # 整体快照(由 /save 写入检查点,也允许客户端通过 /ops 提交):
            # 收到方直接用 data 覆盖本地画布,避免全量 reset 重下
            data = op.get("data")
            if not isinstance(data, dict):
                return None, "bad_op"
            packed, err = pack_canvas_data(data)
            if err:
                return None, err
            name, err = normalize_canvas_name(op.get("name"))
            if err:
                return None, err
            normalized.append({"type": kind, "name": name, "data": json.loads(packed)})
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
        elif kind == "board.snapshot":
            incoming = op["data"] if isinstance(op.get("data"), dict) else {}
            snapshot = dict(incoming)
            nodes = [dict(item) for item in snapshot.get("nodes", []) if isinstance(item, dict)]
            edges = [dict(item) for item in snapshot.get("edges", []) if isinstance(item, dict)]
            node_index = {item.get("id"): item for item in nodes if isinstance(item.get("id"), str) and item["id"]}
            edge_index = {canvas_edge_key(item): item for item in edges if canvas_edge_key(item) is not None}
            name = op["name"]
    snapshot["nodes"] = nodes
    snapshot["edges"] = edges
    return snapshot, name


def validate_cli_canvas_ops(board, ops):
    data = (board or {}).get("data") or {}
    node_types = {
        item.get("id"): item.get("type")
        for item in data.get("nodes", []) if isinstance(item, dict)
    }
    for op in ops:
        if op["type"] == "node.create":
            node = op["node"]
            if node["id"] in node_types:
                return "node_exists"
            node_types[node["id"]] = node["type"]
        elif op["type"] == "node.patch":
            if op["id"] not in node_types:
                return "node_not_found"
        elif op["type"] == "edge.create":
            edge = op["edge"]
            source = node_types.get(edge["from"]["node"])
            target = node_types.get(edge["to"]["node"])
            if source in {"text", "reverse"} and target in {"gen", "video"}:
                port = "prompt"
            elif source in {"image", "gen"} and target in {"reverse", "gen", "video"}:
                port = "image"
            else:
                return "incompatible_edge"
            if edge["from"]["port"] != port or edge["to"]["port"] != port:
                return "incompatible_edge"
    return None

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

def canvas_online_users(c, board_id, now=None):
    now = int(time.time()) if now is None else int(now)
    cutoff = now - CANVAS_PRESENCE_WINDOW_SECONDS
    c.execute("DELETE FROM canvas_presence WHERE board_id=? AND last_seen<?", (board_id, cutoff))
    rows = c.execute("""SELECT DISTINCT p.username, u.display_name
                        FROM canvas_presence p
                        JOIN canvas_boards b ON b.id=p.board_id
                        JOIN users u ON u.username=p.username
                        LEFT JOIN canvas_members m ON m.board_id=p.board_id AND m.username=p.username
                        WHERE p.board_id=? AND p.last_seen>=?
                          AND (p.username=b.owner_username OR m.role IN ('viewer','editor'))
                        ORDER BY p.username""", (board_id, cutoff)).fetchall()
    return [{"username": r["username"], "name": r["display_name"] or r["username"]} for r in rows]

def canvas_online_count(c, board_id, now=None):
    return len(canvas_online_users(c, board_id, now))

def apply_canvas_ops(username, board_id, payload, cli_safe=False):
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
            try:
                existing_ops = json.loads(existing["ops_json"])
            except Exception:
                existing_ops = None
            if (existing["username"] != username or existing["client_id"] != normalized["client_id"]
                    or existing_ops != normalized["ops"]):
                c.rollback()
                return None, "idempotency_conflict"
            board = public_canvas_board(row, role, include_data=True, members_count=canvas_member_count(c, board_id))
            board["members"] = list_canvas_members(c, board_id)
            result = {
                "version": int(row["version"]),
                "batch": public_canvas_batch(existing),
                "board": board,
            }
            c.rollback()
            return result, None
        current_version = int(row["version"] or 1)
        if normalized["base_version"] != current_version:
            # 客户端基于过期版本提交：拒绝并告知当前版本，让客户端先 sync 再重试，
            # 避免后到请求静默覆盖其他协作者已提交的改动。
            c.rollback()
            return {"version": current_version}, "conflict"
        rate_since = int(time.time()) - CANVAS_OPS_RATE_WINDOW_SECONDS
        recent = c.execute("""SELECT COUNT(*) AS n FROM canvas_ops
                              WHERE board_id=? AND username=? AND created_at>=?""",
                           (board_id, username, rate_since)).fetchone()
        if recent and int(recent["n"] or 0) >= CANVAS_OPS_RATE_MAX_PER_WINDOW:
            c.rollback()
            return {"retry_after": CANVAS_OPS_RATE_WINDOW_SECONDS}, "rate_limited"
        try:
            data = json.loads(row["data_json"] or "{}")
        except Exception:
            data = {}
        if cli_safe:
            err = validate_cli_canvas_ops({"data": data}, normalized["ops"])
            if err:
                c.rollback()
                return None, err
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

def canvas_board_version_probe(board_id):
    # 纯只读版本探测: 普通 SELECT(延迟事务), 不获取 SQLite 写锁,
    # 供长轮询等待阶段轮询; 画板被删时返回 None
    c = db()
    try:
        row = c.execute("SELECT version FROM canvas_boards WHERE id=?", (board_id,)).fetchone()
        return int(row["version"]) if row else None
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
        online_users = canvas_online_users(c, board_id)
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
        result = {"version": current_version, "role": role, "batches": batches, "reset": reset,
                  "online_count": len(online_users), "online_users": online_users}
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
        prev = c.execute("SELECT username, last_seen FROM canvas_presence WHERE board_id=? AND client_id=?",
                         (board_id, client_id.strip())).fetchone()
        # 仅"同用户+同端+间隔过近"才去重; 其他成员复用同 client_id 时走 upsert,
        # 把该行 username/last_seen 更新为当前心跳用户, 避免在线身份失真
        if (prev and prev["username"] == username
                and now - int(prev["last_seen"] or 0) < CANVAS_PRESENCE_MIN_INTERVAL_SECONDS):
            # 周期心跳去重: 间隔过近不写库, 直接回当前在线数
            users = canvas_online_users(c, board_id, now)
            result = {"online_count": len(users), "online_users": users, "deduped": True}
            c.commit()
            return result, None
        c.execute("""INSERT INTO canvas_presence(board_id, client_id, username, last_seen)
                     VALUES(?,?,?,?) ON CONFLICT(board_id, client_id) DO UPDATE SET
                       username=excluded.username, last_seen=excluded.last_seen""",
                  (board_id, client_id.strip(), username, now))
        users = canvas_online_users(c, board_id, now)
        result = {"online_count": len(users), "online_users": users}
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
        existing_member = c.execute("SELECT 1 FROM canvas_members WHERE board_id=? AND username=?",
                                    (board_id, target_username)).fetchone()
        if not existing_member:
            cnt = c.execute("SELECT COUNT(*) AS n FROM canvas_members WHERE board_id=?",
                            (board_id,)).fetchone()
            if cnt and int(cnt["n"] or 0) >= CANVAS_MAX_MEMBERS_PER_BOARD:
                c.rollback()
                return None, "too_many_members"
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

def leave_canvas_board(username, board_id):
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        access, board = canvas_role_and_board(c, username, board_id)
        if not access:
            c.rollback()
            return None, "not_found"
        if access == "owner":
            c.rollback()
            return None, "owner_cannot_leave"
        c.execute("DELETE FROM canvas_members WHERE board_id=? AND username=?", (board_id, username))
        c.execute("DELETE FROM canvas_presence WHERE board_id=? AND username=?", (board_id, username))
        c.commit()
        return {"ok": True}, None
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


def _write_audit(c, who_admin, username, delta, before, after, reason, transaction_key=None):
    """在【同一个事务里】写审计流水。分开写会出现「扣了点但审计没记」或反过来。"""
    c.execute(
        "INSERT INTO points_audit(who_admin, username, delta, before_points, after_points, reason, created_at, transaction_key) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (who_admin, username, delta, before, after, (reason or "")[:120], int(time.time()), transaction_key))


def deduct_points(username, amount, reason="", transaction_key=""):
    """任务提交时预扣点。reason 形如 'job:collect#1354'，由调用方传入。

    在补上审计之前，points_audit 只记录管理员加减点和充值审批 —— 任务扣点/退点完全隐形，
    对账时无法追溯「这个用户的点数为什么少了」。那 21 条「既退点又出结果」的僵尸记录
    (280 点)也因此没法核。
    """
    amount = int(amount or 0)
    if amount < 0:
        raise ValueError("amount must be >= 0")
    transaction_key = str(transaction_key or "").strip()
    if len(transaction_key) > 160:
        raise ValueError("transaction_key too long")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if transaction_key:
            prior = c.execute(
                "SELECT username,delta FROM points_audit WHERE transaction_key=?",
                (transaction_key,),
            ).fetchone()
            if prior:
                if prior["username"] != username or int(prior["delta"] or 0) != -amount:
                    c.rollback()
                    return None, "transaction_conflict"
                row = get_points_row(username, c)
                c.rollback()
                return (public_points(row), None) if row else (None, "not_found")
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
            _write_audit(c, SYSTEM_ACTOR, username, -amount, before, int(row["points"] or 0), reason,
                         transaction_key or None)
        c.commit()
        return public_points(row), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def refund_points(username, amount, reason="", transaction_key=""):
    """任务失败/超时后退点；稳定 transaction_key 让重试只入账一次。"""
    amount = int(amount or 0)
    if amount < 0:
        raise ValueError("amount must be >= 0")
    transaction_key = str(transaction_key or "").strip()
    if len(transaction_key) > 160:
        raise ValueError("transaction_key too long")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if transaction_key:
            prior = c.execute(
                "SELECT username,delta FROM points_audit WHERE transaction_key=?",
                (transaction_key,),
            ).fetchone()
            if prior:
                if prior["username"] != username or int(prior["delta"] or 0) != amount:
                    c.rollback()
                    return None, "transaction_conflict"
                row = get_points_row(username, c)
                c.rollback()
                return (public_points(row), None) if row else (None, "not_found")
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
            _write_audit(c, SYSTEM_ACTOR, username, amount, before, int(row["points"] or 0), reason,
                         transaction_key or None)
        c.commit()
        return public_points(row), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def public_admin_user(row, mask_account=False):
    username = row["username"]
    data = {
        "id": row["id"],
        "username": username,
        "display_name": row["display_name"] or username,
        "points": row["points"],
        "role": row["role"],
        "must_change": bool(row["must_change"]),
        "created_at": row["created_at"],
    }
    if mask_account:
        data["account"] = invites.masked_admin_account(data.pop("username"))
        data["display_name"] = invites.masked_admin_account(data["display_name"])
    data.update(membership_for_row(row))
    return data

def list_admin_users(query="", sort="created_at", direction="desc", limit=100, offset=0):
    allowed_sort = {"username", "display_name", "points", "role", "must_change", "created_at", "membership_expires_at"}
    sort = sort if sort in allowed_sort else "created_at"
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    limit = max(1, min(300, int(limit or 100)))
    offset = max(0, int(offset or 0))
    query = (query or "").strip()
    select_sql = """SELECT id, username, display_name, points, role, must_change, created_at,
                           membership_tier, membership_started_at, membership_expires_at FROM users"""
    count_sql = "SELECT COUNT(*) AS n FROM users"
    args = []
    where_sql = ""
    if query:
        where_sql = " WHERE username LIKE ? OR display_name LIKE ?"
        like = "%" + query + "%"
        args.extend([like, like])
    sql = select_sql + where_sql
    sql += " ORDER BY %s %s, id %s LIMIT ? OFFSET ?" % (sort, direction, direction)
    c = db()
    try:
        rows = c.execute(sql, args + [limit, offset]).fetchall()
        total = c.execute(count_sql + where_sql, args).fetchone()["n"]
        return {
            "items": [public_admin_user(r, mask_account=True) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        c.close()


def reset_password_admin(username, new_password):
    username = str(username or "").strip()
    if not username:
        return None, "missing_username"
    if not isinstance(new_password, str):
        return None, "invalid_password"
    if len(username) > USERNAME_MAX_LENGTH:
        return None, "username_too_long"
    if len(new_password) < 6:
        return None, "password_too_short"
    if len(new_password) > PASSWORD_MAX_LENGTH:
        return None, "password_too_long"
    salt = secrets.token_hex(16)
    password_hash = hash_pw(new_password, salt)
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE users SET pw_hash=?, pw_salt=?, must_change=1 WHERE username=?",
            (password_hash, salt, username),
        )
        if cur.rowcount != 1:
            c.rollback()
            return None, "not_found"
        c.execute("DELETE FROM tokens WHERE username=?", (username,))
        c.commit()
        return {"username": username, "must_change": True, "reauth": True}, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _notification_text(title, detail):
    title = str(title or "").strip()
    detail = str(detail or "").strip()
    if not title:
        return None, None, "missing_title"
    if len(title) > 80:
        return None, None, "title_too_long"
    if not detail:
        return None, None, "missing_detail"
    if len(detail) > 1000:
        return None, None, "detail_too_long"
    return title, detail, None


def _shanghai_day_end(epoch):
    local = datetime.datetime.fromtimestamp(int(epoch), SHANGHAI_TZ)
    end = local.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(end.timestamp())


def _shanghai_next_midnight(epoch):
    local = datetime.datetime.fromtimestamp(int(epoch), SHANGHAI_TZ)
    tomorrow = local.date() + datetime.timedelta(days=1)
    return int(datetime.datetime.combine(tomorrow, datetime.time(), SHANGHAI_TZ).timestamp())


def public_user_notification(row):
    campaign_id = row_get(row, "campaign_id")
    published_at = int(row_get(row, "published_at", 0) or 0)
    return {
        "id": int(row["id"]),
        "campaign_id": int(campaign_id) if campaign_id is not None else None,
        "kind": row["kind"] or "system",
        "title": row["title"],
        "detail": row["detail"],
        "created_at": int(row["created_at"] or 0),
        "read_at": int(row_get(row, "read_at", 0) or 0),
        "popup_snoozed_until": int(row_get(row, "popup_snoozed_until", 0) or 0),
        "popup_until": _shanghai_day_end(published_at) if campaign_id is not None and published_at else 0,
    }


def normalize_announcement_audience(audience):
    if not isinstance(audience, dict):
        return None, "invalid_audience"
    mode = str(audience.get("mode") or "").strip()
    if mode == "all":
        return {"mode": "all"}, None
    if mode != "tiers" or not isinstance(audience.get("tiers"), list):
        return None, "invalid_audience"
    requested = set()
    for tier in audience["tiers"]:
        if not isinstance(tier, str) or tier not in MEMBERSHIP_TIERS:
            return None, "invalid_tier"
        requested.add(tier)
    tiers = [tier for tier in MEMBERSHIP_TIERS if tier in requested]
    if not tiers:
        return None, "missing_tiers"
    return {"mode": "tiers", "tiers": tiers}, None


def select_announcement_audience(connection, audience, cutoff):
    audience, err = normalize_announcement_audience(audience)
    if err:
        return None, err
    cutoff = int(cutoff)
    where = "role IN ('member','admin') AND COALESCE(account_status,'active')='active'"
    params = []
    if audience["mode"] == "tiers":
        holes = ",".join("?" * len(audience["tiers"]))
        where += " AND membership_tier IN (%s) AND COALESCE(membership_expires_at,0)>?" % holes
        params.extend(audience["tiers"])
        params.append(cutoff)
    rows = connection.execute(
        "SELECT username,membership_tier,membership_expires_at FROM users WHERE " + where + " ORDER BY id",
        params,
    ).fetchall()
    breakdown = {"none": 0, **{tier: 0 for tier in MEMBERSHIP_TIERS}}
    for row in rows:
        tier = str(row["membership_tier"] or "")
        if tier not in MEMBERSHIP_TIERS or int(row["membership_expires_at"] or 0) <= cutoff:
            tier = "none"
        breakdown[tier] += 1
    return {
        "audience": audience,
        "cutoff": cutoff,
        "count": len(rows),
        "breakdown": breakdown,
        "_where": where,
        "_params": params,
    }, None


def preview_announcement(audience, now=None):
    cutoff = int(time.time() if now is None else now)
    c = db()
    try:
        selected, err = select_announcement_audience(c, audience, cutoff)
        if err:
            return None, err
        config = wechat_subscribe.config(wechat_subscribe.ANNOUNCEMENT_EVENT_TYPE)
        push_count = 0
        if config and config["configured"]:
            push_count = int(c.execute(
                """SELECT COUNT(*) AS n FROM users u
                   JOIN wechat_subscription_grants g ON g.username=u.username
                   WHERE """ + selected["_where"] + """
                     AND g.event_type=? AND g.template_id=? AND g.remaining>0 AND g.openid<>''""",
                list(selected["_params"]) + [config["event_type"], config["template_id"]],
            ).fetchone()["n"])
        result = {key: value for key, value in selected.items() if not key.startswith("_")}
        result.update({
            "wechat_push_configured": bool(config and config["configured"]),
            "wechat_subscriber_count": push_count,
        })
        return result, None
    finally:
        c.close()


def public_announcement_campaign(row):
    try:
        audience = json.loads(row["audience_json"] or "{}")
    except Exception:
        audience = {}
    try:
        breakdown = json.loads(row_get(row, "breakdown_json", "{}") or "{}")
    except Exception:
        breakdown = {}
    published_at = int(row["published_at"] or 0)
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "detail": row["detail"],
        "audience": audience,
        "status": row["status"],
        "recipient_count": int(row["recipient_count"] or 0),
        "breakdown": breakdown,
        "created_by": row["created_by"],
        "request_id": row["request_id"],
        "created_at": int(row["created_at"] or 0),
        "published_at": published_at,
        "popup_until": _shanghai_day_end(published_at) if published_at else 0,
        "recalled_at": int(row["recalled_at"] or 0),
        "recalled_by": row["recalled_by"] or "",
        "wechat_push_requested": bool(row_get(row, "wechat_push_requested", 0)),
        "wechat_recipient_count": int(row_get(row, "wechat_recipient_count", 0) or 0),
    }


def publish_announcement(title, detail, audience, request_id, created_by, now=None, wechat_push=False):
    title, detail, err = _notification_text(title, detail)
    if err:
        return None, err
    audience, err = normalize_announcement_audience(audience)
    if err:
        return None, err
    if not isinstance(request_id, str) or not request_id.strip():
        return None, "missing_request_id"
    request_id = request_id.strip()
    if len(request_id) > ANNOUNCEMENT_REQUEST_ID_MAX_LENGTH:
        return None, "request_id_too_long"
    created_by = str(created_by or "admin")[:80]
    if not isinstance(wechat_push, bool):
        return None, "invalid_wechat_push"
    push_config = wechat_subscribe.config(wechat_subscribe.ANNOUNCEMENT_EVENT_TYPE)
    if wechat_push and not (push_config and push_config["configured"]):
        return None, "wechat_not_configured"
    cutoff = int(time.time() if now is None else now)
    audience_json = json.dumps(audience, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT * FROM announcement_campaigns WHERE request_id=?", (request_id,),
        ).fetchone()
        if existing:
            if (existing["title"], existing["detail"], existing["audience_json"],
                    bool(row_get(existing, "wechat_push_requested", 0))) != (
                    title, detail, audience_json, wechat_push):
                c.rollback()
                return None, "request_id_conflict"
            campaign = public_announcement_campaign(existing)
            c.rollback()
            return {
                "campaign": campaign,
                "count": campaign["recipient_count"],
                "breakdown": campaign["breakdown"],
                "duplicate": True,
            }, None
        selected, err = select_announcement_audience(c, audience, cutoff)
        if err:
            c.rollback()
            return None, err
        cur = c.execute(
            """INSERT INTO announcement_campaigns(
                   title,detail,audience_json,status,recipient_count,breakdown_json,
                   created_by,request_id,created_at,published_at,wechat_push_requested)
               VALUES(?,?,?,'published',0,?,?,?,?,?,?)""",
            (title, detail, audience_json,
             json.dumps(selected["breakdown"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             created_by, request_id, cutoff, cutoff, int(wechat_push)),
        )
        campaign_id = int(cur.lastrowid)
        c.execute(
            """INSERT INTO user_notifications(
                   username,kind,title,detail,created_by,created_at,campaign_id)
               SELECT username,'announcement',?,?,?,?,? FROM users WHERE """ + selected["_where"],
            [title, detail, created_by, cutoff, campaign_id] + list(selected["_params"]),
        )
        recipient_count = int(c.execute(
            "SELECT COUNT(*) AS n FROM user_notifications WHERE campaign_id=?", (campaign_id,),
        ).fetchone()["n"])
        wechat_recipient_count = 0
        if wechat_push:
            payload = json.dumps({
                "title": title[:20],
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff)),
                "tip": detail.replace("\n", " ")[:20],
            }, ensure_ascii=False)
            c.execute(
                """INSERT OR IGNORE INTO wechat_subscription_outbox(
                       username,event_type,business_id,job_id,kind,template_id,status,
                       payload_json,created_at,updated_at)
                   SELECT n.username,?, ?, ?, 'announcement',?, 'pending',?,?,?
                   FROM user_notifications n
                   JOIN wechat_subscription_grants g ON g.username=n.username
                   WHERE n.campaign_id=? AND g.event_type=? AND g.template_id=?
                     AND g.remaining>0 AND g.openid<>''""",
                (push_config["event_type"], "announcement:%d" % campaign_id, campaign_id,
                 push_config["template_id"], payload, cutoff, cutoff, campaign_id,
                 push_config["event_type"], push_config["template_id"]),
            )
            wechat_recipient_count = int(c.execute(
                "SELECT COUNT(*) AS n FROM wechat_subscription_outbox WHERE event_type=? AND business_id=?",
                (push_config["event_type"], "announcement:%d" % campaign_id),
            ).fetchone()["n"])
        c.execute(
            """UPDATE announcement_campaigns
               SET recipient_count=?,wechat_recipient_count=? WHERE id=?""",
            (recipient_count, wechat_recipient_count, campaign_id),
        )
        row = c.execute("SELECT * FROM announcement_campaigns WHERE id=?", (campaign_id,)).fetchone()
        c.commit()
        campaign = public_announcement_campaign(row)
        return {
            "campaign": campaign,
            "count": recipient_count,
            "breakdown": selected["breakdown"],
            "wechat_recipient_count": wechat_recipient_count,
            "duplicate": False,
        }, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def list_announcement_campaigns(limit=50):
    limit = max(1, min(100, int(limit or 50)))
    c = db()
    try:
        rows = c.execute(
            "SELECT * FROM announcement_campaigns ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        total = int(c.execute("SELECT COUNT(*) AS n FROM announcement_campaigns").fetchone()["n"])
        return {
            "items": [public_announcement_campaign(row) for row in rows],
            "total": total,
            "limit": limit,
        }
    finally:
        c.close()


def recall_announcement(campaign_id, recalled_by, now=None):
    try:
        campaign_id = int(campaign_id)
    except (TypeError, ValueError):
        return None, "invalid_id"
    if campaign_id <= 0:
        return None, "invalid_id"
    recalled_at = int(time.time() if now is None else now)
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM announcement_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            c.rollback()
            return None, "not_found"
        changed = row["status"] != "recalled"
        if changed:
            c.execute(
                """UPDATE announcement_campaigns
                   SET status='recalled',recalled_at=?,recalled_by=? WHERE id=?""",
                (recalled_at, str(recalled_by or "admin")[:80], campaign_id),
            )
            row = c.execute("SELECT * FROM announcement_campaigns WHERE id=?", (campaign_id,)).fetchone()
        removed = c.execute(
            "DELETE FROM user_notifications WHERE campaign_id=?", (campaign_id,),
        ).rowcount
        c.execute(
            """UPDATE wechat_subscription_outbox
               SET status='dropped',last_error='announcement recalled',updated_at=?
               WHERE event_type=? AND business_id=? AND status IN ('pending','failed')""",
            (recalled_at, wechat_subscribe.ANNOUNCEMENT_EVENT_TYPE,
             "announcement:%d" % campaign_id),
        )
        if changed or removed:
            c.commit()
        else:
            c.rollback()
        return {"campaign": public_announcement_campaign(row), "already_recalled": not changed}, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def create_user_notification(username, title, detail, created_by):
    username = str(username or "").strip()
    if not username:
        return None, "missing_username"
    title, detail, err = _notification_text(title, detail)
    if err:
        return None, err
    c = db()
    try:
        if not c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return None, "not_found"
        now = int(time.time())
        cur = c.execute(
            """INSERT INTO user_notifications(username,kind,title,detail,created_by,created_at)
               VALUES(?,?,?,?,?,?)""",
            (username, "system", title, detail, str(created_by or "admin")[:80], now),
        )
        row = c.execute("SELECT * FROM user_notifications WHERE id=?", (cur.lastrowid,)).fetchone()
        c.commit()
        return public_user_notification(row), None
    finally:
        c.close()


def _user_notification_row(connection, username, notification_id):
    return connection.execute(
        """SELECT n.*,c.published_at FROM user_notifications n
           LEFT JOIN announcement_campaigns c ON c.id=n.campaign_id
           WHERE n.username=? AND n.id=?
             AND (n.campaign_id IS NULL OR c.status='published')""",
        (str(username or "").strip(), int(notification_id)),
    ).fetchone()


def list_user_notifications(username, limit=50):
    limit = max(1, min(100, int(limit or 50)))
    c = db()
    try:
        rows = c.execute(
            """SELECT n.*,c.published_at FROM user_notifications n
               LEFT JOIN announcement_campaigns c ON c.id=n.campaign_id
               WHERE n.username=? AND (n.campaign_id IS NULL OR c.status='published')
               ORDER BY n.id DESC LIMIT ?""",
            (str(username or "").strip(), limit),
        ).fetchall()
        return [public_user_notification(row) for row in rows]
    finally:
        c.close()


def mark_user_notification_read(username, notification_id, now=None):
    try:
        notification_id = int(notification_id)
    except (TypeError, ValueError):
        return None, "invalid_id"
    c = db()
    try:
        row = _user_notification_row(c, username, notification_id)
        if not row:
            return None, "not_found"
        c.execute(
            "UPDATE user_notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND username=?",
            (int(time.time() if now is None else now), notification_id, str(username or "").strip()),
        )
        row = _user_notification_row(c, username, notification_id)
        c.commit()
        return public_user_notification(row), None
    finally:
        c.close()


def snooze_user_notification_today(username, notification_id, now=None):
    try:
        notification_id = int(notification_id)
    except (TypeError, ValueError):
        return None, "invalid_id"
    now = int(time.time() if now is None else now)
    c = db()
    try:
        row = _user_notification_row(c, username, notification_id)
        if not row:
            return None, "not_found"
        c.execute(
            "UPDATE user_notifications SET popup_snoozed_until=? WHERE id=? AND username=?",
            (_shanghai_next_midnight(now), notification_id, str(username or "").strip()),
        )
        row = _user_notification_row(c, username, notification_id)
        c.commit()
        return public_user_notification(row), None
    finally:
        c.close()


def mark_all_user_notifications_read(username, now=None):
    c = db()
    try:
        cur = c.execute(
            """UPDATE user_notifications SET read_at=?
               WHERE username=? AND read_at IS NULL
                 AND (campaign_id IS NULL OR campaign_id IN (
                     SELECT id FROM announcement_campaigns WHERE status='published'
                 ))""",
            (int(time.time() if now is None else now), str(username or "").strip()),
        )
        c.commit()
        return int(cur.rowcount or 0)
    finally:
        c.close()


def admin_user_insights(username="", user_id=None):
    username = str(username or "").strip()
    if user_id is not None:
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
    if not username and not user_id:
        return None
    c = db()
    try:
        user = c.execute(
            """SELECT id,username,display_name,points,role,must_change,created_at,
                      membership_tier,membership_started_at,membership_expires_at
               FROM users WHERE %s=?""" % ("id" if user_id else "username"),
            (user_id if user_id else username,),
        ).fetchone()
        if not user:
            return None
        username = user["username"]
        manual = c.execute(
            "SELECT * FROM recharge_orders WHERE username=? ORDER BY created_at DESC,order_id DESC",
            (username,),
        ).fetchall()
        virtual = c.execute(
            "SELECT * FROM virtual_pay_orders WHERE username=? ORDER BY created_at DESC,order_id DESC",
            (username,),
        ).fetchall()
        invite_rewards = invites.admin_reward_points(
            c, {"inviter_user_id": user["id"]}, limit=20,
        )
        invite_rewards["items"] = [
            invites.admin_reward_view(item) for item in invite_rewards["items"]
        ]
        invite_relations = invites.admin_user_relations(c, user["id"], limit=20)
        invite_relations["referrer"] = invites.admin_user_relation_view(invite_relations["referrer"])
        invite_relations["invitees"]["items"] = [
            invites.admin_user_relation_view(item)
            for item in invite_relations["invitees"]["items"]
        ]
    finally:
        c.close()

    recent = []
    paid_orders = 0
    paid_amount_fen = 0
    pending = 0
    abnormal = 0
    for row in manual:
        status = row["status"] or "unknown"
        amount_fen = int(round(float(row["amount"] or 0) * 100))
        if status == "approved":
            paid_orders += 1
            paid_amount_fen += amount_fen
        elif status == "pending":
            pending += 1
        else:
            abnormal += 1
        recent.append({
            "source": "主站充值",
            "order_id": row["order_id"],
            "amount_fen": amount_fen,
            "points": int(row["points"] or 0),
            "status": status,
            "order_type": row["order_type"] or "points",
            "created_at": int(row["created_at"] or 0),
            "detail": row["review_note"] or row["note"] or "",
        })
    for row in virtual:
        status = row["status"] or "unknown"
        amount_fen = int(row["amount_fen"] or 0)
        if status == "credited":
            paid_orders += 1
            paid_amount_fen += amount_fen
        elif status == "created":
            pending += 1
        else:
            abnormal += 1
        recent.append({
            "source": "微信小程序",
            "order_id": row["order_id"],
            "amount_fen": amount_fen,
            "points": int(row["points"] or 0),
            "status": status,
            "order_type": row["order_type"] or "points",
            "created_at": int(row["created_at"] or 0),
            "detail": row["last_error"] or "",
        })
    recent.sort(key=lambda item: (item["created_at"], item["order_id"]), reverse=True)
    return {
        "user": public_admin_user(user),
        "payments": {
            "order_count": len(manual) + len(virtual),
            "paid_order_count": paid_orders,
            "paid_amount_fen": paid_amount_fen,
            "pending_count": pending,
            "abnormal_count": abnormal,
            "recent": recent[:20],
        },
        "ledger": list_points_audit(username=username, limit=20),
        "invite_rewards": invite_rewards,
        "invite_relations": invite_relations,
    }


def _write_membership_audit(c, username, before, after, operator, reason, now):
    cur = c.execute(
        """INSERT INTO membership_audit(
               username,before_tier,after_tier,before_expires_at,after_expires_at,operator,reason,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            username,
            before["membership_tier"] or "",
            after["membership_tier"] or "",
            before["membership_expires_at"] or None,
            after["membership_expires_at"] or None,
            (operator or "system")[:80],
            (reason or "")[:300],
            int(now),
        ),
    )
    return int(cur.lastrowid)


def _grant_membership_voice_slot_entitlement(c, username, source, source_order_id, now):
    """在会员事务内写入一次性免费槽位权益。

    内容服务使用确定性槽位 ID 落地，失败时可安全重试；续费和重复回调不会
    产生第二条权益，也不会发生点数扣除。
    """
    c.execute(
        """INSERT OR IGNORE INTO membership_voice_slot_entitlements(
               username,source,source_order_id,created_at
           ) VALUES(?,?,?,?)""",
        (
            (username or "").strip(),
            (source or "membership")[:40],
            (source_order_id or "")[:160] or None,
            int(now),
        ),
    )


def membership_voice_slot_entitlement(username, now=None):
    username = (username or "").strip()
    if not username:
        return {"eligible": False, "username": ""}
    c = db()
    try:
        row = c.execute(
            """SELECT e.username,e.source,e.source_order_id,e.created_at,
                      u.membership_tier,u.membership_started_at,u.membership_expires_at
                 FROM membership_voice_slot_entitlements e
                 JOIN users u ON u.username=e.username
                WHERE e.username=?""",
            (username,),
        ).fetchone()
        active = membership_for_row(row, now) if row else membership_public(now=now)
        return {
            "eligible": bool(row and active["membership_active"]),
            "username": username,
            "source": row["source"] if row else "",
            "source_order_id": (row["source_order_id"] or "") if row else "",
        }
    finally:
        c.close()


def set_membership_admin(who_admin, username, tier, reason="", now=None):
    username = (username or "").strip()
    tier = (tier or "").strip()
    if not username:
        return None, "missing_username"
    if tier not in set(MEMBERSHIP_TIERS) | {""}:
        return None, "invalid_tier"
    now = int(now or time.time())
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            c.rollback()
            return None, "not_found"
        if tier:
            invites.invited_membership_limit(c, row["id"], tier)
        before = membership_for_row(row, now)
        started_at = now if tier else None
        expires_at = now + MEMBERSHIP_YEAR_SECONDS if tier else None
        c.execute(
            "UPDATE users SET membership_tier=?,membership_started_at=?,membership_expires_at=? WHERE username=?",
            (tier, started_at, expires_at, username),
        )
        if tier:
            _grant_membership_voice_slot_entitlement(
                c, username, "admin_set", "membership-admin:%s:%d" % (username, now), now,
            )
        fresh = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        after = membership_for_row(fresh, now)
        audit_id = _write_membership_audit(
            c, username, before, after, who_admin, reason or "管理员设置会员", now,
        )
        invites.record_membership_upgrade(
            c, fresh["id"], before["membership_tier"], after["membership_tier"],
            "offline_admin", source_order_id="membership-audit:%d" % audit_id,
            operator=who_admin, now=now,
        )
        reward_result = invites.settle_pending_for_user(c, fresh["id"], now=now) if tier else {
            "count": 0, "total_points": 0, "claim_ids": [],
        }
        if not tier:
            invites.void_pending_claims_for_invitee(
                c, fresh["id"], "membership_revoked", now=now,
            )
        c.commit()
        result = public_admin_user(fresh)
        result["invite_reward_result"] = reward_result
        return result, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def membership_recharge_preview(username, tier, now=None):
    username = (username or "").strip()
    tier = (tier or "").strip()
    if not username:
        return None, "missing_username"
    if tier not in MEMBERSHIP_TIERS:
        return None, "invalid_tier"
    now = int(now or time.time())
    c = db()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None, "not_found"
        before = membership_for_row(row, now)
        invites.invited_membership_limit(c, row["id"], tier)
        same_active_tier = before["membership_active"] and before["membership_tier"] == tier
        base = max(now, int(row["membership_expires_at"] or 0)) if same_active_tier else now
        reward = invites.reward_upgrade_preview(c, row["id"], tier, now=now)
        return {
            "username": username,
            "current_tier": before["membership_tier"],
            "current_name": before["membership_name"],
            "current_expires_at": before["membership_expires_at"],
            "target_tier": tier,
            "target_name": MEMBERSHIP_TIERS.get(tier, tier),
            "target_expires_at": base + MEMBERSHIP_YEAR_SECONDS,
            "reward": reward,
        }, None
    finally:
        c.close()


def recharge_membership_admin(who_admin, username, tier, reason="", request_id="", now=None):
    """后台充值一年会员；同等级续费从现有到期日顺延，升级从当前时间起算。"""
    username = (username or "").strip()
    tier = (tier or "").strip()
    if not username:
        return None, "missing_username"
    if tier not in MEMBERSHIP_TIERS:
        return None, "invalid_tier"
    request_id = (request_id or "").strip()[:120]
    if not request_id:
        return None, "missing_request_id"
    now = int(now or time.time())
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT * FROM membership_recharge_records WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            if existing["username"] != username or existing["tier"] != tier:
                c.rollback()
                return None, "request_id_conflict"
            fresh = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            c.commit()
            result = public_admin_user(fresh)
            result["membership_recharge_duplicate"] = True
            result["membership_recharge_request_id"] = request_id
            return result, None
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            c.rollback()
            return None, "not_found"
        invites.invited_membership_limit(c, row["id"], tier)
        before = membership_for_row(row, now)
        same_active_tier = before["membership_active"] and before["membership_tier"] == tier
        base = max(now, int(row["membership_expires_at"] or 0)) if same_active_tier else now
        expires_at = base + MEMBERSHIP_YEAR_SECONDS
        c.execute(
            "UPDATE users SET membership_tier=?,membership_started_at=?,membership_expires_at=? WHERE username=?",
            (tier, now, expires_at, username),
        )
        _grant_membership_voice_slot_entitlement(
            c, username, "admin_recharge", "membership-recharge:%s" % request_id, now,
        )
        fresh = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        after = membership_for_row(fresh, now)
        audit_id = _write_membership_audit(
            c, username, before, after, who_admin, reason or "管理员充值一年会员", now,
        )
        invites.record_membership_upgrade(
            c, fresh["id"], before["membership_tier"], after["membership_tier"],
            "offline_admin", source_order_id="membership-recharge:%s" % request_id,
            operator=who_admin, now=now,
        )
        reward_result = invites.settle_pending_for_user(c, fresh["id"], now=now)
        c.execute(
            """INSERT INTO membership_recharge_records(
                request_id,username,tier,before_expires_at,after_expires_at,operator,reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (request_id, username, tier, before["membership_expires_at"], expires_at,
             who_admin, (reason or "管理员充值一年会员")[:300], now),
        )
        c.commit()
        result = public_admin_user(fresh)
        result["membership_recharge_duplicate"] = False
        result["membership_recharge_request_id"] = request_id
        result["invite_reward_result"] = reward_result
        return result, None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _activate_experience_membership(c, username, operator, reason, now, source_order_id=None, renewal=False):
    row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return None, "user_not_found"
    before = membership_for_row(row, now)
    if renewal and str(row["membership_tier"] or "") != "experience":
        return None, "membership_renewal_not_eligible"
    base = max(int(now), int(row["membership_expires_at"] or 0))
    expires_at = base + MEMBERSHIP_YEAR_SECONDS
    if renewal:
        c.execute("UPDATE users SET membership_expires_at=? WHERE username=?", (expires_at, username))
    else:
        c.execute("""UPDATE users SET membership_tier='experience',membership_started_at=?,membership_expires_at=?
                   WHERE username=?""", (int(now), expires_at, username))
        _grant_membership_voice_slot_entitlement(
            c, username, "online", source_order_id or "membership-online:%s:%d" % (username, now), now,
        )
    fresh = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    after = membership_for_row(fresh, now)
    audit_id = _write_membership_audit(c, username, before, after, operator, reason, now)
    invites.record_membership_upgrade(
        c, fresh["id"], before["membership_tier"], after["membership_tier"],
        "online", source_order_id=source_order_id or "membership-audit:%d" % audit_id,
        operator=operator, now=now, event_type="renewal" if renewal else "upgrade",
    )
    reward_result = invites.settle_pending_for_user(c, fresh["id"], now=now)
    c.execute("""INSERT OR IGNORE INTO membership_recharge_records(
        request_id,username,tier,before_expires_at,after_expires_at,operator,reason,created_at
    ) VALUES(?,?,?,?,?,?,?,?)""", (
        "membership-order:" + str(source_order_id or audit_id), username, "experience",
        int(row["membership_expires_at"] or 0), expires_at, operator, reason[:300], now,
    ))
    after = dict(after)
    after["invite_reward_result"] = reward_result
    return after, None

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

def get_points_transaction(transaction_key):
    """Return one internal ledger row for crash reconciliation."""
    key = str(transaction_key or "").strip()
    if not key or len(key) > 160:
        return None
    c = db()
    try:
        row = c.execute(
            "SELECT username,delta,after_points,created_at FROM points_audit "
            "WHERE transaction_key=?", (key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def list_points_audit(username="", limit=100, actor="", direction=""):
    """actor='admin' 只看人工加减点/充值审批，'system' 只看任务扣退点，''(默认) 全看。

    任务流水接入后，条数远多于人工操作，不过滤的话后台第一页会被任务刷屏。
    """
    limit = max(1, min(300, int(limit or 100)))
    username = (username or "").strip()
    direction = (direction or "").strip()
    sql = """SELECT id, who_admin, username, delta, before_points, after_points, reason, created_at, transaction_key
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
    if direction == "debit":
        where.append("delta<0")
    elif direction == "credit":
        where.append("delta>0")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql += where_sql
    sql += " ORDER BY id DESC LIMIT ?"
    summary_sql = """SELECT COUNT(*) AS total,
                            COALESCE(SUM(CASE WHEN delta>0 THEN delta ELSE 0 END), 0) AS credits,
                            COALESCE(SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END), 0) AS debits,
                            COALESCE(SUM(delta), 0) AS net
                     FROM points_audit""" + where_sql
    c = db()
    try:
        summary = dict(c.execute(summary_sql, args).fetchone())
        rows = c.execute(sql, args + [limit]).fetchall()
        return {"items": [dict(r) for r in rows], "total": summary["total"], "summary": summary}
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
        "order_type": row["order_type"] or "points",
        "list_amount": row["list_amount"] if "list_amount" in row.keys() and row["list_amount"] is not None else row["amount"],
        "pricing_tier": row["pricing_tier"] if "pricing_tier" in row.keys() else "",
        "discount_bps": int(row["discount_bps"] or 10000) if "discount_bps" in row.keys() else 10000,
    }

def create_recharge_order(username, amount, points, note="", order_type="points",
                          list_amount=None, pricing_tier="", discount_bps=None):
    username = (username or "").strip()
    amount = float(amount or 0)
    points = int(points or 0)
    note = (note or "").strip()[:300]
    order_type = (order_type or "points").strip()
    if not username:
        return None, "missing_username"
    if amount <= 0:
        return None, "amount_invalid"
    if points < 0 or (points == 0 and order_type != MEMBERSHIP_RENEWAL_ORDER_TYPE):
        return None, "points_invalid"
    if order_type not in {"points", MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE}:
        return None, "order_type_invalid"
    list_amount = float(amount if list_amount is None else list_amount)
    if list_amount <= 0:
        return None, "list_amount_invalid"
    pricing_tier = str(pricing_tier or "").strip()
    discount_bps = int(discount_bps or membership_discount_bps(pricing_tier))
    now = int(time.time())
    order_id = "R%d%s" % (now, secrets.token_hex(3).upper())
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if order_type in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
            user = c.execute(
                "SELECT membership_tier,membership_started_at,membership_expires_at FROM users WHERE username=?",
                (username,),
            ).fetchone()
            if not user:
                c.rollback()
                return None, "user_not_found"
            purchase_error = membership_purchase_error(user, order_type, now)
            if purchase_error:
                c.rollback()
                return None, purchase_error
            if c.execute(
                "SELECT 1 FROM recharge_orders WHERE username=? AND order_type=? AND status='pending' LIMIT 1",
                (username, order_type),
            ).fetchone():
                c.rollback()
                return None, "membership_order_exists"
        c.execute(
            """INSERT INTO recharge_orders(
                   order_id,username,amount,points,status,note,created_at,order_type,
                   list_amount,pricing_tier,discount_bps
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id, username, amount, points, "pending", note, now, order_type,
                list_amount, pricing_tier, discount_bps,
            ),
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

def review_recharge_order(who_admin, order_id, action, reason="", transaction_id="", pay_channel=""):
    who_admin = (who_admin or "").strip()
    order_id = (order_id or "").strip()
    action = (action or "").strip().lower()
    reason = (reason or "").strip()[:300]
    transaction_id = (transaction_id or "").strip()
    pay_channel = (pay_channel or "").strip()
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
        if transaction_id:
            duplicate = c.execute(
                "SELECT order_id FROM recharge_orders WHERE transaction_id=? AND order_id<>? LIMIT 1",
                (transaction_id, order_id),
            ).fetchone()
            if duplicate:
                c.rollback()
                return None, "transaction_in_use"
        now = int(time.time())
        if action == "approve":
            user = c.execute("SELECT * FROM users WHERE username=?", (order["username"],)).fetchone()
            if not user:
                c.rollback()
                return None, "user_not_found"
            order_type = order["order_type"] or "points"
            purchase_error = membership_purchase_error(
                user, order_type, now, require_active_renewal=False,
            )
            if purchase_error:
                c.rollback()
                return None, purchase_error
            before = int(user["points"] or 0)
            delta = int(order["points"] or 0)
            after = before + delta
            if delta:
                c.execute("UPDATE users SET points=? WHERE username=?", (after, order["username"]))
                c.execute(
                    """INSERT INTO points_audit(who_admin, username, delta, before_points, after_points, reason, created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (who_admin, order["username"], delta, before, after, "充值审批: %s %s" % (order_id, reason), now),
                )
            if order_type in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
                _, membership_err = _activate_experience_membership(
                    c, order["username"], who_admin,
                    ("体验官续费订单: %s" if order_type == MEMBERSHIP_RENEWAL_ORDER_TYPE else "体验官开通订单: %s") % order_id,
                    now, source_order_id=order_id,
                    renewal=order_type == MEMBERSHIP_RENEWAL_ORDER_TYPE,
                )
                if membership_err:
                    c.rollback()
                    return None, membership_err
            status = "approved"
        else:
            status = "rejected"
        c.execute(
            """UPDATE recharge_orders SET status=?, reviewed_by=?, reviewed_at=?, review_note=?,
                                              transaction_id=?, pay_channel=?
               WHERE order_id=?""",
            (status, who_admin, now, reason, transaction_id or order["transaction_id"],
             pay_channel or order["pay_channel"], order_id),
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


def fail_recharge_order(order_id, detail):
    if not order_id:
        return
    c = db()
    try:
        c.execute(
            "UPDATE recharge_orders SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=? WHERE order_id=? AND status='pending'",
            (SYSTEM_ACTOR, int(time.time()), str(detail or "支付下单失败")[:300], order_id),
        )
        c.commit()
    finally:
        c.close()


def reconcile_wxpay_recharge(order_row, payment, actor="wxpay-query"):
    """校验微信支付结果并幂等到账；notify 与主动查单共用同一条安全边界。"""
    if not order_row:
        return None, "not_found"
    if (order_row["status"] or "") == "approved":
        return public_recharge_order(order_row), None
    if (order_row["status"] or "") != "pending":
        return public_recharge_order(order_row), "not_pending"
    if not isinstance(payment, dict) or payment.get("trade_state") != "SUCCESS":
        return public_recharge_order(order_row), "payment_pending"
    if not wxpay.payment_identity_matches(payment):
        return None, "identity_mismatch"
    order_id = (payment.get("out_trade_no") or "").strip()
    if order_id != order_row["order_id"]:
        return None, "order_mismatch"
    paid_total = (payment.get("amount") or {}).get("total")
    if paid_total != int(round(float(order_row["amount"]) * 100)):
        return None, "amount_mismatch"
    transaction_id = (payment.get("transaction_id") or "").strip()
    if not transaction_id:
        return None, "missing_transaction_id"
    order, err = review_recharge_order(
        actor, order_id, "approve", "%s txn=%s" % (actor, transaction_id),
        transaction_id=transaction_id, pay_channel="wxpay",
    )
    if err == "already_reviewed":
        fresh = get_recharge_order(order_id)
        return public_recharge_order(fresh), None
    return order, err

def public_virtual_pay_order(row):
    return {
        "order_id": row["order_id"],
        "package_id": row["package_id"],
        "amount_fen": row["amount_fen"],
        "points": row["points"],
        "status": row["status"],
        "created_at": row["created_at"],
        "paid_at": row["paid_at"],
        "credited_at": row["credited_at"],
        "delivered_at": row["delivered_at"],
        "last_error": row["last_error"] or "",
        "list_amount_fen": row["list_amount_fen"] if "list_amount_fen" in row.keys() else row["amount_fen"],
        "pricing_tier": row["pricing_tier"] if "pricing_tier" in row.keys() else "",
        "discount_bps": int(row["discount_bps"] or 10000) if "discount_bps" in row.keys() else 10000,
        "order_type": row["order_type"] if "order_type" in row.keys() else "points",
    }


def public_virtual_pay_packages(membership_tier=""):
    items = []
    discount_bps = membership_discount_bps(membership_tier)
    for item in wechat_vpay.products():
        if item.get("custom_amount") or item.get("order_type") != "points":
            continue
        pay_fen = (int(item["price_fen"]) * discount_bps + 5000) // 10000
        items.append({
            "id": item["id"],
            "title": item["title"],
            "list_price_fen": item["price_fen"],
            "price_fen": pay_fen,
            "price_yuan": "%.2f" % (pay_fen / 100.0),
            "points": item["points"],
            "recommended": item["recommended"],
            "membership_tier": str(membership_tier or ""),
            "discount_bps": discount_bps,
        })
    return items


def public_virtual_pay_custom(membership_tier=""):
    item = wechat_vpay.custom_product()
    if not item:
        return None
    discount_bps = membership_discount_bps(membership_tier)
    return {
        "package_id": item["id"],
        "min_amount_yuan": wechat_vpay.CUSTOM_MIN_AMOUNT_YUAN,
        "max_amount_yuan": wechat_vpay.CUSTOM_MAX_AMOUNT_YUAN,
        "points_per_yuan": item["points"],
        "price_fen_per_list_yuan": (int(item["price_fen"]) * discount_bps + 5000) // 10000,
        "membership_tier": str(membership_tier or ""),
        "discount_bps": discount_bps,
    }


def _existing_membership_order(c, username, order_type=MEMBERSHIP_ORDER_TYPE):
    return c.execute(
        """SELECT * FROM virtual_pay_orders
             WHERE username=? AND order_type=? AND status IN ('created','refund_review')
             ORDER BY created_at DESC LIMIT 1""",
        (username, order_type),
    ).fetchone()


def _refresh_unpaid_membership_order(row):
    if not row or row["status"] != "created":
        return "blocked" if row else "none"
    try:
        result = wechat_vpay.query_order(row["openid"], row["order_id"], row["env"])
    except Exception:
        return "blocked"
    wx_order = result.get("order") or {}
    if wx_order.get("order_id") and wx_order.get("order_id") != row["order_id"]:
        return "blocked"
    if wx_order.get("status") is None:
        return "blocked"
    try:
        wx_status = int(wx_order["status"])
    except (TypeError, ValueError):
        return "blocked"
    if wx_status in (2, 3, 4):
        confirmed, err = confirm_virtual_pay_order(
            row["username"], row["order_id"], verified_wx_order=wx_order
        )
        if not err and confirmed and confirmed["status"] == "credited":
            return "activated"
        return "blocked"
    if wx_status == 7:
        c = db()
        try:
            c.execute(
                """UPDATE virtual_pay_orders SET status='refund_review',last_error=?,raw_order_json=?
                     WHERE order_id=? AND status='created'""",
                ("微信订单退款失败，需人工核对", json.dumps(wx_order, ensure_ascii=False), row["order_id"]),
            )
            c.commit()
        finally:
            c.close()
        return "blocked"
    if wx_status not in (0, 5, 6, 8):
        return "blocked"
    next_status = "refunded" if wx_status in (5, 8) else "failed"
    detail = "微信未支付订单已关闭" if wx_status in (0, 6) else "微信订单已退款"
    c = db()
    try:
        cursor = c.execute(
            """UPDATE virtual_pay_orders SET status=?,last_error=?,raw_order_json=?
                 WHERE order_id=? AND status='created'""",
            (next_status, detail, json.dumps(wx_order, ensure_ascii=False), row["order_id"]),
        )
        c.commit()
        return "blocked" if cursor.rowcount == 0 else "retired"
    finally:
        c.close()


def _virtual_order_is_terminal(row):
    status = row["status"]
    return status in ("refunded", "refund_review") or (
        (row["order_type"] or "points") in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE) and status == "failed"
    )


def create_virtual_pay_order(username, package_id, wx_code, custom_amount_yuan=None):
    package_id = (package_id or "").strip()
    if not miniprogram_payments_enabled():
        return None, "payment_disabled"
    if not wechat_vpay.is_configured():
        return None, "not_configured"
    product = wechat_vpay.product_by_id(package_id)
    if not product:
        return None, "package_not_found"
    c = db()
    try:
        pricing_user = c.execute(
            """SELECT username,membership_tier,membership_started_at,membership_expires_at
                 FROM users WHERE username=?""",
            (username,),
        ).fetchone()
    finally:
        c.close()
    if not pricing_user:
        return None, "user_not_found"
    membership = membership_for_row(pricing_user)
    order_type = product.get("order_type") or "points"
    if order_type in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
        purchase_error = membership_purchase_error(pricing_user, order_type)
        if purchase_error:
            return None, purchase_error
        c = db()
        try:
            existing_order = _existing_membership_order(c, username, order_type)
        finally:
            c.close()
        if existing_order:
            refresh_result = _refresh_unpaid_membership_order(existing_order)
            if refresh_result == "activated":
                return None, "membership_already_active"
            if refresh_result == "blocked":
                return None, "membership_order_exists"
        pricing_tier = ""
        discount_bps = 10000
    else:
        if membership_enforcement_enabled() and not membership["membership_active"]:
            return None, "membership_required"
        pricing_tier = membership["membership_tier"]
        discount_bps = membership_discount_bps(pricing_tier)
    priced_product = dict(product)
    priced_product["price_fen"] = (
        int(product["price_fen"]) * discount_bps + 5000
    ) // 10000
    try:
        purchase = wechat_vpay.purchase_for(priced_product, custom_amount_yuan)
    except wechat_vpay.VirtualPayError as exc:
        if exc.code == "invalid_custom_amount":
            return None, exc.code
        raise
    session = wechat_vpay.code_to_session(wx_code)
    openid = session["openid"]
    now = int(time.time())
    order_id = "HQ%s%s" % (time.strftime("%y%m%d%H%M%S", time.localtime(now)), secrets.token_hex(5).upper())
    payment = wechat_vpay.payment_params(product, order_id, session["session_key"], purchase)
    list_amount_fen = int(product["price_fen"]) * int(purchase["quantity"])

    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        user = c.execute(
            """SELECT username,membership_tier,membership_started_at,membership_expires_at
                 FROM users WHERE username=?""",
            (username,),
        ).fetchone()
        if not user:
            c.rollback()
            return None, "user_not_found"
        if order_type in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
            purchase_error = membership_purchase_error(user, order_type)
            if purchase_error:
                c.rollback()
                return None, purchase_error
            if _existing_membership_order(c, username, order_type):
                c.rollback()
                return None, "membership_order_exists"
        c.execute(
            """INSERT INTO virtual_pay_orders(
                 order_id,username,openid,package_id,product_id,amount_fen,points,env,status,created_at,
                 list_amount_fen,pricing_tier,discount_bps,order_type
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, username, openid, package_id, product["product_id"], purchase["amount_fen"],
             purchase["points"], wechat_vpay.pay_env(), "created", now,
             list_amount_fen, pricing_tier, discount_bps, order_type),
        )
        row = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (order_id,)).fetchone()
        c.commit()
        return {"order": public_virtual_pay_order(row), "payment": payment}, None
    except sqlite3.IntegrityError:
        c.rollback()
        return None, "conflict"
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _mark_delivery(order_id, env):
    try:
        wechat_vpay.notify_provide_goods(order_id, env)
    except Exception as exc:
        c = db()
        try:
            c.execute("UPDATE virtual_pay_orders SET last_error=? WHERE order_id=?",
                      (("发货通知失败: " + str(exc))[:300], order_id))
            c.commit()
        finally:
            c.close()
        return False
    c = db()
    try:
        c.execute("UPDATE virtual_pay_orders SET delivered_at=?,last_error='' WHERE order_id=?",
                  (int(time.time()), order_id))
        c.commit()
    finally:
        c.close()
    return True


def confirm_virtual_pay_order(username, order_id, verified_wx_order=None):
    order_id = (order_id or "").strip()
    c = db()
    row = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=? AND username=?", (order_id, username)).fetchone()
    c.close()
    if not row:
        return None, "not_found"

    # 已加点的订单只重试未完成的发货通知，绝不重复加点。
    if row["status"] == "credited":
        if not row["delivered_at"]:
            _mark_delivery(row["order_id"], row["env"])
            c = db(); row = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (order_id,)).fetchone(); c.close()
        return public_virtual_pay_order(row), None
    if _virtual_order_is_terminal(row):
        return public_virtual_pay_order(row), None

    if verified_wx_order is None:
        result = wechat_vpay.query_order(row["openid"], row["order_id"], row["env"])
        wx_order = result.get("order") or {}
    else:
        wx_order = verified_wx_order
    wx_status = int(wx_order.get("status") or 0)
    if wx_status == 1:
        c = db()
        c.execute("UPDATE virtual_pay_orders SET last_error='' WHERE order_id=?", (order_id,))
        c.commit(); row = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (order_id,)).fetchone(); c.close()
        return public_virtual_pay_order(row), "pending"
    if wx_status not in (2, 3, 4):
        next_status = "failed"
        error_detail = "微信订单状态异常: %s" % wx_status
        if (row["order_type"] or "points") in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
            if wx_status in (5, 8):
                next_status = "refunded"
                error_detail = "微信订单已退款"
            elif wx_status == 7:
                next_status = "refund_review"
                error_detail = "微信订单退款失败，需人工核对"
        c = db()
        c.execute("UPDATE virtual_pay_orders SET status=?,last_error=? WHERE order_id=?",
                  (next_status, error_detail, order_id))
        c.commit(); row = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (order_id,)).fetchone(); c.close()
        return public_virtual_pay_order(row), "not_paid"
    if wx_order.get("order_id") and wx_order.get("order_id") != order_id:
        return None, "order_mismatch"
    if int(wx_order.get("order_fee") or 0) != int(row["amount_fen"]):
        return None, "amount_mismatch"

    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        fresh = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=? AND username=?", (order_id, username)).fetchone()
        if not fresh:
            c.rollback()
            return None, "not_found"
        if _virtual_order_is_terminal(fresh):
            c.rollback()
            return public_virtual_pay_order(fresh), None
        if fresh["status"] != "credited":
            user = c.execute("SELECT points FROM users WHERE username=?", (username,)).fetchone()
            if not user:
                c.rollback()
                return None, "user_not_found"
            before = int(user["points"] or 0)
            delta = int(fresh["points"] or 0)
            after = before + delta
            now = int(time.time())
            if delta:
                c.execute("UPDATE users SET points=? WHERE username=?", (after, username))
                _write_audit(c, SYSTEM_ACTOR, username, delta, before, after, "微信虚拟支付: " + order_id)
            if (fresh["order_type"] or "points") in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
                _, membership_err = _activate_experience_membership(
                    c, username, SYSTEM_ACTOR,
                    ("微信虚拟支付续费体验官: " if fresh["order_type"] == MEMBERSHIP_RENEWAL_ORDER_TYPE else "微信虚拟支付开通体验官: ") + order_id,
                    now, source_order_id=order_id, renewal=fresh["order_type"] == MEMBERSHIP_RENEWAL_ORDER_TYPE,
                )
                if membership_err:
                    c.rollback()
                    return None, membership_err
            c.execute(
                """UPDATE virtual_pay_orders
                   SET status='credited',paid_at=?,credited_at=?,wx_order_id=?,wxpay_order_id=?,
                       raw_order_json=?,last_error=''
                   WHERE order_id=?""",
                (int(wx_order.get("paid_time") or now), now, str(wx_order.get("wx_order_id") or ""),
                 str(wx_order.get("wxpay_order_id") or ""), json.dumps(wx_order, ensure_ascii=False), order_id),
            )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

    _mark_delivery(order_id, row["env"])
    c = db(); final = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (order_id,)).fetchone(); c.close()
    return public_virtual_pay_order(final), None


def list_virtual_pay_orders(username, limit=20):
    limit = max(1, min(100, int(limit or 20)))
    c = db()
    try:
        rows = c.execute(
            "SELECT * FROM virtual_pay_orders WHERE username=? ORDER BY created_at DESC,order_id DESC LIMIT ?",
            (username, limit),
        ).fetchall()
        return [public_virtual_pay_order(row) for row in rows]
    finally:
        c.close()


def reconcile_created_virtual_pay_orders(limit=VIRTUAL_PAY_RECONCILE_BATCH):
    """周期查单兜底：即使微信回调丢失、用户也没有重进页面，仍会自动发货。"""
    limit = max(1, min(VIRTUAL_PAY_RECONCILE_BATCH, int(limit or VIRTUAL_PAY_RECONCILE_BATCH)))
    c = db()
    try:
        rows = c.execute(
            """SELECT username,order_id FROM virtual_pay_orders
               WHERE status='created' AND created_at<=?
               ORDER BY created_at,order_id LIMIT ?""",
            (int(time.time()) - VIRTUAL_PAY_RECONCILE_MIN_AGE_SECONDS, limit),
        ).fetchall()
    finally:
        c.close()

    stats = {"checked": 0, "credited": 0, "terminal": 0, "pending": 0, "errors": 0}
    for row in rows:
        stats["checked"] += 1
        try:
            order, err = confirm_virtual_pay_order(row["username"], row["order_id"])
            status = (order or {}).get("status")
            if status == "credited":
                stats["credited"] += 1
            elif status == "created" or err == "pending":
                stats["pending"] += 1
            elif status:
                stats["terminal"] += 1
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


def _virtual_pay_reconcile_loop():
    while True:
        try:
            if wechat_vpay.is_configured():
                stats = reconcile_created_virtual_pay_orders()
                if stats["credited"] or stats["terminal"] or stats["errors"]:
                    print("[virtual-pay-reconcile] %s" % json.dumps(stats, sort_keys=True), flush=True)
        except Exception as exc:
            print("[virtual-pay-reconcile] loop error: %s" % type(exc).__name__, file=sys.stderr, flush=True)
        time.sleep(VIRTUAL_PAY_RECONCILE_INTERVAL_SECONDS)


def _virtual_pay_event_payload(message):
    if not isinstance(message, dict):
        return {}
    candidates = [message]
    candidates.extend(value for value in message.values() if isinstance(value, dict))
    for candidate in candidates:
        keys = {str(key).lower() for key in candidate}
        if keys.intersection({"pay_order_id", "order_id", "out_trade_no", "product_id"}):
            return candidate
    return message


def _virtual_pay_event_name(message):
    if not isinstance(message, dict):
        return ""
    for key in ("Event", "event", "event_type", "EventType", "MsgType", "msg_type"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value.strip().lower()
    text = json.dumps(message, ensure_ascii=False).lower()
    for name in (
        "xpay_subscribe_ios_refund_query_notify",
        "xpay_refund_notify",
        "xpay_goods_deliver_notify",
        "xpay_complaint_notify",
    ):
        if name in text:
            return name
    return ""


def _virtual_pay_order_by_reference(reference):
    reference = str(reference or "").strip()
    if not reference:
        return None
    c = db()
    try:
        return c.execute(
            """SELECT * FROM virtual_pay_orders
               WHERE order_id=? OR wx_order_id=? OR wxpay_order_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (reference, reference, reference),
        ).fetchone()
    finally:
        c.close()


def _virtual_pay_event_order(message):
    payload = _virtual_pay_event_payload(message)
    for key in ("order_id", "out_trade_no", "pay_order_id", "wx_order_id", "wxpay_order_id"):
        row = _virtual_pay_order_by_reference(payload.get(key))
        if row:
            return row
    return None


def _revert_membership_order(c, order, now):
    """作废一笔已确认退款的会员权益；后续状态已变化时只进人工复核。"""
    order_id = order["order_id"]
    recharge = c.execute(
        "SELECT * FROM membership_recharge_records WHERE request_id=?",
        ("membership-order:" + order_id,),
    ).fetchone()
    user = c.execute("SELECT * FROM users WHERE username=?", (order["username"],)).fetchone()
    if not recharge or not user or int(user["membership_expires_at"] or 0) != int(recharge["after_expires_at"] or 0):
        return "refund_review", "退款不覆盖后续会员变更，需人工核对权益"
    points = int(order["points"] or 0)
    if order["order_type"] == MEMBERSHIP_ORDER_TYPE and int(user["points"] or 0) < points:
        return "refund_review", "首购点数余额不足，需人工核对权益"

    c.execute("""UPDATE invite_reward_point_records SET status='voided',voided_at=?,
                 void_reason='membership_refund',voided_by=?
                 WHERE upgrade_record_id IN (SELECT id FROM membership_upgrade_records
                 WHERE source='online' AND source_order_id=?) AND status<>'voided'""",
              (now, SYSTEM_ACTOR, order_id))
    c.execute("""UPDATE invite_reward_claims SET status='voided',voided_at=?,
                 reason='membership_refund',updated_at=?
                 WHERE upgrade_record_id IN (SELECT id FROM membership_upgrade_records
                 WHERE source='online' AND source_order_id=?) AND status='pending_upgrade'""",
              (now, now, order_id))
    c.execute("""UPDATE membership_upgrade_records SET status='voided',voided_at=?,
                 void_reason='membership_refund'
                 WHERE source='online' AND source_order_id=? AND status<>'voided'""",
              (now, order_id))
    if order["order_type"] == MEMBERSHIP_ORDER_TYPE:
        c.execute(
            "DELETE FROM membership_voice_slot_entitlements WHERE username=? AND source_order_id=?",
            (order["username"], order_id),
        )
    before_membership = membership_for_row(user, now)
    if order["order_type"] == MEMBERSHIP_RENEWAL_ORDER_TYPE:
        c.execute(
            "UPDATE users SET membership_expires_at=? WHERE username=?",
            (int(recharge["before_expires_at"] or 0) or None, order["username"]),
        )
    else:
        before_points = int(user["points"] or 0)
        if points:
            c.execute("UPDATE users SET points=? WHERE username=?", (before_points - points, order["username"]))
            _write_audit(
                c, SYSTEM_ACTOR, order["username"], -points, before_points, before_points - points,
                "会员首购退款: " + order_id,
            )
        c.execute(
            "UPDATE users SET membership_tier='',membership_started_at=NULL,membership_expires_at=NULL WHERE username=?",
            (order["username"],),
        )
    after_user = c.execute("SELECT * FROM users WHERE username=?", (order["username"],)).fetchone()
    _write_membership_audit(
        c, order["username"], before_membership, membership_for_row(after_user, now),
        SYSTEM_ACTOR, "会员订单退款: " + order_id, now,
    )
    return "refunded", ""


def refund_recharge_order(order_id, refund):
    """处理微信支付 V3 已验签、已解密的全额退款通知。"""
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        order = c.execute("SELECT * FROM recharge_orders WHERE order_id=?", (str(order_id or ""),)).fetchone()
        if not order:
            c.rollback()
            return None, "not_found"
        if order["status"] in ("refunded", "refund_review"):
            c.rollback()
            return public_recharge_order(order), None
        if order["status"] != "approved":
            c.rollback()
            return public_recharge_order(order), "not_approved"
        amount = refund.get("amount") or {}
        total = int(round(float(order["amount"]) * 100))
        if refund.get("refund_status") != "SUCCESS":
            c.rollback()
            return public_recharge_order(order), "refund_mismatch"
        if int(amount.get("total") or 0) != total:
            status, detail = "refund_review", "退款原订单金额不一致，需人工核对"
        elif int(amount.get("refund") or 0) != total:
            status, detail = "refund_review", "部分退款需人工核对权益"
        elif order["transaction_id"] and refund.get("transaction_id") != order["transaction_id"]:
            status, detail = "refund_review", "退款流水与原支付不一致，需人工核对"
        elif (order["order_type"] or "points") in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
            status, detail = _revert_membership_order(c, order, int(time.time()))
        else:
            user = c.execute("SELECT points FROM users WHERE username=?", (order["username"],)).fetchone()
            points = int(order["points"] or 0)
            if not user or int(user["points"] or 0) < points:
                status, detail = "refund_review", "点数余额不足，需人工核对退款"
            else:
                before = int(user["points"] or 0)
                c.execute("UPDATE users SET points=? WHERE username=?", (before - points, order["username"]))
                _write_audit(c, SYSTEM_ACTOR, order["username"], -points, before, before - points, "微信支付退款: " + order["order_id"])
                status, detail = "refunded", ""
        c.execute(
            "UPDATE recharge_orders SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE order_id=?",
            (status, SYSTEM_ACTOR, int(time.time()), detail, order["order_id"]),
        )
        final = c.execute("SELECT * FROM recharge_orders WHERE order_id=?", (order["order_id"],)).fetchone()
        c.commit()
        return public_recharge_order(final), None
    except (TypeError, ValueError):
        c.rollback()
        return None, "refund_mismatch"
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def refund_virtual_pay_order(message):
    row = _virtual_pay_event_order(message)
    if not row:
        return None, "not_found"
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        fresh = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (row["order_id"],)).fetchone()
        if not fresh:
            c.rollback()
            return None, "not_found"
        if fresh["status"] in ("refunded", "refund_review"):
            c.rollback()
            return public_virtual_pay_order(fresh), None
        if (fresh["order_type"] or "points") in (MEMBERSHIP_ORDER_TYPE, MEMBERSHIP_RENEWAL_ORDER_TYPE):
            status, detail = _revert_membership_order(c, fresh, int(time.time()))
            c.execute("UPDATE virtual_pay_orders SET status=?,last_error=? WHERE order_id=?",
                      (status, detail, fresh["order_id"]))
            final = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (fresh["order_id"],)).fetchone()
            c.commit()
            return public_virtual_pay_order(final), None
        if fresh["status"] == "credited":
            user = c.execute("SELECT points FROM users WHERE username=?", (fresh["username"],)).fetchone()
            if not user:
                c.rollback()
                return None, "user_not_found"
            before = int(user["points"] or 0)
            delta = -int(fresh["points"] or 0)
            after = before + delta
            c.execute("UPDATE users SET points=? WHERE username=?", (after, fresh["username"]))
            _write_audit(
                c, SYSTEM_ACTOR, fresh["username"], delta, before, after,
                "微信虚拟支付退款: " + fresh["order_id"],
            )
        c.execute(
            "UPDATE virtual_pay_orders SET status='refunded',last_error='' WHERE order_id=?",
            (fresh["order_id"],),
        )
        final = c.execute("SELECT * FROM virtual_pay_orders WHERE order_id=?", (fresh["order_id"],)).fetchone()
        c.commit()
        return public_virtual_pay_order(final), None
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def process_virtual_pay_message(message):
    event = _virtual_pay_event_name(message)
    payload = _virtual_pay_event_payload(message)
    if event == "xpay_subscribe_ios_refund_query_notify":
        row = _virtual_pay_event_order(message)
        if row and row["status"] == "credited":
            return {
                "result_code": 1,
                "result_info": "虚拟点数已发放",
                "evidence": "订单 %s 已发放 %s 点，最终退款结果将由 Apple 审核。" % (
                    row["order_id"], row["points"]
                ),
            }
        return {
            "result_code": 0,
            "result_info": "未确认发放或未找到订单，建议退款",
            "evidence": "pay_order_id=%s" % str(payload.get("pay_order_id") or "unknown")[:80],
        }
    if event == "xpay_refund_notify":
        order, err = refund_virtual_pay_order(message)
        if err not in (None, "not_found"):
            raise RuntimeError("虚拟支付退款处理失败: " + err)
        return {"errcode": 0, "errmsg": "ok", "order": order or {}}
    if event == "xpay_goods_deliver_notify":
        row = _virtual_pay_event_order(message)
        if row and row["status"] != "credited" and not _virtual_order_is_terminal(row):
            _, err = confirm_virtual_pay_order(row["username"], row["order_id"])
            if err not in (None, "pending"):
                raise RuntimeError("虚拟支付发货通知处理失败: " + err)
        return {"errcode": 0, "errmsg": "ok"}
    if event == "xpay_complaint_notify":
        return {"errcode": 0, "errmsg": "ok"}
    return {"errcode": 0, "errmsg": "ignored"}

def cleanup_expired_tokens(c=None):
    own = c is None
    if own: c = db()
    c.execute("DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at <= ?", (int(time.time()),))
    if own:
        c.commit(); c.close()

def issue_token(username, c=None, ttl=None, scope="account"):
    own = c is None
    if own: c = db()
    cleanup_expired_tokens(c)
    tok = secrets.token_urlsafe(32)
    c.execute("INSERT INTO tokens(token,username,expires_at,scope) VALUES(?,?,?,?)",
              (tok, username, int(time.time()) + int(TOKEN_TTL if ttl is None else ttl), scope))
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
        req_id = error_contract.request_id(self.headers)
        public_obj, hq_code = error_contract.normalize(code, obj, req_id)
        error_contract.audit(code, obj, req_id, hq_code)
        body = json.dumps(public_obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if hq_code:
            self.send_header("X-HQ-Error-Code", hq_code)
            self.send_header("X-HQ-Request-ID", req_id)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _send_raw(self, code, body, content_type="text/plain; charset=utf-8", extra_headers=None):
        body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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
    def _register_rate_key(self, device_id=""):
        device_id = str(device_id or "").strip()[:256]
        device_key = (
            hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:24]
            if device_id else "missing-device"
        )
        return self._client_ip() + "|" + device_key
    def _consume_register_attempt(self, device_id=""):
        now = time.time()
        device_key = self._register_rate_key(device_id)
        ip_key = "ip|" + self._client_ip()
        with REGISTER_HITS_LOCK:
            device_hits = [
                t for t in REGISTER_HITS.get(device_key, [])
                if now - t < REGISTER_WINDOW
            ]
            ip_hits = [
                t for t in REGISTER_HITS.get(ip_key, [])
                if now - t < REGISTER_IP_WINDOW
            ]
            REGISTER_HITS[device_key] = device_hits
            REGISTER_HITS[ip_key] = ip_hits
            if len(device_hits) >= REGISTER_MAX or len(ip_hits) >= REGISTER_IP_MAX:
                return False
            device_hits.append(now)
            ip_hits.append(now)
            return True
    def _user_from_token(self, tok, scope="account"):
        if not tok:
            return None
        c = db()
        r = c.execute("""SELECT u.* FROM tokens t JOIN users u ON u.username=t.username
                         WHERE t.token=? AND (t.expires_at IS NULL OR t.expires_at > ?)
                           AND COALESCE(t.scope,'account')=?
                           AND COALESCE(u.account_status,'active')='active'""",
                      (tok, int(time.time()), scope)).fetchone()
        c.close()
        return r
    def _user(self):
        return self._user_from_token(request_token(self.headers))
    def _cookie_user(self):
        return self._user_from_token(cookie_token(self.headers.get("Cookie")))
    def _card_token_user(self):
        return self._user_from_token(self.headers.get("X-HQ-Card-Token"), "card")
    def _card_user(self):
        return self._card_token_user() if self.headers.get("X-HQ-Card-Token") else self._user()
    def _cli_user(self):
        return hq_cli_api.authenticate(db, bearer_token(self.headers.get("Authorization")))
    def _cli_send(self, code, body):
        return self._send(code, body, {"Cache-Control": "no-store"})
    def _cli_public_user(self, row):
        return public_user(
            row["username"], row["display_name"], row["points"], row["role"], row["must_change"],
            row["account_id"] or ensure_account_id(row["username"]), row["membership_tier"],
            row["membership_started_at"], row["membership_expires_at"],
        )
    def _cli_proxy(self, plan, username):
        token = issue_token(username, ttl=hq_cli_api.BRIDGE_TOKEN_TTL)
        try:
            return hq_cli_api.proxy_json(plan, token, INTERNAL_TOKEN)
        finally:
            c = db()
            c.execute("DELETE FROM tokens WHERE token=?", (token,))
            c.commit(); c.close()

    def _cli_image_upload(self):
        auth = self._cli_user()
        if not auth:
            return self._cli_send(401, {"detail": "CLI 未登录或授权已过期", "code": "cli_unauthorized"})
        row, scopes = auth
        if "assets:upload" not in scopes:
            return self._cli_send(403, {"detail": "当前 CLI 授权缺少权限：assets:upload", "code": "insufficient_scope"})
        if (self.headers.get("X-HQ-Confirm") or "").strip().lower() != "true":
            return self._cli_send(409, {"detail": "上传本地图片需要显式确认", "code": "confirmation_required"})
        if self.headers.get("Transfer-Encoding"):
            return self._cli_send(400, {"detail": "图片上传必须提供 Content-Length", "code": "invalid_image_upload"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > hq_cli_api.IMAGE_UPLOAD_MAX_BYTES:
            return self._cli_send(413, {"detail": "图片大小必须在 1B 到 10MB 之间", "code": "invalid_image_upload"})
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            return self._cli_send(400, {"detail": "只支持 PNG / JPG / WebP", "code": "invalid_image_upload"})
        digest = (self.headers.get("X-HQ-Image-SHA256") or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            return self._cli_send(400, {"detail": "缺少有效的图片摘要", "code": "invalid_image_upload"})
        if not hq_cli_api.IMAGE_UPLOAD_SLOTS.acquire(blocking=False):
            return self._cli_send(429, {"detail": "图片上传繁忙，请稍后重试", "code": "upload_busy"})
        token = ""
        try:
            token = issue_token(row["username"], ttl=hq_cli_api.BRIDGE_TOKEN_TTL)
            status, result = hq_cli_api.proxy_image_upload(
                self.rfile, length, token, INTERNAL_TOKEN, content_type, digest,
            )
        except hq_cli_api.CLIAPIError as exc:
            status, result = exc.status, {"detail": exc.detail, "code": exc.code}
        finally:
            try:
                if token:
                    connection = db()
                    connection.execute("DELETE FROM tokens WHERE token=?", (token,))
                    connection.commit(); connection.close()
            finally:
                hq_cli_api.IMAGE_UPLOAD_SLOTS.release()
        return self._cli_send(status, result)

    def _cli_action(self, body):
        auth = self._cli_user()
        if not auth:
            return self._cli_send(401, {"detail": "CLI 未登录或授权已过期", "code": "cli_unauthorized"})
        row, scopes = auth
        if not isinstance(body, dict):
            return self._cli_send(400, {"detail": "请求体必须是 JSON 对象"})
        unknown = sorted(set(body) - {"action", "input", "confirm", "quote_token"})
        if unknown:
            return self._cli_send(400, {"detail": "不支持的参数：" + unknown[0]})
        action = body.get("action")
        if not isinstance(action, str) or not action or len(action) > 80:
            return self._cli_send(400, {"detail": "action 不合法"})
        input_body = body.get("input", {})
        confirm = body.get("confirm", False)
        if not isinstance(confirm, bool):
            return self._cli_send(400, {"detail": "confirm 必须是布尔值"})
        quote_token = body.get("quote_token", "")
        if not isinstance(quote_token, str) or len(quote_token) > 4096:
            return self._cli_send(400, {"detail": "quote_token 不合法"})
        try:
            plan = hq_cli_api.action_plan(action, input_body)
            if plan["scope"] not in scopes:
                raise hq_cli_api.CLIAPIError(403, "当前 CLI 授权缺少权限：" + plan["scope"], "insufficient_scope")
            if action in hq_cli_api.CONFIRMATION_ACTIONS and not confirm:
                raise hq_cli_api.CLIAPIError(409, "该操作需要显式确认", "confirmation_required")
            if plan["kind"] == "account":
                return self._cli_send(200, {"user": self._cli_public_user(row), "scopes": list(scopes),
                                            "expires_at": int(row["cli_expires_at"])})
            if plan["kind"] == "channels":
                return self._cli_send(200, {"channels": list(hq_cli_api.CHANNEL_CATALOG),
                                            "total": len(hq_cli_api.CHANNEL_CATALOG),
                                            "account": row["username"]})
            if plan["kind"] == "canvas-list":
                boards, total, err = list_canvas_boards(row["username"], plan["limit"], plan["offset"])
                if err:
                    raise hq_cli_api.CLIAPIError(400, err)
                return self._cli_send(200, {"boards": boards, "total": total})
            if plan["kind"] == "canvas-get":
                board, err = get_canvas_board(row["username"], plan["board_id"])
                if err:
                    raise hq_cli_api.CLIAPIError(404, "画布不存在", "not_found")
                return self._cli_send(200, {"board": board})
            if plan["kind"] == "canvas-create":
                board, err = create_canvas_board(row["username"], {"name": plan["name"], "data": plan["data"]})
                if err == "too_many_boards":
                    raise hq_cli_api.CLIAPIError(429, "画布数量已达上限")
                if err:
                    raise hq_cli_api.CLIAPIError(400, "画布创建失败：" + err)
                return self._cli_send(200, {"board": board,
                    "url": hq_cli_api.PUBLIC_ORIGIN + "/workbench/canvas?collab=" + urllib.parse.quote(board["id"])})
            if plan["kind"] == "canvas-ops":
                result, err = apply_canvas_ops(row["username"], plan["board_id"], {
                    **plan["payload"], "client_id": "hq-cli",
                }, cli_safe=True)
                if err == "not_found":
                    raise hq_cli_api.CLIAPIError(404, "画布不存在", "not_found")
                if err == "forbidden":
                    raise hq_cli_api.CLIAPIError(403, "当前账号没有画布编辑权限", "forbidden")
                if err == "conflict":
                    raise hq_cli_api.CLIAPIError(409, "画布已更新，请读取最新版本后重试", "canvas_version_conflict")
                if err == "idempotency_conflict":
                    raise hq_cli_api.CLIAPIError(409, "op_id 已绑定其他画布操作", "idempotency_conflict")
                if err == "rate_limited":
                    raise hq_cli_api.CLIAPIError(429, "画布操作过于频繁，请稍后重试", "rate_limited")
                if err in {"too_many_ops", "too_large"}:
                    raise hq_cli_api.CLIAPIError(413, "画布操作数据过大", err)
                if err:
                    raise hq_cli_api.CLIAPIError(400, "画布写入失败：" + err, err)
                return self._cli_send(200, result)
            if plan["kind"] == "generation":
                generation_kind, payload = plan["generation_kind"], plan["payload"]
                if confirm:
                    if "generation:submit" not in scopes:
                        raise hq_cli_api.CLIAPIError(403, "当前 CLI 授权不能提交扣点生成", "insufficient_scope")
                    if not quote_token:
                        raise hq_cli_api.CLIAPIError(409, "提交生成前必须先取得 quote_token", "quote_required")
                    claims = hq_cli_api.verify_quote(
                        INTERNAL_TOKEN, quote_token, row["username"], generation_kind, payload,
                    )
                    submit_body = dict(payload)
                    if plan.get("quoted_cost_field"):
                        submit_body[plan["quoted_cost_field"]] = claims["c"]
                    submit_headers = {
                        "Idempotency-Key": "hqcli-" + claims["n"],
                        "X-HQ-Expected-Cost": str(claims["c"]),
                    }
                    submit_headers.update(plan.get("submit_headers") or {})
                    submit_plan = {
                        "base": hq_cli_api.CONTENT_BASE, "path": plan["endpoint"], "method": "POST",
                        "body": submit_body, "timeout": 30, "internal": True,
                        "headers": submit_headers,
                    }
                    status, result = self._cli_proxy(submit_plan, row["username"])
                    return self._cli_send(status, result)
                if quote_token:
                    raise hq_cli_api.CLIAPIError(400, "quote_token 只能与 confirm=true 同时使用")
                quote_plan = {
                    "base": hq_cli_api.CONTENT_BASE,
                    "path": plan.get("quote_endpoint", "/api/gen/cli/quote"), "method": "POST",
                    "body": plan.get("quote_body", {"kind": generation_kind, "payload": payload}),
                    "timeout": 30, "internal": True,
                }
                status, result = self._cli_proxy(quote_plan, row["username"])
                if not 200 <= status < 300:
                    return self._cli_send(status, result)
                token, claims = hq_cli_api.issue_quote(
                    INTERNAL_TOKEN, row["username"], generation_kind, payload, result.get("cost"),
                )
                return self._cli_send(200, {
                    "quote_token": token, "kind": generation_kind, "cost": claims["c"],
                    "points": result.get("points"), "expires_in": hq_cli_api.QUOTE_TTL,
                    "confirmation_required": True,
                })
            if action == "ip12-message":
                claim, previous_status = hq_cli_api.begin_action_request(
                    db, row["username"], action, plan["request_id"], plan["project_id"], plan["request_hash"],
                )
                if claim == "conflict":
                    raise hq_cli_api.CLIAPIError(409, "request_id 已绑定其他输入", "idempotency_conflict")
                if claim == "in_progress":
                    raise hq_cli_api.CLIAPIError(409, "该轮对话仍在处理中，请使用相同 request_id 稍后查询", "idempotency_in_progress")
                if claim == "uncertain":
                    raise hq_cli_api.CLIAPIError(409, "上次结果未知，请先读取项目再决定是否发起新一轮", "result_unknown")
                if claim == "busy":
                    raise hq_cli_api.CLIAPIError(429, "该项目已有一轮 CLI 对话正在处理", "project_busy")
                if claim == "rate_limited":
                    raise hq_cli_api.CLIAPIError(429, "IP12 CLI 对话请求过于频繁，请稍后重试", "rate_limited")
                if claim == "completed":
                    if previous_status and 200 <= int(previous_status) < 300:
                        return self._cli_send(200, {
                            "ok": True, "replayed": True, "project_id": plan["project_id"],
                            "detail": "该轮已处理；请读取项目取得最新回复和进度。",
                        })
                    raise hq_cli_api.CLIAPIError(409, "该轮此前已处理但未成功，请先读取项目", "previous_attempt_completed")
                try:
                    status, result = self._cli_proxy(plan, row["username"])
                except Exception:
                    hq_cli_api.finish_action_request(
                        db, row["username"], action, plan["request_id"], uncertain=True,
                    )
                    raise
                hq_cli_api.finish_action_request(
                    db, row["username"], action, plan["request_id"], http_status=status,
                )
                return self._cli_send(status, result)
            status, result = self._cli_proxy(plan, row["username"])
            if 200 <= status < 300 and action == "ip12-create" and isinstance(result, dict):
                project = result.get("project") or {}
                project_id = project.get("id") or result.get("id")
                if project_id:
                    result["url"] = (hq_cli_api.PUBLIC_ORIGIN + "/workbench/ip12/?conversation_id="
                                     + urllib.parse.quote(str(project_id)))
            return self._cli_send(status, result)
        except hq_cli_api.CLIAPIError as exc:
            return self._cli_send(exc.status, {"detail": exc.detail, "code": exc.code})
        except Exception:
            return self._cli_send(500, {"detail": "CLI 操作暂时不可用", "code": "cli_internal_error"})
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

    def _require_membership(self, row):
        if not membership_enforcement_enabled():
            return True
        if row and membership_for_row(row)["membership_active"]:
            return True
        self._send(403, {
            "detail": "请先开通会员后再使用该功能",
            "code": "membership_required",
            "membership_url": "/workbench/recharge",
            "membership_enforcement_enabled": True,
        })
        return False

    def _require_purchase_password(self, row):
        if not initial_password_change_required(row):
            return True
        self._send(403, {
            "detail": "充值或开通会员前请先修改初始密码",
            "code": "initial_password_change_required",
        })
        return False

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/auth/internal/canvas/access":
            if not self._require_internal():
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "request body must be valid JSON"})
            if not isinstance(d, dict) or set(d) != {"username", "board_id"}:
                return self._send(400, {"detail": "request fields must be username and board_id"})
            username = d.get("username")
            board_id = d.get("board_id")
            if not isinstance(username, str) or not username.strip():
                return self._send(400, {"detail": "username is required"})
            if not isinstance(board_id, str) or not board_id.strip():
                return self._send(400, {"detail": "board_id is required"})
            c = db()
            try:
                role, board = canvas_role_and_board(c, username.strip(), board_id.strip())
            finally:
                c.close()
            if not role or not board:
                return self._send(404, {"detail": "canvas access not found"})
            return self._send(200, {
                "board_id": board["id"],
                "board_owner_username": board["owner_username"],
                "role": role,
            })
        if p == "/api/auth/cli/device/start":
            if self._content_length_exceeds(8192):
                return self._cli_send(413, {"detail": "请求过大"})
            d = self._body()
            if self._bad_json():
                return self._cli_send(400, {"detail": "请求体不是合法 JSON"})
            try:
                return self._cli_send(200, hq_cli_api.start_device(db, d, self._client_ip()))
            except hq_cli_api.CLIAPIError as exc:
                return self._cli_send(exc.status, {"detail": exc.detail, "code": exc.code})
        if p == "/api/auth/cli/device/info":
            if not hq_cli_api.origin_allowed(self.headers.get("Origin")):
                return self._cli_send(403, {"detail": "来源校验失败", "code": "origin_forbidden"})
            if not self._cookie_user():
                return self._cli_send(401, {"detail": "请先登录黄雀账号"})
            if self._content_length_exceeds(4096):
                return self._cli_send(413, {"detail": "请求过大"})
            d = self._body()
            if self._bad_json():
                return self._cli_send(400, {"detail": "请求体不是合法 JSON"})
            try:
                return self._cli_send(200, hq_cli_api.device_info(db, d))
            except hq_cli_api.CLIAPIError as exc:
                return self._cli_send(exc.status, {"detail": exc.detail, "code": exc.code})
        if p == "/api/auth/cli/device/approve":
            if not hq_cli_api.origin_allowed(self.headers.get("Origin")):
                return self._cli_send(403, {"detail": "来源校验失败", "code": "origin_forbidden"})
            row = self._cookie_user()
            if not row:
                return self._cli_send(401, {"detail": "请先登录黄雀账号"})
            if self._content_length_exceeds(4096):
                return self._cli_send(413, {"detail": "请求过大"})
            d = self._body()
            if self._bad_json():
                return self._cli_send(400, {"detail": "请求体不是合法 JSON"})
            try:
                return self._cli_send(200, hq_cli_api.approve_device(db, row["username"], d))
            except hq_cli_api.CLIAPIError as exc:
                return self._cli_send(exc.status, {"detail": exc.detail, "code": exc.code})
        if p == "/api/auth/cli/device/poll":
            if self._content_length_exceeds(4096):
                return self._cli_send(413, {"detail": "请求过大"})
            d = self._body()
            if self._bad_json():
                return self._cli_send(400, {"detail": "请求体不是合法 JSON"})
            try:
                return self._cli_send(200, hq_cli_api.poll_device(db, d))
            except hq_cli_api.CLIAPIError as exc:
                return self._cli_send(exc.status, {"detail": exc.detail, "code": exc.code})
        if p == "/api/auth/cli/logout":
            token = bearer_token(self.headers.get("Authorization"))
            hq_cli_api.revoke(db, token)
            return self._cli_send(200, {"ok": True})
        if p == "/api/auth/cli/image-upload":
            return self._cli_image_upload()
        if p == "/api/auth/cli/action":
            if self._content_length_exceeds(128 * 1024):
                return self._cli_send(413, {"detail": "CLI 输入不能超过 128 KiB"})
            d = self._body()
            if self._bad_json():
                return self._cli_send(400, {"detail": "请求体不是合法 JSON"})
            return self._cli_action(d)
        if p == "/api/auth/subscription/choices":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                result, err = record_subscription_choices(row["username"], d.get("choices"), d.get("wx_code"))
                if err == "not_configured":
                    return self._send(503, {"detail": "订阅消息模板未配置"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, **result})
            except Exception:
                return self._send(502, {"detail": "订阅授权保存失败，请稍后重试"})
        if p == "/api/auth/internal/subscription/video-complete":
            if not self._require_internal():
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            username = str(d.get("username") or "").strip()
            try:
                job_id = int(d.get("job_id") or 0)
            except (TypeError, ValueError):
                job_id = 0
            kind = str(d.get("kind") or "").strip()
            if not username or job_id <= 0 or kind not in VIDEO_SUBSCRIPTION_KINDS:
                return self._send(400, {"detail": "missing or invalid video event"})
            result = enqueue_video_subscription(username, job_id, kind)
            if result["status"] == "unknown_user":
                return self._send(200, {"ok": True, "status": "ignored"})
            return self._send(200, {"ok": True, **result})
        notice_prefix = "/api/auth/invite/notices/"
        if p.startswith(notice_prefix) and p.endswith("/read"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            value = p[len(notice_prefix):-len("/read")].strip("/")
            try:
                notice_id = int(value)
            except (TypeError, ValueError):
                return self._send(400, {"detail": "提醒记录无效"})
            c = db()
            try:
                changed = invites.ack_reward_notice(c, row["id"], notice_id)
                if not changed:
                    return self._send(404, {"detail": "提醒不存在"})
                c.commit()
                return self._send(200, {"ok": True})
            finally:
                c.close()
        reward_prefix = "/api/auth/admin/invite/reward-points/"
        if p.startswith(reward_prefix):
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            parts = p[len(reward_prefix):].strip("/").split("/")
            if len(parts) != 2:
                return self._send(404, {"detail": "not found"})
            try:
                reward_id = int(parts[0])
            except (TypeError, ValueError):
                return self._send(400, {"detail": "奖励记录 ID 不正确"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            c = db()
            try:
                reward = invites.admin_reward_action(
                    c, reward_id, parts[1], d.get("reason"), admin["username"],
                )
                c.commit()
                return self._send(200, {"ok": True, "reward": reward})
            except invites.InviteError as exc:
                c.rollback()
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "奖励台账操作失败"})
            finally:
                c.close()
        admin_invite_prefix = "/api/auth/admin/invite/relations/"
        if p.startswith(admin_invite_prefix):
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            parts = p[len(admin_invite_prefix):].strip("/").split("/")
            if len(parts) != 2:
                return self._send(404, {"detail": "not found"})
            try:
                relation_id = int(parts[0])
            except (TypeError, ValueError):
                return self._send(400, {"detail": "关系 ID 不正确"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            c = db()
            try:
                relation = invites.admin_relation_action(
                    c, relation_id, parts[1], d.get("reason"), admin["id"],
                )
                c.commit()
                return self._send(200, {"ok": True, "relation": relation})
            except invites.InviteError as exc:
                c.rollback()
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "邀请关系处理失败"})
            finally:
                c.close()
        if p in ("/api/invite/code/rotate", "/api/auth/invite/code/rotate"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if row["role"] != "admin":
                return self._send(403, {"detail": "邀请码轮换需要管理员权限"})
            c = db()
            try:
                code_row = invites.rotate_user_code(
                    c, row["id"], enforce_membership=False,
                )
                c.commit()
                return self._send(200, {
                    "ok": True,
                    "code": code_row["code"],
                    "invite_link": INVITE_PUBLIC_BASE_URL + "/register?invite=" + code_row["code"],
                })
            except invites.InviteError as exc:
                c.rollback()
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "邀请码轮换失败"})
            finally:
                c.close()
        if p == "/api/auth/wechat/message-push":
            if not wechat_vpay.message_push_configured():
                return self._send(503, {"detail": "message push not configured"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 1024 * 1024:
                    return self._send(400, {"detail": "bad message body"})
                body = json.loads(self.rfile.read(n))
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                message, encrypted = wechat_vpay.decode_message_push(query, body)
                response = process_virtual_pay_message(message)
                encoded = wechat_vpay.encode_message_push(response, encrypted)
                return self._send_raw(
                    200, json.dumps(encoded, ensure_ascii=False, separators=(",", ":")),
                    "application/json; charset=utf-8",
                )
            except wechat_vpay.MessagePushError as exc:
                return self._send(403, {"detail": str(exc)})
            except Exception:
                return self._send(500, {"detail": "message push failed"})
        if p == "/api/auth/notifications/read-all":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            try:
                count = mark_all_user_notifications_read(row["username"])
                return self._send(200, {"ok": True, "updated_count": count})
            except Exception:
                return self._send(500, {"detail": "通知已读状态保存失败"})
        notification_action_prefix = "/api/auth/notifications/"
        if p.startswith(notification_action_prefix):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            parts = p[len(notification_action_prefix):].strip("/").split("/")
            if len(parts) != 2 or parts[1] not in {"read", "snooze-today"}:
                return self._send(404, {"detail": "not found"})
            try:
                notification_id = int(parts[0])
            except (TypeError, ValueError):
                return self._send(400, {"detail": "通知 ID 不正确"})
            try:
                if parts[1] == "read":
                    notice, err = mark_user_notification_read(row["username"], notification_id)
                else:
                    notice, err = snooze_user_notification_today(row["username"], notification_id)
                if err == "not_found":
                    return self._send(404, {"detail": "通知不存在"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "notification": notice})
            except Exception:
                return self._send(500, {"detail": "通知状态保存失败"})
        if p == "/api/auth/admin/announcements/preview":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                preview, err = preview_announcement(d.get("audience"))
                if err:
                    messages = {
                        "invalid_audience": "公告受众格式不正确",
                        "invalid_tier": "公告会员等级无效",
                        "missing_tiers": "请至少选择一个会员等级",
                    }
                    return self._send(400, {"detail": messages.get(err, err), "code": err})
                return self._send(200, {"ok": True, **preview})
            except Exception:
                return self._send(500, {"detail": "公告受众预览失败"})
        if p == "/api/auth/admin/announcements":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                result, err = publish_announcement(
                    d.get("title"), d.get("detail"), d.get("audience"), d.get("request_id"),
                    admin["username"], wechat_push=d.get("wechat_push", False),
                )
                if err:
                    messages = {
                        "missing_title": "通知标题不能为空",
                        "title_too_long": "通知标题最多 80 个字符",
                        "missing_detail": "通知内容不能为空",
                        "detail_too_long": "通知内容最多 1000 个字符",
                        "invalid_audience": "公告受众格式不正确",
                        "invalid_tier": "公告会员等级无效",
                        "missing_tiers": "请至少选择一个会员等级",
                        "missing_request_id": "缺少公告请求编号",
                        "request_id_too_long": "公告请求编号最多 128 个字符",
                        "invalid_wechat_push": "微信订阅消息选项格式不正确",
                        "wechat_not_configured": "微信公告订阅模板尚未配置",
                    }
                    status = 409 if err == "request_id_conflict" else 400
                    return self._send(status, {
                        "detail": messages.get(err, "公告请求编号与原请求不一致"), "code": err,
                    })
                return self._send(200, {"ok": True, **result})
            except Exception:
                return self._send(500, {"detail": "公告发布失败"})
        announcement_recall_prefix = "/api/auth/admin/announcements/"
        if p.startswith(announcement_recall_prefix) and p.endswith("/recall"):
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            campaign_id = p[len(announcement_recall_prefix):-len("/recall")].strip("/")
            try:
                result, err = recall_announcement(campaign_id, admin["username"])
                if err == "invalid_id":
                    return self._send(400, {"detail": "公告 ID 不正确"})
                if err == "not_found":
                    return self._send(404, {"detail": "公告不存在"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, **result})
            except Exception:
                return self._send(500, {"detail": "公告召回失败"})
        if p == "/api/auth/admin/notifications":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            notice, err = create_user_notification(
                d.get("username"), d.get("title"), d.get("detail"), admin["username"],
            )
            messages = {
                "missing_username": "缺少用户账号",
                "missing_title": "通知标题不能为空",
                "title_too_long": "通知标题最多 80 个字符",
                "missing_detail": "通知内容不能为空",
                "detail_too_long": "通知内容最多 1000 个字符",
            }
            if err == "not_found":
                return self._send(404, {"detail": "用户不存在"})
            if err:
                return self._send(400, {"detail": messages.get(err, err)})
            return self._send(200, {"ok": True, "notification": notice})
        if p == "/api/auth/admin/password/reset":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json() or not isinstance(d, dict):
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                result, err = reset_password_admin(d.get("username"), d.get("new_password"))
                messages = {
                    "missing_username": "缺少用户账号",
                    "invalid_password": "新密码格式不正确",
                    "username_too_long": "账号最多 64 位",
                    "password_too_short": "新密码至少 6 位",
                    "password_too_long": "新密码最多 128 位",
                }
                if err == "not_found":
                    return self._send(404, {"detail": "用户不存在"})
                if err:
                    return self._send(400, {"detail": messages.get(err, err)})
                return self._send(200, {"ok": True, "reset": result})
            except Exception:
                return self._send(500, {"detail": "重置密码失败"})
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
        if p == "/api/auth/admin/membership/set":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                user, err = set_membership_admin(
                    admin["username"], d.get("username"), d.get("tier"), d.get("reason") or "",
                )
                if err == "not_found":
                    return self._send(404, {"detail": "用户不存在"})
                if err == "invalid_tier":
                    return self._send(400, {"detail": "会员等级无效"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "user": user})
            except invites.InviteError as exc:
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                return self._send(500, {"detail": "会员设置失败"})
        if p == "/api/auth/admin/membership/recharge/preview":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                preview, err = membership_recharge_preview(d.get("username"), d.get("tier"))
                if err == "not_found":
                    return self._send(404, {"detail": "用户不存在"})
                if err == "invalid_tier":
                    return self._send(400, {"detail": "会员等级无效"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "preview": preview})
            except invites.InviteError as exc:
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                return self._send(500, {"detail": "会员充值预览失败"})
        if p == "/api/auth/admin/membership/recharge":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                user, err = recharge_membership_admin(
                    admin["username"], d.get("username"), d.get("tier"), d.get("reason") or "",
                    d.get("request_id") or "",
                )
                if err == "not_found":
                    return self._send(404, {"detail": "用户不存在"})
                if err == "invalid_tier":
                    return self._send(400, {"detail": "会员等级无效"})
                if err == "missing_request_id":
                    return self._send(400, {"detail": "缺少充值请求编号"})
                if err == "request_id_conflict":
                    return self._send(409, {"detail": "充值请求编号与原请求不一致"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "user": user})
            except invites.InviteError as exc:
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                return self._send(500, {"detail": "会员充值失败"})
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
                if err in {"membership_already_owned", "membership_renewal_not_eligible"}:
                    return self._send(409, {"detail": "会员资格已变化，订单需人工核对", "code": err})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "order": order})
            except Exception:
                return self._send(500, {"detail": "recharge review failed"})
        if p == "/api/auth/virtual-pay/order":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if not self._require_purchase_password(row):
                return
            if not miniprogram_payments_enabled():
                return self._send(503, {
                    "detail": "小程序支付功能暂时关闭",
                    "code": "payment_disabled",
                })
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                result, err = create_virtual_pay_order(
                    row["username"], d.get("package_id"), d.get("wx_code"), d.get("custom_amount_yuan")
                )
                if err == "not_configured":
                    return self._send(503, {"detail": "虚拟支付正在配置中，请稍后再试"})
                if err == "payment_disabled":
                    return self._send(503, {
                        "detail": "小程序支付功能暂时关闭",
                        "code": "payment_disabled",
                    })
                if err == "package_not_found":
                    return self._send(404, {"detail": "充值套餐不存在"})
                if err == "membership_required":
                    return self._send(403, {"detail": "请先开通会员", "code": err})
                if err == "membership_already_owned":
                    return self._send(409, {"detail": "该账号已有会员记录，不能重复领取首购权益", "code": err})
                if err == "membership_renewal_not_eligible":
                    return self._send(409, {"detail": "仅有效体验官可自助续费", "code": err})
                if err == "membership_already_active":
                    return self._send(409, {"detail": "当前会员仍在有效期内", "code": err})
                if err == "membership_order_exists":
                    return self._send(409, {
                        "detail": "已有待处理的同类会员订单",
                        "code": err,
                    })
                if err == "invalid_custom_amount":
                    return self._send(400, {"detail": "自定义充值金额须为1~5000元整数"})
                if isinstance(err, str) and err.startswith("openid_in_use:"):
                    bound_username = err.split(":", 1)[1]
                    return self._send(409, {
                        "detail": "当前微信账号已绑定黄雀账号：%s" % bound_username,
                        "code": "openid_in_use",
                        "bound_username": bound_username,
                    })
                if err == "openid_mismatch":
                    return self._send(409, {
                        "detail": "当前黄雀账号已绑定其他微信账号，请使用原微信或联系管理员",
                        "code": "openid_mismatch",
                    })
                if err == "user_not_found":
                    return self._send(404, {"detail": "用户不存在"})
                if err:
                    return self._send(409, {"detail": "订单创建失败，请重试"})
                return self._send(200, {"ok": True, **result})
            except wechat_vpay.VirtualPayError as exc:
                status = 503 if exc.code == "not_configured" else 502
                if exc.code in {"bad_request", "code2session_failed"}:
                    status = 400
                return self._send(status, {"detail": str(exc), "code": exc.code})
            except Exception:
                return self._send(500, {"detail": "虚拟支付订单创建失败"})
        if p == "/api/auth/virtual-pay/confirm":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                order, err = confirm_virtual_pay_order(row["username"], d.get("order_id"))
                if err == "not_found":
                    return self._send(404, {"detail": "订单不存在"})
                if err == "pending":
                    return self._send(202, {"ok": False, "pending": True, "order": order})
                if err == "not_paid":
                    return self._send(409, {"detail": "微信订单未支付或已关闭", "order": order})
                if err in {"order_mismatch", "amount_mismatch"}:
                    return self._send(409, {"detail": "微信订单校验失败", "code": err})
                if err:
                    return self._send(400, {"detail": err})
                points_row = get_points_row(row["username"])
                return self._send(200, {
                    "ok": True,
                    "order": order,
                    "points": points_row["points"] if points_row else None,
                })
            except wechat_vpay.VirtualPayError as exc:
                return self._send(502, {"detail": str(exc), "code": exc.code})
            except Exception:
                return self._send(500, {"detail": "支付结果确认失败"})
        if p == "/api/auth/recharge/order":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if not self._require_purchase_password(row):
                return
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            pricing_tier = membership_for_row(row)["membership_tier"]
            quote = purchase_quote(
                d.get("amount"), d.get("product_type") or "points",
                membership_tier=pricing_tier,
            )
            if quote is None:
                return self._send(400, {"detail": "充值商品或金额无效"})
            amount, points, order_type = quote
            if order_type == "points" and not self._require_membership(row):
                return
            purchase_error = membership_purchase_error(row, order_type)
            if purchase_error:
                detail = "该账号已有会员记录，不能重复领取首购权益" if purchase_error == "membership_already_owned" else "仅有效体验官可自助续费"
                return self._send(409, {"detail": detail, "code": purchase_error})
            try:
                order, err = create_recharge_order(
                    row["username"], amount, points, d.get("note") or "", order_type,
                    list_amount=d.get("amount"), pricing_tier=pricing_tier,
                    discount_bps=membership_discount_bps(pricing_tier) if order_type == "points" else 10000,
                )
                if err:
                    status = 409 if err in {"membership_already_owned", "membership_renewal_not_eligible", "membership_order_exists"} else 400
                    return self._send(status, {"detail": err, "code": err})
                return self._send(200, {"ok": True, "order": order})
            except Exception:
                return self._send(500, {"detail": "充值申请提交失败"})
        if p == "/api/auth/wxpay/native":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if not self._require_purchase_password(row):
                return
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"detail": "微信支付未配置"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            pricing_tier = membership_for_row(row)["membership_tier"]
            quote = purchase_quote(
                d.get("amount"), d.get("product_type") or "points",
                membership_tier=pricing_tier,
            )
            if quote is None:
                return self._send(400, {"detail": "充值商品或金额无效"})
            amount, points, order_type = quote
            if order_type == "points" and not self._require_membership(row):
                return
            purchase_error = membership_purchase_error(row, order_type)
            if purchase_error:
                detail = "该账号已有会员记录，不能重复领取首购权益" if purchase_error == "membership_already_owned" else "仅有效体验官可自助续费"
                return self._send(409, {"detail": detail, "code": purchase_error})
            order = None
            try:
                order, err = create_recharge_order(
                    row["username"], amount, points,
                    "微信扫码充值" if order_type == "points" else ("微信扫码续费体验官" if order_type == MEMBERSHIP_RENEWAL_ORDER_TYPE else "微信扫码开通体验官"),
                    order_type,
                    list_amount=d.get("amount"), pricing_tier=pricing_tier,
                    discount_bps=membership_discount_bps(pricing_tier) if order_type == "points" else 10000,
                )
                if err:
                    status = 409 if err in {"membership_already_owned", "membership_renewal_not_eligible", "membership_order_exists"} else 400
                    return self._send(status, {"detail": err, "code": err})
                code_url = wxpay.create_native(
                    order["order_id"],
                    "黄雀点数充值 %d点" % points if order_type == "points" else "黄雀体验官会员（一年）",
                    int(round(amount * 100)))
                return self._send(200, {"ok": True, "order": order, "code_url": code_url})
            except Exception as e:
                fail_recharge_order((order or {}).get("order_id"), "微信扫码下单失败")
                return self._send(502, {"detail": "微信下单失败", "error": str(e)[:200]})
        if p == "/api/auth/wxpay/jsapi":
            # 小程序内充值:需登录态(定位黄雀账号) + wx.login 的 js_code(换微信 openid)
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if not self._require_purchase_password(row):
                return
            if not miniprogram_payments_enabled():
                return self._send(503, {
                    "detail": "小程序支付功能暂时关闭",
                    "code": "payment_disabled",
                })
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"detail": "微信支付未配置"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            js_code = (d.get("js_code") or "").strip()
            if not js_code:
                return self._send(400, {"detail": "缺少 js_code"})
            pricing_tier = membership_for_row(row)["membership_tier"]
            quote = purchase_quote(
                d.get("amount"), d.get("product_type") or "points", jsapi=True,
                membership_tier=pricing_tier,
            )
            if quote is None:
                return self._send(400, {"detail": "充值商品或金额无效"})
            amount, points, order_type = quote
            if order_type == "points" and not self._require_membership(row):
                return
            purchase_error = membership_purchase_error(row, order_type)
            if purchase_error:
                detail = "该账号已有会员记录，不能重复领取首购权益" if purchase_error == "membership_already_owned" else "仅有效体验官可自助续费"
                return self._send(409, {"detail": detail, "code": purchase_error})
            order = None
            try:
                openid = wxpay.jscode2session(js_code)
                order, err = create_recharge_order(
                    row["username"], amount, points,
                    "微信小程序充值" if order_type == "points" else ("微信小程序续费体验官" if order_type == MEMBERSHIP_RENEWAL_ORDER_TYPE else "微信小程序开通体验官"),
                    order_type,
                    list_amount=d.get("amount"), pricing_tier=pricing_tier,
                    discount_bps=membership_discount_bps(pricing_tier) if order_type == "points" else 10000,
                )
                if err:
                    status = 409 if err in {"membership_already_owned", "membership_renewal_not_eligible", "membership_order_exists"} else 400
                    return self._send(status, {"detail": err, "code": err})
                prepay_id = wxpay.create_jsapi(
                    order["order_id"],
                    "黄雀点数充值 %d点" % points if order_type == "points" else "黄雀体验官会员（一年）",
                    int(round(amount * 100)), openid)
                pay = wxpay.jsapi_pay_params(prepay_id)   # 客户端 wx.requestPayment 参数
                return self._send(200, {"ok": True, "order": order, "pay": pay})
            except Exception as e:
                fail_recharge_order((order or {}).get("order_id"), "微信小程序下单失败")
                return self._send(502, {"detail": "微信下单失败", "error": str(e)[:200]})
        if p == "/api/auth/wxpay/reconcile":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"detail": "微信支付未配置"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            order_id = (d.get("order_id") or "").strip()
            order_row = get_recharge_order(order_id)
            if not order_row or order_row["username"] != row["username"]:
                return self._send(404, {"detail": "订单不存在"})
            if order_row["status"] == "approved":
                return self._send(200, {"ok": True, "order": public_recharge_order(order_row)})
            if order_row["status"] != "pending":
                return self._send(409, {
                    "detail": "订单已关闭", "code": "order_closed",
                    "order": public_recharge_order(order_row),
                })
            try:
                payment = wxpay.query_transaction(order_id)
                order, err = reconcile_wxpay_recharge(order_row, payment)
                if err == "payment_pending":
                    return self._send(409, {
                        "detail": "微信订单尚未支付成功", "code": "payment_pending",
                        "order": order,
                    })
                if err:
                    return self._send(409, {"detail": "微信订单校验失败", "code": err})
                return self._send(200, {"ok": True, "order": order})
            except Exception:
                return self._send(502, {"detail": "微信订单查询失败", "code": "query_failed"})
        if p == "/api/auth/wxpay/notify":
            # 微信服务器回调:不带登录态/内部 token,靠 V3 签名验真。必须读原始字节验签。
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            if wxpay is None or not wxpay.configured():
                return self._send(503, {"code": "FAIL", "message": "not configured"})
            if not wxpay.verify_notify(self.headers, raw):
                return self._send(401, {"code": "FAIL", "message": "签名验证失败"})
            try:
                envelope = json.loads(raw or b"{}")
                resource = wxpay.decrypt_resource(envelope["resource"])
            except Exception:
                return self._send(400, {"code": "FAIL", "message": "解密失败"})
            order_id = (resource.get("out_trade_no") or "").strip()
            order_row = get_recharge_order(order_id)
            if not order_row:
                return self._send(200, {"code": "SUCCESS"})   # 未知订单,回200止重推,不加点
            if str(envelope.get("event_type") or "").startswith("REFUND."):
                if envelope.get("event_type") != "REFUND.SUCCESS":
                    return self._send(200, {"code": "SUCCESS"})
                try:
                    refund_recharge_order(order_id, resource)
                    return self._send(200, {"code": "SUCCESS"})
                except Exception:
                    return self._send(500, {"code": "FAIL", "message": "退款权益处理失败"})
            try:
                _, err = reconcile_wxpay_recharge(order_row, resource, actor="wxpay")
                if err in {
                    "payment_pending", "identity_mismatch", "order_mismatch",
                    "amount_mismatch", "missing_transaction_id", "transaction_in_use",
                    "not_pending",
                }:
                    return self._send(200, {"code": "SUCCESS"})
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
            transaction_key = str(d.get("transaction_key") or "").strip()
            if len(transaction_key) > 160:
                return self._send(400, {"detail": "transaction_key too long"})
            try:
                if p.endswith("/deduct"):
                    if membership_enforcement_enabled() and not user_has_active_membership(username):
                        return self._send(403, {
                            "detail": "请先开通会员后再使用该功能",
                            "code": "membership_required",
                            "membership_enforcement_enabled": True,
                        })
                    points, err = deduct_points(username, amount, reason, transaction_key)
                    if err == "transaction_conflict":
                        return self._send(409, {"detail": "transaction_key conflict"})
                    if err == "insufficient":
                        return self._send(402, {"detail": "点数不足", "need": amount})
                else:
                    points, err = refund_points(username, amount, reason, transaction_key)
                    if err == "transaction_conflict":
                        return self._send(409, {"detail": "transaction_key 已用于另一笔退款"})
                if err == "not_found":
                    return self._send(404, {"detail": "user not found"})
                return self._send(200, {"ok": True, "points": points["points"], "user": points})
            except Exception:
                return self._send(500, {"detail": "points update failed"})
        if p == "/api/auth/register":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            if not self._consume_register_attempt(d.get("device_id")):
                return self._send(429, {"detail": "注册次数过多，请稍后再试"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            name = (d.get("display_name") or u).strip() or u
            source = "web_link" if str(d.get("invite_source") or "") == "web_link" else "web_manual"
            result, err = register_account(
                u, pw, name, invite_code=d.get("invite_code"), invite_source=source,
                client_ip=self._client_ip(), device_id=d.get("device_id"), card=d.get("card"),
            )
            if err:
                return self._send(err["status"], {"detail": err["detail"], "code": err["code"]})
            return self._send(
                200, {"user": result["user"], "invite_bound": result["invite_bound"]},
                {"Set-Cookie": auth_cookie_header(result["token"])},
            )
        if p == "/api/auth/login":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            length_error = credential_length_error(u, pw)
            if length_error:
                return self._send(400, length_error)
            if self._login_limited(u):
                return self._send(429, {"detail": "登录失败次数过多，请稍后再试"})
            c = db(); row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); c.close()
            if not row or hash_pw(pw, row["pw_salt"]) != row["pw_hash"]:
                self._record_login_failure(u)
                return self._send(401, {"detail": "账号或密码错误"})
            if row["account_status"] != "active":
                return self._send(403, {"detail": "账号已被停用，请联系管理员", "code": "account_banned"})
            self._clear_login_failures(u)
            account_id = row["account_id"] or ensure_account_id(u)
            tok = issue_token(u)
            return self._send(200, {"user": public_user(
                u, row["display_name"], row["points"], row["role"], row["must_change"], account_id,
                row["membership_tier"], row["membership_started_at"], row["membership_expires_at"],
            )}, {"Set-Cookie": auth_cookie_header(tok)})
        if p == "/api/auth/card/wechat/bind":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            openid, err = miniprogram_openid(d.get("wx_code"))
            if err:
                return self._send(err["status"], {"detail": err["detail"], "code": err["code"]})
            c = db()
            try:
                c.execute("BEGIN IMMEDIATE")
                business_cards.bind_miniprogram_openid(c, row["id"], openid)
                card = card_for_owner(c, row["id"])
                card_token = issue_token(row["username"], c, scope="card")
                c.commit()
                return self._send(200, {
                    "card": card, "wechat_bound": True,
                    "card_token": card_token,
                    "ai_account": card["ai_account"],
                    "initial_password": card["initial_password"],
                })
            except business_cards.CardError as exc:
                c.rollback()
                return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
            finally:
                c.close()
        if p in ("/api/auth/miniprogram/card-login", "/api/auth/miniprogram/card-session"):
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            openid, err = miniprogram_openid(d.get("wx_code"))
            if err:
                return self._send(err["status"], {"detail": err["detail"], "code": err["code"]})
            c = db()
            try:
                owner = c.execute("""SELECT u.* FROM business_cards bc JOIN users u ON u.id=bc.user_id
                                     WHERE bc.miniprogram_openid=? AND u.account_status='active'""", (openid,)).fetchone()
                if not owner:
                    return self._send(404, {"detail": "微信尚未绑定名片", "code": "card_unbound"})
                card = card_for_owner(c, owner["id"])
                if p.endswith("/card-session"):
                    token = issue_token(owner["username"], c, scope="card")
                    c.commit()
                    return self._send(200, {
                        "card_token": token, "card": card,
                        "ai_account": card["ai_account"],
                        "initial_password": card["initial_password"],
                    })
                account_id = owner["account_id"] or ensure_account_id(owner["username"], c)
                token = issue_token(owner["username"], c)
                c.commit()
                return self._send(200, {"token": token, "user": public_user(
                    owner["username"], owner["display_name"], owner["points"], owner["role"], owner["must_change"], account_id,
                    owner["membership_tier"], owner["membership_started_at"], owner["membership_expires_at"],
                ), "card": card})
            finally:
                c.close()
        if p == "/api/auth/miniprogram/card-account-login":
            owner = self._card_token_user()
            if not owner:
                return self._send(401, {"detail": "微信名片授权已失效", "code": "card_session_invalid"})
            account_id = owner["account_id"] or ensure_account_id(owner["username"])
            token = issue_token(owner["username"])
            return self._send(200, {"token": token, "user": public_user(
                owner["username"], owner["display_name"], owner["points"], owner["role"], owner["must_change"], account_id,
                owner["membership_tier"], owner["membership_started_at"], owner["membership_expires_at"],
            )})
        if p == "/api/auth/invite/journey/start":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            try:
                attribution = business_cards.verify_attribution(
                    d.get("invite_attribution_token"), INVITE_HASH_SECRET,
                )
                if not self._consume_register_attempt("invite-journey:" + str(attribution["card_public_id"])):
                    return self._send(429, {"detail": "操作过于频繁，请稍后再试", "code": "rate_limited"})
                c = db()
                try:
                    c.execute("BEGIN IMMEDIATE")
                    invite = invites.validate_code(
                        c, attribution["code"], enforce_membership=False,
                    )
                    if (
                        int(invite["inviter_user_id"]) != int(attribution["owner_user_id"])
                        or business_cards.public_owner(c, attribution["card_public_id"]) != int(attribution["owner_user_id"])
                    ):
                        raise business_cards.CardError("invalid_invite_attribution", "邀请归因已失效", 409)
                    journey = business_cards.start_referral_journey(c, attribution, invite["campaign_id"])
                    c.commit()
                    return self._send(200, {"ok": True, "started": bool(journey)})
                finally:
                    c.close()
            except invites.InviteError as exc:
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except business_cards.CardError as exc:
                return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
        if p == "/api/auth/miniprogram/card-register":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            if not self._consume_register_attempt(d.get("device_id")):
                return self._send(429, {"detail": "注册次数过多，请稍后再试"})
            result, err = register_miniprogram_card(
                d.get("wx_code"), d.get("phone"), d.get("card"), d.get("device_id"),
                invite_code=d.get("invite_code"), invite_attribution_token=d.get("invite_attribution_token"),
                client_ip=self._client_ip(), separate_sessions=d.get("separate_sessions") is True,
            )
            if err:
                return self._send(err["status"], {"detail": err["detail"], "code": err["code"]})
            return self._send(200, result)
        if p == "/api/auth/miniprogram-login":
            # 小程序 wx.request 不像浏览器那样自动带 httpOnly cookie，专供小程序客户端：
            # token 放响应体让小程序自己存起来，后续走 Authorization: Bearer 请求头（网站登录/注册不受影响，仍只走 cookie）。
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            length_error = credential_length_error(u, pw)
            if length_error:
                return self._send(400, length_error)
            if self._login_limited(u):
                return self._send(429, {"detail": "登录失败次数过多，请稍后再试"})
            c = db(); row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); c.close()
            if not row or hash_pw(pw, row["pw_salt"]) != row["pw_hash"]:
                self._record_login_failure(u)
                return self._send(401, {"detail": "账号或密码错误"})
            if row["account_status"] != "active":
                return self._send(403, {"detail": "账号已被停用，请联系管理员", "code": "account_banned"})
            self._clear_login_failures(u)
            account_id = row["account_id"] or ensure_account_id(u)
            tok = issue_token(u)
            return self._send(200, {"token": tok, "user": public_user(
                u, row["display_name"], row["points"], row["role"], row["must_change"], account_id,
                row["membership_tier"], row["membership_started_at"], row["membership_expires_at"],
            )})
        if p == "/api/auth/miniprogram-register":
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            if not self._consume_register_attempt(d.get("device_id")):
                return self._send(429, {"detail": "注册次数过多，请稍后再试"})
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            name = (d.get("display_name") or u).strip() or u
            result, err = register_account(
                u, pw, name, invite_code=d.get("invite_code"), invite_source="miniprogram",
                client_ip=self._client_ip(), device_id=d.get("device_id"), card=d.get("card"),
                invite_attribution_token=d.get("invite_attribution_token"),
            )
            if err:
                return self._send(err["status"], {"detail": err["detail"], "code": err["code"]})
            response = {
                "token": result["token"], "user": result["user"],
                "invite_bound": result["invite_bound"],
            }
            if result.get("card") is not None:
                response["card"] = result["card"]
            return self._send(200, response)
        if p in ("/api/auth/card/publish", "/api/auth/card/unpublish", "/api/auth/card/media"):
            row = self._card_user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if p.endswith("/media") and self._content_length_exceeds(28 * 1024 * 1024):
                return self._send(413, {"detail": "媒体文件过大", "code": "media_too_large"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            key = None
            if p.endswith("/media"):
                field = d.get("field")
                work_type = "image" if field in business_cards.WORK_IMAGE_FIELDS else (
                    "video" if field in business_cards.WORK_VIDEO_FIELDS else ""
                )
                work_fields = business_cards.WORK_IMAGE_FIELDS if work_type == "image" else business_cards.WORK_VIDEO_FIELDS
                work_slot = work_fields.index(field) + 1 if work_type else 0
                title = d.get("title") if "title" in d else None
                if work_slot and (title is not None and (not isinstance(title, str) or len(title.strip()) > 160)):
                    return self._send(400, {"detail": "作品标题无效", "code": "invalid_title"})
                try:
                    upload = business_cards.upload_video if work_type == "video" else business_cards.upload_image
                    key = upload(d.get("data"), field, prefix="cards/%s" % row["id"] if work_slot else "cards")
                except business_cards.CardError as exc:
                    return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
                except Exception:
                    return self._send(503, {"detail": "名片媒体服务暂不可用", "code": "media_unavailable"})
            c = db()
            try:
                c.execute("BEGIN IMMEDIATE")
                if p.endswith("/publish") or p.endswith("/unpublish"):
                    business_cards.publish(c, row["id"], "unpublished" if p.endswith("/unpublish") else (d.get("status") or "published"))
                    if not p.endswith("/unpublish"):
                        c.execute(
                            "UPDATE card_referral_journeys SET published_at=COALESCE(published_at,?) WHERE registered_user_id=?",
                            (int(time.time()), row["id"]),
                        )
                    card = card_for_owner(c, row["id"])
                    result = {"ok": True, "card": card}
                else:
                    if work_slot:
                        business_cards.set_work_media_key(c, row["id"], work_type, work_slot, key, title)
                    else:
                        business_cards.set_media_key(c, row["id"], d.get("field"), key)
                    card = card_for_owner(c, row["id"])
                    if work_slot:
                        work = next(
                            item for item in card["works"]
                            if isinstance(item, dict) and item.get("type") == work_type and item.get("slot") == work_slot
                        )
                        result = {"ok": True, "url": work.get("url", ""), "key": key, "work": work, "card": card}
                    else:
                        result = {"ok": True, "url": card[d.get("field")], "card": card}
                c.commit()
                return self._send(200, result)
            except business_cards.CardError as exc:
                c.rollback(); return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                c.rollback(); return self._send(503, {"detail": "名片媒体服务暂不可用", "code": "media_unavailable"})
            finally:
                c.close()
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
            row = self._card_user()
            if not row: return self._send(401, {"detail": "未登录"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            oldp = d.get("old_password") or ""
            newp = d.get("new_password") or ""
            if not oldp: return self._send(400, {"detail": "请填写当前密码"})
            if len(newp) < 6: return self._send(400, {"detail": "新密码至少 6 位"})
            if len(oldp) > PASSWORD_MAX_LENGTH or len(newp) > PASSWORD_MAX_LENGTH:
                return self._send(400, {"detail": "密码最多 128 位", "code": "password_too_long"})
            salt = secrets.token_hex(16)
            c = db()
            try:
                fresh = c.execute("SELECT pw_hash,pw_salt FROM users WHERE username=?", (row["username"],)).fetchone()
                if not fresh or hash_pw(oldp, fresh["pw_salt"]) != fresh["pw_hash"]:
                    return self._send(400, {"detail": "当前密码不正确"})
                c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change=0, card_initial_password=0 WHERE username=?",
                          (hash_pw(newp, salt), salt, row["username"]))
                c.execute("DELETE FROM tokens WHERE username=? AND COALESCE(scope,'account')='account'", (row["username"],))
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
                fresh["account_id"] or ensure_account_id(fresh["username"]),
                fresh["membership_tier"], fresh["membership_started_at"], fresh["membership_expires_at"],
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
                if err == "too_many_boards":
                    return self._send(429, {"detail": "画布数量已达上限，请先删除不再需要的画布"})
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
                if err == "conflict":
                    return self._send(409, {"detail": "画布已被其他成员更新，请先同步",
                                            "version": (result or {}).get("version")})
                if err == "rate_limited":
                    return self._send(429, {"detail": "操作太频繁，请稍候",
                                            "retry_after": (result or {}).get("retry_after")})
                if err == "idempotency_conflict":
                    return self._send(409, {"detail": "op_id 已绑定其他操作", "code": err})
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
                if err == "too_many_members":
                    return self._send(429, {"detail": "画布成员数量已达上限"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "members": members})
            except Exception:
                return self._send(500, {"detail": "canvas member update failed"})
        if p.startswith(canvas_prefix) and p.endswith("/leave"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            board_id = urllib.parse.unquote(p[len(canvas_prefix):-len("/leave")])
            try:
                result, err = leave_canvas_board(row["username"], board_id)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在或你不是成员"})
                if err == "owner_cannot_leave":
                    return self._send(400, {"detail": "创建者不能退出画布，可以选择删除画布"})
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, result)
            except Exception:
                return self._send(500, {"detail": "canvas leave failed"})
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

    def do_PUT(self):
        p = self.path.split("?", 1)[0]
        if p == "/api/auth/card/me":
            row = self._card_user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if self._content_length_exceeds(business_cards.MAX_JSON_BYTES):
                return self._send(413, {"detail": "请求过大", "code": "card_too_large"})
            d = self._body()
            if self._bad_json():
                return self._send(400, {"detail": "请求体不是合法 JSON"})
            c = db()
            try:
                c.execute("BEGIN IMMEDIATE")
                business_cards.update(c, row["id"], d)
                card = card_for_owner(c, row["id"])
                c.commit()
                return self._send(200, {"ok": True, "card": card})
            except business_cards.CardError as exc:
                c.rollback(); return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
            finally:
                c.close()
        if p != "/api/auth/admin/invite/config":
            return self._send(404, {"detail": "not found"})
        if not self._require_internal():
            return
        admin = self._require_admin_user()
        if not admin:
            return
        d = self._body()
        if self._bad_json():
            return self._send(400, {"detail": "请求体不是合法 JSON"})
        c = db()
        try:
            config = invites.admin_update_config(c, d, admin["id"])
            c.commit()
            return self._send(200, {"ok": True, "config": config})
        except invites.InviteError as exc:
            c.rollback()
            return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
        except Exception:
            c.rollback()
            return self._send(500, {"detail": "邀请活动配置保存失败"})
        finally:
            c.close()

    def do_DELETE(self):
        p = self.path.split("?")[0]
        friend_prefix = "/api/auth/friends/"
        canvas_prefix = "/api/auth/canvas/boards/"
        row = self._user()
        if not row:
            return self._send(401, {"detail": "未登录"})
        if p.startswith(friend_prefix):
            friend_username = urllib.parse.unquote(p[len(friend_prefix):])
            friends, err = remove_friend(row["username"], friend_username)
            if err == "not_found":
                return self._send(404, {"detail": "好友关系不存在"})
            return self._send(200, {"ok": True, "friends": friends})
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
        if p == "/api/auth/points/transaction":
            if not self._require_internal():
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = str((query.get("transaction_key") or [""])[0]).strip()
            if not key or len(key) > 160:
                return self._send(400, {"detail": "invalid transaction_key"})
            return self._send(200, {
                "ok": True, "transaction": get_points_transaction(key),
            })
        if p in ("/api/auth/card/me", "/api/auth/card/public", "/api/auth/network/ancestors", "/api/auth/network/children", "/api/admin/invite/network", "/api/auth/admin/invite/network") or p.startswith("/api/auth/card/media/"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if p.startswith("/api/auth/card/media/"):
                parts = p[len("/api/auth/card/media/"):].split("/")
                if len(parts) != 2:
                    return self._send(404, {"detail": "not found"})
                c = db()
                try:
                    key = business_cards.media_key(c, parts[0], parts[1])
                    if not key:
                        return self._send(404, {"detail": "not found"})
                    try:
                        from .content_domains import cos
                    except ImportError:
                        from content_domains import cos
                    return self._send(200, {"ok": True, "url": cos.object_url(key, private=True)})
                except business_cards.CardError as exc:
                    return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
                except Exception:
                    return self._send(503, {"detail": "媒体服务暂不可用", "code": "media_unavailable"})
                finally:
                    c.close()
            if p == "/api/auth/card/public":
                c = db()
                try:
                    public_id = (query.get("id") or [""])[0]
                    card = business_cards.public(c, public_id)
                    now = int(time.time())
                    response = {"ok": True, "card": card, "invite_valid": False, "server_time": now}
                    code = invites.normalize_code((query.get("invite") or [""])[0])
                    owner_id = business_cards.owner(c, public_id)
                    try:
                        card["invite_code"] = invites.ensure_user_code(
                            c, owner_id, now=now, enforce_membership=False,
                        )["code"]
                    except invites.InviteError:
                        card["invite_code"] = ""
                    if code and INVITE_HASH_SECRET:
                        try:
                            invite = invites.validate_code(c, code, enforce_membership=False)
                            if int(invite["inviter_user_id"]) == owner_id:
                                response["invite_valid"] = True
                                response["invite_validated_at"] = now
                                response["invite_expires_at"] = now + 7 * 24 * 3600
                                response["invite_attribution_token"] = business_cards.attribution_token(
                                    code, public_id, owner_id, INVITE_HASH_SECRET, now=now,
                                    journey_id=secrets.token_urlsafe(18),
                                )
                        except invites.InviteError:
                            pass
                    c.commit()
                    return self._send(200, response)
                except business_cards.CardError as exc:
                    return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
                finally:
                    c.close()
            if p == "/api/auth/card/me":
                row = self._card_user()
                if not row:
                    return self._send(401, {"detail": "未登录"})
                c = db()
                try:
                    c.execute("BEGIN IMMEDIATE")
                    business_cards.create_draft(c, row["id"])
                    card = card_for_owner(c, row["id"])
                    c.commit()
                    return self._send(200, {
                        "ok": True, "card": card,
                        "ai_account": card["ai_account"],
                        "initial_password": card["initial_password"],
                        "wechat_bound": card["wechat_bound"],
                    })
                finally:
                    c.close()
            if p in ("/api/admin/invite/network", "/api/auth/admin/invite/network"):
                if not self._require_internal():
                    return
                if not self._require_admin_user():
                    return
                c = db()
                try:
                    target = (query.get("user_id") or [""])[0]
                    if not target and (query.get("search") or [""])[0]:
                        term = "%" + (query.get("search") or [""])[0].strip() + "%"
                        users = c.execute("SELECT id,username FROM users WHERE username LIKE ? OR display_name LIKE ? ORDER BY id LIMIT 20", (term, term)).fetchall()
                        items = [{"user_id": int(r["id"]), "username": invites.masked_admin_account(r["username"]), **business_cards.public_network_person(c, r["id"], admin=True)} for r in users]
                        c.commit()
                        return self._send(200, {"ok": True, "items": items})
                    user = c.execute("SELECT id,username FROM users WHERE CAST(id AS TEXT)=? OR username=?", (target, target)).fetchone()
                    if not user:
                        return self._send(404, {"detail": "not found"})
                    parent = (query.get("parent_id") or [""])[0]
                    parent_id = business_cards.node_user_id(c, parent) if parent else int(user["id"])
                    if parent and not parent_id:
                        try: parent_id = int(parent)
                        except ValueError: return self._send(404, {"detail": "not found"})
                    data = business_cards.children(c, parent_id, (query.get("before_id") or ["0"])[0], (query.get("limit") or ["12"])[0], admin=True)
                    root = business_cards.public_network_person(c, user["id"], admin=True)
                    root.update({"user_id": int(user["id"]), "username": invites.masked_admin_account(user["username"])})
                    ancestors = business_cards.ancestors(c, user["id"], admin=True)
                    c.commit()
                    return self._send(200, {"ok": True, "root": root, "user": root, "ancestors": ancestors, "children": data["items"], **data})
                except (ValueError, business_cards.CardError):
                    return self._send(400, {"detail": "分页参数无效"})
                finally:
                    c.close()
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            c = db()
            try:
                if p.endswith("/ancestors"):
                    result = {"ok": True, "root": business_cards.public_network_person(c, row["id"]), "items": business_cards.ancestors(c, row["id"])}
                    c.commit()
                    return self._send(200, result)
                parent = (query.get("parent") or ["self"])[0]
                parent_id = row["id"]
                if parent != "self":
                    parent_id = business_cards.node_user_id(c, parent)
                    if not parent_id:
                        return self._send(404, {"detail": "not found"})
                    if int(row["id"]) not in set(business_cards.ancestor_ids(c, parent_id)):
                        return self._send(404, {"detail": "not found"})
                data = business_cards.children(c, parent_id, (query.get("before_id") or ["0"])[0], (query.get("limit") or ["12"])[0])
                result = {"ok": True, "root": business_cards.public_network_person(c, parent_id), **data}
                c.commit()
                return self._send(200, result)
            except (ValueError, business_cards.CardError):
                return self._send(400, {"detail": "分页参数无效"})
            finally:
                c.close()
        if p == "/api/auth/cli/status":
            auth = self._cli_user()
            if not auth:
                return self._cli_send(401, {"detail": "CLI 未登录或授权已过期", "code": "cli_unauthorized"})
            row, scopes = auth
            return self._cli_send(200, {"user": self._cli_public_user(row), "scopes": list(scopes),
                                        "expires_at": int(row["cli_expires_at"])})
        if p == "/api/auth/subscription/status":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            try:
                return self._send(200, {"ok": True, **subscription_status(row["username"])})
            except Exception:
                return self._send(500, {"detail": "订阅状态读取失败"})
        if p.startswith("/api/auth/admin/invite/"):
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            c = db()
            try:
                if p == "/api/auth/admin/invite/config":
                    row = c.execute("SELECT * FROM invite_campaigns ORDER BY id DESC LIMIT 1").fetchone()
                    return self._send(200, {"ok": True, "config": dict(row) if row else None})
                if p == "/api/auth/admin/invite/stats":
                    return self._send(200, {"ok": True, **invites.admin_stats(c, (query.get("days") or ["30"])[0])})
                if p == "/api/auth/admin/invite/journeys":
                    journey_filters = {
                        key: (query.get(key) or [""])[0]
                        for key in ("user", "status")
                    }
                    data = invites.admin_referral_journeys(
                        c, journey_filters, (query.get("days") or ["30"])[0],
                        (query.get("limit") or ["100"])[0],
                        (query.get("offset") or ["0"])[0],
                    )
                    return self._send(200, {"ok": True, **data})
                filters = {
                    key: (query.get(key) or [""])[0]
                    for key in ("inviter", "invitee", "code", "status", "risk_status", "start_at", "end_at")
                }
                if p == "/api/auth/admin/invite/relations":
                    data = invites.admin_relations(
                        c, filters, (query.get("limit") or ["50"])[0], (query.get("offset") or ["0"])[0],
                    )
                    data["items"] = [invites.admin_relation_view(item) for item in data["items"]]
                    return self._send(200, {"ok": True, **data})
                if p == "/api/auth/admin/invite/audit":
                    return self._send(200, {"ok": True, "items": invites.admin_audit(c, (query.get("limit") or ["100"])[0])})
                if p == "/api/auth/admin/invite/reward-points":
                    reward_filters = {key: (query.get(key) or [""])[0] for key in ("inviter", "invitee", "status")}
                    data = invites.admin_reward_points(
                        c, reward_filters, (query.get("limit") or ["100"])[0],
                        (query.get("offset") or ["0"])[0],
                    )
                    data["items"] = [invites.admin_reward_view(item) for item in data["items"]]
                    return self._send(200, {"ok": True, **data})
                if p == "/api/auth/admin/invite/reward-claims":
                    claim_filters = {
                        key: (query.get(key) or [""])[0]
                        for key in ("inviter", "invitee", "status")
                    }
                    data = invites.admin_reward_claims(
                        c, claim_filters, (query.get("limit") or ["100"])[0],
                        (query.get("offset") or ["0"])[0],
                    )
                    return self._send(200, {"ok": True, **data})
                if p == "/api/auth/admin/invite/export.xlsx":
                    body = invites.export_relations_xlsx(c, filters)
                    filename = "invite-relations-%s.xlsx" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    return self._send_raw(200, body,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        {"Content-Disposition": 'attachment; filename="%s"' % filename})
                return self._send(404, {"detail": "not found"})
            except invites.InviteError as exc:
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                return self._send(500, {"detail": "邀请管理查询失败"})
            finally:
                c.close()
        if p in ("/api/invite/config", "/api/auth/invite/config"):
            c = db()
            try:
                return self._send(200, {"ok": True, **invites.campaign_config(c)})
            finally:
                c.close()
        if p in ("/api/invite/validate", "/api/auth/invite/validate"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (query.get("code") or [""])[0]
            c = db()
            try:
                row = invites.validate_code(
                    c, code, enforce_membership=False,
                )
                return self._send(200, {
                    "ok": True, "code": row["code"], "inviter": invites.public_inviter(row),
                })
            except invites.InviteError as exc:
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            finally:
                c.close()
        if p in ("/api/invite/code", "/api/auth/invite/code"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            c = db()
            try:
                c.execute("BEGIN IMMEDIATE")
                code_row = invites.ensure_user_code(
                    c, row["id"], enforce_membership=False,
                )
                c.commit()
                return self._send(200, {
                    "ok": True,
                    "code": code_row["code"],
                    "invite_link": INVITE_PUBLIC_BASE_URL + "/register?invite=" + code_row["code"],
                })
            except invites.InviteError as exc:
                c.rollback()
                return self._send(exc.http_status, {"detail": exc.detail, "code": exc.code})
            except Exception:
                c.rollback()
                return self._send(500, {"detail": "邀请码获取失败"})
            finally:
                c.close()
        if p in ("/api/invite/dashboard", "/api/auth/invite/dashboard"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            c = db()
            try:
                return self._send(200, {"ok": True, **invites.dashboard(c, row["id"])})
            finally:
                c.close()
        if p in ("/api/invite/users", "/api/auth/invite/users"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            c = db()
            try:
                data = invites.invited_users(
                    c, row["id"],
                    level=(query.get("level") or ["1"])[0],
                    limit=(query.get("limit") or ["10"])[0],
                    offset=(query.get("offset") or ["0"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except (TypeError, ValueError):
                return self._send(400, {"detail": "分页参数无效"})
            finally:
                c.close()
        if p in ("/api/invite/downlines", "/api/auth/invite/downlines"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            c = db()
            try:
                data = invite_network.downlines_page(
                    c, row["id"], INVITE_HASH_SECRET,
                    cursor=(query.get("cursor") or ["0"])[0],
                    limit=(query.get("limit") or ["20"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except invite_network.NetworkError as exc:
                return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
            except (TypeError, ValueError):
                return self._send(400, {"detail": "分页参数无效"})
            finally:
                c.close()
        if p in ("/api/invite/network", "/api/auth/invite/network"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            c = db()
            try:
                data = invite_network.network_page(
                    c, row["id"], (query.get("grant") or [""])[0], INVITE_HASH_SECRET,
                    cursor=(query.get("cursor") or ["0"])[0],
                    limit=(query.get("limit") or ["20"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except invite_network.NetworkError as exc:
                return self._send(exc.status, {"detail": exc.detail, "code": exc.code})
            except (TypeError, ValueError):
                return self._send(400, {"detail": "分页参数无效"})
            finally:
                c.close()
        if p in ("/api/invite/notices/next", "/api/auth/invite/notices/next"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            membership = membership_for_row(row)
            hidden = membership["membership_tier"] in ("partner", "initiator")
            c = db()
            try:
                notice = invites.next_reward_notice(c, row["id"])
                if notice and hidden:
                    if "reward_points" in notice:
                        notice["reward_points"] = 0
                    if "total_points" in notice:
                        notice["total_points"] = 0
                return self._send(200, {
                    "ok": True,
                    "notice": notice,
                    "server_time": int(time.time()),
                })
            finally:
                c.close()
        if p in ("/api/invite/reward-points", "/api/auth/invite/reward-points"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            membership = membership_for_row(row)
            hide_member_rewards = membership["membership_tier"] in ("partner", "initiator")
            c = db()
            try:
                data = invites.reward_points(
                    c, row["id"],
                    limit=(query.get("limit") or ["20"])[0],
                    offset=(query.get("offset") or ["0"])[0],
                    hidden=hide_member_rewards,
                )
                return self._send(200, {"ok": True, **data})
            except (TypeError, ValueError):
                return self._send(400, {"detail": "分页参数无效"})
            finally:
                c.close()
        if p in ("/api/invite/referrer", "/api/auth/invite/referrer"):
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            c = db()
            try:
                return self._send(200, {"ok": True, "referrer": invites.referrer(c, row["id"])})
            finally:
                c.close()
        if p == "/api/auth/wechat/message-push":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                echo = wechat_vpay.verify_message_url(query)
                return self._send_raw(200, echo)
            except wechat_vpay.MessagePushError:
                return self._send_raw(403, "forbidden")
        if p == "/api/auth/admin/announcements":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = list_announcement_campaigns((q.get("limit") or ["50"])[0])
                return self._send(200, {"ok": True, **data})
            except (TypeError, ValueError):
                return self._send(400, {"detail": "分页参数无效"})
            except Exception:
                return self._send(500, {"detail": "公告列表读取失败"})
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
                    offset=(q.get("offset") or ["0"])[0],
                )
                return self._send(200, {"ok": True, **data})
            except Exception:
                return self._send(500, {"detail": "users query failed"})
        if p == "/api/auth/admin/user-insights":
            if not self._require_internal():
                return
            admin = self._require_admin_user()
            if not admin:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                data = admin_user_insights(
                    (q.get("username") or [""])[0],
                    (q.get("user_id") or [None])[0],
                )
                if not data:
                    return self._send(404, {"detail": "用户不存在"})
                return self._send(200, {"ok": True, **data})
            except Exception:
                return self._send(500, {"detail": "用户详情查询失败"})
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
                    direction=(q.get("direction") or [""])[0],
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
        if p == "/api/auth/recharge/packages":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if not self._require_membership(row):
                return
            tier = membership_for_row(row)["membership_tier"]
            return self._send(200, {"ok": True, **public_recharge_packages(tier)})
        if p == "/api/auth/virtual-pay/packages":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            if not self._require_membership(row):
                return
            try:
                enabled = miniprogram_payments_enabled()
                tier = membership_for_row(row)["membership_tier"]
                return self._send(200, {
                    "ok": True,
                    "enabled": enabled,
                    "configured": enabled and wechat_vpay.is_configured(),
                    "environment": "production" if wechat_vpay.pay_env() == 0 else "sandbox",
                    "membership_tier": tier,
                    "discount_bps": membership_discount_bps(tier),
                    "items": public_virtual_pay_packages(tier) if enabled else [],
                    "custom": public_virtual_pay_custom(tier) if enabled else None,
                })
            except Exception:
                return self._send(500, {"detail": "充值套餐读取失败"})
        if p == "/api/auth/virtual-pay/orders":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                items = list_virtual_pay_orders(row["username"], (q.get("limit") or ["20"])[0])
                return self._send(200, {"ok": True, "items": items})
            except Exception:
                return self._send(500, {"detail": "支付订单查询失败"})
        if p == "/api/auth/notifications":
            row = self._user()
            if not row:
                return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                items = list_user_notifications(
                    row["username"], (q.get("limit") or ["50"])[0],
                )
                return self._send(200, {"ok": True, "items": items})
            except (TypeError, ValueError):
                return self._send(400, {"detail": "分页参数无效"})
            except Exception:
                return self._send(500, {"detail": "通知读取失败"})
        if p == "/api/auth/membership/voice-slot-entitlement":
            if not self._require_internal():
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            username = (q.get("username", [""])[0] or "").strip()
            if not username:
                return self._send(400, {"detail": "missing username"})
            return self._send(200, {
                "ok": True,
                "entitlement": membership_voice_slot_entitlement(username),
            })
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
            user = public_user(
                row["username"], row["display_name"], row["points"], row["role"], row["must_change"], account_id,
                row["membership_tier"], row["membership_started_at"], row["membership_expires_at"],
            )
            user["initial_password"] = initial_password_change_required(row)
            return self._send(200, {
                "user": user,
                "membership_enforcement_enabled": membership_enforcement_enabled(),
            })
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
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                limit_raw = (q.get("limit") or [""])[0]
                limit = None if limit_raw == "" else int(limit_raw)
                offset = int((q.get("offset") or ["0"])[0])
            except Exception:
                return self._send(400, {"detail": "bad_pagination"})
            if offset < 0 or (limit is not None and limit < 1):
                return self._send(400, {"detail": "bad_pagination"})
            if limit is not None:
                limit = min(limit, 100)
            try:
                boards, total, err = list_canvas_boards(row["username"], limit=limit, offset=offset)
                if err:
                    return self._send(400, {"detail": err})
                return self._send(200, {"ok": True, "boards": boards, "total": total})
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
                wait_seconds = int((query.get("wait") or ["0"])[0])
            except Exception:
                return self._send(400, {"detail": "bad_wait"})
            if wait_seconds < 0:
                return self._send(400, {"detail": "bad_wait"})
            wait_seconds = min(wait_seconds, CANVAS_SYNC_MAX_WAIT_SECONDS)
            try:
                # 首次完整同步(含成员校验)。有变化或 wait=0 → 直接返回
                result, err = sync_canvas_ops(row["username"], board_id, since_version)
                if err == "not_found":
                    return self._send(404, {"detail": "协作画布不存在"})
                if err:
                    return self._send(400, {"detail": err})
                changed = (result.get("reset") or result.get("batches")
                           or int(result.get("version") or 0) != since_version)
                if changed or wait_seconds <= 0:
                    return self._send(200, {"ok": True, **result})
                if not _canvas_sync_wait_acquire(row["username"]):
                    # 等待名额已满: 降级为立即返回当前状态, 客户端可继续普通轮询
                    return self._send(200, {"ok": True, **result, "wait_degraded": True})
                try:
                    # 等待阶段只做只读版本探测(不拿写锁); 检测到变化或超时才做完整同步
                    deadline = time.time() + wait_seconds
                    while time.time() < deadline:
                        time.sleep(0.5)
                        probed = canvas_board_version_probe(board_id)
                        if probed is None or probed != since_version:
                            result, err = sync_canvas_ops(row["username"], board_id, since_version)
                            if err == "not_found":
                                return self._send(404, {"detail": "协作画布不存在"})
                            if err:
                                return self._send(400, {"detail": err})
                            return self._send(200, {"ok": True, **result})
                    result, err = sync_canvas_ops(row["username"], board_id, since_version)
                    if err == "not_found":
                        return self._send(404, {"detail": "协作画布不存在"})
                    if err:
                        return self._send(400, {"detail": err})
                    return self._send(200, {"ok": True, **result})
                finally:
                    _canvas_sync_wait_release(row["username"])
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
    threading.Thread(
        target=_virtual_pay_reconcile_loop,
        name="virtual-pay-reconcile",
        daemon=True,
    ).start()
    threading.Thread(
        target=_video_subscription_worker,
        name="video-subscription-outbox",
        daemon=True,
    ).start()
    print("huangque-auth on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
