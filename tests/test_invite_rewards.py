import importlib
import os
import tempfile
import unittest


class InviteRewardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.init_db()
        self.auth.create_user("admin", "secret123", 0, "admin")
        self.auth.create_user("inviter", "secret123", 88)
        self.now = 1800000000
        user, err = self.auth.set_membership_admin(
            "admin", "inviter", "partner", "测试邀请人", now=self.now,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "partner")

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _connect(self):
        c = self.auth.db()
        c.row_factory = __import__("sqlite3").Row
        return c

    def _user_id(self, c, username):
        return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    def _invite_code(self):
        c = self._connect()
        try:
            row = self.auth.invites.ensure_user_code(c, self._user_id(c, "inviter"), now=self.now)
            c.commit()
            return row["code"]
        finally:
            c.close()

    def test_partner_rewards_are_non_stacking_and_do_not_change_consumable_points(self):
        code = self._invite_code()
        first, err = self.auth.register_account("first", "secret123", invite_code=code)
        self.assertIsNone(err)
        first_points = first["user"]["points"]

        _, err = self.auth.set_membership_admin(
            "admin", "first", "experience", "先升体验官", now=self.now + 1,
        )
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "first", "partner", "再升合伙人", now=self.now + 2,
        )
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "first", "partner", "重复设置", now=self.now + 3,
        )
        self.assertIsNone(err)

        second, err = self.auth.register_account("second", "secret123", invite_code=code)
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "second", "partner", "直接升合伙人", now=self.now + 4,
        )
        self.assertIsNone(err)

        c = self._connect()
        try:
            inviter_id = self._user_id(c, "inviter")
            rewards = self.auth.invites.reward_points(c, inviter_id)
            self.assertEqual(rewards["total_reward_points"], 3000)
            self.assertEqual(rewards["total"], 3)
            first_records = [r for r in rewards["records"] if r["invitee_username"] == "first"]
            self.assertEqual(sorted(r["reward_points"] for r in first_records), [240, 1260])
            self.assertEqual(max(r["reward_total_after"] for r in first_records), 1500)
            second_record = next(r for r in rewards["records"] if r["invitee_username"] == "second")
            self.assertEqual(second_record["reward_points"], 1500)
            self.assertEqual(
                c.execute("SELECT points FROM users WHERE username='first'").fetchone()[0],
                first_points,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM points_audit WHERE username IN ('first','inviter')").fetchone()[0],
                0,
            )
        finally:
            c.close()

    def test_reward_schema_and_matrix_exist(self):
        c = self._connect()
        try:
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("membership_upgrade_records", tables)
            self.assertIn("invite_reward_point_records", tables)
            self.assertEqual(self.auth.invites.INVITE_REWARD_TOTALS["partner"]["partner"], 1500)
            self.assertEqual(self.auth.invites.INVITE_REWARD_TOTALS["initiator"]["initiator"], 15000)
        finally:
            c.close()

    def test_invited_user_cannot_exceed_direct_inviter_tier(self):
        code = self._invite_code()
        _, err = self.auth.register_account("limited", "secret123", invite_code=code)
        self.assertIsNone(err)
        user, err = self.auth.set_membership_admin(
            "admin", "limited", "partner", "允许同级", now=self.now + 1,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "partner")
        with self.assertRaises(self.auth.invites.InviteError) as caught:
            self.auth.set_membership_admin(
                "admin", "limited", "initiator", "不允许越级", now=self.now + 2,
            )
        self.assertEqual(caught.exception.code, "invite_membership_limit")

    def test_admin_reward_ledger_can_void_and_restore_without_changing_user_points(self):
        code = self._invite_code()
        created, err = self.auth.register_account("ledger-user", "secret123", invite_code=code)
        self.assertIsNone(err)
        before_points = created["user"]["points"]
        _, err = self.auth.set_membership_admin(
            "admin", "ledger-user", "experience", "生成奖励", now=self.now + 1,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 240)
            reward_id = ledger["items"][0]["id"]
            self.auth.invites.admin_reward_action(c, reward_id, "void", "测试作废", "admin", self.now + 2)
            c.commit()
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 0)
            self.assertEqual(ledger["voided_points"], 240)
            self.auth.invites.admin_reward_action(c, reward_id, "restore", "测试恢复", "admin", self.now + 3)
            c.commit()
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 240)
            self.assertEqual(
                c.execute("SELECT points FROM users WHERE username='ledger-user'").fetchone()[0],
                before_points,
            )
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
