import base64
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

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
CREATE TABLE short_drama_conversations (
  project_id TEXT PRIMARY KEY, current_version_id TEXT
);
CREATE TABLE short_drama_script_snapshots (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, script_json TEXT NOT NULL
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

    def test_scene_reference_is_grouped_locked_and_available_to_video_request(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        scenes = graph.scene_workspace(self.db, "alice", "p1")
        self.assertEqual(1, len(scenes["scenes"]))
        scene = scenes["scenes"][0]
        raw = b"\x89PNG\r\n\x1a\n" + b"scene-reference"
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        output = Path(self.temp.name) / "output"
        fake_image = types.SimpleNamespace(OUT_DIR=output)
        fake_uploads = types.SimpleNamespace(
            MAX_BYTES=10 * 1024 * 1024,
            MIME_EXTENSIONS={"image/png": ".png"},
            detect_mime=lambda value: "image/png" if value.startswith(b"\x89PNG") else "",
        )
        with mock.patch.dict(sys.modules, {
            "server.content_domains.image": fake_image,
            "server.content_domains.cli_uploads": fake_uploads,
        }):
            created = graph.set_scene_reference(self.db, "alice", "alice", {
                "project_id": "p1", "graph_revision": scenes["graph_revision"],
                "scene_key": scene["scene_key"], "source": "upload",
                "image_data": data_url, "filename": "street.png",
                "prompt": "雨夜街道，霓虹灯倒影",
            })
        self.assertFalse(created["scenes"][0]["locked"])
        self.assertEqual("upload", created["scenes"][0]["preview"]["source"])
        locked = graph.lock_scene_reference(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": created["graph_revision"],
            "scene_key": scene["scene_key"],
        })
        self.assertTrue(locked["scenes"][0]["locked"])
        with closing(self.db()) as conn:
            reference = graph.locked_scene_reference(conn, "p1", "shot_001")
        self.assertEqual("雨夜街道", reference["name"])
        self.assertTrue(reference["file"].startswith("short_drama_scene_uploads/scene_"))

    def test_committed_scene_upload_survives_response_assembly_failure(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        before = graph.scene_workspace(self.db, "alice", "p1")
        scene = before["scenes"][0]
        raw = b"\x89PNG\r\n\x1a\ncommitted-scene"
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        output = Path(self.temp.name) / "output"
        fake_image = types.SimpleNamespace(OUT_DIR=output)
        fake_uploads = types.SimpleNamespace(
            MAX_BYTES=10 * 1024 * 1024,
            MIME_EXTENSIONS={"image/png": ".png"},
            detect_mime=lambda value: "image/png" if value.startswith(b"\x89PNG") else "",
        )
        with mock.patch.dict(sys.modules, {
            "server.content_domains.image": fake_image,
            "server.content_domains.cli_uploads": fake_uploads,
        }), mock.patch.object(
            graph, "scene_workspace", side_effect=RuntimeError("response assembly failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "response assembly failed"):
                graph.set_scene_reference(self.db, "alice", "alice", {
                    "project_id": "p1", "graph_revision": before["graph_revision"],
                    "scene_key": scene["scene_key"], "source": "upload",
                    "image_data": data_url, "filename": "street.png",
                })
        persisted = graph.scene_workspace(self.db, "alice", "p1")
        reference_file = persisted["scenes"][0]["preview"]["file"]
        self.assertTrue(reference_file)
        self.assertTrue((output / reference_file).is_file())

    def test_scene_asset_url_must_belong_to_selected_multi_image_job(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        before = graph.scene_workspace(self.db, "alice", "p1")
        scene = before["scenes"][0]
        with closing(self.db()) as conn:
            conn.execute(
                "CREATE TABLE jobs ("
                "id INTEGER PRIMARY KEY,username TEXT,kind TEXT,status TEXT,result TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs(id,username,kind,status,result) "
                "VALUES(91,'alice','image','done',?)",
                (json.dumps({
                    "urls": [
                        "/api/gen/file/scene-a.png",
                        "/api/gen/file/scene-b.png",
                    ],
                    "files": ["scene-a.png", "scene-b.png"],
                }),),
            )
            conn.commit()
        fake_image = types.SimpleNamespace(
            _trusted_short_drama_file=lambda value, file_url=False: (
                str(value or "").removeprefix("/api/gen/file/")
            ),
        )

        with mock.patch.dict(sys.modules, {
            "server.content_domains.image": fake_image,
        }):
            with self.assertRaises(graph.AssetGraphError) as raised:
                graph.set_scene_reference(self.db, "alice", "alice", {
                    "project_id": "p1",
                    "graph_revision": before["graph_revision"],
                    "scene_key": scene["scene_key"],
                    "source": "asset",
                    "asset_job_id": 91,
                    "asset_url": "/api/gen/file/not-from-job.png",
                    "filename": "tampered.png",
                })

        self.assertEqual("scene_asset_invalid", raised.exception.code)
        self.assertEqual(before, graph.scene_workspace(self.db, "alice", "p1"))

    def test_scene_workspace_falls_back_to_locked_script_shots(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_projects(id,username,revision,deleted) "
                "VALUES ('p3','alice',1,0)"
            )
            conn.execute(
                "INSERT INTO short_drama_conversations(project_id,current_version_id) "
                "VALUES ('p3','v3')"
            )
            conn.execute(
                "INSERT INTO short_drama_script_snapshots(id,project_id,script_json) "
                "VALUES ('v3','p3',?)",
                ('{"shots":[{"shot_key":"shot_01","sort_order":1,'
                 '"scene":"小区长椅","character_keys":[]},'
                 '{"shot_key":"shot_02","sort_order":2,'
                 '"scene":"小区长椅","character_keys":[]}]}',),
            )
            conn.commit()
        graph.sync_foundation(self.db, "alice", "alice", "p3")
        scenes = graph.scene_workspace(self.db, "alice", "p3")
        self.assertEqual(1, len(scenes["scenes"]))
        self.assertEqual(["shot_01", "shot_02"], [
            shot["shot_key"] for shot in scenes["scenes"][0]["shots"]
        ])


if __name__ == "__main__":
    unittest.main()
