import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama


class ShortDramaPr94SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content_jobs.db")
        self.db = lambda: sqlite3.connect(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _legacy_schema():
        removed = {
            "creation_status TEXT NOT NULL DEFAULT 'formal' CHECK (creation_status IN ('draft','formal'))",
            "character_contract_migration_json TEXT NOT NULL DEFAULT '{}'",
            "core_story_json TEXT NOT NULL DEFAULT '{}'",
            "core_story_confirmed_at INTEGER",
            "reference_source TEXT NOT NULL DEFAULT ''",
            "reference_asset_id TEXT NOT NULL DEFAULT ''",
            "reference_name TEXT NOT NULL DEFAULT ''",
        }
        lines = []
        for line in short_drama._SCHEMA.splitlines():
            normalized = line.strip().rstrip(",")
            if normalized in removed:
                continue
            lines.append(line)
        return "\n".join(lines)

    def _seed_legacy_database(self):
        contract = [{
            "character_key": "character_1",
            "name": "Lin Yi",
            "role_type": "main",
            "gender": "female",
            "fixed_clothing": "white shirt",
            "reference_views": ["front_full", "side_full", "front_half"],
        }]
        conn = self.db()
        try:
            conn.executescript(self._legacy_schema())
            conn.execute(
                "INSERT INTO short_drama_projects "
                "(id,username,title,synopsis,ratio,target_duration,shot_count,"
                "visual_style,target_platform,point_budget,spent_points,stage,"
                "revision,deleted,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-project", "alice", "Legacy project",
                    "A legacy live action project that must survive migration.",
                    "16:9", 30, 6, "cinematic", "douyin", 0, 0,
                    "draft", 1, 0, 100, 100,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_script_imports "
                "(id,username,project_id,idempotency_key,request_hash,source_text,"
                "source_hash,filename,content_type,character_contract_json,"
                "roles_saved_at,import_mode,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-import", "alice", "legacy-project", "legacy-key",
                    "request-hash", "Legacy live action source text.", "source-hash",
                    "legacy.txt", "live_action", json.dumps(contract), 100,
                    "faithful", "completed", 100, 100,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,sort_order) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "legacy-character", "legacy-project", "character_1",
                    "Lin Yi", "ai_character", 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return contract

    def test_legacy_database_upgrade_is_idempotent_and_preserves_old_contract(self):
        contract = self._seed_legacy_database()

        short_drama.init_db(self.db)
        first = short_drama.get_project(self.db, "alice", "legacy-project")
        short_drama.init_db(self.db)
        second = short_drama.get_project(self.db, "alice", "legacy-project")

        self.assertEqual("draft", second["creation_status"])
        self.assertEqual(contract, second["script_import"]["character_contract"])
        self.assertEqual(
            first["script_import"]["character_contract_migration"],
            second["script_import"]["character_contract_migration"],
        )
        migration = second["script_import"]["character_contract_migration"]
        self.assertTrue(migration["required"])
        self.assertEqual("back_full_confirmation_required", migration["code"])
        self.assertEqual(["character_1"], migration["character_keys"])
        self.assertEqual(["back_full"], migration["missing_reference_views"])
        self.assertEqual(
            ["front_full", "side_full", "front_half"],
            migration["legacy_reference_views"],
        )

        conn = self.db()
        try:
            project_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_projects)"
                )
            }
            import_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_script_imports)"
                )
            }
            character_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_characters)"
                )
            }
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("creation_status", project_columns)
            self.assertTrue({
                "core_story_json", "core_story_confirmed_at",
                "character_contract_migration_json",
            }.issubset(import_columns))
            self.assertTrue({
                "reference_source", "reference_asset_id", "reference_name",
            }.issubset(character_columns))
            self.assertTrue({
                "short_drama_provider_shot_execution_overrides",
                "short_drama_provider_shot_selections",
            }.issubset(tables))
            # The previous application version selects only its known columns;
            # additive migration must keep that query working after rollback.
            legacy_row = conn.execute(
                "SELECT source_text,character_contract_json "
                "FROM short_drama_script_imports WHERE id='legacy-import'"
            ).fetchone()
            self.assertEqual("Legacy live action source text.", legacy_row[0])
            self.assertEqual(contract, json.loads(legacy_row[1]))
            self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()

    def test_migration_marker_clears_only_after_replacement_reference_is_locked(self):
        self._seed_legacy_database()
        short_drama.init_db(self.db)
        before = short_drama.get_project(self.db, "alice", "legacy-project")
        self.assertTrue(
            before["script_import"]["character_contract_migration"]["required"]
        )

        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_script_imports "
                "SET core_story_confirmed_at=101 WHERE project_id='legacy-project'"
            )
            conn.execute(
                "UPDATE short_drama_characters SET "
                "reference_file='replacement.png',"
                "reference_url='https://cdn.example/replacement.png',"
                "reference_version=1,reference_locked=0,reference_source='upload' "
                "WHERE project_id='legacy-project' AND character_key='character_1'"
            )
            conn.commit()
        finally:
            conn.close()

        confirmed = short_drama.confirm_character_reference(
            self.db, "alice", "legacy-project", before["revision"],
            "character_1", 1,
        )
        self.assertTrue(confirmed["characters"][0]["reference_locked"])
        self.assertEqual(
            {}, confirmed["script_import"]["character_contract_migration"]
        )


if __name__ == "__main__":
    unittest.main()
