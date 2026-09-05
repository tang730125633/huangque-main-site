import json
import pathlib
import tempfile
import unittest
from unittest import mock

import server.admin_api as admin_api


SAMPLE = "\n".join(
    [
        '127.0.0.1 - - [09/Jul/2026:08:41:19 +0800] "GET /api/claim?token=worker-secret HTTP/1.1" 200 12 "-" "Python-urllib/3.11"',
        '127.0.0.1 - - [09/Jul/2026:08:41:30 +0800] "GET /api/admin/overview?days=7 HTTP/1.1" 200 900 "-" "Mozilla/5.0"',
        '1.2.3.4 - - [09/Jul/2026:08:42:00 +0800] "POST /api/gen/image HTTP/1.1" 502 88 "-" "Mozilla/5.0" rt=1.234 rid=req_image_1234 hq=HQ-UPSTREAM-001 u=tang%20wu',
        '5.6.7.8 - - [09/Jul/2026:08:42:30 +0800] "GET /api/gen/job/42?api_key=abc&ratio=1:1 HTTP/1.1" 200 55 "-" "Mozilla/5.0"',
        # 畸形分号分隔 + 嵌套 URL 编码密钥 + basic auth 用户名带空格
        '2.2.2.2 - - [09/Jul/2026:08:42:40 +0800] "GET /api/gen/x?a=1;token=evil HTTP/1.1" 200 10 "-" "curl/8"',
        '3.3.3.3 - - [09/Jul/2026:08:42:50 +0800] "GET /api/gen/dl?url=https%3A%2F%2Fx.com%2Fv%3Ftoken%3Dleak HTTP/1.1" 200 10 "-" "curl/8"',
        '4.4.4.4 - tang wu [09/Jul/2026:08:42:55 +0800] "GET /api/gen/health HTTP/1.1" 200 10 "-" "curl/8"',
        '6.6.6.6 - - [09/Jul/2026:08:42:58 +0800] "GET /api/gen/dl?url=https%3A%2F%2Fok.com%2Fv.mp4&dk=wxdecode HTTP/1.1" 200 10 "-" "curl/8"',
        '9.9.9.9 - - [09/Jul/2026:08:43:00 +0800] "GET /index.html HTTP/1.1" 200 100 "-" "Mozilla/5.0"',
        "",
    ]
)

