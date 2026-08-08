import http.cookiejar
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path


class AuthUserInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("admin", "secret123", 0, "admin")
        self.auth.create_user("alice", "secret123", 100, "member")
        self.auth.create_user("bob", "secret123", 20, "member")
        with closing(sqlite3.connect(self.auth.DB)) as c:
            c.execute(
                """INSERT INTO recharge_orders(
                       order_id,username,amount,points,status,created_at,reviewed_at,order_type
                   ) VALUES('R1','alice',499,1000,'approved',10,11,'membership_experience')"""
            )
            c.execute(
                """INSERT INTO recharge_orders(
                       order_id,username,amount,points,status,created_at,order_type
                   ) VALUES('R2','alice',20,200,'pending',12,'points')"""
            )
            c.execute(
                """INSERT INTO virtual_pay_orders(
                       order_id,username,openid,package_id,product_id,amount_fen,points,env,
                       status,created_at,paid_at,credited_at,order_type
                   ) VALUES('V1','alice','o','p','x',9900,100,0,'credited',20,21,22,'points')"""
            )
            c.execute(
                """INSERT INTO virtual_pay_orders(
                       order_id,username,openid,package_id,product_id,amount_fen,points,env,
                       status,created_at,order_type
                   ) VALUES('V2','alice','o','p','x',500,5,0,'created',23,'points')"""
            )
            c.execute(
                """INSERT INTO points_audit(
                       who_admin,username,delta,before_points,after_points,reason,created_at
                    ) VALUES('system','alice',-10,110,100,'job:image',30)"""
            )
            alice_id = c.execute(
                "SELECT id FROM users WHERE username='alice'",
            ).fetchone()[0]
            bob_id = c.execute(
                "SELECT id FROM users WHERE username='bob'",
            ).fetchone()[0]
            campaign_id = c.execute("SELECT id FROM invite_campaigns LIMIT 1").fetchone()[0]
            relation_id = c.execute(
                """INSERT INTO user_invites(
                       campaign_id,inviter_user_id,invitee_user_id,invite_code,source,
                       status,risk_status,bound_at,updated_at
                   ) VALUES(?,?,?,'ABC234','test','bound','normal',40,40)""",
                (campaign_id, alice_id, bob_id),
            ).lastrowid
            first_upgrade = c.execute(
                """INSERT INTO membership_upgrade_records(
                       user_id,from_level,to_level,source,source_order_id,status,created_at
                   ) VALUES(?,'','experience','test','reward-1','effective',41)""",
                (bob_id,),
            ).lastrowid
            second_upgrade = c.execute(
                """INSERT INTO membership_upgrade_records(
                       user_id,from_level,to_level,source,source_order_id,status,created_at
                   ) VALUES(?,'experience','partner','test','reward-2','effective',42)""",
                (bob_id,),
            ).lastrowid
            c.execute(
                """INSERT INTO invite_reward_point_records(
                       invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
                       inviter_level_snapshot,invitee_level,reward_points,reward_total_after,
                       status,created_at
                   ) VALUES(?,?,?,?, 'partner','experience',240,240,'recorded',41)""",
                (relation_id, first_upgrade, alice_id, bob_id),
            )
            c.execute(
                """INSERT INTO invite_reward_point_records(
                       invite_relation_id,upgrade_record_id,inviter_user_id,invitee_user_id,
                       inviter_level_snapshot,invitee_level,reward_points,reward_total_after,
                       status,created_at,voided_at,void_reason,voided_by
                   ) VALUES(?,?,?,?, 'partner','partner',1260,1500,'voided',42,43,
                            '测试作废','admin')""",
                (relation_id, second_upgrade, alice_id, bob_id),
            )
            c.commit()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def client(self, username):
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar),
        )
        self.post(client, "/api/auth/login", {"username": username, "password": "secret123"})
        return client

    def post(self, client, path, payload, internal=False):
        headers = {"Content-Type": "application/json"}
        if internal:
            headers["X-HQ-Internal-Token"] = "test-internal-token"
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        with client.open(req, timeout=3) as response:
            return json.loads(response.read())

    def get(self, client, path):
        with client.open(self.base + path, timeout=3) as response:
            return json.loads(response.read())

    def test_payment_and_ledger_summary_is_exact_user_only(self):
        data = self.auth.admin_user_insights("alice")
        self.assertEqual(data["user"]["username"], "alice")
        self.assertEqual(data["payments"]["order_count"], 4)
        self.assertEqual(data["payments"]["paid_order_count"], 2)
        self.assertEqual(data["payments"]["paid_amount_fen"], 59800)
        self.assertEqual(data["payments"]["pending_count"], 2)
        self.assertEqual(data["ledger"]["summary"]["debits"], 10)
        self.assertEqual(data["invite_rewards"]["recorded_points"], 240)
        self.assertEqual(data["invite_rewards"]["voided_points"], 1260)
        self.assertEqual(data["invite_rewards"]["total"], 2)
        self.assertEqual(
            [item["invitee_account"] for item in data["invite_rewards"]["items"]],
            ["bob", "bob"],
        )
        self.assertIsNone(data["invite_relations"]["referrer"])
        self.assertEqual(data["invite_relations"]["invitees"]["total"], 1)
        self.assertEqual(
            data["invite_relations"]["invitees"]["items"][0]["account"], "bob",
        )
        bob = self.auth.admin_user_insights("bob")
        self.assertEqual(bob["invite_relations"]["referrer"]["account"], "alice")
        self.assertEqual(bob["invite_relations"]["invitees"]["total"], 0)
        self.assertEqual(
            bob["invite_rewards"]["total"], 0,
        )
        self.assertIsNone(self.auth.admin_user_insights("missing"))

    def test_phone_accounts_are_masked_in_nested_invite_data(self):
        with closing(sqlite3.connect(self.auth.DB)) as c:
            c.execute("UPDATE users SET username='13800000031',display_name='13800000031' WHERE username='bob'")
            c.commit()
        data = self.auth.admin_user_insights("alice")
        relation = data["invite_relations"]["invitees"]["items"][0]
        reward = data["invite_rewards"]["items"][0]
        self.assertEqual(relation["account"], "138****0031")
        self.assertEqual(relation["display_name"], "138****0031")
        self.assertEqual(reward["invitee_account"], "138****0031")
        self.assertNotIn("invitee_username", reward)
        self.assertEqual(
            self.auth.admin_user_insights(user_id=relation["user_id"])["user"]["username"],
            "13800000031",
        )

    def test_notification_admin_boundary_and_user_isolation(self):
        admin = self.client("admin")
        alice = self.client("alice")
        bob = self.client("bob")
        with self.assertRaises(urllib.error.HTTPError) as no_internal:
            self.post(admin, "/api/auth/admin/notifications", {
                "username": "alice", "title": "抱歉声明", "detail": "任务已恢复。",
            })
        self.assertEqual(no_internal.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as not_admin:
            self.post(alice, "/api/auth/admin/notifications", {
                "username": "alice", "title": "伪造", "detail": "不应成功",
            }, internal=True)
        self.assertEqual(not_admin.exception.code, 403)

        sent = self.post(admin, "/api/auth/admin/notifications", {
            "username": "alice", "title": "抱歉声明", "detail": "任务已恢复。",
            "created_by": "alice",
        }, internal=True)
        self.assertTrue(sent["ok"])
        self.assertNotIn("created_by", sent["notification"])
        with closing(sqlite3.connect(self.auth.DB)) as c:
            actor = c.execute("SELECT created_by FROM user_notifications").fetchone()[0]
        self.assertEqual(actor, "admin")
        self.assertEqual(self.get(alice, "/api/auth/notifications")["items"][0]["title"], "抱歉声明")
        self.assertEqual(self.get(bob, "/api/auth/notifications")["items"], [])

    def test_notification_validation_does_not_silently_truncate(self):
        notice, err = self.auth.create_user_notification("alice", "x" * 81, "正文", "admin")
        self.assertIsNone(notice)
        self.assertEqual(err, "title_too_long")
        notice, err = self.auth.create_user_notification("alice", "标题", "x" * 1001, "admin")
        self.assertIsNone(notice)
        self.assertEqual(err, "detail_too_long")

    def test_password_reset_requires_admin_and_revokes_existing_sessions(self):
        admin = self.client("admin")
        alice = self.client("alice")
        with self.assertRaises(urllib.error.HTTPError) as no_internal:
            self.post(admin, "/api/auth/admin/password/reset", {
                "username": "alice", "new_password": "temporary456",
            })
        self.assertEqual(no_internal.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as not_admin:
            self.post(alice, "/api/auth/admin/password/reset", {
                "username": "alice", "new_password": "temporary456",
            }, internal=True)
        self.assertEqual(not_admin.exception.code, 403)

        reset = self.post(admin, "/api/auth/admin/password/reset", {
            "username": "alice", "new_password": "temporary456",
        }, internal=True)
        self.assertTrue(reset["reset"]["must_change"])
        with self.assertRaises(urllib.error.HTTPError) as revoked:
            self.get(alice, "/api/auth/me")
        self.assertEqual(revoked.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as old_password:
            self.post(alice, "/api/auth/login", {"username": "alice", "password": "secret123"})
        self.assertEqual(old_password.exception.code, 401)
        relogin = self.post(alice, "/api/auth/login", {
            "username": "alice", "password": "temporary456",
        })
        self.assertTrue(relogin["user"]["must_change"])


class AdminTaskInsightsTests(unittest.TestCase):
    def setUp(self):
        import server.admin_api as admin_api

        self.admin = admin_api
        self.old_db = admin_api.JOB_DB
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.path = Path(path)
        self.admin.JOB_DB = self.path
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as c:
            c.execute(
                """CREATE TABLE jobs(
                       id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,status TEXT,
                       payload TEXT,created_at INTEGER,updated_at INTEGER)"""
            )
            c.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                [
                    (1, "xiaole_video", "alice", 20, "done", '{"channel":"micro","model":"seedance"}', now - 60, now - 50),
                    (2, "xiaole_video", "alice", 30, "error", '{"channel":"omni","model":"omni"}', now - 40, now - 30),
                    (3, "image", "alice", 8, "pending", '{"provider":"seedream","model":"seedream"}', now - 20, now - 20),
                    (4, "image", "bob", 8, "done", '{"provider":"openai"}', now - 10, now - 10),
                ],
            )
            c.commit()

    def tearDown(self):
        self.admin.JOB_DB = self.old_db
        self.path.unlink(missing_ok=True)

    def test_cumulative_status_channel_model_and_recent_are_separated(self):
        data = self.admin.user_job_insights("alice")
        self.assertEqual((data["total"], data["done"], data["error"], data["running"]), (3, 1, 1, 1))
        self.assertEqual(data["success_rate"], 0.5)
        self.assertEqual({x["name"] for x in data["by_channel"]}, {"micro", "omni", "seedream"})
        self.assertEqual({x["name"] for x in data["by_model"]}, {"seedance", "omni", "seedream"})
        self.assertEqual([x["id"] for x in data["recent"]], [3, 2, 1])
        self.assertEqual(self.admin._job_payload('{"channel":"micro"}')["channel"], "micro")

    def test_operations_stats_use_customer_feature_names(self):
        by_kind = {item["kind"]: item for item in self.admin.job_stats(7)["by_kind"]}
        self.assertEqual(by_kind["seedance_video"]["done"], 1)
        self.assertEqual(by_kind["omni_video"]["error"], 1)
        self.assertNotIn("xiaole_video", by_kind)
        self.assertEqual(self.admin.feature_flags.CATALOG_MAP["grok_video"]["page"], "视频生成")
        self.assertEqual(self.admin.feature_flags.CATALOG_MAP["tryon"]["name"], "换装换背景")


class AdminUserInsightsFrontendTests(unittest.TestCase):
    def test_admin_and_notification_center_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "site/admin/index.html").read_text(encoding="utf-8")
        admin_api = (root / "server/admin_api.py").read_text(encoding="utf-8")
        shell = (root / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
        for marker in (
            'id="userDetailBox"', 'data-act="detail"', 'data-act="notice"',
            "/api/admin/users/detail?username=", "/api/admin/users/notification",
            "noticeSending", 'id="detailPassword"', 'type="password" minlength="6" maxlength="128"',
            "/api/admin/users/password/reset", "invite_rewards", "inviteRewardRows",
            "invite_relations", "inviteRelationRows", "邀请关系", "直接邀请用户",
            "邀请奖励明细", "有效奖励", "已作废奖励",
            'id="inviteClaimBox"', 'id="inviteClaimStatus"',
            "/api/admin/invite/reward-claims", "待升级领取", "无合格领取人",
            'id="inviteJourneyBox"', 'id="inviteJourneyStats"',
            "/api/admin/invite/journeys", "权益只以服务端邀请关系与奖励台账为准",
        ):
            self.assertIn(marker, html)
        self.assertIn('"/api/admin/invite/journeys"', admin_api)
        self.assertIn("/api/auth/notifications?limit=50", shell)
        self.assertIn("server-notice-", shell)
        self.assertIn("escapeHtml(x.title)", shell)

    def test_support_layout_uses_sidebar_dashboard_and_customer_drawer(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "site/admin/index.html").read_text(encoding="utf-8")
        for marker in (
            'class="admin-sidebar"', 'data-module-tab="dashboard"',
            'data-module-tab="users"', 'data-module-tab="logs"',
            'data-module-tab="recharge"', 'data-module-switch="points"',
            'data-module-tab="invite"', 'data-module-tab="inspirations"',
            'data-module-tab="operations"',
            'data-module-tab="features"', 'data-module-switch="pricing"', 'data-module="dashboard"',
            'data-module="operations"', 'id="operationsBox"', "function renderOperations(data)",
            "基础运行探针（非功能验收）", "可达 · 仅辅助定位", "不会假绿", "功能运行中心",
            "生产中心", "客户经营", "内容运营", "系统管理", "消息与公告", "灵感案例",
            'class="ops-workspace"', 'class="ops-catalog"', 'class="ops-selected"', 'class="ops-inspector"',
            'data-operation-select=', "sidebarModule = {points:'recharge',pricing:'features'}",
            'id="operationsPage"', 'data-operations-page=', "该客户页尚未盘点",
            "开发待归档", "后台生产链测试", "尚无真实接单证据",
            "任务记录了点数，账务台账待核对", "operationsPage:'video'",
            'id="globalUserSearch"', 'id="customerLayer"',
            "/api/admin/activity?limit=8", "/api/admin/recharge/orders?status=pending",
            "module:'dashboard'", "aria-current", "/workbench/hq-icons-duotone.js",
            'class="side-nav-icon"', "prefers-reduced-motion:reduce",
            "dashboard:'home'", "users:'users'", "logs:'clock'", "recharge:'coins'",
            "points:'trend'", "invite:'userPlus'", "inspirations:'sparkles'",
            "operations:'layers'", "channels:'lock'", "features:'sliders'",
            '@media (max-width:1280px){.ops-workspace',
            '.module-card[data-module="operations"]>.section-head{flex-direction:column}',
            'id="sidebarResizer"', 'role="separator"', 'aria-orientation="vertical"',
            'function setSidebarWidth', 'setPointerCapture(e.pointerId)', 'setSidebarWidth(e.clientX)',
            "window.addEventListener('pointermove'", 'function stopSidebarResize',
            '.sidebar-resizer{display:none}', '@media (min-width:901px)',
            'id="operationsChannelLayer"', 'function renderOperationsChannelDrawer',
            '.operations-channel-drawer .channel-table{min-width:980px}',
            '.operations-channel-drawer .channel-table td:nth-child(5):before{content:"操作"}',
            'data-operation-channel-open=', '当前接单配置，不代表历史任务实际使用线路',
            '完整 Key 可在当前抽屉按 5 秒审计查看',
            "bindSecretRevealActions('operationsChannelBox')", 'value="minimax"',
            'clearSecretWindows();restoreProviderEditor()',
            "boxId==='operationsChannelBox'&&el('operationsChannelLayer').hidden",
            "&&el('providerKeyEditor').hidden)renderOperationsChannelDrawer()",
            "selections[dep.alternative_group]===(dep.selection_value||dep.key)",
            "function probeSelectedOperation(force)", "key_probes", "keyProbes:{}",
            "最近鉴权通过", "凭据已失效", "最近连通 · 未验证凭据",
            "探针数据已过期", "号池鉴权证据已过期", "后台按渠道定时巡检",
            "var deps=registryActiveDependencies(feature,mode)",
            "data-server-probe-status", "function updateServerProbeNodes(key)",
            "state.module==='dashboard'||state.module==='operations'", "credential_version",
            "var routeUnverified=", "&force=1",
            "var evidence=registryRouteEvidence({key:meta.key}",
            "后台生产链控制台", "后台生产链可交付，客户页面仍需浏览器验收",
            "data-operation-validation-enable=", "openOperationValidation",
            "运行一次生产链验收", "/api/admin/e2e/run", "startE2EPoll",
            'id="operationsBatch"', "一键完整旅程验收", "runE2EBatch",
            "Number((preflight||{}).ready_count||0)",
            "/api/admin/e2e/batch/preflight", "/api/admin/e2e/batch/run", "RUN_BATCH",
            "/api/admin/e2e/audio-fixture/prepare", "准备个人测试音色",
            "prepareAudioE2EFixture", "audio_fixture_required",
            "后台自动加载预设素材、分批提交并汇总八段证据",
            "同步或内部步骤没有上游任务 ID 时标记“不适用”",
            "同一次旅程全部通过才显示 8/8",
            "仅表示当前允许接单",
            "return state.overviewPromise", "loadE2EPreflight(operationKey,true)",
            "本次预设测试包", "素材只在服务器私有目录使用", "查看 '+stages.length+' 段证据",
            "/api/admin/e2e/preflight", "测试包已准备 · 待执行",
            "产物已登记 · 私有测试素材不展示",
            ".ops-e2e-stages{display:grid",
            ".ops-e2e-stage span{min-width:0;overflow-wrap:anywhere",
            '.ops-catalog{position:static;max-height:none}',
        ):
            self.assertIn(marker, html)
        self.assertNotIn("window.open(target", html)
        self.assertNotIn('id="operationsValidationLayer"', html)
        self.assertNotIn('id="operationsValidationFrame"', html)
        self.assertNotIn("hq_video_prefill", html)
        self.assertNotIn("产物：<code>'+esc(x.result_url", html)
        self.assertNotIn("<img src=", html[html.index("function registryValidationPanel"):html.index("function renderOperationsDeveloper")])
        self.assertNotIn('data-module-tab="ops"', html)
        self.assertNotIn('data-module-tab="points"', html)
        self.assertNotIn('data-module-tab="pricing"', html)
        self.assertNotIn('data-module-tab="services"', html)
        self.assertNotIn('data-module-tab="channels"', html)
        self.assertNotIn('id="sidebarCollapse"', html)
        self.assertNotIn('sidebar-collapsed', html)
        self.assertNotIn('data-module="services"', html)
        self.assertNotIn("function renderServices", html)
        self.assertNotIn("服务在线", html)
        self.assertNotIn("健康接口可达", html)
        self.assertNotIn("尚未证明全链路", html)
        self.assertNotIn("当前尚未统一采集", html)
        self.assertNotIn('id="operationsSearch"', html)
        self.assertNotIn("function operationTerms", html)
        self.assertNotIn("已发现服务器凭据 · 尚未鉴权", html)
        self.assertNotIn("var label=!configured?'未配置':(healthy?'鉴权探针通过':'已配置')", html)
        self.assertNotIn("rows.push({key:stat.kind", html)
        self.assertNotIn('class="module-tabs', html)

    @unittest.skipUnless(shutil.which("node"), "node is required for admin probe behavior test")
    def test_route_evidence_executes_and_never_marks_unverified_route_green(self):
        html = (Path(__file__).resolve().parents[1] / "site/admin/index.html").read_text(encoding="utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        route_start = script.index("function registryRouteEvidence")
        route_end = script.index("function updateServerProbeNodes", route_start)
        verdict_start = script.index("function registryVerdict")
        verdict_end = script.index("function registryModeCard", verdict_start)
        probe = "\n".join([
            "var state={keyProbes:{openai:{signature:'v1',loading:false,result:{status:'auth_ok',checked_at:Math.floor(Date.now()/1000),latency_ms:7}}}};",
            "function fmtTime(){return '12:00';}",
            script[route_start:route_end],
            "var data={keys:[{key:'openai',name:'OpenAI',configured:true,auto_probe:true,probe_interval:600,credential_version:'v1'}],provider_keys:{items:[]}};",
            "var evidence=registryRouteEvidence({key:'openai',credential_source:'env'},data);",
            "if(evidence.state!=='ok')throw new Error('configured auth result did not render');",
            "state.keyProbes.openai={signature:'old',loading:false,result:{status:'auth_ok',checked_at:Math.floor(Date.now()/1000)}};",
            "if(registryRouteEvidence({key:'openai',credential_source:'env'},data).state==='ok')throw new Error('rotated credential reused stale success');",
            "function registrySelections(){return {};}",
            "function registryDependencyApplies(){return true;}",
            "function registryFailureBreakpoint(){return 'break';}",
            "function registryE2ERun(){return null;}",
            "function registryE2EPassed(){return false;}",
            "function registryE2EFresh(){return false;}",
            "registryRouteEvidence=function(dep){return {state:dep.testState};};",
            script[verdict_start:verdict_end],
            "var verdict=registryVerdict({acceptance_health:true,dependencies:[{requirement:'required',testState:'warn'}]}, {evidence_contract:{},dependencies:[]}, null, {status:'enabled'}, {online:true}, {});",
            "if(verdict[0]==='ok')throw new Error('unverified required route became green');",
            "verdict=registryVerdict({acceptance_health:true,dependencies:[]}, {evidence_contract:{},dependencies:[]}, {latest:{status:'failed',error:'timeout'}}, {status:'disabled'}, {online:true}, {});",
            "if(verdict[0]!=='fail'||verdict[1]!=='最近一次失败')throw new Error('disabled flag hid latest failure');",
            "verdict=registryVerdict({acceptance_health:true,dependencies:[]}, {evidence_contract:{},dependencies:[]}, null, {status:'disabled'}, {online:true}, {});",
            "if(verdict[0]!=='warn'||verdict[1]!=='暂停接单')throw new Error('disabled flag without failure was not yellow');",
        ])
        result = subprocess.run([shutil.which("node"), "-e", probe], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
