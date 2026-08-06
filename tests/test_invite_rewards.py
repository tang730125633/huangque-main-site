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

    def test_invited_user_can_exceed_direct_inviter_tier(self):
        code = self._invite_code()
        _, err = self.auth.register_account("limited", "secret123", invite_code=code)
        self.assertIsNone(err)
        user, err = self.auth.set_membership_admin(
            "admin", "limited", "partner", "允许同级", now=self.now + 1,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "partner")
        user, err = self.auth.set_membership_admin(
            "admin", "limited", "initiator", "允许按业务升级", now=self.now + 2,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "initiator")

    def test_admin_upgrade_unlocks_pending_rewards_for_the_upgraded_inviter(self):
        self.auth.create_user("free-inviter", "secret123", 0)
        c = self._connect()
        try:
            free_id = self._user_id(c, "free-inviter")
            code = self.auth.invites.ensure_user_code(
                c, free_id, now=self.now, enforce_membership=False,
            )["code"]
            c.commit()
        finally:
            c.close()
        _, err = self.auth.register_account("partner-child", "secret123", invite_code=code)
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "partner-child", "partner", "下线升级", now=self.now + 1,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            self.assertEqual(c.execute(
                "SELECT status FROM invite_reward_claims WHERE direct_inviter_user_id=?",
                (free_id,),
            ).fetchone()[0], "pending_upgrade")
        finally:
            c.close()
        upgraded, err = self.auth.set_membership_admin(
            "admin", "free-inviter", "partner", "领取邀请奖励", now=self.now + 2,
        )
        self.assertIsNone(err)
        self.assertEqual(upgraded["invite_reward_result"]["count"], 1)
        self.assertEqual(upgraded["invite_reward_result"]["total_points"], 1500)
        c = self._connect()
        try:
            self.assertEqual(c.execute(
                "SELECT status FROM invite_reward_claims WHERE direct_inviter_user_id=?",
                (free_id,),
            ).fetchone()[0], "credited")
        finally:
            c.close()

    def test_pending_review_progression_holds_cap(self):
        code = self._invite_code()
        _, err = self.auth.register_account("review-user", "secret123", invite_code=code)
        self.assertIsNone(err)
        c = self._connect()
        try:
            relation = c.execute("SELECT * FROM user_invites WHERE invitee_user_id=?", (self._user_id(c, "review-user"),)).fetchone()
            c.execute("UPDATE user_invites SET risk_status='review' WHERE id=?", (relation["id"],))
            c.commit()
        finally:
            c.close()
        self.auth.set_membership_admin("admin", "review-user", "experience", "待复核升级", now=self.now + 1)
        self.auth.set_membership_admin("admin", "review-user", "partner", "待复核升级", now=self.now + 2)
        c = self._connect()
        try:
            rewards = c.execute(
                "SELECT reward_points,status FROM invite_reward_point_records WHERE invite_relation_id=? ORDER BY id",
                (relation["id"],),
            ).fetchall()
            self.assertEqual([(r["reward_points"], r["status"]) for r in rewards], [(240, "pending_review"), (1260, "pending_review")])
            self.auth.invites.admin_relation_action(c, relation["id"], "restore", "", self._user_id(c, "admin"), self.now + 3)
            c.commit()
            self.assertEqual(c.execute(
                "SELECT SUM(reward_points) FROM invite_reward_point_records WHERE invite_relation_id=? AND status='recorded'",
                (relation["id"],),
            ).fetchone()[0], 1500)
        finally:
            c.close()

    def test_void_then_upgrade_cannot_restore_above_cap(self):
        code = self._invite_code()
        _, err = self.auth.register_account("restore-user", "secret123", invite_code=code)
        self.assertIsNone(err)
        self.auth.set_membership_admin("admin", "restore-user", "experience", "先升体验官", now=self.now + 1)
        c = self._connect()
        try:
            first_id = c.execute(
                "SELECT id FROM invite_reward_point_records WHERE invitee_user_id=?",
                (self._user_id(c, "restore-user"),),
            ).fetchone()[0]
            self.auth.invites.admin_reward_action(c, first_id, "void", "人工复核作废", "admin", self.now + 2)
            c.commit()
        finally:
            c.close()
        self.auth.set_membership_admin("admin", "restore-user", "partner", "再升合伙人", now=self.now + 3)
        c = self._connect()
        try:
            self.assertEqual(c.execute(
                "SELECT SUM(reward_points) FROM invite_reward_point_records WHERE invitee_user_id=? AND status='recorded'",
                (self._user_id(c, "restore-user"),),
            ).fetchone()[0], 1500)
            with self.assertRaises(self.auth.invites.InviteError) as caught:
                self.auth.invites.admin_reward_action(c, first_id, "restore", "尝试超额恢复", "admin", self.now + 4)
            self.assertEqual(caught.exception.code, "reward_cap_exceeded")
        finally:
            c.close()

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

    def test_refunded_membership_reward_cannot_be_restored(self):
        code = self._invite_code()
        _, err = self.auth.register_account("refunded-user", "secret123", invite_code=code)
        self.assertIsNone(err)
        self.auth.set_membership_admin("admin", "refunded-user", "experience", "生成奖励", now=self.now + 1)
        c = self._connect()
        try:
            reward_id = c.execute(
                "SELECT id FROM invite_reward_point_records WHERE invitee_user_id=?",
                (self._user_id(c, "refunded-user"),),
            ).fetchone()[0]
            c.execute(
                "UPDATE invite_reward_point_records SET status='voided',void_reason='membership_refund' WHERE id=?",
                (reward_id,),
            )
            with self.assertRaises(self.auth.invites.InviteError) as caught:
                self.auth.invites.admin_reward_action(c, reward_id, "restore", "尝试恢复", "admin", self.now + 2)
            self.assertEqual(caught.exception.code, "refunded_reward_not_restorable")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
