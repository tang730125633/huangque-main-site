import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = (ROOT / "site" / "workbench" / "settings.html").read_text(encoding="utf-8")


class PointTransferUiTests(unittest.TestCase):
    def test_settings_exposes_transfer_form_and_history(self):
        for text in (
            'id="pointsGiftForm"',
            'id="giftAccountId"',
            'id="giftAmount"',
            'id="giftHistory"',
            '每日最多',
            '邀请奖励独立记录，不计入可赠送余额',
        ):
            self.assertIn(text, SETTINGS)
        self.assertIn('id="pointsGiftCard" class="settings-card" hidden', SETTINGS)
        self.assertIn("user.membership_tier==='partner'||user.membership_tier==='initiator'", SETTINGS)
        self.assertIn("if(canGiftPoints(res.data.user)) loadGiftHistory(true);", SETTINGS)

    def test_recipient_is_resolved_before_password_confirmed_transfer(self):
        self.assertIn("/api/auth/points/transfer/recipient?account_id=", SETTINGS)
        self.assertIn('id="giftConfirmModal"', SETTINGS)
        self.assertIn('id="giftPassword" type="password"', SETTINGS)
        self.assertIn("/api/auth/points/transfer'", SETTINGS)
        self.assertIn("giftRecipient.account_id!==draft.recipient_account_id", SETTINGS)

    def test_transfer_request_has_client_idempotency_key(self):
        self.assertIn("window.crypto.randomUUID", SETTINGS)
        self.assertIn("request_id:giftRequestId", SETTINGS)
        self.assertIn("if(!giftRequestId) giftRequestId=newGiftRequestId()", SETTINGS)

    def test_ui_uses_account_id_and_safe_public_recipient_shape(self):
        self.assertIn("party.account_id", SETTINGS)
        self.assertNotIn("giftRecipient.username", SETTINGS)
        self.assertNotIn("party.username", SETTINGS)
        self.assertNotIn("giftRecipient.phone", SETTINGS)


if __name__ == "__main__":
    unittest.main()
