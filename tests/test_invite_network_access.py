import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from server import business_cards, invite_network, invites


NOW = 1_800_000_000
SECRET = "network-test-secret"


class InviteNetworkAccessTests(unittest.TestCase):
    def test_auth_server_supports_direct_module_imports(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "server")
        checked = subprocess.run(
            [sys.executable, "-c", "import invite_network; import auth_server"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

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
        business_cards.init_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO users(id,username,display_name,membership_tier,membership_expires_at) VALUES(?,?,?,?,?)",
            [
                (1, "root", "根用户", "partner", NOW + 999999),
                (2, "viewer", "查看者", "", None),
                (3, "child", "下线", "experience", NOW + 999999),
                (4, "grandchild", "下线的下线", "experience", NOW + 999999),
            ],
        )
        self.conn.executemany("""INSERT INTO user_invites(
            campaign_id,inviter_user_id,invitee_user_id,invite_code,source,status,
            risk_status,bound_at,updated_at
        ) VALUES(1,?,?,?,'admin','bound','normal',?,?)""", [
            (1, 2, "VIEW22", NOW, NOW),
            (2, 3, "CHILD3", NOW, NOW),
            (3, 4, "GRAND4", NOW, NOW),
        ])
        card = business_cards.create_draft(
            self.conn, 3, {"name": "下线名片", "title": "设计师", "company": "黄雀"}, NOW,
        )
        business_cards.publish(self.conn, 3, "published", NOW)
        self.child_card_id = card["public_id"]

    def tearDown(self):
        self.conn.close()

    def test_nonmember_sees_own_children_but_cannot_open_network(self):
        page = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )
        self.assertFalse(page["can_browse_network"])
        self.assertEqual(page["items"][0]["username"], "child")
        self.assertEqual(page["items"][0]["membership_name"], "体验官")
        self.assertEqual(page["items"][0]["card_public_id"], self.child_card_id)
        self.assertEqual(page["items"][0]["name"], "下线名片")
        self.assertEqual(page["items"][0]["title"], "设计师")
        self.assertEqual(page["items"][0]["avatar"], "")
        self.assertEqual(page["items"][0]["node_grant"], "")

    def test_member_grant_opens_one_layer_and_allows_parent_and_child_navigation(self):
        self.conn.execute(
            "UPDATE users SET membership_tier='experience',membership_expires_at=? WHERE id=2",
            (NOW + 999999,),
        )
        home = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )
        opened = invite_network.network_page(
            self.conn, 2, home["items"][0]["node_grant"], SECRET,
            cursor=0, limit=20, now=NOW,
        )
        self.assertEqual(opened["node"]["username"], "child")
        self.assertEqual(opened["parent"]["username"], "viewer")
        self.assertEqual(opened["items"][0]["username"], "grandchild")
        parent = invite_network.network_page(
            self.conn, 2, opened["parent"]["node_grant"], SECRET,
            cursor=0, limit=20, now=NOW,
        )
        self.assertEqual(parent["node"]["username"], "viewer")

    def test_grants_are_viewer_bound_and_expire(self):
        grant = invite_network.issue_node_grant(2, 3, SECRET, NOW)
        self.assertEqual(invite_network.verify_node_grant(grant, 2, SECRET, NOW + 1), 3)
        self.assertIsNone(invite_network.verify_node_grant(grant, 1, SECRET, NOW + 1))
        self.assertIsNone(invite_network.verify_node_grant(grant, 2, SECRET, NOW + 601))

    def test_other_user_rows_have_only_the_allowed_public_fields(self):
        self.conn.execute(
            "UPDATE users SET membership_tier='experience',membership_expires_at=? WHERE id=2",
            (NOW + 999999,),
        )
        item = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )["items"][0]
        self.assertEqual(set(item), {
            "username", "membership_tier", "membership_name", "relation",
            "card_available", "card_public_id", "name", "title", "avatar",
            "node_grant", "reward_points",
            "reward_status", "reward_expires_at",
        })
        self.assertNotIn("phone", item)
        self.assertNotIn("email", item)
        self.assertNotIn("address", item)
        self.assertNotIn("wechat_qr", item)

    def test_unpublished_card_does_not_expose_profile_fields(self):
        self.conn.execute(
            "UPDATE business_cards SET status='unpublished' WHERE user_id=3"
        )
        item = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )["items"][0]
        self.assertFalse(item["card_available"])
        self.assertEqual(item["card_public_id"], "")
        self.assertEqual(item["name"], "")
        self.assertEqual(item["title"], "")
        self.assertEqual(item["avatar"], "")

    def test_inactive_card_owner_does_not_expose_profile_fields(self):
        self.conn.execute(
            "UPDATE users SET account_status='disabled' WHERE id=3"
        )
        item = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )["items"][0]
        self.assertFalse(item["card_available"])
        self.assertEqual(item["card_public_id"], "")
        self.assertEqual(item["name"], "")
        self.assertEqual(item["title"], "")
        self.assertEqual(item["avatar"], "")

    def test_undiscoverable_card_does_not_expose_profile_fields(self):
        self.conn.execute(
            "UPDATE users SET membership_tier='experience',membership_expires_at=? WHERE id=2",
            (NOW + 999999,),
        )
        self.conn.execute(
            "UPDATE business_cards SET discoverable_in_network=0 WHERE user_id=3"
        )
        downline = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )["items"][0]
        grant = invite_network.issue_node_grant(2, 3, SECRET, NOW)
        network_node = invite_network.network_page(
            self.conn, 2, grant, SECRET, cursor=0, limit=20, now=NOW,
        )["node"]
        for item in (downline, network_node):
            self.assertFalse(item["card_available"])
            self.assertEqual(item["card_public_id"], "")
            self.assertEqual(item["name"], "")
            self.assertEqual(item["title"], "")
            self.assertEqual(item["avatar"], "")

    def test_partner_reward_display_is_zero_even_when_real_ledger_exists(self):
        self.conn.execute("""INSERT INTO membership_upgrade_records(
            id,user_id,from_level,to_level,source,status,created_at,event_type
        ) VALUES(10,3,'','experience','admin','effective',?,'upgrade')""", (NOW,))
        relation_id = self.conn.execute(
            "SELECT id FROM user_invites WHERE invitee_user_id=3"
        ).fetchone()[0]
        self.conn.execute("""INSERT INTO invite_reward_point_records(
            invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
            inviter_level_snapshot,invitee_level,reward_points,reward_total_after,
            status,created_at,event_type
        ) VALUES(?,10,2,3,'partner','experience',240,240,'recorded',?,'upgrade')""", (relation_id, NOW))
        self.conn.execute(
            "UPDATE users SET membership_tier='partner',membership_expires_at=? WHERE id=2",
            (NOW + 999999,),
        )
        page = invite_network.downlines_page(
            self.conn, 2, SECRET, cursor=0, limit=20, now=NOW,
        )
        self.assertEqual(page["total_reward_points"], 0)
        self.assertEqual(page["items"][0]["reward_points"], 0)


if __name__ == "__main__":
    unittest.main()