SAMPLE2 = "\n".join(
    [
        # 第二个日志文件（时间夹在中间，验证跨文件按时间合并）
        '8.8.8.8 - - [09/Jul/2026:08:42:45 +0800] "GET /api/keywords HTTP/1.1" 200 20 "-" "Mozilla/5.0"',
        "",
    ]
)


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        self.files = []
        for content in (SAMPLE, SAMPLE2):
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
            tmp.write(content)
            tmp.close()
            self.files.append(pathlib.Path(tmp.name))
        self.old_logs = admin_api.NGINX_ACCESS_LOGS
        admin_api.NGINX_ACCESS_LOGS = list(self.files)

    def tearDown(self):
        admin_api.NGINX_ACCESS_LOGS = self.old_logs
        for p in self.files:
            p.unlink(missing_ok=True)

    def test_only_api_paths_and_noise_hidden(self):
        items = admin_api.request_logs()["items"]
        paths = [x["path"] for x in items]
        self.assertTrue(all(p.startswith("/api/") for p in paths))
        self.assertFalse(any(p.startswith("/api/claim") or p.startswith("/api/admin/") for p in paths))
        self.assertNotIn("/index.html", paths)

    def test_secret_masked_incl_semicolon_and_nested(self):
        items = admin_api.request_logs(include_noise=True)["items"]
        joined = " ".join(x["path"] for x in items)
        self.assertIn("/api/claim?token=***", joined)
        self.assertIn("api_key=***", joined)
        self.assertIn("ratio=1:1", joined)          # 非敏感参数原样保留
        self.assertIn(";token=***", joined)          # 分号分隔也打码
        self.assertIn("url=***", joined)             # 嵌套编码 URL 里带 token → 整值打码
        self.assertIn("dk=***", joined)              # 视频号解密密钥参数
        self.assertNotIn("worker-secret", joined)
        self.assertNotIn("evil", joined)
        self.assertNotIn("leak", joined)
        self.assertNotIn("wxdecode", joined)

    def test_spaced_remote_user_still_parsed(self):
        items = admin_api.request_logs()["items"]
        self.assertIn("/api/gen/health", [x["path"] for x in items])

    def test_status_filter(self):
        items = admin_api.request_logs(status="5")["items"]
        self.assertEqual([x["status"] for x in items], [502])
        items = admin_api.request_logs(status="200")["items"]
        self.assertTrue(items and all(x["status"] == 200 for x in items))

    def test_merge_across_files_sorted_desc(self):
        items = admin_api.request_logs(q="/api/")["items"]
        times = [x["time"] for x in items]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertIn("/api/keywords", [x["path"] for x in items])  # 来自第二个文件

    def test_observability_suffix_is_parsed_without_breaking_legacy_lines(self):
        items = {x["path"]: x for x in admin_api.request_logs(include_noise=True)["items"]}
        self.assertEqual(items["/api/gen/image"]["duration_sec"], 1.234)
        self.assertEqual(items["/api/gen/image"]["request_id"], "req_image_1234")
        self.assertEqual(items["/api/gen/image"]["hq_code"], "HQ-UPSTREAM-001")
        self.assertEqual(items["/api/gen/image"]["user"], "tang wu")
        self.assertIsNone(items["/api/gen/health"]["duration_sec"])
        self.assertEqual(items["/api/gen/health"]["request_id"], "")
        self.assertEqual(items["/api/gen/health"]["hq_code"], "")

    def test_hq_error_code_is_searchable(self):
        items = admin_api.request_logs(q="HQ-UPSTREAM-001")["items"]
        self.assertEqual([x["path"] for x in items], ["/api/gen/image"])

    def test_nginx_logs_but_does_not_expose_authenticated_user_header(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        for name in ("deploy/nginx-huangquechuanmei.conf", "server/nginx-huangquechuanmei.conf"):
            config = (root / name).read_text(encoding="utf-8")
            self.assertIn("u=$upstream_http_x_hq_log_user", config)
            self.assertIn("proxy_hide_header X-HQ-Log-User;", config)

    def test_missing_log_file(self):
        admin_api.NGINX_ACCESS_LOGS = [pathlib.Path(str(self.files[0]) + ".nope")]
        data = admin_api.request_logs()
        self.assertEqual(data["items"], [])
        self.assertIn("找不到", data["message"])


class RequestLogUserTests(unittest.TestCase):
    """任务号反查用户/功能 + 路径→功能名。"""

    LOG = "\n".join(
        [
            '1.1.1.1 - - [09/Jul/2026:09:00:00 +0800] "GET /api/gen/job/1226 HTTP/1.1" 200 55 "-" "Mozilla/5.0"',
            '1.1.1.1 - - [09/Jul/2026:09:00:05 +0800] "GET /api/gen/job/9999 HTTP/1.1" 404 20 "-" "Mozilla/5.0"',
            '2.2.2.2 - - [09/Jul/2026:09:00:10 +0800] "POST /api/auth/login HTTP/1.1" 200 30 "-" "Mozilla/5.0"',
            '3.3.3.3 - - [09/Jul/2026:09:00:15 +0800] "GET /api/gen/banana/health HTTP/1.1" 200 10 "-" "curl/8"',
            "",
        ]
    )

    def setUp(self):
        import sqlite3

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        tmp.write(self.LOG)
        tmp.close()
        self.log_path = pathlib.Path(tmp.name)

        dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        dbf.close()
        self.db_path = pathlib.Path(dbf.name)
        audit = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        import json
        import time as _time
        audit.write(json.dumps({
            "time": int(_time.time()), "event": "metered_request", "status": 200,
            "username": "member-a", "role": "member", "method": "POST",
            "path": "/api/foundation-report/generate", "ip": "10.0.0.8",
            "detail": "", "duration_ms": 432.1, "request_id": "hermes_req_1234",
        }) + "\n")
        audit.close()
        self.audit_path = pathlib.Path(audit.name)
        c = sqlite3.connect(str(self.db_path))
        c.execute(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY, username TEXT, kind TEXT,"
            " cost INTEGER, status TEXT, payload TEXT, created_at INTEGER, updated_at INTEGER)"
        )
        now = int(_time.time())
        c.execute(
            "INSERT INTO jobs VALUES(1226,'tang','xiaole_video',13,'done','{}',?,?)",
            (now - 100, now - 40),
        )
        c.execute(
            "CREATE TABLE short_drama_provider_shot_jobs("
            "id TEXT PRIMARY KEY, owner_username TEXT, provider TEXT, status TEXT,"
            "cost INTEGER, created_at INTEGER, updated_at INTEGER, shot_key TEXT)"
        )
        c.execute(
            "INSERT INTO short_drama_provider_shot_jobs VALUES("
            "'shot-job-1','tang','minimax_h3','failed',42,?,?, 'shot_09')",
            (now - 50, now - 35),
        )
        c.commit()
        c.close()

        self.old_logs = admin_api.NGINX_ACCESS_LOGS
        self.old_db = admin_api.JOB_DB
        self.old_hermes_logs = admin_api.HERMES_AUDIT_LOGS
        admin_api.NGINX_ACCESS_LOGS = [self.log_path]
        admin_api.JOB_DB = self.db_path
        admin_api.HERMES_AUDIT_LOGS = [self.audit_path]

    def tearDown(self):
        admin_api.NGINX_ACCESS_LOGS = self.old_logs
        admin_api.JOB_DB = self.old_db
        admin_api.HERMES_AUDIT_LOGS = self.old_hermes_logs
        self.log_path.unlink(missing_ok=True)
        self.db_path.unlink(missing_ok=True)
        self.audit_path.unlink(missing_ok=True)

    def test_enrichment(self):
        items = {x["path"]: x for x in admin_api.request_logs()["items"]}
        poll = items["/api/gen/job/1226"]
        self.assertEqual(poll["user"], "tang")
        # 「视频 · 小乐」是旧名字 —— 它把果肉/豆姐/欧米三个渠道混成了一个。
        # 现在按 payload.channel 分开；没有 channel 的老任务不冒充任何客户功能。
        self.assertEqual(poll["func"], "历史未归档视频任务 · 轮询")
        # 任务库里没有的任务号
        self.assertEqual(items["/api/gen/job/9999"]["user"], "-")
        self.assertEqual(items["/api/gen/job/9999"]["func"], "任务轮询")
        # 非任务请求：有功能名、无用户
        self.assertEqual(items["/api/auth/login"]["func"], "登录")
        self.assertEqual(items["/api/auth/login"]["user"], "-")
        self.assertEqual(items["/api/gen/banana/health"]["func"], "健康检查")
        # 内部字段不外传
        self.assertNotIn("_jid", poll)

    def test_activity_merges_jobs_and_http(self):
        data = admin_api.activity_logs()
        items = data["items"]
        sources = {x["source"] for x in items}
        self.assertEqual(sources, {"job", "http", "ip12"})
        # 任务行：带用户/功能/点数；时间线按时间倒序
        job_rows = [x for x in items if x["source"] == "job"]
        self.assertEqual(job_rows[0]["user"], "tang")
        self.assertEqual(job_rows[0]["func"], "短剧 · 麦克视频 · shot_09")
        self.assertEqual(job_rows[0]["cost"], 42)
        self.assertEqual(job_rows[0]["cat"], "fail")
        self.assertEqual(job_rows[0]["path"], "短剧任务 #shot-job-1")
        times = [x["time"] for x in items]
        self.assertEqual(times, sorted(times, reverse=True))
        ip12 = next(x for x in items if x["source"] == "ip12")
        self.assertEqual(ip12["user"], "member-a")
        self.assertEqual(ip12["func"], "IP12 · 生成初稿 PDF")
        self.assertAlmostEqual(ip12["duration_sec"], 0.4321)
        self.assertEqual(ip12["request_id"], "hermes_req_1234")
        self.assertIn("error_catalog", data)

    def test_shared_short_drama_job_is_enriched_without_duplicate_activity_row(self):
        import sqlite3
        import time as _time

        now = int(_time.time())
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                "INSERT INTO short_drama_provider_shot_jobs VALUES("
                "'1226','tang','minimax_h3','succeeded',13,?,?, 'shot_01')",
                (now - 100, now - 40),
            )
            connection.commit()
        finally:
            connection.close()

        items = admin_api.call_logs(7, 20)["items"]
        shared_rows = [item for item in items if str(item["id"]) == "1226"]

        self.assertEqual(len(shared_rows), 1)
        self.assertEqual(shared_rows[0]["func"], "短剧 · 麦克视频")
        self.assertEqual(shared_rows[0]["operation"], "shot_01")
        self.assertEqual(shared_rows[0]["path_label"], "短剧任务 #1226")
        self.assertTrue(any(item["id"] == "shot-job-1" for item in items))

    def test_activity_filters(self):
        # source 过滤
        only_jobs = admin_api.activity_logs(source="job")["items"]
        self.assertTrue(only_jobs and all(x["source"] == "job" for x in only_jobs))
        only_http = admin_api.activity_logs(source="http")["items"]
        self.assertTrue(only_http and all(x["source"] == "http" for x in only_http))
        only_ip12 = admin_api.activity_logs(source="ip12")["items"]
        self.assertTrue(only_ip12 and all(x["source"] == "ip12" for x in only_ip12))
        # 统一状态：fail = HTTP >=400（本样本 404）
        fails = admin_api.activity_logs(category="fail")["items"]
        self.assertTrue(fails and all(x["cat"] == "fail" for x in fails))
        attributed = admin_api.activity_logs(category="fail", attributed=True)["items"]
        self.assertTrue(attributed and all(x["user"] not in (None, "", "-") for x in attributed))
        # 关键词搜用户名 → 命中任务行
        hit = admin_api.activity_logs(q="tang")["items"]
        self.assertTrue(hit and all("tang" in (x["user"] or "") or "tang" in x["path"] for x in hit))
        request_hit = admin_api.activity_logs(q="hermes_req_1234")["items"]
        self.assertEqual([x["source"] for x in request_hit], ["ip12"])

    def test_activity_fail_filter_not_crowded_out(self):
        # 404 行不在最新 2 条里；fail 条件下推到采集层后依然能查到
        fails = admin_api.activity_logs(category="fail", limit=2, source="http")["items"]
        self.assertTrue(any(x["status_text"] == "404" for x in fails))

    def test_activity_pagination_returns_stable_pages_and_total(self):
        all_items = admin_api.activity_logs(limit=100)["items"]
        first = admin_api.activity_logs(limit=2, offset=0)
        second = admin_api.activity_logs(limit=2, offset=2)
        self.assertEqual(first["items"] + second["items"], all_items[:4])
        self.assertEqual(first["total"], len(all_items))
        self.assertEqual(first["offset"], 0)
        self.assertEqual(second["offset"], 2)


