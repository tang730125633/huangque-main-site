# auth-service identity 域迁移测试：SQLite 可跑部分（回填器纯函数、只读、迁移结构）
# + PostgreSQL 部分（HQ_DATABASE_URL 存在时才跑；本机无 PG 时自动跳过）。
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIGRATION_PATH = ROOT / "server/db/migrations/versions/20260916_0011_auth_identity.py"
BACKFILL_PATH = ROOT / "scripts/migrate_auth_identity.py"

SPEC = importlib.util.spec_from_file_location("migrate_auth_identity", BACKFILL_PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

PG_URL = os.environ.get("HQ_DATABASE_URL")

LEDGER_TABLES = (
    "points_audit", "point_transfers", "recharge_orders", "virtual_pay_orders",
    "membership_recharge_records", "membership_upgrade_records",
    "invite_reward_point_records", "sqlite_sequence",
)

# 源库真实 DDL 片段（与 /home/ubuntu/auth-service/users.db 的 .schema 一致）
SOURCE_DDL = {
    "users": """CREATE TABLE users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        pw_salt TEXT NOT NULL,
        display_name TEXT,
        points INTEGER DEFAULT 0,
        role TEXT DEFAULT 'member',
        must_change INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    , account_id TEXT, wx_openid TEXT, account_status TEXT NOT NULL DEFAULT 'active',
      membership_tier TEXT NOT NULL DEFAULT '', membership_started_at INTEGER,
      membership_expires_at INTEGER, card_initial_password INTEGER NOT NULL DEFAULT 0)""",
    "cli_device_grants": """CREATE TABLE cli_device_grants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_code_hash TEXT NOT NULL UNIQUE,
        user_code_hash TEXT NOT NULL UNIQUE,
        client_name TEXT NOT NULL,
        requested_scopes_json TEXT NOT NULL,
        approved_scopes_json TEXT,
        username TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        approved_at INTEGER,
        last_poll_at INTEGER NOT NULL DEFAULT 0,
        token_hash TEXT UNIQUE,
        token_expires_at INTEGER,
        revoked_at INTEGER
    )""",
    "business_cards": """CREATE TABLE business_cards(
        user_id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL DEFAULT '', headline TEXT NOT NULL DEFAULT '',
        company TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]', works_json TEXT NOT NULL DEFAULT '[]',
        links_json TEXT NOT NULL DEFAULT '[]',
        avatar_key TEXT NOT NULL DEFAULT '', wechat_qr_key TEXT NOT NULL DEFAULT '',
        phone_public INTEGER NOT NULL DEFAULT 0, email_public INTEGER NOT NULL DEFAULT 0,
        address_public INTEGER NOT NULL DEFAULT 0, wechat_qr_public INTEGER NOT NULL DEFAULT 0,
        discoverable_in_network INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'draft', created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, published_at INTEGER
    , miniprogram_openid TEXT)""",
}
SUBSET = tuple(t for t in migration.TABLES if t.source in SOURCE_DDL)
USER_COLUMNS = [t for t in migration.TABLES if t.source == "users"][0].columns


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_tables(path, definitions):
    conn = _connect(path)
    try:
        for ddl in definitions.values():
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def _full_fixture(path, rows_per_table=1):
    """按回填清单建全 26 张表，每表写 rows_per_table 行（列名即清单列名）。"""
    definitions, inserts = {}, {}
    for table in migration.TABLES:
        definitions[table.source] = "CREATE TABLE %s(%s)" % (
            table.source,
            ", ".join("%s INTEGER" % column if column == table.columns[0] else column
                      for column in table.columns))
        values = []
        for column in table.columns:
            if column in table.key or column.endswith("_id") or column in (
                    "points", "version", "created_at", "updated_at"):
                values.append(1)
            else:
                values.append("x")
        inserts[table.source] = values
    _create_tables(path, definitions)
    conn = _connect(path)
    try:
        for table in migration.TABLES:
            for _ in range(rows_per_table):
                conn.execute(
                    "INSERT INTO %s(%s) VALUES (%s)"
                    % (table.source, ",".join(table.columns),
                       ",".join("?" for _ in table.columns)),
                    inserts[table.source],
                )
        conn.commit()
    finally:
        conn.close()


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_migration():
    """用 AST 解析迁移文件（本机可能没装 alembic/sqlalchemy，不能 import 它）。"""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    tables, indexes = {}, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = getattr(node.func, "attr", None)
        if func not in ("create_table", "create_index") or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        if func == "create_table":
            columns = [
                arg.args[0].value for arg in node.args[1:]
                if isinstance(arg, ast.Call)
                and getattr(arg.func, "attr", None) == "Column"
                and arg.args and isinstance(arg.args[0], ast.Constant)
            ]
            tables[first.value] = columns
        else:
            indexes.append(first.value)
    return source, tree, tables, indexes


class PureFunctionTest(unittest.TestCase):
    """回填器里不依赖数据库的那部分：键、校验和、布尔归一、护栏、敏感列。"""

    def test_row_key_single_and_composite(self):
        users = [t for t in migration.TABLES if t.source == "users"][0]
        grants = [t for t in migration.TABLES
                  if t.source == "wechat_subscription_grants"][0]
        actions = [t for t in migration.TABLES
                   if t.source == "cli_action_requests"][0]
        self.assertEqual(migration.row_key(users, {"id": 7, "username": "tang"}), "7")
        self.assertEqual(
            migration.row_key(actions, {"username": "tang1", "action": "ip12-message",
                                        "request_id": "m0replay-01"}),
            "tang1/ip12-message/m0replay-01")
        self.assertEqual(
            migration.row_key(grants, {"username": "u", "event_type": "work_complete",
                                       "template_id": "T"}),
            "u/work_complete/T")

    def test_canonical_checksum_is_stable_and_order_free(self):
        first = migration.canonical_checksum({"a": {"x": 1}, "b": {"y": "中文"}})
        second = migration.canonical_checksum({"b": {"y": "中文"}, "a": {"x": 1}})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        # 规范化口径：sort_keys + 紧凑分隔符 + default=str（与 M3A 回填器一致）
        expected = hashlib.sha256(
            json.dumps({"a": {"x": 1}, "b": {"y": "中文"}}, ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"), default=str
                       ).encode("utf-8")).hexdigest()
        self.assertEqual(first, expected)
        self.assertNotEqual(first, migration.canonical_checksum(
            {"a": {"x": 2}, "b": {"y": "中文"}}))

    def test_boolean_columns_are_normalised_and_none_stays_none(self):
        for table_name, columns in migration.BOOLEAN_COLUMNS.items():
            for column in columns:
                self.assertIs(migration._normalize(table_name, column, 1), True)
                self.assertIs(migration._normalize(table_name, column, 0), False)
                self.assertIsNone(migration._normalize(table_name, column, None))
                # 源(0/1) 与目标(true/false) 的比对口径必须一致
                self.assertIs(
                    migration._comparable(table_name, column, 1),
                    migration._comparable(table_name, column, True))
                self.assertIs(
                    migration._comparable(table_name, column, 0),
                    migration._comparable(table_name, column, False))
        # 非布尔列不改造：0 仍然是整数 0，不会被当成 False
        self.assertEqual(migration._normalize("users", "points", 0), 0)
        self.assertEqual(migration._comparable("users", "points", 0), "0")

    def test_boolean_columns_exist_in_the_declared_tables(self):
        for table in migration.TABLES:
            declared = migration.BOOLEAN_COLUMNS.get(table.source, frozenset())
            self.assertTrue(declared <= set(table.columns),
                            "%s 声明了不存在的布尔列" % table.source)

    def test_checksum_row_normalises_source_and_target_identically(self):
        columns = list(USER_COLUMNS)
        source_row = {column: 0 for column in columns}
        source_row["id"] = 1
        source_row["must_change"] = 1
        target_row = dict(source_row, must_change=True)
        self.assertEqual(
            migration._checksum_row("users", columns, source_row),
            migration._checksum_row("users", columns, target_row))

    def test_conflict_guard_only_fires_when_target_is_newer(self):
        self.assertTrue(migration.updated_at_conflict(1700000001, 1700000000))
        self.assertFalse(migration.updated_at_conflict(1700000000, 1700000000))
        self.assertFalse(migration.updated_at_conflict(1699999999, 1700000000))
        self.assertFalse(migration.updated_at_conflict(None, 1700000000))
        self.assertTrue(migration.updated_at_conflict(1, None))

    def test_sensitive_columns_are_never_echoed(self):
        for column in ("pw_hash", "pw_salt", "token", "token_hash",
                       "device_code_hash", "user_code_hash", "ip_hash",
                       "device_hash", "openid", "wx_openid", "miniprogram_openid",
                       "phone", "email"):
            self.assertIn(column, migration.SENSITIVE_COLUMNS)
            self.assertEqual(migration._safe(column, "supersecret"),
                             "<redacted 11 chars>")
        self.assertEqual(migration._safe("username", "tang"), "tang")
        self.assertIsNone(migration._safe("pw_hash", None))

    def test_sequence_target_covers_ids_and_sqlite_sequence(self):
        users = [t for t in migration.TABLES if t.source == "users"][0]
        entry = {"rows": {"1": {"id": 1}, "9": {"id": 9}}, "identity_seq": 12}
        self.assertEqual(migration.sequence_target(users, entry), 13)
        # 源序列落后于最大 id 时以最大 id 为准（绝不回退到会撞主键的值）
        entry = {"rows": {"1": {"id": 1}, "40": {"id": 40}}, "identity_seq": 12}
        self.assertEqual(migration.sequence_target(users, entry), 41)
        entry = {"rows": {}, "identity_seq": None}
        self.assertEqual(migration.sequence_target(users, entry), 1)

    def test_scope_is_exactly_26_tables_and_excludes_ledger(self):
        self.assertEqual(len(migration.TABLES), 26)
        sources = {table.source for table in migration.TABLES}
        self.assertEqual(len(sources), 26)
        self.assertEqual(sources & set(LEDGER_TABLES), set())
        self.assertEqual(migration.DOMAIN, "identity")
        # 自增表才需要对齐序列：源 sqlite_sequence 里有记账的 15 张
        identity = {table.source for table in migration.TABLES if table.identity}
        self.assertEqual(len(identity), 15)
        self.assertNotIn("business_cards", identity)     # user_id 是 users 的主键
        self.assertNotIn("canvas_boards", identity)      # TEXT 主键

    def test_source_columns_are_all_declared(self):
        for table in migration.TABLES:
            self.assertTrue(table.key, table.source)
            self.assertTrue(set(table.key) <= set(table.columns), table.source)


class ReadSourceTest(unittest.TestCase):
    """源读取：只读、值原样、列清单漂移即停。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hq-identity-")
        self.db = Path(self.tmp.name) / "users.db"
        _create_tables(self.db, SOURCE_DDL)
        conn = _connect(self.db)
        try:
            conn.execute(
                "INSERT INTO users(id, username, pw_hash, pw_salt, display_name, "
                "points, role, must_change, created_at, account_id, wx_openid, "
                "account_status, membership_tier, membership_started_at, "
                "membership_expires_at, card_initial_password) "
                "VALUES(1,'tang','h','s','tang',1801,'member',0,"
                "'2026-06-25 16:54:52','HQ53J2AQ','openid-1','active','experience',"
                "1784915923,1816451923,0)")
            conn.execute(
                "INSERT INTO cli_device_grants(id, device_code_hash, user_code_hash, "
                "client_name, requested_scopes_json, status, created_at, expires_at, "
                "last_poll_at) VALUES(1,'d','u','HQ CLI live smoke','[]','pending',"
                "1785396829,1785397429,0)")
            conn.execute(
                "INSERT INTO business_cards(user_id, public_id, name, headline, company, "
                "bio, phone, email, address, tags_json, works_json, links_json, "
                "avatar_key, wechat_qr_key, phone_public, email_public, address_public, "
                "wechat_qr_public, discoverable_in_network, status, created_at, "
                "updated_at, published_at, miniprogram_openid) "
                "VALUES(3,'JIlMUHKxDOBk','fang','主理人','黄雀','','16670402196','','',"
                "'[]','[]','[]','cards/avatar/x.jpg','',0,0,0,0,1,'published',"
                "1785589959,1785908435,1785590011,NULL)")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_source_is_read_only_and_keeps_source_conventions(self):
        before = _sha256(self.db)
        data = migration.read_source(self.db, SUBSET)
        self.assertEqual(before, _sha256(self.db))       # 只读打开，源库逐字节不变
        self.assertEqual(sorted(data), sorted(SOURCE_DDL))
        users = data["users"]["rows"]["1"]
        self.assertEqual(users["points"], 1801)              # 余额快照原样照搬
        self.assertEqual(users["must_change"], 0)            # SQLite 侧仍是整数
        self.assertEqual(users["created_at"], "2026-06-25 16:54:52")   # TEXT 口径
        self.assertEqual(users["membership_started_at"], 1784915923)   # epoch 口径
        self.assertEqual(data["users"]["identity_seq"], 1)
        self.assertEqual(
            data["business_cards"]["rows"]["3"]["discoverable_in_network"], 1)
        self.assertIsNone(data["business_cards"]["rows"]["3"]["miniprogram_openid"])

    def test_read_source_rejects_a_source_that_gained_a_column(self):
        conn = _connect(self.db)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN sneaky TEXT")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(RuntimeError, "列集合与回填清单不一致"):
            migration.read_source(self.db, SUBSET)

    def test_missing_source_file_raises_before_touching_anything(self):
        with self.assertRaisesRegex(RuntimeError, "SQLite 源不存在"):
            migration.read_source(Path(self.tmp.name) / "nope.db", SUBSET)

    def test_summary_reports_counts_and_checksum_without_touching_pg(self):
        with mock.patch.object(migration.postgres, "transaction",
                               side_effect=AssertionError("dry-run 不得开 PG 事务")):
            data = migration.read_source(self.db, SUBSET)
            report = migration.summary(data, SUBSET)
        self.assertEqual(report["users"], 1)
        self.assertEqual(len(report["source_checksum"]), 64)


class CliContractTest(unittest.TestCase):
    """dry-run 默认绝不写；--apply 必须带 --code-sha；空源拒绝导入。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hq-identity-cli-")
        self.db = Path(self.tmp.name) / "users.db"
        self.no_write = mock.patch.object(
            migration.postgres, "transaction",
            side_effect=AssertionError("dry-run 不得开 PG 事务"))
        self.no_write.start()

    def tearDown(self):
        self.no_write.stop()
        self.tmp.cleanup()

    def _run(self, argv):
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["migrate_auth_identity.py"] + argv):
            with contextlib.redirect_stdout(stdout):
                code = migration.main()
        return code, stdout.getvalue()

    def test_dry_run_is_the_default_and_writes_nothing(self):
        _full_fixture(self.db)
        before = _sha256(self.db)
        code, output = self._run(["--source", str(self.db)])
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report["mode"], "dry-run")
        for table in migration.TABLES:
            self.assertEqual(report[table.source], 1, table.source)
        self.assertEqual(len(report["source_checksum"]), 64)
        self.assertEqual(before, _sha256(self.db))

    def test_apply_without_code_sha_is_refused(self):
        _full_fixture(self.db)
        with self.assertRaisesRegex(RuntimeError, "必须带 --code-sha"):
            self._run(["--source", str(self.db), "--apply"])

    def test_apply_with_short_code_sha_is_refused(self):
        _full_fixture(self.db)
        with self.assertRaisesRegex(RuntimeError, "必须带 --code-sha"):
            self._run(["--source", str(self.db), "--apply", "--code-sha", "abc"])

    def test_empty_source_is_refused(self):
        _full_fixture(self.db, rows_per_table=0)
        data = migration.read_source(self.db)
        with self.assertRaisesRegex(RuntimeError, "没有任何行"):
            migration.apply(data, self.db, "abcdef1")


