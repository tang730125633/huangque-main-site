import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CreatorAgentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "site/workbench/creator-agent.html").read_text(encoding="utf-8")
        cls.auth = (ROOT / "server/auth_server.py").read_text(encoding="utf-8")
        cls.service = (ROOT / "server/creator_agent/service.py").read_text(encoding="utf-8")
        cls.flags = (ROOT / "server/content_domains/feature_flags.py").read_text(encoding="utf-8")
        cls.registry = (ROOT / "server/content_domains/function_registry.py").read_text(encoding="utf-8")
        cls.shell = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
        cls.nginx = (ROOT / "deploy/nginx-huangquechuanmei.conf").read_text(encoding="utf-8")
        cls.unit = (ROOT / "deploy/systemd/huangque-creator-agent.service").read_text(encoding="utf-8")
        cls.release = (ROOT / "deploy/creator-agent-release.sh").read_text(encoding="utf-8")
        cls.design = (ROOT / "docs/创作Agent-V1设计与并行边界.md").read_text(encoding="utf-8")

    def test_product_name_and_two_column_surface(self):
        self.assertIn("AI 创作助手", self.page)
        self.assertIn("黄雀创作 Agent", self.page)
        self.assertIn('class="ca-chat"', self.page)
        self.assertIn('class="ca-output"', self.page)
        self.assertIn('data-tab="progress"', self.page)
        self.assertIn('data-tab="content"', self.page)
        self.assertIn('data-tab="tasks"', self.page)

    def test_single_continuous_conversation_has_no_new_conversation_ui(self):
        self.assertNotIn("新建会话", self.page)
        self.assertNotIn("历史会话", self.page)
        self.assertNotIn("newConversation", self.page)
        self.assertIn("画像项目", self.page)

    def test_only_three_platforms_and_no_official_account_or_bilibili(self):
        planner = (ROOT / "server/creator_agent/planner.py").read_text(encoding="utf-8")
        constant = planner[planner.index("ALLOWED_PLATFORMS"):planner.index("PLATFORM_LABELS")]
        self.assertIn("douyin", constant)
        self.assertIn("xiaohongshu", constant)
        self.assertIn("wechat_channels", constant)
        self.assertNotIn("wechat_official", constant)
        self.assertNotIn("bilibili", constant)

    def test_decisions_stay_left_and_right_is_display_only(self):
        output = self.page[self.page.index('<aside class="ca-output">'):self.page.index('</aside>')]
        self.assertNotIn("data-intent", output)
        self.assertNotIn("确认扣点", output)
        self.assertIn("confirm_payment", self.page)
        self.assertIn("confirm('确认消耗 ", self.page)

    def test_feature_switch_is_default_on_but_still_discoverable(self):
        block = self.flags[self.flags.index('"key": "creator_agent_v1"'):]
        self.assertIn('"default_enabled": True', block[:500])
        self.assertIn('data-nav-feature="creator_agent_v1"', self.shell)
        self.assertIn("/api/creator-agent/capability", self.shell)

    def test_independent_namespace_does_not_edit_hermes_source(self):
        self.assertIn("/api/auth/internal/creator-agent/catalog", self.auth)
        self.assertIn("/api/auth/internal/creator-agent/action", self.auth)
        self.assertIn("不修改同事的 `server/hermes_ip12/**`", self.design)
        self.assertIn("CREATOR_AGENT_IP12_URL", (
            ROOT / "deploy/huangque-secrets.env.example"
        ).read_text(encoding="utf-8"))

    def test_registry_and_shell_expose_agent_as_real_page(self):
        self.assertIn('(\"creator-agent\", \"AI 创作助手\", \"/workbench/creator-agent.html\")', self.registry)
        self.assertIn('href="creator-agent.html" class="hq-side-bots hq-side-ai-entry', self.shell)
        nav = self.shell[self.shell.index("var NAV=["):self.shell.index("];", self.shell.index("var NAV=["))]
        self.assertNotIn("creator-agent", nav)

    def test_paid_confirmation_is_idempotent_and_tokens_are_private(self):
        service = (ROOT / "server/creator_agent/service.py").read_text(encoding="utf-8")
        store = (ROOT / "server/creator_agent/store.py").read_text(encoding="utf-8")
        self.assertIn("claim_confirmation", service)
        self.assertIn("idempotency_key=idempotency_key", service)
        self.assertIn("submit_idempotency_key TEXT NOT NULL", store)
        self.assertIn('_PRIVATE_KEYS = {"quote_token", "job_id", "idempotency_key", "confirmation_id"}', service)
        self.assertIn("quote_token TEXT NOT NULL", store)

    def test_quote_expiry_is_absolute_atomic_and_visible(self):
        store = (ROOT / "server/creator_agent/store.py").read_text(encoding="utf-8")
        content = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        idempotency = (
            ROOT / "server/content_domains/submission_idempotency.py"
        ).read_text(encoding="utf-8")
        attempt = (
            ROOT / "server/content_domains/matrix_template_submission.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"expires_at": claims["e"]', self.auth)
        self.assertIn("quote_expires_at INTEGER NOT NULL", store)
        self.assertIn("submit_quote_expires_at INTEGER NOT NULL", store)
        self.assertIn("raise QuoteExpired", store)
        self.assertIn("ca-quote-expiry", self.page)
        self.assertIn("报价已过期，请重新报价", self.page)
        self.assertIn("expected_quote_expires_at", self.page)
        self.assertIn("/api/auth/internal/creator-agent/reconcile", self.auth)
        self.assertIn("/api/gen/internal/submission-reconcile", content)
        self.assertIn("accept_in_transaction", idempotency)
        self.assertIn("self.bridge.reconcile", self.service)
        self.assertIn("matrix_template_submission_attempts", attempt)
        self.assertIn("charge_key TEXT NOT NULL UNIQUE", attempt)
        self.assertIn("refund_key TEXT NOT NULL UNIQUE", attempt)
        self.assertIn("'prepared','charging','charged','refund_pending'", attempt)
        self.assertIn("_retry_matrix_template_submissions", content)
        self.assertIn("matrix_template_submission.recoverable", content)

    def test_browser_persists_and_recovers_pending_requests(self):
        self.assertIn("hq-creator-agent-pending-v2", self.page)
        self.assertIn("savePending(pending);executePending(pending)", self.page)
        self.assertIn("project_id:currentProject().id", self.page)
        self.assertIn("payload.expected_revision=batch.revision", self.page)
        self.assertIn("pending.body.intent==='confirm_payment'", self.page)
        self.assertIn("'/refresh'", self.page)

    def test_service_is_loopback_and_release_is_atomic(self):
        self.assertIn("--host 127.0.0.1 --port 8114", self.unit)
        self.assertIn("NoNewPrivileges=true", self.unit)
        self.assertIn("ReadWritePaths=/var/lib/huangque-creator-agent", self.unit)
        self.assertIn("EnvironmentFile=-/home/ubuntu/auth-service/auth.env", self.unit)
        self.assertIn('environment.get("HQ_INTERNAL_TOKEN")', self.service)
        self.assertIn("proxy_pass http://127.0.0.1:8114/", self.nginx)
        self.assertIn('CURRENT="$RUNTIME/current"', self.release)
        self.assertIn('mv -Tf "$CURRENT.next" "$CURRENT"', self.release)
        self.assertIn("nginx -t", self.release)
        self.assertIn('d.get("ready") is True', self.release)

    def test_deploy_contract_does_not_duplicate_shared_internal_token(self):
        example = (ROOT / "deploy/huangque-secrets.env.example").read_text(encoding="utf-8")
        self.assertIn("HQ_INTERNAL_TOKEN=", example)
        creator_block = example[example.index("### /etc/huangque/creator-agent.env"):]
        self.assertNotIn("CREATOR_AGENT_INTERNAL_TOKEN=", creator_block)
        self.assertIn("/api/auth/internal/creator-agent/health", self.auth)

    def test_inline_javascript_parses(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", self.page)
        source = next(value for value in reversed(scripts) if value.strip())
        result = subprocess.run(["node", "--check", "-"], input=source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_qa_covers_desktop_and_mobile(self):
        source = (ROOT / "tests/creator_agent_page_browser.js").read_text(encoding="utf-8")
        self.assertIn("desktop: { width: 1440, height: 900 }", source)
        self.assertIn("mobile: { width: 390, height: 844 }", source)
        self.assertIn("document.documentElement.scrollWidth", source)


if __name__ == "__main__":
    unittest.main()
