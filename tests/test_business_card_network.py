import importlib
import os
import sqlite3
import tempfile
import time
import unittest


class BusinessCardNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import server.auth_server as auth_server
        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.path.join(self.tmp.name, "users.db")
        self.auth.INVITE_HASH_SECRET = "test"
        self.auth.init_db()
        self.auth.create_user("root", "secret123")
        now = int(time.time())
        with self.conn() as c:
            c.execute("UPDATE users SET display_name='根用户',membership_tier='experience',membership_expires_at=? WHERE username='root'", (now + 999999,))
            code = self.auth.invites.ensure_user_code(c, self.uid(c, "root"), enforce_membership=False)["code"]
        self.child, err = self.auth.register_account("child", "secret123", "子用户", invite_code=code, card={"headline": "设计师"})
        self.assertIsNone(err)

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        c = sqlite3.connect(self.auth.DB); c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def uid(c, username):
        return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    def test_card_privacy_and_registration_are_atomic(self):
        with self.conn() as c:
            child_id = self.uid(c, "child")
            mine = self.auth.business_cards.mine(c, child_id)
            self.assertEqual(mine["status"], "draft")
            self.assertEqual(mine["headline"], "设计师")
            with self.assertRaises(self.auth.business_cards.CardError):
                self.auth.business_cards.public(c, mine["public_id"])
            self.auth.business_cards.update(c, child_id, {"phone": "13800000000", "phone_public": False})
            self.auth.business_cards.publish(c, child_id, "published")
            public = self.auth.business_cards.public(c, mine["public_id"])
            self.assertNotIn("phone", public)
            self.assertNotIn("username", public)
            self.auth.business_cards.publish(c, child_id, "unpublished")
            with self.assertRaises(self.auth.business_cards.CardError):
                self.auth.business_cards.public(c, mine["public_id"])
            self.assertEqual(self.auth.business_cards.mine(c, child_id)["headline"], "设计师")
        result, err = self.auth.register_account("badcard", "secret123", card={"headline": []})
        self.assertIsNone(result); self.assertEqual(err["code"], "invalid_headline")
        with self.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM users WHERE username='badcard'").fetchone())

    def test_card_attribution_is_owner_bound_and_expires_server_side(self):
        with self.conn() as c:
            root_id = self.uid(c, "root")
            card = self.auth.business_cards.create_draft(c, root_id)
            self.auth.business_cards.publish(c, root_id, "published")
            code = self.auth.invites.ensure_user_code(c, root_id, enforce_membership=False)["code"]
            token = self.auth.business_cards.attribution_token(code, card["public_id"], root_id, self.auth.INVITE_HASH_SECRET)
        result, err = self.auth.register_account("attributed", "secret123", invite_code=code, card={}, invite_attribution_token=token)
        self.assertIsNone(err); self.assertTrue(result["invite_bound"])
        stale = self.auth.business_cards.attribution_token(code, card["public_id"], root_id, self.auth.INVITE_HASH_SECRET, now=100)
        with self.assertRaises(self.auth.business_cards.CardError):
            self.auth.business_cards.verify_attribution(stale, self.auth.INVITE_HASH_SECRET)

    def test_network_masks_undiscoverable_and_stops_cycles(self):
        with self.conn() as c:
            root_id, child_id = self.uid(c, "root"), self.uid(c, "child")
            self.auth.business_cards.create_draft(c, root_id)
            self.auth.business_cards.publish(c, root_id, "published")
            tree = self.auth.business_cards.children(c, root_id)
            self.assertEqual(tree["items"][0]["name"], "匿名用户")
            c.execute("UPDATE business_cards SET status='published',discoverable_in_network=1 WHERE user_id=?", (child_id,))
            c.execute("UPDATE user_invites SET inviter_user_id=? WHERE invitee_user_id=?", (child_id, root_id))
            self.assertLessEqual(len(self.auth.business_cards.ancestors(c, root_id)), 100)

    def test_two_renewals_reward_independently_without_points_or_voice_grant(self):
        now = int(time.time())
        with self.conn() as c:
            child_id = self.uid(c, "child")
            c.execute("UPDATE users SET membership_tier='experience',membership_started_at=?,membership_expires_at=? WHERE id=?", (now - 10, now + 50, child_id))
            self.auth._activate_experience_membership(c, "child", "system", "renew", now, "renew-1", renewal=True)
            first = c.execute("SELECT membership_expires_at FROM users WHERE id=?", (child_id,)).fetchone()[0]
            self.auth._activate_experience_membership(c, "child", "system", "renew", now + 1, "renew-2", renewal=True)
            second = c.execute("SELECT membership_expires_at FROM users WHERE id=?", (child_id,)).fetchone()[0]
            rewards = c.execute("SELECT event_type,reward_points FROM invite_reward_point_records ORDER BY id").fetchall()
            self.assertEqual(second, first + self.auth.MEMBERSHIP_YEAR_SECONDS)
            self.assertEqual([(r["event_type"], r["reward_points"]) for r in rewards], [("renewal", 200), ("renewal", 200)])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM points_audit WHERE username='child'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM membership_voice_slot_entitlements WHERE username='child'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
