"""登录设备痕迹 + 踢出登录（2026-09-12）。

背景：``tokens`` 表原来只有 token/username/时间/scope —— 出了盗号连"从哪台设备
登的"都查不到；而且旧 token 从不过期，tang1 因此攒了 **99 个全部有效**的登录
（IP12 Agent 每 15 分钟重登一次）。

这次做三件事：
1. 登录时记下 IP / User-Agent；
2. 每账号只保留最近 ``SESSION_KEEP`` 个登录，更早的自动删（多设备共存但不无限增长）；
3. 后台能列出登录设备、能踢单条、能全踢。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# auth_server 以脚本方式从 server/ 启动，测例要把它放进 sys.path（同 test_auth_points）
SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)


class LoginDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server
        self.auth = auth_server
        # 模块在导入时就把 DB 路径固定了，每个用例都要重新指到自己的临时库
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.init_db()
        c = self.auth.db()
        c.execute(
            "INSERT INTO users(username,pw_hash,pw_salt,role,account_status,points)"
            " VALUES('alice','x','s','member','active',0)")
        c.commit()
        c.close()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _issue(self, ip="", ua=""):
        return self.auth.issue_token("alice", ip=ip, ua=ua)

    def _rows(self):
        c = self.auth.db()
        rows = c.execute(
            "SELECT token,ip,ua,last_seen_at FROM tokens WHERE username='alice'"
            " ORDER BY rowid DESC").fetchall()
        c.close()
        return [dict(r) for r in rows]

    # ---- 1. 记录设备 ----

    def test_login_records_ip_and_user_agent(self):
        self._issue(ip="113.65.24.169", ua="Mozilla/5.0 (Macintosh) Chrome/131")
        row = self._rows()[0]
        self.assertEqual(row["ip"], "113.65.24.169")
        self.assertIn("Chrome", row["ua"])
        self.assertTrue(row["last_seen_at"] > 0)

    def test_device_fields_are_truncated_not_unbounded(self):
        """别让客户端用一个超长 UA 把库撑爆。"""
        self._issue(ip="1.2.3.4", ua="A" * 5000)
        row = self._rows()[0]
        self.assertLessEqual(len(row["ua"]), 300)
        self.assertLessEqual(len(row["ip"]), 64)

    def test_missing_device_info_is_empty_not_an_error(self):
        self._issue()          # 内部调用/老代码路径不带 ip/ua
        row = self._rows()[0]
        self.assertEqual(row["ip"], "")
        self.assertEqual(row["ua"], "")

    # ---- 2. 收敛旧登录 ----

    def test_keeps_only_the_most_recent_sessions(self):
        for i in range(self.auth.SESSION_KEEP + 4):
            self._issue(ip="10.0.0.%d" % i)
        rows = self._rows()
        self.assertEqual(len(rows), self.auth.SESSION_KEEP,
                         "每账号应只保留最近 %d 个登录" % self.auth.SESSION_KEEP)
        # 留下的是最新的那几个
        self.assertEqual(rows[0]["ip"], "10.0.0.%d" % (self.auth.SESSION_KEEP + 3))

    def test_trimming_does_not_touch_other_accounts(self):
        c = self.auth.db()
        c.execute("INSERT INTO users(username,pw_hash,pw_salt,role,account_status,points)"
                  " VALUES('bob','x','s','member','active',0)")
        c.commit(); c.close()
        for _ in range(self.auth.SESSION_KEEP + 3):
            self._issue()
        self.auth.issue_token("bob")
        c = self.auth.db()
        bob = c.execute("SELECT COUNT(*) FROM tokens WHERE username='bob'").fetchone()[0]
        c.close()
        self.assertEqual(bob, 1, "收敛只应作用于登录的那个账号")

    # ---- 3. 后台：看设备 / 踢人 ----

    def test_list_sessions_hides_the_raw_token(self):
        raw = self._issue(ip="113.65.24.169", ua="Chrome/Mac")
        data = self.auth.list_user_sessions("alice")
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["ip"], "113.65.24.169")
        self.assertIn("Chrome", item["ua"])
        # 后台只应拿到能定位的摘要，不该拿到完整凭据
        self.assertNotIn(raw, str(item))
        import hashlib
        self.assertEqual(item["token_key"], hashlib.sha256(raw.encode()).hexdigest()[:32])

    def test_revoke_one_session(self):
        a = self._issue(ip="1.1.1.1")
        b = self._issue(ip="2.2.2.2")
        import hashlib
        key = hashlib.sha256(a.encode()).hexdigest()[:32]
        result = self.auth.revoke_user_sessions("alice", token_key=key)
        self.assertEqual(result["revoked"], 1)
        left = [r["token"] for r in self._rows()]
        self.assertNotIn(a, left)
        self.assertIn(b, left)

    def test_revoke_all_sessions_also_kills_cli_device_grants(self):
        self._issue(); self._issue()
        # 用真表（hq_cli_api 建的），别再手造一个假的
        import hq_cli_api
        c = self.auth.db()
        hq_cli_api.ensure_device_tables(c) if hasattr(hq_cli_api, "ensure_device_tables") else None
        now = int(time.time())
        c.execute(
            "INSERT INTO cli_device_grants(device_code_hash,user_code_hash,client_name,"
            "requested_scopes_json,username,status,created_at,expires_at)"
            " VALUES('dch','uch','hq','[]','alice','issued',?,?)", (now, now + 600))
        c.commit(); c.close()

        result = self.auth.revoke_user_sessions("alice", revoke_all=True)
        self.assertEqual(result["revoked"], 2)
        self.assertEqual(len(self._rows()), 0)
        c = self.auth.db()
        left = c.execute("SELECT COUNT(*) FROM cli_device_grants"
                         " WHERE username='alice' AND revoked_at IS NULL").fetchone()[0]
        c.close()
        self.assertEqual(left, 0, "全踢时必须同时吊销 CLI 设备授权，否则能刷新换回来")

    def test_revoke_unknown_session_fails_loudly(self):
        self._issue()
        with self.assertRaises(ValueError):
            self.auth.revoke_user_sessions("alice", token_key="0" * 32)

    def test_list_sessions_requires_a_username(self):
        with self.assertRaises(ValueError):
            self.auth.list_user_sessions("")


if __name__ == "__main__":
    unittest.main()
