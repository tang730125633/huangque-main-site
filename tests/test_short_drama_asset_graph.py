import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from server.content_domains import short_drama_asset_graph as graph


BASE_SCHEMA = """
CREATE TABLE short_drama_projects (
  id TEXT PRIMARY KEY, username TEXT NOT NULL, revision INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE short_drama_characters (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, character_key TEXT NOT NULL,
  name TEXT NOT NULL, identity_text TEXT NOT NULL DEFAULT '',
  appearance_prompt TEXT NOT NULL DEFAULT '', wardrobe_prompt TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE short_drama_shots (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_key TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0, scene_description TEXT NOT NULL DEFAULT '',
  camera_description TEXT NOT NULL DEFAULT '', image_prompt TEXT NOT NULL DEFAULT '',
  video_prompt TEXT NOT NULL DEFAULT '', character_keys_json TEXT NOT NULL DEFAULT '[]'
);
"""


class ShortDramaAssetGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "content.db"

        def db_factory():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = db_factory
        with closing(self.db()) as conn:
            conn.executescript(BASE_SCHEMA)
            conn.execute(
                "INSERT INTO short_drama_projects(id,username,revision,deleted) "
                "VALUES ('p1','alice',3,0)"
            )
            conn.execute(
                "INSERT INTO short_drama_characters"
                "(id,project_id,character_key,name,identity_text,appearance_prompt,"
                "wardrobe_prompt,sort_order) VALUES "
                "('c1','p1','hero','阿明','少年侦探','短发，蓝色外套','蓝色外套',1)"
            )
            conn.execute(
                "INSERT INTO short_drama_shots"
                "(id,project_id,shot_key,sort_order,scene_description,camera_description,"
                "image_prompt,video_prompt,character_keys_json) VALUES "
                "('s1','p1','shot_001',1,'雨夜街道','中景','阿明站在雨中','阿明抬头',"
                "'[\"hero\"]')"
            )
            conn.commit()
        graph.init_db(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _lock_seeded(self, workspace):
        revision = workspace["graph_revision"]
        for entity in workspace["entities"]:
            if entity["asset_type"] not in {"character", "scene"}:
                continue
            result = graph.lock_version(self.db, "alice", "alice", {
                "project_id": "p1", "graph_revision": revision,
                "version_id": entity["versions"][0]["id"],
            })
            revision = result["graph_revision"]
        return revision

    def test_sync_is_idempotent_and_snapshot_requires_locked_versions(self):
        first = graph.sync_foundation(self.db, "alice", "alice", "p1")
        self.assertEqual(first["created"], 3)
        workspace = graph.workspace(self.db, "alice", "p1")
        self.assertEqual(len(workspace["entities"]), 3)
        self.assertEqual(len(workspace["relations"]), 3)

        second = graph.sync_foundation(
            self.db, "alice", "alice", "p1", workspace["graph_revision"],
        )
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["graph_revision"], workspace["graph_revision"])

        blocked = graph.build_snapshot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": workspace["graph_revision"],
            "shot_id": "s1",
        })
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any(
            item["code"] == "asset_version_unlocked" for item in blocked["blockers"]
        ))

        revision = self._lock_seeded(workspace)
        ready = graph.build_snapshot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": revision, "shot_id": "s1",
        })
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(len(ready["package"]["assets"]), 2)
        self.assertEqual(
            graph.current_package(self.db, "alice", "p1", "s1")["id"],
            ready["id"],
        )

    def test_new_version_does_not_mutate_existing_snapshot(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.workspace(self.db, "alice", "p1")
        revision = self._lock_seeded(workspace)
        original = graph.build_snapshot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": revision, "shot_id": "s1",
        })
        character = next(
            item for item in workspace["entities"] if item["asset_type"] == "character"
        )
        created = graph.create_version(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": revision,
            "entity_id": character["id"], "prompt": "成年后的阿明，黑色风衣",
            "attributes": {"episode": 2},
        })
        graph.lock_version(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": created["graph_revision"],
            "version_id": created["id"],
        })
        current = graph.current_package(self.db, "alice", "p1", "s1")
        self.assertEqual(current["package"]["package_hash"], original["package"]["package_hash"])
        with closing(self.db()) as conn:
            with self.assertRaises(graph.AssetGraphError) as raised:
                graph.generation_package(conn, "p1", "s1")
        self.assertEqual(raised.exception.code, "asset_snapshot_stale")

    def test_generation_package_is_optional_for_legacy_and_ready_when_locked(self):
        with closing(self.db()) as conn:
            self.assertIsNone(graph.generation_package(conn, "p1", "s1"))
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.workspace(self.db, "alice", "p1")
        revision = self._lock_seeded(workspace)
        snapshot = graph.build_snapshot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": revision, "shot_id": "s1",
        })
        with closing(self.db()) as conn:
            package = graph.generation_package(conn, "p1", "s1")
        self.assertEqual(package["package_hash"], snapshot["package"]["package_hash"])
        self.assertIn("阿明", graph.prompt_context(package))

    def test_graph_enabled_project_never_falls_back_to_legacy_without_relations(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        with closing(self.db()) as conn:
            conn.execute(
                "DELETE FROM short_drama_graph_relations "
                "WHERE project_id='p1' AND source_scope='shot' AND source_id='s1'"
            )
            conn.commit()
        with closing(self.db()) as conn:
            with self.assertRaises(graph.AssetGraphError) as raised:
                graph.generation_package(conn, "p1", "s1")
        self.assertEqual("asset_snapshot_missing", raised.exception.code)

    def test_stale_revision_and_cross_project_binding_are_rejected(self):
        synced = graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.workspace(self.db, "alice", "p1")
        with self.assertRaisesRegex(graph.AssetGraphError, "已更新"):
            graph.create_asset(self.db, "alice", "alice", {
                "project_id": "p1", "graph_revision": synced["graph_revision"] - 1,
                "asset_key": "prop:umbrella", "asset_type": "prop", "name": "黑伞",
            })

        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_projects(id,username,revision,deleted) "
                "VALUES ('p2','alice',1,0)"
            )
            conn.commit()
        other = graph.create_asset(self.db, "alice", "alice", {
            "project_id": "p2", "graph_revision": 1,
            "asset_key": "prop:key", "asset_type": "prop", "name": "钥匙",
        })
        with self.assertRaises(graph.AssetGraphError) as raised:
            graph.bind_asset(self.db, "alice", "alice", {
                "project_id": "p1", "graph_revision": workspace["graph_revision"],
                "shot_id": "s1", "relation_type": "uses", "entity_id": other["id"],
            })
        self.assertEqual(raised.exception.code, "asset_not_found")

    def test_workspace_read_does_not_create_graph_state(self):
        result = graph.workspace(self.db, "alice", "p1")
        self.assertEqual(result["graph_revision"], 1)
        with closing(self.db()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_graph_state WHERE project_id='p1'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