class JobPayloadTests(unittest.TestCase):
    def test_truncated_payload_still_names_func(self):
        # 模拟 substr 截断的 payload：JSON 不完整,但 mode 字段在前缀里
        truncated = '{"mode": "text", "prompt": "' + "x" * 5000
        data = admin_api._job_payload(truncated)
        self.assertEqual(data.get("mode"), "text")
        # 名字对齐产品里的叫法：视频页那个功能页签就叫「数字化 IP」
        self.assertEqual(admin_api.call_func_name("video", data), "数字化 IP · 文案")
        # 完整 JSON 走正常解析
        self.assertEqual(admin_api._job_payload('{"model": "nb2"}'), {"model": "nb2"})
        self.assertEqual(admin_api._job_payload(None), {})


class CatalogAndBalanceTests(unittest.TestCase):
    def test_catalog_keys_match_key_groups(self):
        group_keys = {g["key"] for g in admin_api.KEY_GROUPS}
        for k, eps in admin_api.ENDPOINT_CATALOG.items():
            self.assertIn(k, group_keys)
            for e in eps:
                self.assertTrue(e["m"] and e["p"] and e["d"])
                self.assertIn("fee", e)
        # key_status 把清单带给前端
        with_eps = [i for i in admin_api.key_status() if i["endpoints"]]
        self.assertGreaterEqual(len(with_eps), 8)

    def test_find_balance(self):
        f = admin_api._find_balance
        self.assertEqual(f({"data": {"remaining_quota": 120}}), 120)
        self.assertEqual(f({"code": 0, "data": {"remainCoins": "58.5"}}), 58.5)
        self.assertEqual(f({"user_data": {"balance": 42, "email": "x"}}), 42)
        self.assertIsNone(f({"data": {"name": "x"}}))
        self.assertIsNone(f({"quota_ok": True}))  # bool 不算余额