class MigrationFileTest(unittest.TestCase):
    """迁移文件结构：单链、每表注释、禁止破坏性降级、列与回填清单一致。"""

    @classmethod
    def setUpClass(cls):
        cls.source, cls.tree, cls.tables, cls.indexes = _parse_migration()

    def test_revision_chain_is_single_head(self):
        assigned = {}
        for node in cls_assignments(self.tree):
            assigned.update(node)
        self.assertEqual(assigned["revision"], "20260916_0011")
        self.assertEqual(assigned["down_revision"], "20260915_0010")
        others = []
        for path in sorted(MIGRATION_PATH.parent.glob("*.py")):
            if path.name == MIGRATION_PATH.name:
                continue
            for assignment in cls_assignments(ast.parse(path.read_text(encoding="utf-8"))):
                if assignment.get("down_revision") == "20260915_0010":
                    others.append(path.name)
        self.assertEqual(others, [], "多个迁移声明了同一个 down_revision：%s" % others)

    def test_downgrade_is_disabled(self):
        func = [n for n in self.tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "downgrade"][0]
        self.assertIsInstance(func.body[0], ast.Raise)
        self.assertIn("downgrade is not supported", self.source)
        self.assertNotIn("DROP TABLE", self.source.upper())
        self.assertNotIn("op.drop", self.source)

    def test_all_26_tables_are_created_in_the_identity_schema(self):
        expected = {table.target for table in migration.TABLES}
        self.assertEqual(set(self.tables), expected)
        self.assertEqual(len(self.tables), 26)
        self.assertEqual(set(_comment_tables(self.tree)), expected)   # 每表一条注释
        self.assertEqual(self.source.count("COMMENT ON TABLE"), 1)    # 循环统一发出
        self.assertEqual(set(re.findall(r'schema="([a-z]+)"', self.source)), {"identity"})
        self.assertNotIn("import sqlite3", self.source)

    def test_migration_columns_match_the_backfill_column_lists(self):
        for table in migration.TABLES:
            self.assertEqual(self.tables[table.target], list(table.columns),
                             "%s 的迁移列与回填列不一致" % table.target)

    def test_every_source_index_is_reproduced(self):
        expected = {
            "idx_users_account_id", "idx_users_wx_openid", "idx_cli_grants_user",
            "idx_cli_refresh_grant", "idx_cli_action_active",
            "idx_membership_audit_user", "idx_friendships_user",
            "idx_friend_requests_from", "idx_friend_requests_to",
            "idx_invite_codes_active_user", "idx_invite_codes_lookup",
            "idx_user_invites_bound_at", "idx_user_invites_campaign_status",
            "idx_user_invites_device_time", "idx_user_invites_inviter_time",
            "idx_user_invites_ip_time", "idx_invite_claims_expiry",
            "idx_invite_claims_owner_status", "idx_invite_reward_notices_user",
            "idx_canvas_boards_owner", "idx_canvas_members_user",
            "idx_canvas_ops_board_version", "idx_canvas_presence_board_seen",
            "idx_cards_miniprogram_openid", "idx_cards_network", "idx_cards_public",
            "idx_card_referral_anonymous_time", "idx_card_referral_expiry",
            "idx_card_referral_inviter_time", "idx_card_referral_registered_time",
            "idx_card_referral_registered_user", "idx_user_notifications_user",
            "idx_user_notifications_campaign_user",
            "idx_announcement_campaigns_created", "idx_wechat_sub_ready",
        }
        created = set(self.indexes) | set(
            re.findall(r"CREATE (?:UNIQUE )?INDEX ([a-z_]+)", self.source))
        self.assertEqual(sorted(expected - created), [])
        # 部分唯一索引必须带谓词，否则语义与源不同（NULL 的处理会变）
        for name in ("idx_users_wx_openid", "idx_cards_miniprogram_openid",
                     "idx_invite_codes_active_user", "idx_user_notifications_campaign_user",
                     "idx_card_referral_registered_user"):
            self.assertRegex(
                self.source,
                r"INDEX %s[\s\S]{0,200}?WHERE " % name)
        # 源库 15 张自增表用 Identity，别退回 BIGSERIAL + 手工序列
        self.assertEqual(self.source.count("sa.Identity(always=False)"), 15)


