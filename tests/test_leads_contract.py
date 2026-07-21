# -*- coding: utf-8 -*-
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))


class LeadsContractTests(unittest.TestCase):
    def test_cost_matches_the_paid_job_formula(self):
        from content_domains.leads_contract import leads_cost, validate_compliance

        self.assertEqual(11, leads_cost(20, 1))
        self.assertEqual(28, leads_cost(30, 3))
        from content_domains import points
        self.assertEqual(11, points.cost_of("leads", {"count": 20, "pages": 1}))
        with self.assertRaisesRegex(ValueError, "合规"):
            validate_compliance({})
        now = int(time.time())
        validate_compliance({"compliance_version": "leads-v1", "compliance_accepted_at": now})
        with self.assertRaisesRegex(ValueError, "重新确认"):
            validate_compliance({"compliance_version": "leads-v1", "compliance_accepted_at": now - 601})

    @mock.patch("content_domains.leads_contract.time.time", return_value=1_784_627_500)
    def test_normalized_lead_has_a_stable_id_and_auditable_times(self, _now):
        from content_domains.leads_contract import classify_comments

        raw = [{
            "platform": "douyin", "comment_id": "c-123", "user_id": "u-1",
            "content": "多少钱一次", "time": 1_784_620_000_000,
            "source_id": "video-9", "video_url": "https://example.test/video-9",
        }]
        first = classify_comments(raw, lambda _t: False, lambda _t: True, lambda _t: {})
        second = classify_comments(raw, lambda _t: False, lambda _t: True, lambda _t: {})
        lead = first["leads"][0]

        self.assertEqual(first["leads"][0]["lead_id"], second["leads"][0]["lead_id"])
        self.assertRegex(lead["legacy_lead_id"], r"^[0-9a-f]{16}$")
        self.assertEqual("c-123", lead["source_comment_id"])
        self.assertEqual("video-9", lead["source_id"])
        self.assertEqual(1_784_620_000, lead["comment_time"])
        self.assertEqual(1_784_627_500, lead["collected_at"])

    def test_every_raw_comment_is_explained_by_the_statistics(self):
        from content_domains.leads_contract import classify_comments

        raw = [
            {"platform": "douyin", "comment_id": "1", "user_id": "u1", "content": "多少钱"},
            {"platform": "douyin", "comment_id": "1", "user_id": "u1", "content": "多少钱"},
            {"platform": "douyin", "comment_id": "2", "user_id": "u2", "content": "广告"},
            {"platform": "douyin", "comment_id": "3", "user_id": "u3", "content": "好看"},
            {"platform": "douyin", "comment_id": "4", "user_id": "u4", "content": ""},
        ]
        result = classify_comments(
            raw,
            lambda text: text == "广告",
            lambda text: text == "多少钱",
            lambda _text: {"intent": "咨询", "intent_score": 90, "intent_reason": "测试"},
        )

        self.assertEqual(5, result["total"])
        self.assertEqual(1, result["leads_count"])
        self.assertEqual(1, result["deduped"])
        self.assertEqual(1, result["spam"])
        self.assertEqual(1, result["chat"])
        self.assertEqual(1, result["empty"])
        self.assertEqual(
            result["total"],
            result["leads_count"] + result["deduped"] + result["spam"] + result["chat"] + result["empty"],
        )

    def test_production_entry_uses_shared_intent_profiles_and_keeps_likes(self):
        import leadgen_api

        search = {"items": [{"id": "v1", "title": "样片", "url": "https://example.test/v1"}], "has_more": False}
        comments = {"items": [{
            "text": "多少钱一次", "user_id": "u1", "user": "甲", "likes": 8,
            "time": 1_784_620_000, "cid": "c1",
        }], "has_more": False}
        with mock.patch.object(leadgen_api.tikhub, "search", return_value=search), \
             mock.patch.object(leadgen_api.tikhub, "comments", return_value=comments):
            result = leadgen_api.gen_leads({"keyword": "美业", "platforms": ["douyin"], "count": 1, "pages": 1})
        self.assertEqual("价格敏感", result["leads"][0]["intent"])
        self.assertEqual(8, result["leads"][0]["like_count"])

    def test_recent_paid_lead_job_enforces_a_user_cooldown(self):
        import leadgen_api

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            old_db = leadgen_api.JOB_DB
            leadgen_api.JOB_DB = os.path.join(tmp, "jobs.db")
            try:
                with sqlite3.connect(leadgen_api.JOB_DB) as db:
                    db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,username TEXT,created_at INTEGER)")
                    db.execute("INSERT INTO jobs(kind,username,created_at) VALUES('leads','alice',?)", (int(time.time()),))
                self.assertGreater(leadgen_api.lead_submit_retry_after("alice"), 0)
                self.assertEqual(0, leadgen_api.lead_submit_retry_after("bob"))
            finally:
                leadgen_api.JOB_DB = old_db

    def test_cooldown_falls_back_to_memory_when_the_job_db_is_unavailable(self):
        import leadgen_api

        old_db, old_marks = leadgen_api.JOB_DB, dict(leadgen_api._LEADS_SUBMIT_AT)
        try:
            leadgen_api.JOB_DB = os.path.join(tempfile.gettempdir(), "missing-leads-db", "jobs.db")
            leadgen_api._LEADS_SUBMIT_AT["alice"] = int(time.time())
            self.assertGreater(leadgen_api.lead_submit_retry_after("alice"), 0)
        finally:
            leadgen_api.JOB_DB = old_db
            leadgen_api._LEADS_SUBMIT_AT.clear()
            leadgen_api._LEADS_SUBMIT_AT.update(old_marks)

    def test_get_points_still_fails_closed_to_zero_when_auth_db_is_unavailable(self):
        import leadgen_api

        old_db = leadgen_api.AUTH_DB
        try:
            leadgen_api.AUTH_DB = os.path.join(tempfile.gettempdir(), "missing-auth-db", "users.db")
            self.assertEqual(0, leadgen_api.get_points("alice"))
        finally:
            leadgen_api.AUTH_DB = old_db

    def test_legacy_crm_record_is_applied_without_replacing_the_new_id(self):
        from content_domains import leads

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            old_db = leads.LEADS_CRM_DB
            leads.LEADS_CRM_DB = os.path.join(tmp, "crm.db")
            try:
                old_id, new_id = "0123456789abcdef", "fedcba9876543210"
                leads.upsert_crm("alice", {"lead_id": old_id, "follow_status": "已成交", "follow_note": "老记录"})
                merged = leads._merge_saved_crm("alice", [{"lead_id": new_id, "legacy_lead_id": old_id}])[0]
                self.assertEqual(new_id, merged["lead_id"])
                self.assertEqual("已成交", merged["follow_status"])
                self.assertEqual("老记录", merged["follow_note"])
            finally:
                leads.LEADS_CRM_DB = old_db


if __name__ == "__main__":
    unittest.main()
