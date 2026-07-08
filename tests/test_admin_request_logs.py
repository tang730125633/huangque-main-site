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
        '9.9.9.9 - - [09/Jul/2026:08:43:00 +0800] "GET /index.html HTTP/1.1" 200 100 "-" "Mozilla/5.0"',
        "",
    ]
)


class RequestLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        self.tmp.write(SAMPLE)
        self.tmp.close()
        self.old_log = admin_api.NGINX_ACCESS_LOG
        admin_api.NGINX_ACCESS_LOG = pathlib.Path(self.tmp.name)

    def tearDown(self):
        admin_api.NGINX_ACCESS_LOG = self.old_log
        pathlib.Path(self.tmp.name).unlink(missing_ok=True)

    def test_only_api_paths_and_noise_hidden(self):
        items = admin_api.request_logs()["items"]
        paths = [x["path"] for x in items]
        self.assertEqual(len(items), 2)  # 静态页、claim 轮询、admin 自身都被滤掉
        self.assertTrue(all(p.startswith("/api/") for p in paths))
        self.assertNotIn("/api/claim?token=***", paths)

    def test_noise_included_and_secret_masked(self):
        items = admin_api.request_logs(include_noise=True)["items"]
        paths = [x["path"] for x in items]
        self.assertIn("/api/claim?token=***", paths)
        job = next(p for p in paths if p.startswith("/api/gen/job/42"))
        self.assertIn("api_key=***", job)
        self.assertIn("ratio=1%3A1", job)

    def test_status_filter(self):
        items = admin_api.request_logs(status="5")["items"]
        self.assertEqual([x["status"] for x in items], [500])
        items = admin_api.request_logs(status="200")["items"]
        self.assertTrue(all(x["status"] == 200 for x in items))

    def test_path_query_and_order(self):
        items = admin_api.request_logs(q="/api/gen/")["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["time"], "07-09 08:42:30")  # 最新在前

    def test_missing_log_file(self):
        admin_api.NGINX_ACCESS_LOG = pathlib.Path(self.tmp.name + ".nope")
        data = admin_api.request_logs()
        self.assertEqual(data["items"], [])
        self.assertIn("找不到", data["message"])


class KeyPingTests(unittest.TestCase):
    def test_pingable_flag_matches_registry(self):
        for item in admin_api.key_status():
            self.assertEqual(item["pingable"], item["key"] in admin_api.KEY_PINGS)
        self.assertEqual(
            set(admin_api.KEY_PINGS), {"openai", "heygen", "tikhub", "runninghub"}
        )

    def test_ping_without_key_configured_fails_fast(self):
        # 不联网：未配置密钥时应直接返回错误而不发请求
        import unittest.mock as mock

        with mock.patch.object(admin_api, "_env_value", return_value=""):
            for fn in admin_api.KEY_PINGS.values():
                out = fn()
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"], "密钥未配置")


if __name__ == "__main__":
    unittest.main()
