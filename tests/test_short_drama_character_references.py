import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import image, short_drama


class ShortDramaCharacterReferenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, username TEXT, kind TEXT, "
            "status TEXT, payload TEXT, result TEXT)"
        )

    def tearDown(self):
        self.connection.close()

    def character(self, job_id=1):
        return {
            "source_type": "ai_character",
            "reference_job_id": job_id,
            "reference_file": "",
            "reference_url": "",
            "reference_version": 0,
            "reference_locked": True,
        }

    def test_owned_completed_banana_job_becomes_locked_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "character.png").write_bytes(b"\x89PNG\r\n\x1a\nreference")
            self.connection.execute(
                "INSERT INTO jobs VALUES (1,'alice','image','done',?,?)",
                (
                    json.dumps({"provider": "banana", "model": "nb2"}),
                    json.dumps({
                        "file": "character.png",
                        "url": "/api/gen/file/character.png",
                    }),
                ),
            )
            characters = [self.character()]
            with mock.patch.object(image, "OUT_DIR", root):
                short_drama._resolve_ai_character_references(
                    self.connection, "alice", characters
                )
        self.assertEqual("character.png", characters[0]["reference_file"])
        self.assertEqual(1, characters[0]["reference_version"])
        self.assertTrue(characters[0]["reference_locked"])

    def test_completed_owned_job_is_locked_even_if_client_sends_false(self):
        self.connection.execute(
            "INSERT INTO jobs VALUES (1,'alice','image','done',?,?)",
            (
                json.dumps({"provider": "banana", "model": "nb2"}),
                json.dumps({
                    "file": "character.png",
                    "url": "/api/gen/file/character.png",
                }),
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "character.png").write_bytes(b"\x89PNG\r\n\x1a\nreference")
            character = self.character()
            character["reference_locked"] = False
            with mock.patch.object(image, "OUT_DIR", root):
                short_drama._resolve_ai_character_references(
                    self.connection, "alice", [character]
                )
        self.assertTrue(character["reference_locked"])

    def test_cross_user_reference_job_is_rejected(self):
        self.connection.execute(
            "INSERT INTO jobs VALUES (1,'mallory','image','done','{}','{}')"
        )
        with self.assertRaisesRegex(ValueError, "不属于当前用户"):
            short_drama._resolve_ai_character_references(
                self.connection, "alice", [self.character()]
            )

    def test_non_banana_reference_job_is_rejected(self):
        self.connection.execute(
            "INSERT INTO jobs VALUES (1,'alice','image','done',?,?)",
            (
                json.dumps({"provider": "seedream"}),
                json.dumps({"file": "character.png", "url": "/file/character.png"}),
            ),
        )
        with self.assertRaisesRegex(ValueError, "Nano Banana 2"):
            short_drama._resolve_ai_character_references(
                self.connection, "alice", [self.character()]
            )


if __name__ == "__main__":
    unittest.main()
