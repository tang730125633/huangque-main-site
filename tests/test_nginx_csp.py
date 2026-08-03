import re
import unittest
from pathlib import Path


class NginxCspTest(unittest.TestCase):
    CONFIGS = (
        "deploy/nginx-huangquechuanmei.conf",
        "server/nginx-huangquechuanmei.conf",
    )

    def _config(self, relative_path):
        return (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")

    def test_csp_is_active_and_consistent(self):
        for relative_path in self.CONFIGS:
            with self.subTest(config=relative_path):
                config = self._config(relative_path)
                policies = re.findall(
                    r'add_header Content-Security-Policy "([^"]+)" always;',
                    config,
                )

                self.assertEqual(len(policies), 4)
                self.assertTrue(all(policy == policies[0] for policy in policies))
                for directive in (
                    "base-uri 'self'",
                    "object-src 'none'",
                    "frame-ancestors 'none'",
                    "script-src 'self' 'unsafe-inline' https://unpkg.com",
                    "style-src 'self' 'unsafe-inline' https://unpkg.com",
                ):
                    self.assertIn(directive, policies[0])

    def test_security_headers_cover_server_and_header_overrides(self):
        expected = (
            'add_header Strict-Transport-Security "max-age=31536000" always;',
            'add_header X-Frame-Options "DENY" always;',
            'add_header X-Content-Type-Options "nosniff" always;',
            'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        )
        for relative_path in self.CONFIGS:
            config = self._config(relative_path)
            with self.subTest(config=relative_path):
                self.assertEqual(config.count("server_tokens off;"), 2)
                for header in expected:
                    self.assertEqual(config.count(header), 4, header)

    def test_root_serves_the_marketing_homepage(self):
        for relative_path in self.CONFIGS:
            config = self._config(relative_path)
            with self.subTest(config=relative_path):
                start = config.index("location = / {")
                end = config.index("\n    }", start)
                block = config[start:end]
                self.assertIn("try_files /index.html =404;", block)
                self.assertNotIn("return 302", block)

    def test_request_id_and_duration_are_logged(self):
        for relative_path in self.CONFIGS:
            config = self._config(relative_path)
            with self.subTest(config=relative_path):
                self.assertIn("log_format huangque_observed", config)
                self.assertIn("rt=$request_time rid=$request_id", config)
                self.assertIn(
                    "access_log /var/log/nginx/huangquechuanmei.access.log huangque_observed;",
                    config,
                )
                self.assertIn("add_header X-Request-ID $request_id always;", config)

        deploy = self._config("deploy/nginx-huangquechuanmei.conf")
        self.assertEqual(deploy.count("proxy_set_header X-Request-ID $request_id;"), 2)

    def test_workbench_ip12_proxies_directly_to_git_managed_hermes(self):
        config = self._config("deploy/nginx-huangquechuanmei.conf")
        self.assertIn(
            "location = /workbench/ip12 { return 301 /workbench/ip12/; }",
            config,
        )
        start = config.index("location ^~ /workbench/ip12/")
        end = config.index("\n    }", start)
        block = config[start:end]
        self.assertIn("proxy_pass http://127.0.0.1:3102/;", block)
        self.assertNotIn("127.0.0.1:3101", block)
        self.assertIn('proxy_set_header Accept-Encoding "";', block)
        self.assertIn("proxy_request_buffering off;", block)
        self.assertIn("proxy_buffering off;", block)
        self.assertIn("client_max_body_size 200m;", block)
        for expected in (
            "sub_filter '\"/api/' '\"/workbench/ip12/api/';",
            "sub_filter \"'/api/\" \"'/workbench/ip12/api/\";",
            "sub_filter '`/api/' '`/workbench/ip12/api/';",
            "sub_filter '\"/media/' '\"/workbench/ip12/media/';",
            "sub_filter \"'/analytics'\" \"'/workbench/ip12/analytics'\";",
            "sub_filter \"'/classic'\" \"'/workbench/ip12/classic'\";",
            "sub_filter \"'/skills'\" \"'/workbench/ip12/skills'\";",
            "sub_filter 'href=\"/agnes-lab\"' 'href=\"/workbench/ip12/agnes-lab\"';",
            "sub_filter 'href=\"/video-factory\"' 'href=\"/workbench/ip12/video-factory\"';",
            "sub_filter 'href=\"/\"' 'href=\"/workbench/ip12/\"';",
        ):
            self.assertIn(expected, block)
        self.assertNotIn("text/event-stream", block)
        self.assertNotIn("sub_filter '\"/' '\"/workbench/ip12/';", block)
        self.assertNotIn("sub_filter \"'/\" \"'/workbench/ip12/\";", block)
        self.assertNotIn("sub_filter '`/' '`/workbench/ip12/';", block)
        self.assertNotIn("auth_basic", block)

    def test_direct_3101_gateway_uses_the_same_flask_service(self):
        config = self._config("deploy/nginx-hermes-ip12-direct.conf")
        self.assertIn("listen 3101;", config)
        self.assertIn("proxy_pass http://127.0.0.1:3102;", config)
        self.assertIn("client_max_body_size 200m;", config)

    def test_cli_image_upload_is_streamed_and_bounded(self):
        for relative_path in self.CONFIGS:
            config = self._config(relative_path)
            with self.subTest(config=relative_path):
                start = config.index("location = /api/auth/cli/image-upload {")
                end = config.index("\n    }", start)
                block = config[start:end]
                self.assertIn("proxy_pass http://127.0.0.1:8095;", block)
                self.assertIn("proxy_request_buffering off;", block)
                self.assertIn("proxy_buffering off;", block)
                self.assertIn("client_max_body_size 10m;", block)
                self.assertIn("limit_req zone=hq_cli_upload_rate burst=8 nodelay;", block)
                self.assertIn("limit_conn hq_cli_upload_conn 2;", block)
                self.assertIn("client_body_timeout 20s;", block)
                self.assertIn(
                    "limit_req_zone $binary_remote_addr zone=hq_cli_upload_rate:10m rate=12r/m;",
                    config,
                )
                self.assertIn(
                    "limit_conn_zone $binary_remote_addr zone=hq_cli_upload_conn:10m;",
                    config,
                )
                self.assertIn('proxy_set_header X-HQ-Internal-Token "";', block)

    def test_card_media_upload_has_a_bounded_streaming_route(self):
        config = self._config("deploy/nginx-huangquechuanmei.conf")
        start = config.index("location = /api/auth/card/media {")
        end = config.index("\n    }", start)
        block = config[start:end]
        self.assertIn("proxy_pass http://127.0.0.1:8095;", block)
        self.assertIn("proxy_request_buffering off;", block)
        self.assertIn("client_max_body_size 30m;", block)
        self.assertIn("client_body_timeout 90s;", block)
        self.assertIn("limit_conn hq_cli_upload_conn 2;", block)

    def test_hermes_runbook_updates_the_actively_loaded_main_site_config(self):
        runbook = self._config("deploy/生产环境清单与还原手册.md")
        release = self._config("deploy/hermes-ip12-release.sh")
        active = "/etc/nginx/sites-enabled/huangquechuanmei"
        self.assertIn("deploy/hermes-ip12-release.sh", runbook)
        self.assertIn(
            f"NGINX_SITE_ENABLED=\"${{HERMES_NGINX_SITE_ENABLED:-{active}}}\"",
            release,
        )
        self.assertIn(
            'backup_file "$NGINX_SITE_ENABLED" '
            "nginx-huangquechuanmei-enabled.conf",
            release,
        )
        self.assertIn(
            '"$HERMES_RELEASE_DIR/deploy/nginx-huangquechuanmei.conf" '
            '"$NGINX_SITE_ENABLED"',
            release,
        )
        self.assertIn(
            'restore_file "$backup/nginx-huangquechuanmei-enabled.conf"',
            release,
        )
        self.assertIn(
            '"$backup/nginx-huangquechuanmei-enabled.conf.state" '
            '"$NGINX_SITE_ENABLED"',
            release,
        )


if __name__ == "__main__":
    unittest.main()
