import pathlib
import tempfile
import unittest

import server.admin_api as admin_api


SAMPLE = "\n".join(
    [
        '127.0.0.1 - - [09/Jul/2026:08:41:19 +0800] "GET /api/claim?token=worker-secret HTTP/1.1" 200 12 "-" "Python-urllib/3.11"',
        '127.0.0.1 - - [09/Jul/2026:08:41:30 +0800] "GET /api/admin/overview?days=7 HTTP/1.1" 200 900 "-" "Mozilla/5.0"',
        '1.2.3.4 - - [09/Jul/2026:08:42:00 +0800] "POST /api/gen/image HTTP/1.1" 500 88 "-" "Mozilla/5.0"',
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
        self.assertEqual([x["status"] for x in items], [500])
        items = admin_api.request_logs(status="200")["items"]
        self.assertTrue(items and all(x["status"] == 200 for x in items))

    def test_merge_across_files_sorted_desc(self):
        items = admin_api.request_logs(q="/api/")["items"]
        times = [x["time"] for x in items]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertIn("/api/keywords", [x["path"] for x in items])  # 来自第二个文件

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
        c = sqlite3.connect(str(self.db_path))
        c.execute(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY, username TEXT, kind TEXT,"
            " cost INTEGER, status TEXT, payload TEXT, created_at INTEGER, updated_at INTEGER)"
        )
        import time as _time

        now = int(_time.time())
        c.execute(
            "INSERT INTO jobs VALUES(1226,'tang','xiaole_video',13,'done','{}',?,?)",
            (now - 100, now - 40),
        )
        c.commit()
        c.close()

        self.old_logs = admin_api.NGINX_ACCESS_LOGS
        self.old_db = admin_api.JOB_DB
        admin_api.NGINX_ACCESS_LOGS = [self.log_path]
        admin_api.JOB_DB = self.db_path

    def tearDown(self):
        admin_api.NGINX_ACCESS_LOGS = self.old_logs
        admin_api.JOB_DB = self.old_db
        self.log_path.unlink(missing_ok=True)
        self.db_path.unlink(missing_ok=True)

    def test_enrichment(self):
        items = {x["path"]: x for x in admin_api.request_logs()["items"]}
        poll = items["/api/gen/job/1226"]
        self.assertEqual(poll["user"], "tang")
        self.assertEqual(poll["func"], "视频 · 小乐 · 轮询")
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
        self.assertEqual(sources, {"job", "http"})
        # 任务行：带用户/功能/点数；时间线按时间倒序
        job_rows = [x for x in items if x["source"] == "job"]
        self.assertEqual(job_rows[0]["user"], "tang")
        self.assertEqual(job_rows[0]["cost"], 13)
        self.assertEqual(job_rows[0]["cat"], "ok")
        times = [x["time"] for x in items]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_activity_filters(self):
        # source 过滤
        only_jobs = admin_api.activity_logs(source="job")["items"]
        self.assertTrue(only_jobs and all(x["source"] == "job" for x in only_jobs))
        only_http = admin_api.activity_logs(source="http")["items"]
        self.assertTrue(only_http and all(x["source"] == "http" for x in only_http))
        # 统一状态：fail = HTTP >=400（本样本 404）
        fails = admin_api.activity_logs(category="fail")["items"]
        self.assertTrue(fails and all(x["cat"] == "fail" for x in fails))
        # 关键词搜用户名 → 命中任务行
        hit = admin_api.activity_logs(q="tang")["items"]
        self.assertTrue(hit and all("tang" in (x["user"] or "") or "tang" in x["path"] for x in hit))

    def test_activity_fail_filter_not_crowded_out(self):
        # 404 行不在最新 2 条里；fail 条件下推到采集层后依然能查到
        fails = admin_api.activity_logs(category="fail", limit=2, source="http")["items"]
        self.assertTrue(any(x["status_text"] == "404" for x in fails))


class JobPayloadTests(unittest.TestCase):
    def test_truncated_payload_still_names_func(self):
        # 模拟 substr 截断的 payload：JSON 不完整,但 mode 字段在前缀里
        truncated = '{"mode": "text", "prompt": "' + "x" * 5000
        data = admin_api._job_payload(truncated)
        self.assertEqual(data.get("mode"), "text")
        self.assertEqual(admin_api.call_func_name("video", data), "视频 · 文案口播")
        # 完整 JSON 走正常解析
        self.assertEqual(admin_api._job_payload('{"model": "nb2"}'), {"model": "nb2"})
        self.assertEqual(admin_api._job_payload(None), {})


class KeyPingTests(unittest.TestCase):
    def test_every_key_group_is_pingable(self):
        for item in admin_api.key_status():
            self.assertTrue(item["pingable"], item["key"])
            self.assertIn(item["key"], admin_api.KEY_PINGS)
        self.assertEqual(
            set(admin_api.KEY_PINGS),
            {
                "openai", "gemini", "zelong", "zelong2", "heygen", "heygen_relay",
                "xiaolevideo", "runninghub", "doubao", "tikhub", "cos",
            },
        )

    def test_ping_without_key_configured_fails_fast(self):
        # 不联网：未配置密钥/地址时应直接返回错误而不发请求
        import unittest.mock as mock

        with mock.patch.object(admin_api, "_env_value", return_value=""), mock.patch.object(
            admin_api, "_ping_upstream", side_effect=AssertionError("不该发起网络请求")
        ):
            # doubao/xiaolevideo 是纯连通性拨测(有默认地址),无密钥也会真发请求,不在此列
            for key in ["openai", "gemini", "zelong", "zelong2", "heygen", "heygen_relay", "tikhub", "runninghub", "cos"]:
                out = admin_api.KEY_PINGS[key]()
                self.assertFalse(out["ok"], key)
                self.assertTrue(out.get("error"), key)


if __name__ == "__main__":
    unittest.main()
