# -*- coding: utf-8 -*-
"""「客户全貌」（后台 /api/admin/users/detail）在生产 PostgreSQL 下 500 的两处成因。

两处都只在 PG 暴露，SQLite 下完全看不见（按下标取值照样对、SUM 回来就是 int），
所以能过本地与 CI，却在生产 500。这里用 PG 语义的假连接把成因钉死：

1. invites.admin_reward_points 的两个聚合列没起别名：PG 下未命名列都叫 coalesce，
   行对象按键合并后只剩最后一个键，旧代码 sums[1] 直接越界（IndexError）。
2. auth_server.list_points_audit 的 SUM(bigint) 返回 decimal.Decimal，裸
   json.dumps 抛 TypeError，被上层 except 吞成 500。兜底由 auth_server._json_default
   承担，_send 统一带上它。

背景：客户全貌抽屉读的是 auth 服务的 admin_user_insights，其中「邀请奖励点数」
走 admin_reward_points、「点数流水」走 list_points_audit —— 两条链路各自踩了上面一个坑。
"""
import decimal
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "server")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import server.auth_server as auth_server
import server.invites as invites


class _PgRow(object):
    """模拟生产 PG 适配层的行对象。

    既能位置索引也能按键取值；同名（未起别名）的列会被合并，后者覆盖前者 ——
    这正是 sums[1] 越界的成因；sqlite3.Row 的位置索引才不会暴露这个问题。
    """

    def __init__(self, pairs):
        self.merged = {}
        for key, value in pairs:
            self.merged[key] = value
        self.values = list(self.merged.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.values[key]
        return self.merged[key]

    def keys(self):
        return list(self.merged)


class _Cursor(object):
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)


class _PgRewardConn(object):
    """按 PG 语义响应 admin_reward_points 的三条查询。"""

    dialect = "postgres"

    def __init__(self):
        self.sums_sql = None

    def execute(self, sql, args=None):
        if sql.startswith("SELECT COUNT(*)"):
            return _Cursor([_PgRow([("count", 2)])])
        if "reward_points ELSE 0 END" in sql:
            self.sums_sql = sql
            if "AS recorded_points" in sql:
                return _Cursor([_PgRow([("recorded_points", 30), ("voided_points", 5)])])
            # 没起别名：PG 下两个列都叫 coalesce，按键合并后只剩最后一个
            return _Cursor([_PgRow([("coalesce", 30), ("coalesce", 5)])])
        return _Cursor([])  # 明细行


class _DecimalSummaryConn(object):
    """模拟 PG：SUM(bigint) 回来的是 decimal.Decimal。"""

    def execute(self, sql, args=None):
        if "COUNT(*)" in sql:
            return _Cursor([{
                "total": 3,
                "credits": decimal.Decimal("120"),
                "debits": decimal.Decimal("45"),
                "net": decimal.Decimal("75"),
            }])
        return _Cursor([])

    def close(self):
        pass


class RewardPointsPgAggregateTests(unittest.TestCase):
    def test_reward_sums_are_read_by_alias(self):
        conn = _PgRewardConn()
        data = invites.admin_reward_points(conn)
        self.assertIn("AS recorded_points", conn.sums_sql)
        self.assertIn("AS voided_points", conn.sums_sql)
        self.assertEqual(data["recorded_points"], 30)
        self.assertEqual(data["voided_points"], 5)

    def test_pg_duplicate_column_names_collapse(self):
        """反证旧行为：不起别名时行对象只剩一个键，按下标取第二个就越界。"""
        row = _PgRow([("coalesce", 30), ("coalesce", 5)])
        self.assertEqual(row["coalesce"], 5)
        self.assertRaises(IndexError, lambda: row[1])


class PointsAuditDecimalTests(unittest.TestCase):
    def setUp(self):
        self.original_db = auth_server.db

    def tearDown(self):
        auth_server.db = self.original_db

    def test_summary_is_int_and_json_serializable(self):
        auth_server.db = lambda: _DecimalSummaryConn()
        data = auth_server.list_points_audit()
        self.assertEqual(
            data["summary"], {"total": 3, "credits": 120, "debits": 45, "net": 75}
        )
        for key in ("total", "credits", "debits", "net"):
            self.assertIsInstance(data["summary"][key], int, key)
        json.dumps(data)  # 旧代码（summary 里是 Decimal）在这一行抛 TypeError


class JsonDefaultTests(unittest.TestCase):
    def test_decimal_becomes_number(self):
        self.assertEqual(auth_server._json_default(decimal.Decimal("12")), 12)
        self.assertIsInstance(auth_server._json_default(decimal.Decimal("12")), int)
        self.assertEqual(auth_server._json_default(decimal.Decimal("12.5")), 12.5)

    def test_non_decimal_still_raises(self):
        """不做静默兜底：其余不可序列化类型照常抛出，避免把 bug 藏进 JSON。"""
        self.assertRaises(TypeError, auth_server._json_default, object())

    def test_send_payload_serializes_decimal(self):
        body = json.dumps(
            {"points": decimal.Decimal("7")}, default=auth_server._json_default
        )
        self.assertEqual(json.loads(body), {"points": 7})


if __name__ == "__main__":
    unittest.main()
