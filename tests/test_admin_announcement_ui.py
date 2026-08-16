import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_HTML = ROOT / "site" / "admin" / "index.html"


class AdminAnnouncementUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = ADMIN_HTML.read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", cls.html, re.S | re.I)
        cls.inline_script = "\n".join(script for script in scripts if script.strip())
        announcement_start = cls.inline_script.index("var ANNOUNCEMENT_TIERS=")
        announcement_end = cls.inline_script.index("function closePasswordReset", announcement_start)
        cls.announcement_script = cls.inline_script[announcement_start:announcement_end]

    def test_sidebar_module_and_api_contract_are_wired(self):
        self.assertIn('data-module-tab="announcements">消息与公告</button>', self.html)
        self.assertIn('data-module="announcements"', self.html)
        self.assertIn("announcements:'message'", self.inline_script)
        self.assertIn("if(name==='announcements'){loadAnnouncements()}", self.inline_script)
        self.assertIn("api('/api/admin/announcements/preview'", self.announcement_script)
        self.assertIn("JSON.stringify({audience:audience})", self.announcement_script)
        self.assertIn("api('/api/admin/announcements',{method:'POST'", self.announcement_script)
        self.assertIn("payload={title:draft.title,detail:draft.detail,audience:draft.audience,wechat_push:draft.wechatPush,request_id:state.announcementRequestId}", self.announcement_script)
        self.assertIn("api('/api/admin/announcements?limit=50')", self.announcement_script)
        self.assertIn("+'/recall',{method:'POST'", self.announcement_script)

    def test_only_official_audience_tiers_are_selectable(self):
        values = set(re.findall(
            r'<input\b(?=[^>]*\bdata-announcement-tier\b)[^>]*\bvalue="([^"]+)"',
            self.html,
        ))
        self.assertEqual(values, {"experience", "partner", "initiator"})
        self.assertIn("var ANNOUNCEMENT_TIERS=['experience','partner','initiator']", self.inline_script)
        self.assertIn('name="announcementAudienceMode" value="all"', self.html)
        self.assertIn('name="announcementAudienceMode" value="tiers"', self.html)
        for forbidden in ("founder", "vip", "member", "admin"):
            self.assertNotIn(f'data-announcement-tier value="{forbidden}"', self.html)

    def test_edit_invalidates_preview_and_publish_has_idempotency_gate(self):
        self.assertIn("function announcementDraftChanged(){invalidateAnnouncementPreview();renderAnnouncementEffect();updateAnnouncementControls()}", self.announcement_script)
        self.assertIn("state.announcementPreview=null;state.announcementPreviewSignature='';state.announcementRequestId=''", self.announcement_script)
        self.assertIn("state.announcementPreviewSignature!==announcementSignature()", self.announcement_script)
        self.assertIn("if(value==null)value=preview.count", self.announcement_script)
        self.assertIn("if(state.announcementPublishing)return", self.announcement_script)
        self.assertIn("if(!state.announcementRequestId)state.announcementRequestId=nextAnnouncementRequestId()", self.announcement_script)
        self.assertIn("重试将复用本次请求编号", self.announcement_script)
        self.assertIn("已送达微信的消息无法撤回", self.announcement_script)
        self.assertIn("confirm('确认发布这条公告？", self.announcement_script)
        self.assertRegex(self.html, r'id="announcementPublish"[^>]*disabled')

    def test_announcement_user_content_uses_text_nodes_only(self):
        self.assertNotIn(".innerHTML", self.announcement_script)
        self.assertNotIn("insertAdjacentHTML", self.announcement_script)
        self.assertIn("document.createElement", self.announcement_script)
        self.assertIn("node.textContent=text", self.announcement_script)
        self.assertIn("tier==='none'?'非会员/过期'", self.announcement_script)
        self.assertIn("white-space:pre-wrap", self.html)

    def test_responsive_accessible_editor_states_are_present(self):
        self.assertRegex(
            self.html,
            r"@media \(max-width:900px\).*?\.announcement-workspace\{grid-template-columns:1fr\}",
        )
        self.assertRegex(
            self.html,
            r"@media \(max-width:560px\).*?\.announcement-tiers\{grid-template-columns:1fr\}",
        )
        self.assertIn('label class="announcement-label" for="announcementTitle"', self.html)
        self.assertIn('label class="announcement-label" for="announcementDetail"', self.html)
        self.assertIn("<fieldset class=\"announcement-audience\">", self.html)
        self.assertIn("<legend>发布受众</legend>", self.html)
        self.assertGreaterEqual(self.html.count('aria-live="polite"'), 3)
        self.assertIn(".announcement-panel input:focus-visible", self.html)
        for state in ("loading", "success", "error"):
            self.assertIn("'" + state + "'", self.announcement_script)

    def test_inline_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, "--check", "-"],
            input=self.inline_script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preview_count_and_none_breakdown_runtime_contract(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        total_start = self.announcement_script.index("function announcementPreviewTotal")
        total_end = self.announcement_script.index("function setAnnouncementState", total_start)
        breakdown_start = self.announcement_script.index("function announcementBreakdown")
        breakdown_end = self.announcement_script.index("function renderAnnouncementBreakdown", breakdown_start)
        probe = "\n".join([
            "var ANNOUNCEMENT_TIERS=['experience','partner','initiator'];",
            self.announcement_script[total_start:total_end],
            self.announcement_script[breakdown_start:breakdown_end],
            "if(announcementPreviewTotal({count:17})!==17)throw new Error('count not supported');",
            "if(announcementPreviewTotal({count:'invalid'})!==0)throw new Error('invalid count not rejected');",
            "var counts=announcementBreakdown({breakdown:{experience:4,partner:2,initiator:1,none:10}});",
            "if(counts.none!==10)throw new Error('none breakdown not supported');",
        ])
        result = subprocess.run(
            [node, "-e", probe], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
