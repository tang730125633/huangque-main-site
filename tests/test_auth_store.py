"""auth_store 适配层的方言翻译 / 域校验 / 兼容行为单元测试。

不含 PG 段的部分在本地即可运行（纯函数）；需要真实 PG 的用例在
HQ_DATABASE_URL 配置时运行（服务器 staging 演练时跑）。

环境变量依赖：翻译与校验函数每次读 HQ_IDENTITY_STORE / HQ_LEDGER_STORE，
测试用 setUp/tearDown 显式设回默认，避免与其他测试互相污染。
"""

import os
import time
import unittest

import server.content_domains.auth_store as auth_store


def _clear_modes():
    for name in ("HQ_IDENTITY_STORE", "HQ_LEDGER_STORE"):
        os.environ.pop(name, None)


class TranslateTests(unittest.TestCase):
    """SQLite → PG 方言翻译（纯函数，无连接）。"""

    def setUp(self):
        _clear_modes()

    def tearDown(self):
        _clear_modes()

    def t(self, sql, params=()):
        return auth_store._translate(sql, params)

    # -- ? → %s -----------------------------------------------------------

    def test_q_to_percent_s_basic(self):
        sql, params, ret = self.t("SELECT * FROM users WHERE id=?", (5,))
        self.assertEqual(sql, "SELECT * FROM users WHERE id=%s")
        self.assertEqual(params, [5])
        self.assertFalse(ret)

    def test_q_inside_string_literal_untouched(self):
        sql, params, ret = self.t(
            "UPDATE user_notifications SET detail='how? what?' WHERE id=?", (3,))
        self.assertIn("'how? what?'", sql)
        self.assertIn("id=%s", sql)
        self.assertFalse(ret)

    def test_q_inside_escaped_quotes_untouched(self):
        sql, _, _ = self.t("UPDATE users SET display_name='it''s a ? test' WHERE id=?", (1,))
        self.assertIn("'it''s a ? test'", sql)
        self.assertIn("id=%s", sql)

    # -- INSERT OR IGNORE ---------------------------------------------------

    def test_insert_or_ignore_becomes_on_conflict_do_nothing(self):
        sql, params, ret = self.t(
            "INSERT OR IGNORE INTO friendships(username, friend_username, created_at) VALUES(?,?,?)",
            ("a", "b", 1))
        self.assertNotIn("OR IGNORE", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertFalse(ret)

    def test_insert_or_ignore_with_semicolon_suffix_before_semicolon(self):
        sql, _, _ = self.t(
            "INSERT OR IGNORE INTO network_node_ids(user_id,node_id,created_at) VALUES(?,?,?);",
            (1, "n1", 2))
        self.assertTrue(sql.endswith("ON CONFLICT DO NOTHING;"))
        self.assertNotIn("OR IGNORE", sql)

    # -- LIMIT -1 → LIMIT ALL ------------------------------------------------

    def test_limit_minus_one_becomes_limit_all(self):
        sql, _, _ = self.t(
            "DELETE FROM canvas_ops WHERE (board_id, version) IN ("
            "SELECT board_id, version FROM canvas_ops WHERE board_id=?"
            " ORDER BY version DESC LIMIT -1 OFFSET ?)",
            ("b1", 3))
        self.assertIn("LIMIT ALL OFFSET %s", sql)
        self.assertNotIn("LIMIT -1", sql)

    # -- RETURNING id 白名单 -------------------------------------------------

    def test_returning_appended_for_whitelist_values_insert(self):
        sql, params, ret = self.t(
            "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", ())
        self.assertTrue(ret)
        self.assertIn("RETURNING id", sql)

    def test_returning_not_appended_for_non_whitelist(self):
        sql, _, ret = self.t(
            "INSERT INTO canvas_ops(board_id, version, op_id, client_id, username, ops_json, created_at)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s)", ())
        self.assertFalse(ret)
        self.assertNotIn("RETURNING", sql)

    def test_returning_not_appended_for_insert_select(self):
        sql, _, ret = self.t(
            "INSERT INTO user_notifications(username,kind,title,detail,created_by,created_at)"
            " SELECT username,'announcement',%s,%s,%s,%s,%s FROM users WHERE id>%s", ())
        self.assertFalse(ret)
        self.assertNotIn("RETURNING", sql)

    def test_returning_not_appended_when_or_ignore(self):
        sql, _, ret = self.t(
            "INSERT OR IGNORE INTO invite_reward_claims(a,b) VALUES(%s,%s)", ())
        self.assertFalse(ret)
        self.assertNotIn("RETURNING", sql)

    # -- 布尔列 --------------------------------------------------------------

    def test_bool_literal_in_values_become_true_false(self):
        # register_account 的 INSERT：must_change 是 SQL 内字面量 0
        sql, params, _ = self.t(
            "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)"
            " VALUES(?,?,?,?,?,?,0,?)",
            ("u", "h", "s", "d", 16, "member", "acc"))
        self.assertIn(",FALSE,", sql.split("VALUES")[1])
        # 直接断言：VALUES 段第 7 个 token 变成 FALSE
        values_part = sql.split("VALUES")[1].split("ON CONFLICT")[0]
        tokens = auth_store._split_top(values_part[values_part.index("(") + 1:values_part.rindex(")")])
        self.assertEqual(tokens[6].strip(), "FALSE")
        self.assertEqual(params[6], "acc")  # 参数未被误动（该列是 account_id）

    def test_bool_literal_card_initial_password(self):
        # register_miniprogram_card：must_change=0, card_initial_password=1
        sql, _, _ = self.t(
            "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,card_initial_password,account_id)"
            " VALUES(?,?,?,?,?,?,0,1,?)",
            ("p", "h", "s", "p", 0, "member", "acc"))
        values_part = sql.split("VALUES")[1].split("ON CONFLICT")[0]
        tokens = auth_store._split_top(values_part[values_part.index("(") + 1:values_part.rindex(")")])
        self.assertEqual(tokens[6].strip(), "FALSE")
        self.assertEqual(tokens[7].strip(), "TRUE")

    def test_bool_literal_in_on_conflict_set(self):
        # create_user 的 ON CONFLICT DO UPDATE SET ... must_change=1
        sql, _, _ = self.t(
            "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)"
            " VALUES(?,?,?,?,?,?,1,?) ON CONFLICT(username) DO UPDATE SET"
            " pw_hash=excluded.pw_hash, pw_salt=excluded.pw_salt,"
            " points=excluded.points, role=excluded.role, must_change=1",
            ("u", "h", "s", "d", 16, "member", "acc"))
        self.assertIn("must_change=TRUE", sql)

    def test_bool_param_in_values_normalised(self):
        # announcement_campaigns：wechat_push_requested 以 int(wechat_push) 参数传入
        # （auth_server 的真实语句：9 个参数，第 9 个是 wechat_push_requested）
        sql, params, _ = self.t(
            "INSERT INTO announcement_campaigns(title,detail,audience_json,status,recipient_count,"
            "breakdown_json,created_by,request_id,created_at,published_at,wechat_push_requested)"
            " VALUES(?,?,?,'published',0,?,?,?,?,?,?)",
            ["t", "d", "{}", "{}", "admin", "req", 1, 1, 0])
        self.assertEqual(params[8], False)  # wechat_push_requested 0 → False

    def test_bool_param_in_update_set_normalised(self):
        sql, params, _ = self.t(
            "UPDATE users SET display_name=?, must_change=? WHERE id=?", ("n", 0, 7))
        self.assertEqual(params[1], False)

    def test_bool_update_set_literal_true(self):
        sql, _, _ = self.t("UPDATE users SET must_change=1 WHERE id=?", (9,))
        self.assertIn("must_change=TRUE", sql)

    def test_non_bool_column_untouched(self):
        # points=0 的字面量绝不能变成 FALSE（列不在布尔名单）
        sql, params, _ = self.t(
            "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change,account_id)"
            " VALUES(?,?,?,?,0,?,0,?)",
            ("u", "h", "s", "d", "member", "acc"))
        values_part = sql.split("VALUES")[1].split("ON CONFLICT")[0]
        tokens = auth_store._split_top(values_part[values_part.index("(") + 1:values_part.rindex(")")])
        self.assertEqual(tokens[4].strip(), "0")     # points 仍是 0
        self.assertEqual(tokens[6].strip(), "FALSE")  # must_change → FALSE

    def test_virtual_pay_orders_env_never_normalised(self):
        # env 是支付环境枚举（0=正式/1=沙箱），不在布尔名单里
        sql, params, _ = self.t(
            "INSERT INTO virtual_pay_orders(order_id,username,openid,package_id,product_id,"
            "amount_fen,points,env,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,0,'created',?)",
            ["o", "u", "op", "pk", "pr", 100, 1000, 1])
        self.assertIn(",0,'created'", sql)
        self.assertEqual(params[7], 1)  # 参数不归一化

    # -- 其他 ----------------------------------------------------------------

    def test_update_with_arithmetic_untouched(self):
        sql, _, _ = self.t(
            "UPDATE canvas_boards SET name=?, data_json=?, version=version+1, updated_at=? WHERE id=?",
            ("n", "{}", 1, "b"))
        self.assertIn("version=version+1", sql)
        self.assertIn("WHERE id=%s", sql)

    def test_params_list_preserved_when_no_bools(self):
        sql, params, _ = self.t("SELECT * FROM tokens WHERE username=? AND scope=?", ("u", "s"))
        self.assertEqual(params, ["u", "s"])


class StatementKindTests(unittest.TestCase):
    def setUp(self):
        _clear_modes()

    def tearDown(self):
        _clear_modes()

    def test_kinds(self):
        self.assertEqual(auth_store._statement_kind("BEGIN IMMEDIATE"), "begin")
        self.assertEqual(auth_store._statement_kind("  begin"), "begin")
        self.assertEqual(auth_store._statement_kind("COMMIT"), "commit")
        self.assertEqual(auth_store._statement_kind("ROLLBACK"), "rollback")
        self.assertEqual(auth_store._statement_kind("END"), "commit")
        self.assertEqual(auth_store._statement_kind("SELECT 1"), "sql")
        self.assertEqual(auth_store._statement_kind("INSERT INTO users"), "sql")


class ForbiddenTests(unittest.TestCase):
    def setUp(self):
        _clear_modes()

    def tearDown(self):
        _clear_modes()

    def test_pragma_raises(self):
        with self.assertRaises(RuntimeError):
            auth_store._check_forbidden("PRAGMA table_info(users)")

    def test_create_table_raises(self):
        with self.assertRaises(RuntimeError):
            auth_store._check_forbidden("CREATE TABLE x(a int)")

    def test_select_allowed(self):
        auth_store._check_forbidden("SELECT * FROM users")  # 不抛


class DomainGuardTests(unittest.TestCase):
    """双权威防护：开关不一致时命中对方域的表必须抛错。"""

    def setUp(self):
        _clear_modes()

    def tearDown(self):
        _clear_modes()

    def test_both_sqlite_identity_table_raises(self):
        os.environ["HQ_IDENTITY_STORE"] = "sqlite"
        os.environ["HQ_LEDGER_STORE"] = "sqlite"
        with self.assertRaises(RuntimeError):
            auth_store._check_domain("SELECT * FROM users WHERE id=?")

    def test_both_sqlite_ledger_table_raises(self):
        os.environ["HQ_IDENTITY_STORE"] = "sqlite"
        os.environ["HQ_LEDGER_STORE"] = "sqlite"
        with self.assertRaises(RuntimeError):
            auth_store._check_domain("INSERT INTO points_audit(a,b) VALUES(1,2)")

    def test_identity_postgres_ledger_sqlite_ledger_table_raises(self):
        os.environ["HQ_IDENTITY_STORE"] = "postgres"
        os.environ["HQ_LEDGER_STORE"] = "sqlite"
        with self.assertRaises(RuntimeError):
            auth_store._check_domain("UPDATE virtual_pay_orders SET status='paid' WHERE order_id=?")

    def test_identity_postgres_ledger_sqlite_identity_table_ok(self):
        os.environ["HQ_IDENTITY_STORE"] = "postgres"
        os.environ["HQ_LEDGER_STORE"] = "sqlite"
        auth_store._check_domain("SELECT * FROM users WHERE id=?")  # 不抛

    def test_both_postgres_all_ok(self):
        os.environ["HQ_IDENTITY_STORE"] = "postgres"
        os.environ["HQ_LEDGER_STORE"] = "postgres"
        auth_store._check_domain("SELECT * FROM users")
        auth_store._check_domain("SELECT * FROM points_audit")

    def test_table_name_inside_string_literal_ignored(self):
        os.environ["HQ_IDENTITY_STORE"] = "sqlite"
        os.environ["HQ_LEDGER_STORE"] = "sqlite"
        # 语句本身不引用任何域表；'users' 只出现在字符串字面量里，不应触发域校验
        auth_store._check_domain("SELECT 'users points_audit' AS note")

    def test_invalid_env_value_raises(self):
        os.environ["HQ_IDENTITY_STORE"] = "pgx"
        with self.assertRaises(RuntimeError):
            auth_store.identity_mode()


class RowCompatTests(unittest.TestCase):
    def setUp(self):
        _clear_modes()

    def tearDown(self):
        _clear_modes()

    def test_indexable_row(self):
        row = auth_store.IndexableRow({"a": 1, "b": 2})
        self.assertEqual(row["a"], 1)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 2)
        self.assertEqual(list(row.keys()), ["a", "b"])
        self.assertEqual(dict(row), {"a": 1, "b": 2})
        self.assertIn("a", row)


