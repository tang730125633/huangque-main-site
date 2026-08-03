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
                "identity_text": "故事主角",
                "personality": "坚韧、温柔",
                "appearance_prompt": "三十岁，短发，沉静面容",
                "wardrobe_prompt": "深蓝色风衣，银色耳钉",
            },
        )
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
        self.assertTrue(selected["profile_ready"])
        self.assertTrue(selected["binding_ready"])
        self.assertEqual("/assets/mother.png", selected["image_url"])
        self.assertTrue(selected["affected_shots"])

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


if __name__ == "__main__":
    unittest.main()
