import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
from content_domains import video


HTML = (pathlib.Path(__file__).resolve().parents[1] / "site/workbench/video.html").read_text(encoding="utf-8")


class TalkingAvatarSafetyTests(unittest.TestCase):
    def test_consent_is_required_and_audited(self):
        with self.assertRaisesRegex(ValueError, "肖像"):
            video.require_portrait_consent({})
        body = video.require_portrait_consent({"portrait_authorized": True})
        self.assertEqual(body["portrait_consent_version"], "2026-07")
        self.assertGreater(body["portrait_consent_at"], 0)

    def test_visual_review_rejects_non_person(self):
        answer = {"choices": [{"message": {"content": json.dumps({
            "real_person": False, "face_count": 0, "face_clear": False
        })}}]}
        with patch.object(video, "_post", return_value=answer):
            with self.assertRaisesRegex(ValueError, "真人"):
                video.review_person_image("data:image/png;base64,iVBORw0KGgo=")

    def test_visual_review_accepts_one_clear_real_face(self):
        answer = {"choices": [{"message": {"content": json.dumps({
            "real_person": True, "face_count": 1, "face_clear": True
        })}}]}
        with patch.object(video, "_post", return_value=answer):
            video.review_person_image("data:image/png;base64,iVBORw0KGgo=")

    def test_page_has_limits_consent_retry_and_idempotency(self):
        for needle in (
            "单张不超过 8MB", 'id="portraitConsent"', "我已获得人物肖像及声音使用授权",
            'id="talkingRetryBtn"', "网络异常，请检查网络后重试", "validateTalkingImageFile",
            "'Idempotency-Key':talkingRequestKey",
        ):
            self.assertIn(needle, HTML)


if __name__ == "__main__":
    unittest.main()
