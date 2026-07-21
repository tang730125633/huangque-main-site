import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
from content_domains import core
from content_domains import text


class CopywriterDomainTests(unittest.TestCase):
    def test_api_url_avoids_duplicate_v1(self):
        self.assertEqual(
            core._api_url("https://sg.example/openai/v1", "/v1/chat/completions"),
            "https://sg.example/openai/v1/chat/completions",
        )
        self.assertEqual(
            core._api_url("https://api.openai.com", "/v1/chat/completions"),
            "https://api.openai.com/v1/chat/completions",
        )

    def test_generated_script_keeps_industry_and_version_lineage(self):
        original = text._chat
        text._chat = lambda *_: json.dumps({"scenes": [{"dur": "3s", "scene": "画面", "line": "台词"}]})
        try:
            result = text.gen_copy({
                "prompt": "新品", "format": "script", "industry": "美业",
                "parent_job_id": 42, "version": 2,
            })
        finally:
            text._chat = original
        self.assertEqual(result["industry"], "美业")
        self.assertEqual(result["parent_job_id"], 42)
        self.assertEqual(result["version"], 2)

    def test_illegal_instruction_is_rejected_before_generation(self):
        with self.assertRaisesRegex(ValueError, "违法"):
            text.validate_copy_payload({"prompt": "制作假证并绕过平台实名认证的操作教程"})

    def test_update_script_requires_owned_completed_copy_job(self):
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp.close()
        conn = sqlite3.connect(temp.name)
        conn.row_factory = sqlite3.Row
        conn.execute("create table jobs(id integer primary key, kind text, username text, status text, result text, updated_at integer)")
        conn.execute("insert into jobs values(1,'copy','alice','done',?,0)",
                     (json.dumps({"mode": "script", "scenes": [{"dur": "3s", "scene": "旧", "line": "旧"}]}),))
        conn.commit()
        conn.close()
        def connect():
            value = sqlite3.connect(temp.name)
            value.row_factory = sqlite3.Row
            return value
        saved = text.update_script(connect, "alice", 1, {
            "scenes": [{"dur": "4s", "scene": "新画面", "line": "新台词"}]
        })
        self.assertEqual(saved["scenes"][0]["scene"], "新画面")
        with self.assertRaises(PermissionError):
            text.update_script(connect, "bob", 1, {"scenes": []})
