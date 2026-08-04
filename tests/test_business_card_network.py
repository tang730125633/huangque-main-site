import base64
import importlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image


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
        self.child, err = self.auth.register_account(
            "child", "secret123", "子用户", invite_code=code,
            card={"name": "子用户", "title": "设计师", "company": "黄雀"},
        )
        self.assertIsNone(err)
        self.assertEqual(self.child["card"]["title"], "设计师")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def conn(self):
        c = sqlite3.connect(self.auth.DB); c.row_factory = sqlite3.Row
        return c

    def request(self, path, payload, headers=None, method="POST"):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

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
            self.auth.business_cards.update(c, child_id, {"phone": "13800000000", "privacy": {"phone": False}})
            self.auth.business_cards.update(c, child_id, {"avatar": "cards/forged.jpg", "wechat_qr": "cards/forged.jpg"})
            self.assertEqual(self.auth.business_cards.mine(c, child_id)["avatar"], "")
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
            card = self.auth.business_cards.create_draft(c, root_id, {"name": "根用户", "title": "老师", "company": "黄雀"})
            self.auth.business_cards.publish(c, root_id, "published")
            code = self.auth.invites.ensure_user_code(c, root_id, enforce_membership=False)["code"]
            token = self.auth.business_cards.attribution_token(code, card["public_id"], root_id, self.auth.INVITE_HASH_SECRET)
        result, err = self.auth.register_account("attributed", "secret123", invite_code=code, card={}, invite_attribution_token=token)
        self.assertIsNone(err); self.assertTrue(result["invite_bound"])
        with self.conn() as c:
            self.auth.business_cards.publish(c, root_id, "unpublished")
        result, err = self.auth.register_account("revoked", "secret123", invite_code=code, card={}, invite_attribution_token=token)
        self.assertIsNone(result); self.assertEqual(err["code"], "invalid_invite_attribution")
        with self.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM users WHERE username='revoked'").fetchone())
        stale = self.auth.business_cards.attribution_token(code, card["public_id"], root_id, self.auth.INVITE_HASH_SECRET, now=100)
        with self.assertRaises(self.auth.business_cards.CardError):
            self.auth.business_cards.verify_attribution(stale, self.auth.INVITE_HASH_SECRET)

    def test_network_masks_undiscoverable_and_stops_cycles(self):
        with self.conn() as c:
            root_id, child_id = self.uid(c, "root"), self.uid(c, "child")
            self.auth.business_cards.create_draft(c, root_id, {"name": "根用户", "title": "老师", "company": "黄雀"})
            self.auth.business_cards.publish(c, root_id, "published")
            tree = self.auth.business_cards.children(c, root_id)
            self.assertEqual(tree["items"][0]["name"], "匿名用户")
            self.assertTrue(tree["items"][0]["node_id"])
            self.assertIn("children_count", tree["items"][0])
            self.assertEqual(tree["next_before_id"], tree["next_cursor"])
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

    def test_wechat_card_identity_bridge_keeps_existing_account_and_invites_unchanged(self):
        with self.conn() as c:
            root_id = self.uid(c, "root")
            card = self.auth.business_cards.create_draft(c, root_id, {"name": "根用户", "title": "老师", "company": "黄雀"})
            before_points = c.execute("SELECT points FROM users WHERE id=?", (root_id,)).fetchone()[0]
            before_invites = c.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0]
        token = self.auth.issue_token("root")
        headers = {"Authorization": "Bearer " + token}
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-root"}):
            status, body = self.request("/api/auth/miniprogram/card-login", {"wx_code": "before-bind"})
            self.assertEqual(status, 404)
            self.assertEqual(body["code"], "card_unbound")
            status, body = self.request("/api/auth/card/wechat/bind", {"wx_code": "bind"}, headers)
            self.assertEqual(status, 200)
            self.assertTrue(body["wechat_bound"])
            self.assertEqual(body["card"]["public_id"], card["public_id"])
            self.assertTrue(body["card"]["initial_password"])
            self.assertNotIn("password", body["card"])
            self.assertEqual(self.request("/api/auth/card/wechat/bind", {"wx_code": "retry"}, headers)[0], 200)
            status, body = self.request("/api/auth/miniprogram/card-login", {"wx_code": "login"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["username"], "root")
        self.assertEqual(body["user"]["points"], before_points)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0], before_invites)
            self.assertEqual(c.execute("SELECT miniprogram_openid FROM business_cards WHERE user_id=?", (root_id,)).fetchone()[0], "openid-root")

    def test_card_session_is_independent_until_explicit_account_login(self):
        with self.conn() as c:
            root_id = self.uid(c, "root")
            self.auth.business_cards.create_draft(c, root_id, {"name": "根用户", "title": "老师", "company": "黄雀"})
        account_headers = {"Authorization": "Bearer " + self.auth.issue_token("child")}
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-root"}):
            self.assertEqual(self.request(
                "/api/auth/card/wechat/bind", {"wx_code": "bind"},
                {"Authorization": "Bearer " + self.auth.issue_token("root")},
            )[0], 200)
            status, session = self.request("/api/auth/miniprogram/card-session", {"wx_code": "session"})
        self.assertEqual(status, 200)
        self.assertIn("card_token", session)
        self.assertNotIn("token", session)

        card_headers = {"X-HQ-Card-Token": session["card_token"]}
        self.assertEqual(self.get("/api/auth/me", card_headers)[0], 401)
        status, mine = self.get("/api/auth/card/me", {**account_headers, **card_headers})
        self.assertEqual(status, 200)
        self.assertEqual(mine["card"]["name"], "根用户")

        status, login = self.request("/api/auth/miniprogram/card-account-login", {}, card_headers)
        self.assertEqual(status, 200)
        self.assertEqual(login["user"]["username"], "root")
        self.assertEqual(self.get("/api/auth/me", {"Authorization": "Bearer " + login["token"]})[0], 200)

        self.assertEqual(self.request("/api/auth/change_password", {
            "old_password": "secret123", "new_password": "changed123",
        }, card_headers)[0], 200)
        self.assertEqual(self.get("/api/auth/me", {"Authorization": "Bearer " + login["token"]})[0], 401)
        self.assertEqual(self.get("/api/auth/me", account_headers)[0], 200)
        self.assertEqual(self.get("/api/auth/card/me", card_headers)[0], 200)

        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-new-card"}):
            status, registered = self.request("/api/auth/miniprogram/card-register", {
                "wx_code": "register", "phone": "13800000008", "device_id": "separate-device",
                "card": {"name": "新名片", "title": "设计师", "company": "黄雀"},
                "separate_sessions": True,
            })
        self.assertEqual(status, 200)
        self.assertIn("card_token", registered)
        self.assertNotIn("token", registered)
        self.assertEqual(self.get("/api/auth/me", {"Authorization": "Bearer " + registered["card_token"]})[0], 401)

    def test_wechat_card_register_rewards_only_valid_attribution_and_is_idempotent(self):
        with self.conn() as c:
            root_id = self.uid(c, "root")
            root_card = self.auth.business_cards.create_draft(c, root_id, {"name": "根用户", "title": "老师", "company": "黄雀"})
            self.auth.business_cards.publish(c, root_id, "published")
            code = self.auth.invites.ensure_user_code(c, root_id, enforce_membership=False)["code"]
        status, shared = self.get("/api/auth/card/public?id=%s&invite=%s" % (root_card["public_id"], code))
        self.assertEqual(status, 200)
        self.assertTrue(shared["invite_valid"])
        attribution = shared["invite_attribution_token"]
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM card_referral_journeys").fetchone()[0], 0)
        status, started = self.request("/api/auth/invite/journey/start", {
            "invite_attribution_token": attribution,
        })
        self.assertEqual(status, 200)
        self.assertTrue(started["started"])
        payload = {
            "wx_code": "first", "phone": "13800000001", "device_id": "device-1",
            "card": {"name": "新用户", "title": "设计师", "company": "黄雀"},
            "invite_code": code, "invite_attribution_token": attribution,
        }
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-new"}):
            status, first = self.request("/api/auth/miniprogram/card-register", payload)
            self.assertEqual(status, 200)
            self.assertEqual(first["user"]["points"], 100)
            self.assertTrue(first["invite_bound"])
            self.assertTrue(first["created"])
            self.assertTrue(first["invite_rewarded"])
            self.assertEqual(first["ai_account"], "13800000001")
            self.assertTrue(first["initial_password"])
            headers = {"Authorization": "Bearer " + first["token"]}
            status, published = self.request("/api/auth/card/publish", {}, headers)
            self.assertEqual(status, 200)
            status, public = self.get(
                "/api/auth/card/public?id=%s&invite=%s" % (
                    published["card"]["public_id"], published["card"]["invite_code"],
                ),
                {"Authorization": "Bearer stale-owner-token"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(public["invite_valid"])
            for forbidden in ("username", "ai_account", "initial_password", "password", "pw_hash", "pw_salt"):
                self.assertNotIn(forbidden, public["card"])
            status, replay = self.request("/api/auth/miniprogram/card-register", payload)
        self.assertEqual(status, 200)
        self.assertEqual(replay["user"]["username"], "13800000001")
        self.assertTrue(replay["invite_bound"])
        self.assertFalse(replay["created"])
        self.assertFalse(replay["invite_rewarded"])
        self.assertEqual(replay["ai_account"], "13800000001")
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE username='13800000001'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT points FROM users WHERE username='13800000001'").fetchone()[0], 100)
            relation = c.execute("SELECT ui.* FROM user_invites ui JOIN users u ON u.id=ui.invitee_user_id WHERE u.username='13800000001'").fetchone()
            self.assertEqual(relation["source"], "miniprogram_card")
            journey = c.execute("SELECT * FROM card_referral_journeys WHERE registered_user_id=?", (relation["invitee_user_id"],)).fetchone()
            self.assertIsNotNone(journey["card_started_at"])
            self.assertEqual(journey["invite_relation_id"], relation["id"])
            audit = c.execute("SELECT * FROM points_audit WHERE transaction_key=?", ("card-referral:" + journey["journey_id"],)).fetchone()
            self.assertEqual(audit["delta"], 100)
            funnel = self.auth.invites.admin_referral_journeys(c, {"user": "13800000001"})
            self.assertEqual(funnel["summary"]["registered"], 1)
            self.assertEqual(funnel["summary"]["trial_rewarded"], 1)
            self.assertEqual(funnel["summary"]["published"], 1)
            self.assertNotIn("invitee_username", funnel["items"][0])
            self.assertEqual(funnel["items"][0]["invitee_account"], "138****0001")
        self.assertEqual(self.request("/api/auth/card/unpublish", {}, headers)[0], 200)
        with self.conn() as c:
            self.assertEqual(self.auth.invites.admin_referral_journeys(c, {"user": "13800000001"})["summary"]["published"], 1)
        reused = dict(payload)
        reused["phone"] = "13800000003"
        reused["wx_code"] = "reused-attribution"
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-reused-attribution"}):
            status, rejected = self.request("/api/auth/miniprogram/card-register", reused)
        self.assertEqual(status, 409)
        self.assertEqual(rejected["code"], "invite_journey_used")
        with self.conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM users WHERE username='13800000003'").fetchone())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM points_audit WHERE reason='名片邀请注册奖励'").fetchone()[0], 1)
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-no-attribution"}):
            status, uncredited = self.request("/api/auth/miniprogram/card-register", {
                "wx_code": "second", "phone": "13800000002", "device_id": "device-2",
                "card": {"name": "无归因", "title": "设计师", "company": "黄雀"},
            })
        self.assertEqual(status, 200)
        self.assertEqual(uncredited["user"]["points"], 0)
        self.assertTrue(uncredited["created"])
        self.assertFalse(uncredited["invite_rewarded"])
        legacy_token = self.auth.business_cards.attribution_token(
            code, root_card["public_id"], root_id, self.auth.INVITE_HASH_SECRET,
        )
        for suffix in ("6", "7"):
            with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-legacy-" + suffix}):
                status, legacy = self.request("/api/auth/miniprogram/card-register", {
                    "wx_code": "legacy-" + suffix, "phone": "1380000000" + suffix,
                    "device_id": "legacy-device-" + suffix,
                    "card": {"name": "旧令牌用户", "title": "设计师", "company": "黄雀"},
                    "invite_code": code, "invite_attribution_token": legacy_token,
                })
            self.assertEqual(status, 200)
            self.assertTrue(legacy["invite_bound"])
            self.assertEqual(legacy["user"]["points"], 0)
            self.assertFalse(legacy["invite_rewarded"])
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM points_audit WHERE reason='名片邀请注册奖励'").fetchone()[0], 1)

    def test_wechat_card_registration_requires_complete_card_and_uses_account_phone(self):
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-required"}):
            status, body = self.request("/api/auth/miniprogram/card-register", {
                "wx_code": "missing", "phone": "13800000004", "device_id": "device-4",
                "card": {"name": "", "title": "设计师", "company": "黄雀"},
            })
            self.assertEqual(status, 400)
            self.assertEqual(body["code"], "incomplete_card")
            status, body = self.request("/api/auth/miniprogram/card-register", {
                "wx_code": "valid", "phone": "13800000004", "device_id": "device-4",
                "card": {"name": "新用户", "title": "设计师", "company": "黄雀", "phone": "13900000000"},
            })
        self.assertEqual(status, 200)
        self.assertEqual(body["card"]["phone"], "13800000004")

    def test_card_initial_password_allows_ai_but_blocks_new_orders_until_changed(self):
        phone = "13800000005"
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-password"}):
            status, registered = self.request("/api/auth/miniprogram/card-register", {
                "wx_code": "register", "phone": phone, "device_id": "device-5",
                "card": {"name": "改密用户", "title": "设计师", "company": "黄雀"},
            })
        self.assertEqual(status, 200)
        self.assertFalse(registered["user"]["must_change"])
        self.assertTrue(registered["card"]["initial_password"])
        headers = {"Authorization": "Bearer " + registered["token"]}
        status, me = self.get("/api/auth/me", headers)
        self.assertEqual(status, 200)
        self.assertTrue(me["user"]["initial_password"])

        for path in (
            "/api/auth/virtual-pay/order", "/api/auth/recharge/order",
            "/api/auth/wxpay/native", "/api/auth/wxpay/jsapi",
        ):
            status, blocked = self.request(path, {}, headers)
            self.assertEqual(status, 403, path)
            self.assertEqual(blocked["code"], "initial_password_change_required", path)

        legacy_headers = {"Authorization": "Bearer " + self.auth.issue_token("root")}
        status, blocked = self.request("/api/auth/recharge/order", {}, legacy_headers)
        self.assertEqual(status, 403)
        self.assertEqual(blocked["code"], "initial_password_change_required")

        status, _ = self.request("/api/auth/change_password", {
            "old_password": phone, "new_password": "changed123",
        }, headers)
        self.assertEqual(status, 200)
        with self.conn() as c:
            row = c.execute(
                "SELECT must_change,card_initial_password FROM users WHERE username=?", (phone,),
            ).fetchone()
            self.assertEqual((row["must_change"], row["card_initial_password"]), (0, 0))

        new_headers = {"Authorization": "Bearer " + self.auth.issue_token(phone)}
        status, me = self.get("/api/auth/me", new_headers)
        self.assertEqual(status, 200)
        self.assertFalse(me["user"]["initial_password"])
        order, err = self.auth.create_recharge_order(phone, 10, 100, "已创建订单")
        self.assertIsNone(err)
        with self.conn() as c:
            c.execute("UPDATE users SET card_initial_password=1 WHERE username=?", (phone,))
        approved, err = self.auth.review_recharge_order("admin", order["order_id"], "approve", "到账")
        self.assertIsNone(err)
        self.assertEqual(approved["status"], "approved")

    def test_wechat_card_registration_never_claims_existing_account_or_other_card_openid(self):
        self.auth.create_user("13800000003", "secret123")
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-conflict-account"}):
            status, body = self.request("/api/auth/miniprogram/card-register", {
                "wx_code": "conflict", "phone": "13800000003", "device_id": "device-3",
                "card": {"name": "冲突", "title": "设计师", "company": "黄雀"},
            })
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "account_exists")
        with self.conn() as c:
            self.assertFalse(c.execute("SELECT 1 FROM business_cards WHERE miniprogram_openid='openid-conflict-account'").fetchone())
        root_token = self.auth.issue_token("root")
        child_token = self.auth.issue_token("child")
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "openid-taken"}):
            self.assertEqual(self.request("/api/auth/card/wechat/bind", {"wx_code": "root"}, {"Authorization": "Bearer " + root_token})[0], 200)
            status, body = self.request("/api/auth/card/wechat/bind", {"wx_code": "child"}, {"Authorization": "Bearer " + child_token})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "openid_in_use")

    def test_work_image_upload_preserves_title_and_refreshes_private_url(self):
        headers = {"Authorization": "Bearer " + self.child["token"]}
        with self.conn() as c:
            child_id = self.uid(c, "child")
        image = io.BytesIO()
        Image.new("RGB", (2, 2), (30, 60, 90)).save(image, "PNG")
        image_data = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
        signed = []

        def signed_url(key):
            if not key:
                return ""
            signed.append(key)
            return "https://signed.example/%s?refresh=%s" % (key, len(signed))

        with patch.object(self.auth.business_cards.miniprogram_security, "check_image"), \
             patch("server.content_domains.cos.enabled", return_value=True), \
             patch("server.content_domains.cos.put_bytes") as put_bytes, \
             patch.object(self.auth.business_cards, "_media_url", side_effect=signed_url):
            status, first = self.request("/api/auth/card/media", {
                "field": "work_image_2", "title": "作品二", "data": image_data,
            }, headers)
            self.assertEqual(status, 200)
            self.assertEqual(first["work"]["slot"], 2)
            self.assertEqual(first["work"]["title"], "作品二")
            self.assertTrue(first["key"].startswith("cards/%s/work_image_2/" % child_id))
            self.assertTrue(first["url"].startswith("https://signed.example/"))
            self.assertTrue(put_bytes.call_args.kwargs["private"])

            status, replaced = self.request("/api/auth/card/media", {
                "field": "work_image_2", "data": image_data,
            }, headers)
            self.assertEqual(status, 200)
            self.assertEqual(replaced["work"]["title"], "作品二")
            self.assertNotEqual(replaced["work"]["key"], first["key"])
            key = replaced["work"]["key"]
            with self.conn() as c:
                stored = json.loads(c.execute(
                    "SELECT works_json FROM business_cards WHERE user_id=?", (child_id,),
                ).fetchone()[0])[0]
            self.assertEqual(stored["key"], key)
            self.assertNotIn("url", stored)

            status, owner = self.get("/api/auth/card/me", headers)
            self.assertEqual(status, 200)
            owner_work = owner["card"]["works"][0]
            self.assertEqual(owner_work["key"], key)
            self.assertTrue(owner_work["url"].startswith("https://signed.example/"))
            self.assertNotEqual(owner_work["url"], replaced["work"]["url"])

            changed = dict(owner_work, title="新标题")
            status, updated = self.request("/api/auth/card/me", {"works": [changed]}, headers, method="PUT")
            self.assertEqual(status, 200)
            self.assertEqual(updated["card"]["works"][0]["title"], "新标题")

            status, bad = self.request("/api/auth/card/me", {"works": [{
                "type": "image", "slot": 2, "key": "cards/999/work_image_2/forged.jpg",
            }]}, headers, method="PUT")
            self.assertEqual(status, 400)
            self.assertEqual(bad["code"], "invalid_work_image")

            status, published = self.request("/api/auth/card/publish", {}, headers)
            self.assertEqual(status, 200)
            status, public = self.get("/api/auth/card/public?id=" + published["card"]["public_id"])
            self.assertEqual(status, 200)
            public_work = public["card"]["works"][0]
            self.assertNotIn("key", public_work)
            self.assertEqual(public_work["title"], "新标题")
            self.assertTrue(public_work["url"].startswith("https://signed.example/"))
            self.assertNotEqual(public_work["url"], owner_work["url"])

    def test_work_image_rejection_names_the_failed_slot(self):
        headers = {"Authorization": "Bearer " + self.child["token"]}
        image = io.BytesIO()
        Image.new("RGB", (2, 2), (30, 60, 90)).save(image, "PNG")
        image_data = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")

        rejected = self.auth.business_cards.miniprogram_security.ContentRejected("内容可能违反平台规范，请修改后再提交")
        with patch.object(self.auth.business_cards.miniprogram_security, "check_image", side_effect=rejected):
            status, body = self.request("/api/auth/card/media", {
                "field": "work_image_2", "data": image_data,
            }, headers)

        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "content_rejected")
        self.assertEqual(body["detail"], "作品图片2未通过微信安全检测：内容可能违反平台规范，请修改后再提交")

    def test_work_video_upload_is_private_persistent_and_publicly_playable(self):
        headers = {"Authorization": "Bearer " + self.child["token"]}
        with self.conn() as c:
            child_id = self.uid(c, "child")
        mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
        video_data = "data:video/mp4;base64," + base64.b64encode(mp4).decode("ascii")

        with patch("server.content_domains.cos.enabled", return_value=True), \
             patch("server.content_domains.cos.put_bytes") as put_bytes, \
             patch.object(self.auth.business_cards, "_media_url", side_effect=lambda key: "https://signed.example/" + key):
            status, uploaded = self.request("/api/auth/card/media", {
                "field": "work_video_3", "title": "品牌故事", "data": video_data,
            }, headers)
            self.assertEqual(status, 200)
            self.assertEqual(uploaded["work"]["type"], "video")
            self.assertEqual(uploaded["work"]["slot"], 3)
            self.assertEqual(uploaded["work"]["title"], "品牌故事")
            self.assertTrue(uploaded["key"].startswith("cards/%s/work_video_3/" % child_id))
            self.assertEqual(put_bytes.call_args.args[2], "video/mp4")
            self.assertTrue(put_bytes.call_args.kwargs["private"])

            status, owner = self.get("/api/auth/card/me", headers)
            self.assertEqual(status, 200)
            owner_video = next(item for item in owner["card"]["works"] if item.get("type") == "video")
            self.assertEqual(owner_video["key"], uploaded["key"])
            self.assertTrue(owner_video["url"].startswith("https://signed.example/"))

            status, bad = self.request("/api/auth/card/me", {"works": [{
                "type": "video", "slot": 3, "key": "cards/999/work_video_3/forged.mp4",
            }]}, headers, method="PUT")
            self.assertEqual(status, 400)
            self.assertEqual(bad["code"], "invalid_work_video")

            status, published = self.request("/api/auth/card/publish", {}, headers)
            self.assertEqual(status, 200)
            status, public = self.get("/api/auth/card/public?id=" + published["card"]["public_id"])
            self.assertEqual(status, 200)
            public_video = next(item for item in public["card"]["works"] if item.get("type") == "video")
            self.assertNotIn("key", public_video)
            self.assertEqual(public_video["title"], "品牌故事")
            self.assertTrue(public_video["url"].startswith("https://signed.example/"))

        status, invalid = self.request("/api/auth/card/media", {
            "field": "work_video_1", "data": "data:video/mp4;base64," + base64.b64encode(b"not-mp4").decode("ascii"),
        }, headers)
        self.assertEqual(status, 400)
        self.assertEqual(invalid["code"], "invalid_video")

    def test_legacy_miniprogram_register_keeps_sixteen_trial_points(self):
        with patch.object(self.auth.wechat_vpay, "code_to_session", return_value={"openid": "unused"}):
            status, body = self.request("/api/auth/miniprogram-register", {"username": "legacy_mp", "password": "secret123"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["points"], 16)


if __name__ == "__main__":
    unittest.main()