def _comment_tables(tree):
    """取出迁移文件里 ``comments`` 字典的键（COMMENT ON TABLE 是循环发出的）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "comments":
                    return [key.value for key in node.value.keys]
    return []


def cls_assignments(tree):
    """取出模块顶层的 revision / down_revision 赋值。"""
    result = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("revision", "down_revision")):
            result.append({node.targets[0].id: node.value.value})
    return result


class PostgresIntegrationTest(unittest.TestCase):
    """PG 实跑（staging / CI 专用）：本机无 HQ_DATABASE_URL 时整类跳过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hq-identity-pg-")
        self.db = Path(self.tmp.name) / "users.db"
        _create_tables(self.db, SOURCE_DDL)
        conn = _connect(self.db)
        try:
            conn.execute(
                "INSERT INTO users(id, username, pw_hash, pw_salt, display_name, "
                "points, role, must_change, created_at, account_id, wx_openid, "
                "account_status, membership_tier, membership_started_at, "
                "membership_expires_at, card_initial_password) "
                "VALUES(4242,'ci-identity-user','h','s','ci',16,'member',1,"
                "'2026-09-16 00:00:00','HQCI0001',NULL,'active','',NULL,NULL,0)")
            conn.execute(
                "INSERT INTO cli_device_grants(id, device_code_hash, user_code_hash, "
                "client_name, requested_scopes_json, status, created_at, expires_at, "
                "last_poll_at) VALUES(4242,'ci-d','ci-u','ci','[]','pending',"
                "1789000000,1789000600,0)")
            conn.execute(
                "INSERT INTO business_cards(user_id, public_id, created_at, updated_at) "
                "VALUES(4242,'ci-public-id',1789000000,1789000000)")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        migration.postgres.close_pool()
        self.tmp.cleanup()

    @unittest.skipUnless(PG_URL, "PostgreSQL integration URL not configured")
    def test_apply_is_idempotent_verified_and_advances_sequences(self):
        data = migration.read_source(self.db, SUBSET)
        first = migration.apply(data, self.db, "ci-test-sha", SUBSET)
        self.assertEqual(first["imported"], 3)
        second = migration.apply(data, self.db, "ci-test-sha", SUBSET)
        self.assertEqual(second["imported"], 3)      # 重跑无差异，只多一条 run 记录
        with migration.postgres.connection() as conn:
            user = conn.execute(
                "SELECT username, points, must_change, created_at, membership_tier "
                "FROM identity.users WHERE id = 4242").fetchone()
            self.assertEqual(user["username"], "ci-identity-user")
            self.assertEqual(user["points"], 16)
            self.assertIs(user["must_change"], True)   # 0/1 已归一为真布尔
            self.assertEqual(user["created_at"], "2026-09-16 00:00:00")
            card = conn.execute(
                "SELECT discoverable_in_network FROM identity.business_cards "
                "WHERE user_id = 4242").fetchone()
            self.assertIs(card["discoverable_in_network"], True)
            runs = conn.execute(
                "SELECT count(*) AS n FROM ops.data_migration_runs "
                "WHERE domain = 'identity' AND state = 'verified'").fetchone()
            self.assertGreaterEqual(runs["n"], 2)
            items = conn.execute(
                "SELECT count(*) AS n FROM ops.data_migration_items i "
                "JOIN ops.data_migration_runs r USING (run_id) "
                "WHERE r.run_id = %s AND i.state = 'verified'",
                (second["run_id"],)).fetchone()
            self.assertEqual(items["n"], 3)
            sequence = conn.execute(
                "SELECT last_value, is_called FROM identity.users_id_seq").fetchone()
            self.assertEqual(sequence["last_value"], 4243)
            self.assertIs(sequence["is_called"], False)


if __name__ == "__main__":
    unittest.main()
