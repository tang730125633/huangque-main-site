import os
import sqlite3
import tempfile
import unittest

from scripts.process_invite_reward_claims import process
from server import invites


NOW = 1_800_000_000


class InviteRewardClaimTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE users(
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT,
            membership_tier TEXT NOT NULL DEFAULT '',
            membership_expires_at INTEGER,
            account_status TEXT NOT NULL DEFAULT 'active'
        )""")
        invites.init_schema(self.conn, now=NOW)
        self.conn.execute(
            "INSERT INTO users(id,username,membership_tier,membership_expires_at) VALUES(1,'inviter','',NULL)"
        )
        self.conn.execute(
            "INSERT INTO users(id,username,membership_tier,membership_expires_at) VALUES(2,'invitee','partner',?)",
            (NOW + 1000,),
        )
        self.conn.execute("""INSERT INTO user_invites(
            campaign_id,inviter_user_id,invitee_user_id,invite_code,source,
            status,risk_status,bound_at,updated_at
        ) VALUES(1,1,2,'ABC234','admin','bound','normal',?,?)""", (NOW, NOW))

    def tearDown(self):
        self.conn.close()

    def test_schema_adds_reward_claim_lifecycle_table(self):
        columns = {
            row["name"] for row in self.conn.execute(
                "PRAGMA table_info(invite_reward_claims)"
            ).fetchall()
        }
        self.assertTrue({
            "upgrade_record_id", "direct_inviter_user_id", "invitee_user_id",
            "target_level", "status", "expires_at", "recipient_user_id",
            "recipient_level_snapshot", "reward_points", "transfer_depth",
            "settled_at", "voided_at", "reason",
        }.issubset(columns))

    def test_lower_tier_inviter_gets_seven_day_upgrade_claim(self):
        result = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-1", now=NOW,
        )
        claim = self.conn.execute(
            "SELECT * FROM invite_reward_claims WHERE upgrade_record_id=?",
            (result["upgrade_record_id"],),
        ).fetchone()
        self.assertIsNotNone(claim)
        self.assertEqual(claim["status"], "pending_upgrade")
        self.assertEqual(claim["target_level"], "partner")
        self.assertEqual(claim["expires_at"], NOW + 7 * 24 * 3600)
        self.assertEqual(claim["reward_points"], 1500)
        self.assertIsNone(result["reward"])

    def test_minimum_reward_points_uses_required_tier(self):
        self.assertEqual(invites.minimum_reward_points("experience"), 200)
        self.assertEqual(invites.minimum_reward_points("partner"), 1500)
        self.assertEqual(invites.minimum_reward_points("initiator"), 15000)

    def test_upgrade_unlocks_pending_claim_and_writes_ledger_once(self):
        created = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-unlock", now=NOW,
        )
        self.conn.execute(
            "UPDATE users SET membership_tier='partner',membership_expires_at=? WHERE id=1",
            (NOW + 1000,),
        )
        first = invites.settle_pending_for_user(self.conn, 1, now=NOW + 10)
        second = invites.settle_pending_for_user(self.conn, 1, now=NOW + 11)
        claim = self.conn.execute(
            "SELECT * FROM invite_reward_claims WHERE upgrade_record_id=?",
            (created["upgrade_record_id"],),
        ).fetchone()
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["total_points"], 1500)
        self.assertEqual(second["count"], 0)
        self.assertEqual(claim["status"], "credited")
        self.assertEqual(claim["recipient_user_id"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM invite_reward_point_records WHERE claim_id=?",
            (claim["id"],),
        ).fetchone()[0], 1)

    def test_eligible_direct_reward_has_credited_claim_audit(self):
        self.conn.execute(
            "UPDATE users SET membership_tier='partner',membership_expires_at=? WHERE id=1",
            (NOW + 1000,),
        )
        first = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-direct", now=NOW,
        )
        second = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-direct", now=NOW + 1,
        )
        claim = self.conn.execute(
            "SELECT * FROM invite_reward_claims WHERE upgrade_record_id=?",
            (first["upgrade_record_id"],),
        ).fetchone()
        self.assertIsNotNone(claim)
        self.assertEqual(claim["status"], "credited")
        self.assertEqual(claim["recipient_user_id"], 1)
        self.assertEqual(claim["reward_points"], 1500)
        self.assertEqual(second["claim"]["id"], claim["id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM invite_reward_point_records WHERE claim_id=?",
            (claim["id"],),
        ).fetchone()[0], 1)

    def test_expiry_skips_ineligible_ancestors_and_transfers_to_first_eligible(self):
        created = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-transfer", now=NOW,
        )
        self.conn.executemany(
            "INSERT INTO users(id,username,membership_tier,membership_expires_at) VALUES(?,?,?,?)",
            [
                (3, "expired", "partner", NOW - 1),
                (4, "too-low", "experience", NOW + 999999),
                (5, "eligible", "initiator", NOW + 999999),
            ],
        )
        self.conn.executemany("""INSERT INTO user_invites(
            campaign_id,inviter_user_id,invitee_user_id,invite_code,source,
            status,risk_status,bound_at,updated_at
        ) VALUES(1,?,?,?,'admin','bound','normal',?,?)""", [
            (3, 1, "EXPRD3", NOW, NOW),
            (4, 3, "LOW444", NOW, NOW),
            (5, 4, "HIGH55", NOW, NOW),
        ])
        summary = invites.expire_pending_claims(
            self.conn, now=NOW + invites.REWARD_CLAIM_TTL_SECONDS + 1,
        )
        claim = self.conn.execute(
            "SELECT * FROM invite_reward_claims WHERE upgrade_record_id=?",
            (created["upgrade_record_id"],),
        ).fetchone()
        self.assertEqual(summary["transferred"], 1)
        self.assertEqual(claim["status"], "transferred")
        self.assertEqual(claim["recipient_user_id"], 5)
        self.assertEqual(claim["recipient_level_snapshot"], "initiator")
        self.assertEqual(claim["reward_points"], 2500)
        self.assertEqual(claim["transfer_depth"], 3)

    def test_expiry_without_eligible_ancestor_records_no_recipient(self):
        created = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-no-recipient", now=NOW,
        )
        summary = invites.expire_pending_claims(
            self.conn, now=NOW + invites.REWARD_CLAIM_TTL_SECONDS + 1,
        )
        claim = self.conn.execute(
            "SELECT status FROM invite_reward_claims WHERE upgrade_record_id=?",
            (created["upgrade_record_id"],),
        ).fetchone()
        self.assertEqual(summary["no_recipient"], 1)
        self.assertEqual(claim["status"], "no_recipient")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM invite_reward_point_records"
        ).fetchone()[0], 0)

    def test_expiry_processor_runs_against_an_existing_database(self):
        invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-script", now=NOW,
        )
        self.conn.commit()
        handle, database = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            target = sqlite3.connect(database)
            self.conn.backup(target)
            target.close()
            result = process(
                database, now=NOW + invites.REWARD_CLAIM_TTL_SECONDS + 1, limit=10,
            )
            check = sqlite3.connect(database)
            status = check.execute(
                "SELECT status FROM invite_reward_claims WHERE source_order_id='membership-script'"
            ).fetchone()[0]
            check.close()
            self.assertEqual(result["processed"], 1)
            self.assertEqual(status, "no_recipient")
        finally:
            os.unlink(database)

    def test_voided_pending_claim_never_settles_or_transfers(self):
        created = invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-refund", now=NOW,
        )
        changed = invites.void_claims_for_upgrade(
            self.conn, created["upgrade_record_id"], "membership_refund", NOW + 1,
        )
        self.conn.execute(
            "UPDATE users SET membership_tier='partner',membership_expires_at=? WHERE id=1",
            (NOW + 999999,),
        )
        unlocked = invites.settle_pending_for_user(self.conn, 1, NOW + 2)
        expired = invites.expire_pending_claims(
            self.conn, NOW + invites.REWARD_CLAIM_TTL_SECONDS + 1,
        )
        claim = self.conn.execute(
            "SELECT status,reason FROM invite_reward_claims WHERE upgrade_record_id=?",
            (created["upgrade_record_id"],),
        ).fetchone()
        self.assertEqual(changed, 1)
        self.assertEqual(unlocked["count"], 0)
        self.assertEqual(expired["processed"], 0)
        self.assertEqual((claim["status"], claim["reason"]), ("voided", "membership_refund"))

    def test_pending_notice_repeats_only_on_a_later_shanghai_day(self):
        invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-reminder", now=NOW,
        )
        first = invites.next_reward_notice(self.conn, 1, now=NOW)
        self.assertEqual(first["notice_type"], "pending_upgrade")
        self.assertEqual(first["required_tier"], "partner")
        invites.ack_reward_notice(self.conn, 1, first["id"], now=NOW)
        self.assertIsNone(invites.next_reward_notice(self.conn, 1, now=NOW + 3600))
        next_day = invites.next_reward_notice(self.conn, 1, now=NOW + 24 * 3600)
        self.assertEqual(next_day["id"], first["id"])

    def test_reward_unlock_notice_is_shown_once(self):
        invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-feedback", now=NOW,
        )
        self.conn.execute(
            "UPDATE users SET membership_tier='partner',membership_expires_at=? WHERE id=1",
            (NOW + 999999,),
        )
        result = invites.settle_pending_for_user(self.conn, 1, now=NOW + 1)
        notice = invites.next_reward_notice(self.conn, 1, now=NOW + 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(notice["notice_type"], "reward_unlocked")
        self.assertEqual(notice["claim_count"], 1)
        self.assertEqual(notice["total_points"], 1500)
        invites.ack_reward_notice(self.conn, 1, notice["id"], now=NOW + 1)
        self.assertIsNone(invites.next_reward_notice(self.conn, 1, now=NOW + 2))

    def test_admin_claim_query_keeps_real_lifecycle_details(self):
        invites.record_membership_upgrade(
            self.conn, 2, "experience", "partner", "admin",
            source_order_id="membership-admin-view", now=NOW,
        )
        self.conn.execute("UPDATE users SET username='13800000031' WHERE id=1")
        data = invites.admin_reward_claims(
            self.conn, filters={"status": "pending_upgrade"}, limit=20, offset=0,
        )
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["direct_inviter_username"], "138****0031")
        self.assertEqual(item["invitee_username"], "invitee")
        self.assertEqual(item["target_level"], "partner")
        self.assertEqual(item["reward_points"], 1500)
        self.assertEqual(item["status"], "pending_upgrade")


if __name__ == "__main__":
    unittest.main()
