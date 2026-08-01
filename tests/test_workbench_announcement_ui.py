from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")


class WorkbenchAnnouncementUITests(unittest.TestCase):
    def test_announcement_is_one_server_notice_with_durable_actions(self):
        for marker in (
            "String(x.kind||'system')==='announcement'",
            "x.read=x.serverId?!!x.serverReadAt:read.indexOf(x.id)>=0",
            "/api/auth/notifications/read-all",
            "+encodeURIComponent(x.serverId)+'/read'",
            "'/snooze-today'",
            "maybeOpenAnnouncement();",
            "eligibleAnnouncement(newest)",
        ):
            self.assertIn(marker, SHELL)

    def test_legacy_local_reads_sync_only_for_ordinary_server_notices(self):
        self.assertIn(
            "if(!x.isAnnouncement&&!x.read&&x.serverId&&read.indexOf(x.id)>=0){x.read=true;syncLegacyNoticeRead(x)}",
            SHELL,
        )
        self.assertIn("var _legacyNoticeReadSync={};", SHELL)
        self.assertIn("delete _legacyNoticeReadSync[x.serverId]", SHELL)

    def test_recalled_open_announcement_closes_on_not_found(self):
        self.assertIn("e.status=r.status;throw e", SHELL)
        self.assertIn(
            "if(err&&err.status===404){closeAnnouncement();loadNotices();return}",
            SHELL,
        )

    def test_dialog_uses_safe_text_and_accessible_reduced_motion_states(self):
        for marker in (
            "ov.id='hqAnnouncementOv'",
            'role="dialog" aria-modal="true"',
            "document.getElementById('hqAnnouncementTitle').textContent",
            "document.getElementById('hqAnnouncementDetail').textContent",
            "今日不再提醒",
            "在消息中心查看",
            "prefers-reduced-motion:reduce",
            "e.key==='Escape'",
            "e.key!=='Tab'",
            "@media(max-width:640px)",
        ):
            self.assertIn(marker, SHELL)

    def test_announcements_bypass_only_the_optional_system_notice_filter(self):
        self.assertIn("if(!announcement&&!enabled('systemNotices',true)) return;", SHELL)
        self.assertNotIn("innerHTML=notice.title", SHELL)
        self.assertNotIn("innerHTML=notice.detail", SHELL)


if __name__ == "__main__":
    unittest.main()
