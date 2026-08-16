import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import (
    short_drama,
    short_drama_autodraft,
    short_drama_character_studio,
    short_drama_conversation,
    short_drama_preflight,
)


def project_payload():
    return {
        "title": "角色工作室",
        "synopsis": "女儿和母亲在雨夜完成一次和解。",
        "ratio": "16:9",
        "target_duration": 30,
        "shot_count": 6,
        "visual_style": "电影写实",
        "target_platform": "抖音",
        "point_budget": 100,
    }


class ShortDramaCharacterStudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db, "alice", project_payload()
        )
        selected = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "方案一 · 情感治愈",
            },
            "character-direction-select",
        )
        confirmed = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": selected["conversation"]["revision"],
                "message": "确认这个方向",
            },
            "character-direction-confirm",
        )
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "母女和解",
            },
            "character-generate",
        )
        self.locked = short_drama_conversation.lock_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "character-lock",
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def avatars(username, limit=120):
        return [
            {
                "id": 7,
                "username": username,
                "name": "母亲形象",
                "image_url": "/assets/mother.png",
                "status": "ready",
                "provider_avatar_id": "provider-mother",
            },
            {
                "id": 8,
                "username": username,
                "name": "女儿形象",
                "image_url": "/assets/daughter.png",
                "status": "ready",
                "provider_avatar_id": "provider-daughter",
            },
        ]

    @staticmethod
    def avatar(username, avatar_id):
        for item in ShortDramaCharacterStudioTests.avatars(username):
            if str(item["id"]) == str(avatar_id):
                return item
        raise LookupError("avatar missing")

    def test_union_extraction_includes_declared_speaking_and_visible_roles(self):
        characters = short_drama_character_studio._script_characters({
            "characters": [
                {"character_key": "daughter", "name": "女儿"},
            ],
            "dialogue_lines": [
                {"id": "line-1", "character_key": "mother", "speaker": "母亲"},
            ],
            "shots": [
                {"character_keys": ["daughter", "mother", "teacher"]},
            ],
        })
        self.assertEqual(
            ["daughter", "mother", "teacher"],
            [item["character_key"] for item in characters],
        )

    def test_confirmed_creation_roles_exclude_script_only_characters(self):
        conn = self.db()
        row = conn.execute(
            "SELECT snapshot.script_json FROM short_drama_conversations conversation "
            "JOIN short_drama_script_snapshots snapshot "
            "ON snapshot.id=conversation.locked_version_id "
            "WHERE conversation.project_id=?",
            (self.project["id"],),
        ).fetchone()
        script = json.loads(row[0])
        confirmed = [dict(item) for item in script["characters"][:2]]
        contract = [{
            "character_key": item["character_key"],
            "name": item["name"],
        } for item in confirmed]
        conn.execute(
            "INSERT INTO short_drama_script_imports "
            "(id,username,project_id,idempotency_key,request_hash,source_text,"
            "source_hash,filename,content_type,character_contract_json,roles_saved_at,"
            "import_mode,status,created_at,updated_at) "
            "VALUES ('confirmed-import','alice',?,'confirmed-roles','request','source',"
            "'source-hash','source.txt','live_action',?,1,'faithful','completed',1,1)",
            (self.project["id"], json.dumps(contract, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO short_drama_characters "
            "(id,project_id,character_key,name,identity_text,personality,source_type,sort_order) "
            "VALUES ('script-extra',?,'friend_a','朋友甲','剧本临时人物','','ai_character',99)",
            (self.project["id"],),
        )
        conn.commit()
        conn.close()

        workspace = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        self.assertEqual(
            [item["character_key"] for item in contract],
            [item["character_key"] for item in workspace["characters"]],
        )
        self.assertNotIn(
            "friend_a", [item["character_key"] for item in workspace["characters"]]
        )

    def test_profile_and_avatar_binding_are_revision_safe_and_visible(self):
        initial = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        self.assertGreaterEqual(initial["summary"]["total"], 1)
        character = initial["characters"][0]
        saved = short_drama_character_studio.save_profile(
            self.db,
            "alice",
            {
                "project_id": self.project["id"],
                "project_revision": initial["project_revision"],
                "character_key": character["character_key"],
                "name": "林雨",
                "identity_text": "故事主角",
                "personality": "坚韧、温柔",
                "appearance_prompt": "三十岁，短发，沉静面容",
                "wardrobe_prompt": "深蓝色风衣，银色耳钉",
            },
        )
        unchanged = short_drama_character_studio.save_profile(
            self.db,
            "alice",
            {
                "project_id": self.project["id"],
                "project_revision": saved["project_revision"],
                "character_key": character["character_key"],
                "name": "林雨",
                "identity_text": "故事主角",
                "personality": "坚韧、温柔",
                "appearance_prompt": "三十岁，短发，沉静面容",
                "wardrobe_prompt": "深蓝色风衣，银色耳钉",
            },
        )
        self.assertEqual(saved["project_revision"], unchanged["project_revision"])
        with self.assertRaises(
            short_drama_character_studio.CharacterStudioError
        ) as stale:
            short_drama_character_studio.save_profile(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "project_revision": initial["project_revision"],
                    "character_key": character["character_key"],
                    "identity_text": "旧请求",
                    "personality": "旧请求",
                    "appearance_prompt": "旧请求",
                    "wardrobe_prompt": "旧请求",
                },
            )
        self.assertEqual("project_revision_conflict", stale.exception.code)

        bound = short_drama_character_studio.bind_avatar(
            self.db,
            "alice",
            {
                "project_id": self.project["id"],
                "project_revision": saved["project_revision"],
                "character_key": character["character_key"],
                "avatar_id": "7",
            },
            avatar_lookup=self.avatar,
        )
        current = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        selected = next(
            item for item in current["characters"]
            if item["character_key"] == character["character_key"]
        )
        self.assertEqual(bound["project_revision"], current["project_revision"])
        self.assertEqual("林雨", saved["name"])
        self.assertEqual("林雨", selected["name"])
        self.assertEqual(character["character_key"], selected["character_key"])
        self.assertTrue(selected["profile_ready"])
        self.assertTrue(selected["binding_ready"])
        self.assertEqual("/assets/mother.png", selected["image_url"])
        self.assertTrue(selected["affected_shots"])

    def test_profile_name_rejects_duplicates_without_changing_character_key(self):
        initial = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertGreaterEqual(len(initial["characters"]), 2)
        character, other = initial["characters"][:2]
        with self.assertRaises(
            short_drama_character_studio.CharacterStudioError
        ) as duplicate:
            short_drama_character_studio.save_profile(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "project_revision": initial["project_revision"],
                    "character_key": character["character_key"],
                    "name": other["name"],
                    "identity_text": "故事主角",
                    "personality": "坚定",
                    "appearance_prompt": "短发，沉静面容",
                    "wardrobe_prompt": "深蓝色风衣",
                },
            )
        self.assertEqual("character_name_duplicate", duplicate.exception.code)
        current = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        selected = next(
            item for item in current["characters"]
            if item["character_key"] == character["character_key"]
        )
        self.assertEqual(character["name"], selected["name"])

    def test_workspace_keeps_reference_image_separate_from_bound_avatar(self):
        initial = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        character = initial["characters"][0]
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_characters SET reference_url=? "
                "WHERE project_id=? AND character_key=?",
                ("/assets/reference.png", self.project["id"],
                 character["character_key"]),
            )
            conn.commit()
        finally:
            conn.close()
        short_drama_character_studio.bind_avatar(
            self.db,
            "alice",
            {
                "project_id": self.project["id"],
                "project_revision": initial["project_revision"],
                "character_key": character["character_key"],
                "avatar_id": "7",
            },
            avatar_lookup=self.avatar,
        )
        current = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        selected = next(
            item for item in current["characters"]
            if item["character_key"] == character["character_key"]
        )
        self.assertEqual("/assets/mother.png", selected["image_url"])
        self.assertEqual(
            "/assets/reference.png", selected["reference_image_url"]
        )

    def test_profile_changes_invalidate_preflight_character_snapshot(self):
        initial = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        conn = self.db()
        try:
            before = short_drama_preflight._project(
                conn, "alice", self.project["id"]
            )["character_snapshot_hash"]
        finally:
            conn.close()
        character = initial["characters"][0]
        short_drama_character_studio.save_profile(
            self.db,
            "alice",
            {
                "project_id": self.project["id"],
                "project_revision": initial["project_revision"],
                "character_key": character["character_key"],
                "identity_text": "记者",
                "personality": "敏锐",
                "appearance_prompt": "短发、清晰面部特征",
                "wardrobe_prompt": "米色风衣",
            },
        )
        conn = self.db()
        try:
            after = short_drama_preflight._project(
                conn, "alice", self.project["id"]
            )["character_snapshot_hash"]
        finally:
            conn.close()
        self.assertNotEqual(before, after)

    def test_production_plan_reports_unbound_roles_and_clears_after_binding(self):
        workspace = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        character = workspace["characters"][0]
        plan = {
            "material_plan": [{
                "shot_key": "shot_01",
                "character_keys": [character["character_key"]],
                "dialogue": [{
                    "character_key": character["character_key"],
                    "text": "回家吧",
                }],
            }],
        }
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            blockers = short_drama_autodraft._character_binding_blockers(
                conn, self.project["id"], plan
            )
        finally:
            conn.close()
        self.assertEqual(
            [character["character_key"]],
            [item["character_key"] for item in blockers],
        )
        short_drama_character_studio.bind_avatar(
            self.db,
            "alice",
            {
                "project_id": self.project["id"],
                "project_revision": workspace["project_revision"],
                "character_key": character["character_key"],
                "avatar_id": "7",
            },
            avatar_lookup=self.avatar,
        )
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            blockers = short_drama_autodraft._character_binding_blockers(
                conn, self.project["id"], plan
            )
        finally:
            conn.close()
        self.assertEqual([], blockers)

    def test_character_reference_prompt_uses_current_visual_profile(self):
        prompt = short_drama._character_reference_prompt({
            "name": "奶奶",
            "identity_text": "退休教师",
            "personality": "慈祥坚定",
            "appearance_prompt": "68岁东亚女性，满头花白短发，圆脸",
            "wardrobe_prompt": "深蓝色布衫，米色围巾",
        })
        self.assertIn("68岁东亚女性，满头花白短发，圆脸", prompt)
        self.assertIn("深蓝色布衫，米色围巾", prompt)
        self.assertIn("奶奶", prompt)

    def test_workspace_surfaces_active_character_reference_job(self):
        initial = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        character = initial["characters"][0]
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_character_reference_jobs "
                "(id,username,owner_username,project_id,character_key,"
                "project_revision,character_snapshot_hash,idempotency_key,"
                "job_id,cost,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'linked',1,1)",
                (
                    "active-reference", "alice", "alice", self.project["id"],
                    character["character_key"], initial["project_revision"],
                    "snapshot", "reference-operation", 321, 2,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        current = next(
            item for item in recovered["characters"]
            if item["character_key"] == character["character_key"]
        )
        self.assertEqual(321, current["reference_job_id"])
        self.assertEqual("linked", current["reference_job_status"])

    def test_workspace_recovers_completed_avatar_job_and_binds_once(self):
        initial = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        character = initial["characters"][0]
        binding = {
            "project_id": self.project["id"],
            "project_revision": initial["project_revision"],
            "character_key": character["character_key"],
        }
        conn = self.db()
        try:
            conn.executescript("""
                CREATE TABLE avatars(
                    id INTEGER PRIMARY KEY,username TEXT,name TEXT,image_file TEXT,
                    provider_avatar_id TEXT,provider_avatar_group_id TEXT,status TEXT
                );
                CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY,kind TEXT,username TEXT,status TEXT,
                    payload TEXT,result TEXT,updated_at INTEGER
                );
            """)
            conn.execute(
                "INSERT INTO avatars VALUES(7,'alice','林雨','avatar.jpg',"
                "'provider-7',NULL,'ready')"
            )
            conn.execute(
                "INSERT INTO avatars VALUES(8,'alice','林雪','avatar-8.jpg',"
                "'provider-8',NULL,'ready')"
            )
            conn.execute(
                "INSERT INTO jobs VALUES(99,'avatar','alice','done',?,?,1)",
                (
                    json.dumps({"short_drama_binding": binding}),
                    json.dumps({"avatar_id": 7, "status": "ready"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        selected = next(
            item for item in recovered["characters"]
            if item["character_key"] == character["character_key"]
        )
        self.assertEqual("7", str(selected["avatar_id"]))
        self.assertEqual(initial["project_revision"] + 1,
                         recovered["project_revision"])

        repeated = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        self.assertEqual(recovered["project_revision"],
                         repeated["project_revision"])
        conn = self.db()
        try:
            result = json.loads(conn.execute(
                "SELECT result FROM jobs WHERE id=99"
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual("bound", result["short_drama_binding"]["status"])

        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO jobs VALUES(100,'avatar','alice','done',?,?,1)",
                (
                    json.dumps({"short_drama_binding": binding}),
                    json.dumps({"avatar_id": 8, "status": "ready"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        stale = short_drama_character_studio.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=self.avatars,
        )
        stale_character = next(
            item for item in stale["characters"]
            if item["character_key"] == character["character_key"]
        )
        self.assertEqual("7", str(stale_character["avatar_id"]))
        conn = self.db()
        try:
            stale_result = json.loads(conn.execute(
                "SELECT result FROM jobs WHERE id=100"
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(
            "conflict", stale_result["short_drama_binding"]["status"]
        )


if __name__ == "__main__":
    unittest.main()
