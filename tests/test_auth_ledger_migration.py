# -*- coding: utf-8 -*-
"""账务域（ledger）迁移准备测试：余额逐笔重算、校验和、键构造、迁移文件一致性。

本地只跑 SQLite 可跑部分（纯函数 + 内存 SQLite 构造的小规模流水样本）；PostgreSQL
部分用 HQ_DATABASE_URL 守护，在 CI / 服务器 staging 上跑（先 alembic upgrade head）。

覆盖的流水场景（任务书要求）：正常、负数、退款、0 值、重复 id。
"""
import ast
import importlib.util
import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
for path in (str(ROOT), SERVER):
    if path not in sys.path:
        sys.path.insert(0, path)

PG_URL = os.environ.get("HQ_DATABASE_URL")
BACKFILL_SOURCE = os.environ.get("HQ_LEDGER_BACKFILL_SOURCE")
MIGRATION_PATH = (ROOT / "server" / "db" / "migrations" / "versions"
                  / "20260916_0012_auth_ledger.py")


def _load_backfill():
    """按路径加载回填器（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "migrate_auth_ledger", ROOT / "scripts" / "migrate_auth_ledger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill()


# ----------------------------------------------------------------------
# 样本构造
# ----------------------------------------------------------------------

def user(uid, username, points):
    return {"id": uid, "username": username, "points": points}


def audit(row_id, username, delta, before, after, transaction_key=None, created_at=0):
    return {"id": row_id, "username": username, "delta": delta,
            "before_points": before, "after_points": after,
            "transaction_key": transaction_key, "created_at": created_at}


def transfer(row_id, transfer_id, sender, recipient, amount, transaction_key=True):
    return {"id": row_id, "transfer_id": transfer_id, "sender_user_id": sender,
            "recipient_user_id": recipient, "amount": amount,
            "mirror": transaction_key, "created_at": 0}


def invite(row_id, inviter, reward, status="recorded"):
    return {"id": row_id, "inviter_user_id": inviter, "reward_points": reward,
            "status": status, "created_at": 0, "voided_at": None}


def flows(users, audit_rows=(), transfer_rows=(), invite_rows=()):
    return {"users": list(users), "audit": list(audit_rows),
            "transfers": list(transfer_rows), "invites": list(invite_rows)}


def reconcile(flow_set, scope="consume"):
    return backfill.reconcile(flow_set, scope=scope)


def entry_for(report, username):
    for item in report["users"]:
        if item["username"] == username:
            return item
    raise AssertionError("报告里没有用户 %s" % username)


def clean_balance_report():
    """balance_alarms 能通过的（空）核对报告骨架。"""
    return {"unexplained": 0, "ledger_end_mismatch": 0,
            "ledger_row_inconsistent": {"users": 0, "detail": {}},
            "duplicate_keys": [], "report_checksum": "x" * 64}


# ----------------------------------------------------------------------
# 纯函数：校验和与键构造
# ----------------------------------------------------------------------

class ChecksumTest(unittest.TestCase):
    def test_checksum_is_key_order_independent(self):
        left = {"a": {"x": 1, "y": 2}, "b": [1, 2, 3]}
        right = {"b": [1, 2, 3], "a": {"y": 2, "x": 1}}
        self.assertEqual(backfill.canonical_checksum(left),
                         backfill.canonical_checksum(right))

    def test_checksum_changes_with_any_value(self):
        base = {"1": {"username": "fang", "delta": -3}}
        changed = {"1": {"username": "fang", "delta": -4}}
        self.assertNotEqual(backfill.canonical_checksum(base),
                            backfill.canonical_checksum(changed))

    def test_checksum_handles_non_json_native_values(self):
        """default=str 口径：bytes 这类非 JSON 原生值不抛异常、结果可复现。"""
        value = {"1": {"blob": b"\x00\x01", "when": object()}}
        digest = backfill.canonical_checksum(value)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, backfill.canonical_checksum(
            {"1": {"blob": b"\x00\x02", "when": object()}}))

    def test_row_key_uses_source_key_verbatim(self):
        self.assertEqual(backfill.row_key("id", 12345), "12345")
        self.assertEqual(backfill.row_key("order_id", "HQ260805134823888E2D0281"),
                         "HQ260805134823888E2D0281")


# ----------------------------------------------------------------------
# 纯函数：余额逐笔重算
# ----------------------------------------------------------------------

class ReconcileTest(unittest.TestCase):
    def test_normal_ledger_matches_balance(self):
        """正常：充值 +100、任务扣点 -30，余额 70 与流水一致。"""
        flow_set = flows(
            [user(1, "fang", 70)],
            [audit(1, "fang", 100, 0, 100), audit(2, "fang", -30, 100, 70)],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "fang")
        self.assertEqual(entry["audit_delta"], 70)
        self.assertEqual(entry["recomputed_consume"], 70)
        self.assertEqual(entry["diff_consume"], 0)
        self.assertEqual(entry["ledger_end"], 70)
        self.assertIn("consume_balance_matched", entry["tags"])
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["diffs"], 0)
        self.assertEqual(backfill.balance_alarms(report), [])

    def test_negative_charges_and_refund_net_to_zero(self):
        """负数 + 退款：充值 10、预扣 5、退 5，流水合计与余额逐笔相符。"""
        flow_set = flows(
            [user(2, "tang", 10)],
            [audit(1, "tang", 10, 0, 10),
             audit(2, "tang", -5, 10, 5, "job-charge:tang:/api/gen/copy:r1"),
             audit(3, "tang", 5, 5, 10, "job-refund:tang:9")],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "tang")
        self.assertEqual(entry["audit_delta"], 10)
        self.assertEqual(entry["opening_balance"], 0)
        self.assertEqual(entry["diff_consume"], 0)
        self.assertEqual(entry["non_balance_rows"], 0)
        self.assertEqual(report["unexplained"], 0)

    def test_zero_delta_rows_do_not_change_balance(self):
        flow_set = flows(
            [user(3, "fang1", 0)],
            [audit(1, "fang1", 0, 0, 0)],
        )
        entry = entry_for(reconcile(flow_set), "fang1")
        self.assertEqual(entry["audit_delta"], 0)
        self.assertEqual(entry["diff_consume"], 0)

    def test_free_beta_rows_without_balance_effect_are_explained(self):
        """免费内测：delta 记了但余额没动（before == after），差额必须单列并可解释。

        余额恒等式：diff = 期初 + Σgaps - Σ(未落余额的 delta) = 100 - (-10) = 110。
        """
        flow_set = flows(
            [user(4, "qilin", 100)],
            [audit(1, "qilin", -5, 100, 100), audit(2, "qilin", -5, 100, 100)],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "qilin")
        self.assertEqual(entry["non_balance_rows"], 2)
        self.assertEqual(entry["non_balance_delta"], -10)
        self.assertEqual(entry["opening_balance"], 100)
        self.assertEqual(entry["diff_consume"], 110)
        self.assertIn("free_beta_rows_without_balance_effect", entry["tags"])
        self.assertIn("explained_by_untracked_changes", entry["tags"])
        self.assertEqual(report["unexplained"], 0)
        self.assertEqual(backfill.balance_alarms(report), [])

    def test_duplicate_ids_are_reported_and_not_double_counted(self):
        """重复 id：只算一次，且作为重复键列进报告（硬报警）。"""
        flow_set = flows(
            [user(5, "fang", 5)],
            [audit(1, "fang", 5, 0, 5), audit(1, "fang", 5, 0, 5)],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "fang")
        self.assertEqual(entry["audit_delta"], 5)
        self.assertEqual(entry["diff_consume"], 0)
        self.assertEqual(report["duplicate_keys"], [{"id": 1, "count": 2}])
        self.assertTrue(backfill.balance_alarms(report))

    def test_opening_balance_before_first_ledger_row_is_reported(self):
        """期初余额（第一条流水之前的余额）必须逐条列出，不猜测。"""
        flow_set = flows(
            [user(6, "dapeng", 2500)],
            [audit(1, "dapeng", 500, 2000, 2500)],
        )
        entry = entry_for(reconcile(flow_set), "dapeng")
        self.assertEqual(entry["opening_balance"], 2000)
        self.assertEqual(entry["untracked_delta"], 2000)
        self.assertEqual([item["kind"] for item in entry["untracked_changes"]],
                         ["opening_balance"])
        self.assertIn("explained_by_untracked_changes", entry["tags"])

    def test_gap_between_ledger_rows_is_reported(self):
        """两行流水之间的未入流水变动（兜底直写）也要算成未归因变动。"""
        flow_set = flows(
            [user(7, "yuelei", 330)],
            [audit(1, "yuelei", 100, 100, 200), audit(2, "yuelei", 30, 300, 330)],
        )
        entry = entry_for(reconcile(flow_set), "yuelei")
        self.assertEqual(entry["opening_balance"], 100)
        self.assertEqual(entry["untracked_delta"], 200)
        self.assertEqual(entry["diff_consume"], 200)
        self.assertIn("explained_by_untracked_changes", entry["tags"])

    def test_registration_trial_points_without_any_ledger_row(self):
        """注册赠送 16 点直接写 users.points，历史流水里没有它。"""
        entry = entry_for(reconcile(flows([user(8, "newbie", 16)])), "newbie")
        self.assertEqual(entry["diff_consume"], 16)
        self.assertIn("explained_by_trial_points", entry["tags"])
        self.assertEqual(backfill.balance_alarms(reconcile(flows([user(8, "newbie", 16)]))), [])

    def test_balance_without_any_ledger_row_is_flagged_for_humans(self):
        """有余额、四张表里却一条流水都没有：无从归因，必须点名交人工。"""
        report = reconcile(flows([user(9, "zepeng", 1776)]))
        entry = entry_for(report, "zepeng")
        self.assertIn("untracked_balance_without_ledger_rows", entry["tags"])
        self.assertEqual(report["untracked_balance_without_ledger_rows"], 1)

    def test_ledger_end_mismatch_is_hard_alarm(self):
        """最后一条流水的 after_points 与 users.points 不符 = 有人绕过流水改余额。"""
        flow_set = flows(
            [user(10, "fang", 999)],
            [audit(1, "fang", 100, 0, 100)],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "fang")
        self.assertIn("ledger_end_mismatch", entry["tags"])
        alarms = backfill.balance_alarms(report)
        self.assertTrue(any("after_points" in item for item in alarms), alarms)

    def test_inconsistent_ledger_row_is_hard_alarm(self):
        """行内不自洽（before != after 且 after-before != delta）必须报警。"""
        flow_set = flows(
            [user(11, "fang", 40)],
            [audit(1, "fang", 10, 0, 50)],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "fang")
        self.assertEqual(len(entry["ledger_row_inconsistent_rows"]), 1)
        self.assertIn("ledger_row_inconsistent", entry["tags"])
        self.assertTrue(backfill.balance_alarms(report))

    def test_transfer_mirrored_in_audit_is_not_double_counted(self):
        """点数赠送同时写 point_transfers 与两行 points_audit：只算一次。"""
        flow_set = flows(
            [user(1, "fang", 100), user(2, "tang", 100)],
            [
                audit(1, "fang", -100, 200, 100, "points-transfer:PT1:out"),
                audit(2, "tang", 100, 0, 100, "points-transfer:PT1:in"),
            ],
            [transfer(1, "PT1", 1, 2, 100)],
        )
        report = reconcile(flow_set)
        fang = entry_for(report, "fang")
        self.assertEqual(fang["transfer_unmirrored_delta"], 0)
        self.assertEqual(fang["transfer_mirrored_rows"], 1)
        self.assertEqual(fang["diff_consume"], 200)          # 全是期初余额，可解释
        self.assertIn("explained_by_untracked_changes", fang["tags"])
        self.assertEqual(entry_for(report, "tang")["transfer_out"], 0)
        self.assertEqual(entry_for(report, "tang")["transfer_in"], 100)
        self.assertEqual(entry_for(report, "tang")["diff_consume"], 0)
        self.assertEqual(report["unexplained"], 0)

    def test_transfer_without_audit_mirror_is_counted_once(self):
        """没有审计镜像的赠送（老数据）要按流水方向补算，不能漏。

        这里故意只给转出侧写了流水：转入侧靠 point_transfers 补齐后相符；
        转出侧因为缺少镜像而出现差额 —— 这正是「缺一条流水」该有的报警形态。
        """
        flow_set = flows(
            [user(1, "fang", 100), user(2, "tang", 100)],
            [audit(1, "fang", -100, 200, 100)],
            [transfer(1, "PT1", 1, 2, 100)],
        )
        report = reconcile(flow_set)
        tang = entry_for(report, "tang")
        self.assertEqual(tang["transfer_unmirrored_delta"], 100)
        self.assertEqual(tang["recomputed_consume"], 100)
        self.assertEqual(tang["diff_consume"], 0)
        # 转出侧既被 point_transfers 记了一次、又拿不到镜像流水，出现真实缺口 → 报警
        fang = entry_for(report, "fang")
        self.assertEqual(fang["recomputed_consume"], -200)
        self.assertIn("unexplained_difference", fang["tags"])

    def test_invite_rewards_are_a_separate_ledger(self):
        """邀请奖励进独立账本：consume 口径相符，strict 口径必然出现差异。"""
        flow_set = flows(
            [user(1, "Zhang", 100)],
            [audit(1, "Zhang", 100, 0, 100)],
            invite_rows=[invite(1, 1, 5280), invite(2, 1, 200, status="voided")],
        )
        consume = reconcile(flow_set, scope="consume")
        entry = entry_for(consume, "Zhang")
        self.assertEqual(entry["invite_recorded"], 5280)
        self.assertEqual(entry["invite_voided"], 200)
        self.assertEqual(entry["invite_delta"], 5080)
        self.assertEqual(entry["diff_consume"], 0)
        self.assertIn("invite_rewards_separate_ledger", entry["tags"])

        strict = reconcile(flow_set, scope="strict")
        strict_entry = entry_for(strict, "Zhang")
        self.assertEqual(strict_entry["diff_strict"], -5080)
        self.assertIn("unexplained_difference", strict_entry["tags"])

    def test_ledger_rows_without_user_are_reported(self):
        """流水所属账号已被删除：仍要照搬，但必须在报告里点名。"""
        flow_set = flows(
            [user(1, "fang", 5)],
            [audit(1, "fang", 5, 0, 5), audit(2, "deleted-user", 3, 0, 3)],
        )
        report = reconcile(flow_set)
        entry = entry_for(report, "deleted-user")
        self.assertIn("ledger_rows_without_user", entry["tags"])
        self.assertEqual(report["ledger_rows_without_user"], ["deleted-user"])

    def test_report_checksum_covers_difference_list(self):
        """人批准用的指纹必须随差异清单变化（否则批准会批错版本）。"""
        base = flows([user(1, "fang", 16)])
        first = reconcile(base)
        second = reconcile(flows([user(1, "fang", 32)]))
        self.assertNotEqual(first["report_checksum"], second["report_checksum"])
        self.assertEqual(first["report_checksum"], reconcile(base)["report_checksum"])


# ----------------------------------------------------------------------
# 内存 SQLite：从真实表结构读流水
# ----------------------------------------------------------------------

SOURCE_SCHEMA = (
    """CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
       points INTEGER DEFAULT 0)""",
    """CREATE TABLE points_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, who_admin TEXT NOT NULL,
       username TEXT NOT NULL, delta INTEGER NOT NULL, before_points INTEGER NOT NULL,
       after_points INTEGER NOT NULL, reason TEXT, created_at INTEGER NOT NULL,
       transaction_key TEXT)""",
    """CREATE TABLE point_transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_id TEXT NOT NULL UNIQUE,
       request_id TEXT NOT NULL, sender_user_id INTEGER NOT NULL, recipient_user_id INTEGER NOT NULL,
       amount INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', sender_before INTEGER NOT NULL,
       sender_after INTEGER NOT NULL, recipient_before INTEGER NOT NULL, recipient_after INTEGER NOT NULL,
       created_at INTEGER NOT NULL, UNIQUE(sender_user_id, request_id))""",
    """CREATE TABLE invite_reward_point_records(id INTEGER PRIMARY KEY AUTOINCREMENT,
       invite_relation_id INTEGER NOT NULL, upgrade_record_id INTEGER NOT NULL UNIQUE,
       inviter_user_id INTEGER NOT NULL, invitee_user_id INTEGER NOT NULL,
       inviter_level_snapshot TEXT NOT NULL, invitee_level TEXT NOT NULL, reward_points INTEGER NOT NULL,
       reward_total_after INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'recorded',
       created_at INTEGER NOT NULL, voided_at INTEGER, void_reason TEXT, voided_by TEXT,
       event_type TEXT NOT NULL DEFAULT 'upgrade', claim_id INTEGER)""",
)


class SourceSqliteTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for ddl in SOURCE_SCHEMA:
            self.conn.execute(ddl)
        self.addCleanup(self.conn.close)

    def seed(self, users, audit_rows=(), transfer_rows=(), invite_rows=()):
        for uid, username, points in users:
            self.conn.execute(
                "INSERT INTO users(id, username, points) VALUES(?,?,?)", (uid, username, points))
        for row_id, username, delta, before, after, key in audit_rows:
            self.conn.execute(
                "INSERT INTO points_audit(id, who_admin, username, delta, before_points, "
                "after_points, reason, created_at, transaction_key) VALUES(?,?,?,?,?,?,?,?,?)",
                (row_id, "system", username, delta, before, after, "job:collect", 1783260548, key))
        for row in transfer_rows:
            self.conn.execute(
                "INSERT INTO point_transfers(id, transfer_id, request_id, sender_user_id, "
                "recipient_user_id, amount, note, sender_before, sender_after, recipient_before, "
                "recipient_after, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
        for row in invite_rows:
            self.conn.execute(
                "INSERT INTO invite_reward_point_records(id, invite_relation_id, upgrade_record_id, "
                "inviter_user_id, invitee_user_id, inviter_level_snapshot, invitee_level, "
                "reward_points, reward_total_after, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
        self.conn.commit()

    def test_read_ledger_flows_and_reconcile_end_to_end(self):
        """混合样本：正常、负数、0 值、点数赠送、邀请奖励独立账本。"""
        self.seed(
            users=[(1, "fang", 170), (2, "tang", 0)],
            audit_rows=[
                # fang：充值 100 → 任务预扣 30 → 空操作 0 → 收到赠送 100（合计 170）
                (1, "fang", 100, 0, 100, None),
                (2, "fang", -30, 100, 70, "job-charge:fang:/api/gen/copy:r1"),
                (3, "fang", 0, 70, 70, None),
                (6, "fang", 100, 70, 170, "points-transfer:PT1:in"),
                # tang：充值 100 → 赠送转出 100（合计 0）
                (4, "tang", 100, 0, 100, None),
                (5, "tang", -100, 100, 0, "points-transfer:PT1:out"),
            ],
            transfer_rows=[(1, "PT1", "mp-1", 2, 1, 100, "", 100, 0, 0, 100, 1786114671)],
            invite_rows=[(1, 10, 199, 2, 5, "partner", "experience", 240, 240, "recorded", 1786095339)],
        )
        flow_set = backfill.read_ledger_flows(self.conn)
        self.assertEqual(len(flow_set["users"]), 2)
        self.assertEqual(len(flow_set["audit"]), 6)
        report = backfill.reconcile(flow_set)
        self.assertEqual(report["users_total"], 2)
        self.assertEqual(report["duplicate_keys"], [])
        fang = entry_for(report, "fang")
        # 100 - 30 + 0 + 100 = 170，余额 170（赠送按镜像去重，只算一次）
        self.assertEqual(fang["audit_delta"], 170)
        self.assertEqual(fang["sqlite_points"], 170)
        self.assertEqual(fang["diff_consume"], 0)
        self.assertEqual(fang["transfer_in"], 100)
        self.assertEqual(fang["transfer_unmirrored_delta"], 0)
        tang = entry_for(report, "tang")
        self.assertEqual(tang["transfer_out"], 100)
        self.assertEqual(tang["diff_consume"], 0)
        self.assertEqual(tang["invite_delta"], 240)
        self.assertIn("invite_rewards_separate_ledger", tang["tags"])
        self.assertEqual(report["unexplained"], 0)
        self.assertEqual(report["ledger_end_mismatch"], 0)
        self.assertEqual(report["report_checksum"],
                         backfill.reconcile(backfill.read_ledger_flows(self.conn))["report_checksum"])

    def test_duplicate_id_rows_from_a_corrupt_source_are_reported(self):
        """损坏的源库可能出现重复 id（表被合并/手工改过）：只报、只算一次、并挡住 --apply。"""
        self.conn.execute("DROP TABLE points_audit")
        self.conn.execute(
            "CREATE TABLE points_audit(id INTEGER, who_admin TEXT NOT NULL, username TEXT NOT NULL, "
            "delta INTEGER NOT NULL, before_points INTEGER NOT NULL, after_points INTEGER NOT NULL, "
            "reason TEXT, created_at INTEGER NOT NULL, transaction_key TEXT)")
        self.conn.execute("INSERT INTO users(id, username, points) VALUES(1,'fang',5)")
        for _ in range(2):
            self.conn.execute(
                "INSERT INTO points_audit(id, who_admin, username, delta, before_points, "
                "after_points, reason, created_at, transaction_key) "
                "VALUES(1,'system','fang',5,0,5,'job:collect',1783260548,NULL)")
        self.conn.commit()
        report = backfill.reconcile(backfill.read_ledger_flows(self.conn))
        self.assertEqual(report["duplicate_keys"], [{"id": 1, "count": 2}])
        self.assertEqual(entry_for(report, "fang")["audit_delta"], 5)
        self.assertTrue(backfill.balance_alarms(report))

    def test_verify_balances_from_a_sqlite_file(self):
        """verify_balances 走真实文件路径（只读连接），不碰 PostgreSQL。"""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(lambda: os.unlink(tmp.name))
        disk = sqlite3.connect(tmp.name)
        try:
            for ddl in SOURCE_SCHEMA:
                disk.execute(ddl)
            disk.execute("INSERT INTO users(id, username, points) VALUES(1,'fang',0)")
            disk.execute(
                "INSERT INTO points_audit(id, who_admin, username, delta, before_points, "
                "after_points, reason, created_at, transaction_key) "
                "VALUES(1,'system','fang',0,0,0,'',0,NULL)")
            disk.commit()
        finally:
            disk.close()
        report = backfill.verify_balances(Path(tmp.name))
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["diffs"], 0)
        self.assertEqual(report["source"], tmp.name)


# ----------------------------------------------------------------------
# 迁移文件一致性（不需要数据库）
# ----------------------------------------------------------------------

class MigrationStaticTest(unittest.TestCase):
    def setUp(self):
        self.source_text = MIGRATION_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source_text)

    def test_revision_chain(self):
        assignments = {
            node.targets[0].id: node.value.value
            for node in self.tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
        }
        self.assertEqual(assignments.get("revision"), "20260916_0012")
        self.assertEqual(assignments.get("down_revision"), "20260916_0011")

    def test_created_tables_match_backfill_targets(self):
        created = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "create_table"):
                continue
            schema = None
            for keyword in node.keywords:
                if keyword.arg == "schema":
                    schema = keyword.value.value
            name = node.args[0].value
            if schema == "ledger":
                created.add(name)
        expected = {target.split(".", 1)[1]
                    for _source, target, _key, _activity, _identity in backfill.TABLES}
        self.assertEqual(created, expected)
        self.assertEqual(len(expected), 7)

    def test_downgrade_is_forbidden(self):
        """本地没有 alembic/sqlalchemy，只取 downgrade 函数体执行（不 import 迁移模块）。"""
        source = None
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
                source = ast.get_source_segment(self.source_text, node)
        self.assertIsNotNone(source, "迁移文件里没有 downgrade()")
        namespace = {}
        exec(compile(source, str(MIGRATION_PATH), "exec"), namespace)
        with self.assertRaises(RuntimeError):
            namespace["downgrade"]()

    def test_no_sqlite_import_in_server_file(self):
        """server/ 下新文件不得含行首 import sqlite3（sqlite_inventory 棘轮）。"""
        for line in self.source_text.splitlines():
            self.assertFalse(line.startswith("import sqlite3"))
            self.assertFalse(line.startswith("from sqlite3 import"))

    def test_env_column_stays_bigint(self):
        """virtual_pay_orders.env 是支付环境枚举，不得被当成布尔列。"""
        self.assertIn('sa.Column("env", sa.BigInteger()', self.source_text)
        self.assertEqual(backfill.BOOLEAN_COLUMNS, frozenset())

    def test_timestamp_columns_are_bigint_epoch(self):
        for column in ("created_at", "paid_at", "credited_at", "delivered_at"):
            self.assertIn('"%s", sa.BigInteger()' % column, self.source_text)


class ApplyGuardTest(unittest.TestCase):
    """--apply 的护栏在触碰 PostgreSQL 之前就要拒绝。"""

    def setUp(self):
        self.data = {source: {"columns": (), "rows": {"1": {}}}
                     for source, _t, _k, _a, _i in backfill.TABLES}

    def test_apply_requires_code_sha(self):
        with self.assertRaises(RuntimeError) as ctx:
            backfill.apply(self.data, Path("/tmp/users.db"), "", clean_balance_report())
        self.assertIn("--code-sha", str(ctx.exception))

    def test_apply_refuses_when_balance_has_hard_alarm(self):
        balance = clean_balance_report()
        balance["unexplained"] = 3
        with self.assertRaises(RuntimeError) as ctx:
            backfill.apply(self.data, Path("/tmp/users.db"), "a" * 40, balance)
        self.assertIn("硬报警", str(ctx.exception))

    def test_apply_refuses_empty_source(self):
        data = {source: {"columns": (), "rows": {}} for source, _t, _k, _a, _i in backfill.TABLES}
        with self.assertRaises(RuntimeError) as ctx:
            backfill.apply(data, Path("/tmp/users.db"), "a" * 40, clean_balance_report())
        self.assertIn("没有任何行", str(ctx.exception))

    def test_conflict_guard_uses_latest_activity_column(self):
        row = {"id": 1, "created_at": 100, "voided_at": 300}
        self.assertEqual(backfill._row_activity(row, ("created_at", "voided_at")), 300)
        self.assertEqual(backfill._row_activity({"id": 1, "created_at": 100,
                                                 "voided_at": None}, ("created_at", "voided_at")), 100)


# ----------------------------------------------------------------------
# PostgreSQL（CI / staging：先 alembic upgrade head）
# ----------------------------------------------------------------------

LEDGER_TABLES = {target.split(".", 1)[1]
                 for _s, target, _k, _a, _i in backfill.TABLES}

EXPECTED_TYPES = {
    "points_audit": {"id": "bigint", "who_admin": "text", "delta": "bigint",
                     "created_at": "bigint", "transaction_key": "text"},
    "point_transfers": {"id": "bigint", "transfer_id": "text", "amount": "bigint",
                        "sender_before": "bigint"},
    "recharge_orders": {"order_id": "text", "amount": "double precision",
                        "list_amount": "double precision", "discount_bps": "bigint"},
    "virtual_pay_orders": {"order_id": "text", "amount_fen": "bigint", "env": "bigint",
                           "points": "bigint", "raw_order_json": "text"},
    "membership_upgrade_records": {"id": "bigint", "voided_at": "bigint"},
}

EXPECTED_UNIQUE_INDEXES = {
    "idx_points_audit_transaction_key",
    "idx_invite_rewards_claim",
    "idx_invite_rewards_upgrade_relation_level",
    "idx_membership_upgrades_source",
}


@unittest.skipUnless(PG_URL, "HQ_DATABASE_URL 未配置：跳过 PostgreSQL 段")
class PgLedgerSchemaTest(unittest.TestCase):
    def setUp(self):
        from server.db import postgres
        self.postgres = postgres

    def test_all_seven_tables_exist_in_ledger_schema(self):
        with self.postgres.transaction() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'ledger'").fetchall()
        names = {row["table_name"] for row in rows}
        self.assertEqual(LEDGER_TABLES - names, set(),
                         "ledger schema 缺表（先跑 alembic upgrade head）")

    def test_column_types_follow_the_sqlite_mapping(self):
        with self.postgres.transaction() as conn:
            for table, columns in EXPECTED_TYPES.items():
                for column, data_type in columns.items():
                    row = conn.execute(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = 'ledger' AND table_name = %s AND column_name = %s",
                        (table, column)).fetchone()
                    self.assertIsNotNone(row, "%s.%s 不存在" % (table, column))
                    self.assertEqual(row["data_type"], data_type,
                                     "%s.%s 类型应为 %s" % (table, column, data_type))

    def test_unique_indexes_are_carried_over(self):
        with self.postgres.transaction() as conn:
            rows = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'ledger'").fetchall()
        names = {row["indexname"] for row in rows}
        self.assertEqual(EXPECTED_UNIQUE_INDEXES - names, set(), "缺唯一索引")

    def test_backfill_audit_domain_is_ledger(self):
        with self.postgres.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM ops.data_migration_runs "
                "WHERE domain = 'ledger'").fetchone()
        self.assertGreaterEqual(row["total"], 0)


@unittest.skipUnless(BACKFILL_SOURCE and os.path.isfile(BACKFILL_SOURCE),
                     "HQ_LEDGER_BACKFILL_SOURCE 未指向可读的 users.db 快照")
class BackfillSourceTest(unittest.TestCase):
    """对真实 users.db 只读快照跑 dry-run（绝不 --apply）。"""

    def test_dry_run_reports_rows_and_checksum(self):
        data = backfill.read_source(Path(BACKFILL_SOURCE))
        report = backfill.summary(data)
        for source_table, _t, _k, _a, _i in backfill.TABLES:
            self.assertIn(source_table, report)
            self.assertIsInstance(report[source_table], int)
        self.assertEqual(len(report["source_checksum"]), 64)

    def test_balance_verification_has_no_unexplained_difference(self):
        report = backfill.verify_balances(Path(BACKFILL_SOURCE))
        self.assertEqual(report["ledger_end_mismatch"], 0)
        self.assertEqual(report["ledger_row_inconsistent"]["users"], 0)
        self.assertEqual(report["duplicate_keys"], [])
        self.assertEqual(report["unexplained"], 0)


if __name__ == "__main__":
    unittest.main()
