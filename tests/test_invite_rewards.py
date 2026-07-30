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

    def _register_and_upgrade(self, username, code, tier="experience", offset=1):
        created, err = self.auth.register_account(username, "secret123", invite_code=code)
        self.assertIsNone(err)
        upgraded, err = self.auth.set_membership_admin(
            "admin", username, tier, "测试会员升级", now=self.now + offset,
        )
        self.assertIsNone(err)
        return created, upgraded

    def test_partner_first_nine_experience_members_do_not_reward_tenth_does(self):
        code = self._invite_code()
        for ordinal in range(1, 11):
            self._register_and_upgrade(
                "experience-%02d" % ordinal, code, offset=ordinal,
            )

        c = self._connect()
        try:
            inviter_id = self._user_id(c, "inviter")
            rewards = self.auth.invites.reward_points(c, inviter_id)
            self.assertEqual(rewards["total_reward_points"], 240)
            self.assertEqual(rewards["total"], 1)
            self.assertEqual(rewards["records"][0]["invitee_username"], "experience-10")
            slots = c.execute(
                """SELECT invitee_user_id,ordinal,reward_eligible
                     FROM partner_experience_reward_slots
                    WHERE inviter_user_id=? ORDER BY ordinal""",
                (inviter_id,),
            ).fetchall()
            self.assertEqual([row["ordinal"] for row in slots], list(range(1, 11)))
            self.assertEqual([row["reward_eligible"] for row in slots], [0] * 9 + [1])
        finally:
            c.close()

    def test_partner_experience_reward_preview_uses_next_upgrade_ordinal(self):
        code = self._invite_code()
        for ordinal in range(1, 9):
            self._register_and_upgrade(
                "preview-seed-%02d" % ordinal, code, offset=ordinal,
            )

        ninth, err = self.auth.register_account(
            "preview-ninth", "secret123", invite_code=code,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            preview = self.auth.invites.reward_upgrade_preview(
                c, self._user_id(c, "preview-ninth"), "experience", self.now + 9,
            )
            self.assertEqual(preview["partner_experience_ordinal"], 9)
            self.assertEqual(preview["reward_points"], 0)
            self.assertEqual(preview["reward_suppressed_reason"], "partner_first_nine_experience")
        finally:
            c.close()

        upgraded, err = self.auth.set_membership_admin(
            "admin", "preview-ninth", "experience", "第九名体验官", now=self.now + 9,
        )
        self.assertIsNone(err)
        tenth, err = self.auth.register_account(
            "preview-tenth", "secret123", invite_code=code,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            preview = self.auth.invites.reward_upgrade_preview(
                c, self._user_id(c, "preview-tenth"), "experience", self.now + 10,
            )
            self.assertEqual(preview["partner_experience_ordinal"], 10)
            self.assertEqual(preview["reward_points"], 240)
            self.assertEqual(preview["reward_suppressed_reason"], "")
        finally:
            c.close()

    def test_partner_experience_slot_backfill_is_ordered_idempotent_and_preserves_rewards(self):
        code = self._invite_code()
        for ordinal in range(1, 4):
            self._register_and_upgrade(
                "legacy-%02d" % ordinal, code, offset=ordinal,
            )
        legacy_current, err = self.auth.register_account(
            "legacy-current-without-upgrade", "secret123", invite_code=code,
        )
        self.assertIsNone(err)

        c = self._connect()
        try:
            c.execute(
                """UPDATE users
                      SET membership_tier='experience',membership_started_at=?,
                          membership_expires_at=?
                    WHERE username='legacy-current-without-upgrade'""",
                (self.now + 4, self.now + 31536000),
            )
            c.execute(
                """UPDATE membership_upgrade_records
                      SET created_at=?
                    WHERE user_id=(SELECT id FROM users WHERE username='legacy-02')
                      AND to_level='experience'""",
                (self.now - 1,),
            )
            relation = c.execute(
                """SELECT ui.id,ui.inviter_user_id,ui.invitee_user_id
                     FROM user_invites ui JOIN users u ON u.id=ui.invitee_user_id
                    WHERE u.username='legacy-01'""",
            ).fetchone()
            upgrade = c.execute(
                """SELECT id,created_at FROM membership_upgrade_records
                    WHERE user_id=? AND to_level='experience' ORDER BY id LIMIT 1""",
                (relation["invitee_user_id"],),
            ).fetchone()
            if not c.execute(
                "SELECT 1 FROM invite_reward_point_records WHERE upgrade_record_id=?",
                (upgrade["id"],),
            ).fetchone():
                c.execute(
                    """INSERT INTO invite_reward_point_records(
                           invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
                           inviter_level_snapshot,invitee_level,reward_points,reward_total_after,
                           status,created_at
                       ) VALUES(?,?,?,?,?,'experience',240,240,'recorded',?)""",
                    (
                        relation["id"], upgrade["id"], relation["inviter_user_id"],
                        relation["invitee_user_id"], "partner", upgrade["created_at"],
                    ),
                )
            rewards_before = [
                tuple(row) for row in c.execute(
                    """SELECT id,invite_relation_id,upgrade_record_id,reward_points,status
                         FROM invite_reward_point_records ORDER BY id""",
                ).fetchall()
            ]
            c.execute("DROP TABLE IF EXISTS partner_experience_reward_slots")
            self.auth.invites.init_schema(c, now=self.now + 100)
            first_slots = [
                tuple(row) for row in c.execute(
                    """SELECT u.username,s.ordinal,s.reward_eligible
                         FROM partner_experience_reward_slots s
                         JOIN users u ON u.id=s.invitee_user_id
                        ORDER BY s.ordinal""",
                ).fetchall()
            ]
            self.auth.invites.init_schema(c, now=self.now + 101)
            second_slots = [
                tuple(row) for row in c.execute(
                    """SELECT u.username,s.ordinal,s.reward_eligible
                         FROM partner_experience_reward_slots s
                         JOIN users u ON u.id=s.invitee_user_id
                        ORDER BY s.ordinal""",
                ).fetchall()
            ]
            rewards_after = [
                tuple(row) for row in c.execute(
                    """SELECT id,invite_relation_id,upgrade_record_id,reward_points,status
                         FROM invite_reward_point_records ORDER BY id""",
                ).fetchall()
            ]
            self.assertEqual(
                [row[0] for row in first_slots],
                ["legacy-02", "legacy-01", "legacy-03", "legacy-current-without-upgrade"],
            )
            self.assertEqual([row[1] for row in first_slots], [1, 2, 3, 4])
            self.assertEqual([row[2] for row in first_slots], [0, 0, 0, 0])
            self.assertEqual(second_slots, first_slots)
            self.assertEqual(rewards_after, rewards_before)
        finally:
            c.rollback()
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
            self.assertEqual(rewards["total"], 2)
            first_records = [r for r in rewards["records"] if r["invitee_username"] == "first"]
            self.assertEqual([r["reward_points"] for r in first_records], [1500])
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
        user, err = self.auth.set_membership_admin(
            "admin", "inviter", "initiator", "测试发起人奖励台账", now=self.now,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "initiator")
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
            self.assertEqual(ledger["recorded_points"], 280)
            reward_id = ledger["items"][0]["id"]
            self.auth.invites.admin_reward_action(c, reward_id, "void", "测试作废", "admin", self.now + 2)
            c.commit()
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 0)
            self.assertEqual(ledger["voided_points"], 280)
            self.auth.invites.admin_reward_action(c, reward_id, "restore", "测试恢复", "admin", self.now + 3)
            c.commit()
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 280)
            self.assertEqual(
                c.execute("SELECT points FROM users WHERE username='ledger-user'").fetchone()[0],
                before_points,
            )
        finally:
            c.close()

    def test_experience_inviter_reward_is_unchanged(self):
        user, err = self.auth.set_membership_admin(
            "admin", "inviter", "experience", "测试体验官邀请奖励", now=self.now,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "experience")
        code = self._invite_code()
        self._register_and_upgrade("experience-invitee", code, offset=1)

        c = self._connect()
        try:
            rewards = self.auth.invites.reward_points(c, self._user_id(c, "inviter"))
            self.assertEqual(rewards["total_reward_points"], 200)
            self.assertEqual(rewards["total"], 1)
        finally:
            c.close()

    def test_voiding_tenth_reward_does_not_release_partner_experience_slot(self):
        code = self._invite_code()
        for ordinal in range(1, 11):
            self._register_and_upgrade("void-slot-%02d" % ordinal, code, offset=ordinal)
        c = self._connect()
        try:
            inviter_id = self._user_id(c, "inviter")
            reward = c.execute(
                """SELECT * FROM invite_reward_point_records
                    WHERE inviter_user_id=? AND status='recorded'""",
                (inviter_id,),
            ).fetchone()
            self.auth.invites.admin_reward_action(
                c, reward["id"], "void", "测试名额不释放", "admin", self.now + 11,
            )
            c.commit()
        finally:
            c.close()

        self._register_and_upgrade("void-slot-11", code, offset=12)
        c = self._connect()
        try:
            inviter_id = self._user_id(c, "inviter")
            slots = c.execute(
                """SELECT ordinal,reward_eligible FROM partner_experience_reward_slots
                    WHERE inviter_user_id=? ORDER BY ordinal""",
                (inviter_id,),
            ).fetchall()
            rewards = self.auth.invites.reward_points(c, inviter_id)
            self.assertEqual(slots[-1]["ordinal"], 11)
            self.assertEqual(slots[-1]["reward_eligible"], 1)
            self.assertEqual(rewards["total_reward_points"], 240)
            self.assertEqual(rewards["records"][0]["invitee_username"], "void-slot-11")
        finally:
            c.close()

    def test_duplicate_upgrade_order_does_not_consume_another_slot(self):
        code = self._invite_code()
        created, err = self.auth.register_account(
            "duplicate-slot", "secret123", invite_code=code,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            user_id = self._user_id(c, "duplicate-slot")
            first = self.auth.invites.record_membership_upgrade(
                c, user_id, "", "experience", "test",
                source_order_id="same-order", operator="admin", now=self.now + 1,
            )
            second = self.auth.invites.record_membership_upgrade(
                c, user_id, "", "experience", "test",
                source_order_id="same-order", operator="admin", now=self.now + 2,
            )
            c.commit()
            slots = c.execute(
                "SELECT * FROM partner_experience_reward_slots WHERE invitee_user_id=?",
                (user_id,),
            ).fetchall()
            self.assertEqual(first["upgrade_record_id"], second["upgrade_record_id"])
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["ordinal"], 1)
        finally:
            c.close()

    def test_rebinding_same_invitee_to_same_partner_does_not_consume_another_slot(self):
        code = self._invite_code()
        created, err = self.auth.register_account(
            "rebound-slot", "secret123", invite_code=code,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            relation = c.execute(
                """SELECT ui.* FROM user_invites ui
                    JOIN users u ON u.id=ui.invitee_user_id
                   WHERE u.username='rebound-slot'""",
            ).fetchone()
            first = self.auth.invites.ensure_partner_experience_reward_slot(
                c, relation, 1001, self.now + 1,
            )
            rebound_relation = dict(relation)
            rebound_relation["id"] = int(relation["id"]) + 1000
            second = self.auth.invites.ensure_partner_experience_reward_slot(
                c, rebound_relation, 1002, self.now + 2,
            )
            slots = c.execute(
                """SELECT * FROM partner_experience_reward_slots
                    WHERE inviter_user_id=? AND invitee_user_id=?""",
                (relation["inviter_user_id"], relation["invitee_user_id"]),
            ).fetchall()
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["ordinal"], 1)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