class KeyPingTests(unittest.TestCase):
    def setUp(self):
        admin_api._KEY_PING_CACHE.clear()
        admin_api._KEY_PING_LOCKS.clear()

    def tearDown(self):
        admin_api._KEY_PING_CACHE.clear()
        admin_api._KEY_PING_LOCKS.clear()

    def test_key_probe_is_cached_forceable_and_never_exposes_upstream_fields(self):
        probe = mock.Mock(return_value={
            "ok": True, "mode": "auth", "latency_ms": 12,
            "balance": 8, "secret": "must-not-leak",
            "error": "Authorization=Bearer must-not-leak",
        })
        with mock.patch.dict(admin_api.KEY_PINGS, {"wavespeed": probe}, clear=False):
            first = admin_api.probe_key("wavespeed")
            second = admin_api.probe_key("wavespeed")
            forced = admin_api.probe_key("wavespeed", force=True)
        self.assertEqual(probe.call_count, 2)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(forced["cached"])
        self.assertIn("checked_at", first)
        self.assertEqual(admin_api.key_probe_status()["wavespeed"]["balance"], 8)
        self.assertNotIn("secret", first)
        self.assertNotIn("error", first)
        self.assertNotIn("must-not-leak", json.dumps(admin_api.key_probe_status()))

    def test_key_rotation_invalidates_cached_success(self):
        probe = mock.Mock(return_value={"ok": True, "mode": "auth"})
        with mock.patch.dict(admin_api.KEY_PINGS, {"openai": probe}, clear=False), \
                mock.patch.object(admin_api, "_credential_version", return_value="v1"):
            admin_api.probe_key("openai")
        with mock.patch.dict(admin_api.KEY_PINGS, {"openai": probe}, clear=False), \
                mock.patch.object(admin_api, "_credential_version", return_value="v2"):
            fresh = admin_api.probe_key("openai")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(fresh["credential_version"], "v2")

    def test_credential_version_covers_every_cos_secret(self):
        item = admin_api.KEY_GROUP_MAP["cos"]
        first = [{"name": "test", "values": {
            "COS_SECRET_ID": "id-same", "COS_SECRET_KEY": "secret-a",
            "COS_REGION": "ap-test", "COS_BUCKET": "bucket",
        }}]
        second = [{"name": "test", "values": dict(first[0]["values"], COS_SECRET_KEY="secret-b")}]
        self.assertNotEqual(
            admin_api._key_group_version(item, first),
            admin_api._key_group_version(item, second),
        )

    def test_probe_status_distinguishes_auth_quota_and_transient_failures(self):
        self.assertEqual(admin_api._probe_status("openai", {"ok": False, "http_status": 401})[0], "credential_rejected")
        self.assertEqual(admin_api._probe_status("openai", {"ok": False, "http_status": 402})[0], "quota_or_plan")
        self.assertEqual(admin_api._probe_status("openai", {"ok": False, "http_status": 429})[0], "throttled")
        self.assertEqual(admin_api._probe_status("openai", {"ok": False, "http_status": 503})[0], "upstream_unavailable")

    def test_heygen_uses_plan_credit_not_top_level_wallet(self):
        detail = {"data": {"remaining_quota": 69, "details": {"api": 69, "plan_credit": 0}}}
        self.assertEqual(admin_api._heygen_balances(detail), (0.0, 69.0))
        status, message = admin_api._probe_status(
            "heygen", {"ok": True, "plan_credit": 0, "api_wallet": 69},
        )
        self.assertEqual(status, "quota_or_plan")
        self.assertIn("已阻断高价 API 钱包兜底", message)

    def test_ping_upstream_rejects_json_rpc_business_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": "probe",
            "result": {"isError": True, "content": []},
        }).encode("utf-8")
        with mock.patch.object(admin_api.DIRECT_OPENER, "open", return_value=response):
            result = admin_api._ping_upstream(
                "POST", "https://example.invalid/mcp", body={}, proxy_url="",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "上游业务错误")

    def test_background_cycle_only_probes_configured_safe_keys(self):
        with mock.patch.object(admin_api, "key_status", return_value=[
            {"key": "openai", "configured": True},
            {"key": "heygen", "configured": False},
            {"key": "seedance", "configured": True},
        ]), mock.patch.object(admin_api, "probe_key") as probe:
            admin_api.probe_configured_keys()
        probe.assert_called_once_with("openai")

    def test_optional_domain_failure_does_not_disable_core_domains(self):
        with mock.patch.object(
            admin_api, "import_module", side_effect=ImportError("optional")
        ):
            self.assertIsNone(
                admin_api._optional_content_domain("optional_failure")
            )
        self.assertIsNotNone(admin_api.egress)
        self.assertIsNotNone(admin_api.feature_flags)
        self.assertIsNotNone(admin_api.provider_keys)

    def test_every_key_group_is_pingable(self):
        for item in admin_api.key_status():
            self.assertTrue(item["pingable"], item["key"])
            self.assertIn(item["key"], admin_api.KEY_PINGS)
        self.assertEqual(
            set(admin_api.KEY_PINGS),
            {
                "openai", "xai", "deepseek", "gemini", "seedance", "minimax", "zelong", "zelong2", "heygen", "heygen_relay",
                "xiaolevideo", "runninghub", "wavespeed", "cosyvoice", "tikhub", "zhipu", "cos",
            },
        )

    def test_deepseek_provider_probe_uses_non_generating_models_endpoint(self):
        import unittest.mock as mock

        with mock.patch.object(admin_api, "_env_value", return_value=""), \
                mock.patch.object(
                    admin_api, "_ping_upstream", return_value={"ok": True}
                ) as ping:
            self.assertTrue(
                admin_api.probe_provider_secret(
                    "deepseek", "test-only-deepseek-secret"
                )["ok"]
            )
        ping.assert_called_once_with(
            "GET",
            "https://api.deepseek.com/models",
            headers={"Authorization": "Bearer test-only-deepseek-secret"},
            proxied=False,
            allow_redirects=False,
        )

    def test_xai_provider_probe_uses_video_egress_route(self):
        import unittest.mock as mock

        proxy = "http://127.0.0.1:10809"
        with mock.patch.object(
            admin_api.egress, "preferred_proxy", return_value=proxy
        ) as preferred, mock.patch.object(
            admin_api, "_ping_upstream", return_value={"ok": True}
        ) as ping:
            self.assertTrue(
                admin_api.probe_provider_secret(
                    "xai", "xai-provider-secret"
                )["ok"]
            )
        preferred.assert_called_once_with(admin_api.PROXY_URL)
        ping.assert_called_once_with(
            "GET",
            "https://api.x.ai/v1/models",
            headers={"Authorization": "Bearer xai-provider-secret"},
            proxy_url=proxy,
        )

    def test_minimax_provider_probe_uses_verified_metaso_route(self):
        import unittest.mock as mock

        with mock.patch.object(
                admin_api, "_env_value",
                return_value="https://custom.example/minimax",
        ) as env_value, \
                mock.patch.object(
                    admin_api, "_ping_upstream", return_value={"ok": True}
                ) as ping:
            self.assertTrue(
                admin_api.probe_provider_secret(
                    "minimax", "test-only-minimax-secret"
                )["ok"]
            )
        ping.assert_called_once_with(
            "GET",
            "https://metaso.cn/api/minimax/v2/query/video_generation?page_num=1&page_size=1",
            headers={"Authorization": "Bearer test-only-minimax-secret"},
            proxied=False,
        )
        env_value.assert_not_called()

    def test_provider_probe_only_quarantines_definite_401(self):
        self.assertTrue(admin_api._probe_is_credential_rejection({"http_status": 401}))
        self.assertFalse(admin_api._probe_is_credential_rejection({"http_status": 403}))
        self.assertFalse(admin_api._probe_is_credential_rejection({"http_status": 402}))

    def test_provider_key_test_keeps_key_on_ambiguous_403(self):
        import unittest.mock as mock

        pool = mock.Mock()
        pool.public_key.return_value = {"provider": "seedance"}
        pool.candidates.return_value = [{"secret": "seedance-secret"}]
        with mock.patch.object(admin_api, "provider_keys", pool), \
                mock.patch.object(admin_api, "probe_provider_secret", return_value={
                    "ok": False, "http_status": 403, "error": "HTTP 403"
                }), mock.patch.object(admin_api, "_admin_audit"):
            result = admin_api.test_provider_key("admin", {"id": "key-1"})
        self.assertFalse(result["ok"])
        pool.set_health.assert_not_called()

    def test_provider_key_test_quarantines_definite_401(self):
        import unittest.mock as mock

        pool = mock.Mock()
        pool.public_key.return_value = {"provider": "seedance"}
        pool.candidates.return_value = [{"secret": "seedance-secret"}]
        with mock.patch.object(admin_api, "provider_keys", pool), \
                mock.patch.object(admin_api, "probe_provider_secret", return_value={
                    "ok": False, "http_status": 401, "error": "HTTP 401", "latency_ms": 10
                }), mock.patch.object(admin_api, "_admin_audit"):
            result = admin_api.test_provider_key("admin", {"id": "key-1"})
        self.assertFalse(result["ok"])
        pool.set_health.assert_called_once_with("key-1", False, 10, "HTTP 401")

    def test_heygen_ping_uses_dedicated_video_egress(self):
        import unittest.mock as mock

        proxy = "http://heygen-only:10809"
        def env(names):
            return "test-key" if "HEYGEN_API_KEY" in names else ""

        with mock.patch.object(admin_api, "_env_value", side_effect=env), \
                mock.patch.object(admin_api.egress, "heygen_proxy", return_value=proxy) as preferred, \
                mock.patch.object(admin_api, "_ping_upstream", return_value={"ok": True}) as ping:
            self.assertTrue(admin_api._key_ping_heygen()["ok"])
        preferred.assert_called_once_with()
        ping.assert_called_once_with(
            "GET",
            "https://api.heygen.com/v2/user/remaining_quota",
            headers={"X-Api-Key": "test-key"},
            proxy_url=proxy,
        )

    def test_heygen_ping_requires_mcp_when_runtime_uses_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        credentials = pathlib.Path(directory.name) / "heygen-mcp.json"
        credentials.write_text(json.dumps({
            "access_token": "mcp-token", "refresh_token": "refresh-token",
            "expires_at": 4102444800,
        }), encoding="utf-8")
        credentials.chmod(0o600)

        def env(names):
            if "HEYGEN_API_KEY" in names:
                return "api-key"
            if "HEYGEN_MCP_CREDENTIALS" in names:
                return str(credentials)
            return ""

        with mock.patch.object(admin_api, "_env_value", side_effect=env), \
                mock.patch.object(admin_api, "_heygen_proxy_url", return_value=""), \
                mock.patch.object(admin_api, "_ping_upstream", side_effect=[
                    {"ok": True, "latency_ms": 4, "plan_credit": 12, "api_wallet": 3},
                    {"ok": True, "latency_ms": 6},
                ]) as ping:
            result = admin_api._key_ping_heygen()
        self.assertTrue(result["ok"])
        self.assertEqual(result["components"], "API Key + MCP OAuth")
        self.assertEqual(result["plan_credit"], 12)
        self.assertEqual(result["latency_ms"], 10)
        self.assertEqual(ping.call_count, 2)

    def test_expired_mcp_is_not_reported_as_heygen_ready(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        credentials = pathlib.Path(directory.name) / "heygen-mcp.json"
        credentials.write_text(json.dumps({
            "access_token": "expired", "refresh_token": "refresh-token", "expires_at": 1,
        }), encoding="utf-8")
        credentials.chmod(0o600)

        def env(names):
            return "api-key" if "HEYGEN_API_KEY" in names else str(credentials)

        with mock.patch.object(admin_api, "_env_value", side_effect=env), \
                mock.patch.object(admin_api, "_ping_upstream", return_value={
                    "ok": True, "latency_ms": 4, "plan_credit": 12, "api_wallet": 3,
                }):
            result = admin_api._key_ping_heygen()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "credential_refresh_pending")

    def test_ping_without_key_configured_fails_fast(self):
        # 不联网：未配置密钥/地址时应直接返回错误而不发请求
        import unittest.mock as mock

        with mock.patch.object(admin_api, "_env_value", return_value=""), mock.patch.object(
            admin_api, "_ping_upstream", side_effect=AssertionError("不该发起网络请求")
        ):
            # xiaolevideo 是纯连通性拨测(有默认地址),无密钥也会真发请求,不在此列
            for key in ["openai", "deepseek", "gemini", "zelong", "zelong2", "heygen", "heygen_relay", "tikhub", "runninghub", "cosyvoice", "cos"]:
                out = admin_api.KEY_PINGS[key]()
                self.assertFalse(out["ok"], key)
                self.assertTrue(out.get("error"), key)

    def test_cosyvoice_ping_validates_the_key(self):
        import unittest.mock as mock

        with mock.patch.object(admin_api, "_env_value", return_value="test-key"), \
                mock.patch.object(
                    admin_api, "_ping_upstream", return_value={"ok": True}
                ) as ping:
            self.assertTrue(admin_api._key_ping_cosyvoice()["ok"])
        ping.assert_called_once_with(
            "POST",
            "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
            headers={
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
            body={
                "model": "voice-enrollment",
                "input": {"action": "list_voice", "page_index": 0, "page_size": 1},
            },
            proxied=False,
        )


if __name__ == "__main__":
    unittest.main()