class AnyPostgresTests(unittest.TestCase):
    def setUp(self):
        _clear_modes()

    def tearDown(self):
        _clear_modes()

    def test_default_is_false(self):
        self.assertFalse(auth_store.any_postgres())

    def test_identity_postgres_is_true(self):
        os.environ["HQ_IDENTITY_STORE"] = "postgres"
        self.assertTrue(auth_store.any_postgres())

    def test_ledger_postgres_is_true(self):
        os.environ["HQ_LEDGER_STORE"] = "postgres"
        self.assertTrue(auth_store.any_postgres())


@unittest.skipUnless(
    os.environ.get("HQ_DATABASE_URL"),
    "HQ_DATABASE_URL not set (run on the server staging PG)",
)
class PostgresIntegrationTests(unittest.TestCase):
    """真实 PG 上的端到端行为（staging 演练时跑）。"""

    def setUp(self):
        self._old_env = {
            k: os.environ.get(k)
            for k in ("HQ_IDENTITY_STORE", "HQ_LEDGER_STORE", "HQ_DATABASE_URL")
        }
        os.environ["HQ_IDENTITY_STORE"] = "postgres"
        os.environ["HQ_LEDGER_STORE"] = "postgres"
        self.suffix = "_pgtest_%d" % int(time.time() * 1000)
        self.conn = auth_store.connect()

    def tearDown(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        auth_store.close_pool()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_crud_roundtrip(self):
        c = self.conn
        c.execute("INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change)"
                  " VALUES(%s,%s,%s,%s,0,'member',0)", ("u" + self.suffix, "h", "s", "d"))
        c.commit()
        row = c.execute("SELECT * FROM users WHERE username=%s",
                        ("u" + self.suffix,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["username"], "u" + self.suffix)
        self.assertEqual(row[0], row["id"])  # 数字下标可用
        c.execute("UPDATE users SET points=5 WHERE username=%s", ("u" + self.suffix,))
        c.commit()
        row = c.execute("SELECT points FROM users WHERE username=%s",
                        ("u" + self.suffix,)).fetchone()
        self.assertEqual(row["points"], 5)
        c.execute("DELETE FROM users WHERE username=%s", ("u" + self.suffix,))
        c.commit()

    def test_lastrowid_from_returning(self):
        c = self.conn
        cur = c.execute("INSERT INTO users(username,pw_hash,pw_salt,must_change)"
                        " VALUES(%s,%s,%s,1)", ("u" + self.suffix, "h", "s"))
        user_id = cur.lastrowid
        self.assertIsInstance(user_id, int)
        c.rollback()

    def test_insert_or_ignore_semantics(self):
        c = self.conn
        c.execute("INSERT INTO friendships(username,friend_username,created_at) VALUES(%s,%s,1)",
                  ("a" + self.suffix, "b" + self.suffix))
        c.commit()
        c.execute("INSERT OR IGNORE INTO friendships(username,friend_username,created_at) VALUES(%s,%s,2)",
                  ("a" + self.suffix, "b" + self.suffix))
        c.commit()
        n = c.execute("SELECT COUNT(*) AS n FROM friendships WHERE username=%s",
                      ("a" + self.suffix,)).fetchone()["n"]
        self.assertEqual(int(n), 1)
        c.execute("DELETE FROM friendships WHERE username=%s", ("a" + self.suffix,))
        c.commit()

    def test_bool_normalised_write_and_read(self):
        c = self.conn
        cur = c.execute("INSERT INTO users(username,pw_hash,pw_salt,must_change)"
                        " VALUES(%s,%s,%s,1)", ("u" + self.suffix, "h", "s"))
        uid = cur.lastrowid
        c.commit()
        row = c.execute("SELECT must_change FROM users WHERE id=%s", (uid,)).fetchone()
        self.assertIs(row["must_change"], True)
        c.execute("UPDATE users SET must_change=0 WHERE id=%s", (uid,))
        c.commit()
        row = c.execute("SELECT must_change FROM users WHERE id=%s", (uid,)).fetchone()
        self.assertIs(row["must_change"], False)
        c.execute("DELETE FROM users WHERE id=%s", (uid,))
        c.commit()

    def test_begin_commit_rollback(self):
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO users(username,pw_hash,pw_salt,must_change) VALUES(%s,%s,%s,0)",
                  ("u" + self.suffix, "h", "s"))
        c.rollback()
        row = c.execute("SELECT 1 AS one FROM users WHERE username=%s",
                        ("u" + self.suffix,)).fetchone()
        self.assertIsNone(row)

    def test_duplicate_username_raises_integrity_error(self):
        c = self.conn
        c.execute("INSERT INTO users(username,pw_hash,pw_salt,must_change) VALUES(%s,%s,%s,0)",
                  ("u" + self.suffix, "h", "s"))
        c.commit()
        with self.assertRaises(auth_store.IntegrityError):
            c.execute("INSERT INTO users(username,pw_hash,pw_salt,must_change) VALUES(%s,%s,%s,0)",
                      ("u" + self.suffix, "h2", "s2"))
        c.rollback()
        c.execute("DELETE FROM users WHERE username=%s", ("u" + self.suffix,))
        c.commit()

    def test_pragma_rejected(self):
        with self.assertRaises(RuntimeError):
            self.conn.execute("PRAGMA table_info(users)")

    def test_limit_minus_one_translated(self):
        c = self.conn
        rows = c.execute(
            "SELECT username FROM users ORDER BY id DESC LIMIT -1 OFFSET 0").fetchall()
        self.assertIsInstance(rows, list)

    def test_ledger_table_cross_domain_allowed_when_both_postgres(self):
        self.conn.execute("SELECT COUNT(*) AS n FROM points_audit").fetchone()


if __name__ == "__main__":
    unittest.main()
