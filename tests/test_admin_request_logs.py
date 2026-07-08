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
