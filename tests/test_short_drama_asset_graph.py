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

    def _set_and_lock_scene_reference(self, workspace, scene_key, prompt):
        raw = b"\x89PNG\r\n\x1a\n" + prompt.encode("utf-8")
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        output = Path(self.temp.name) / "output"
        fake_image = types.SimpleNamespace(OUT_DIR=output)
        fake_uploads = types.SimpleNamespace(
            MAX_BYTES=10 * 1024 * 1024,
            MIME_EXTENSIONS={"image/png": ".png"},
            detect_mime=lambda value: "image/png" if value.startswith(b"\x89PNG") else "",
        )
        with mock.patch.dict(sys.modules, {
            graph.__package__ + ".image": fake_image,
            graph.__package__ + ".cli_uploads": fake_uploads,
        }), mock.patch.object(
            sys.modules[graph.__package__], "image", fake_image, create=True,
        ), mock.patch.object(
            sys.modules[graph.__package__], "cli_uploads", fake_uploads, create=True,
        ):
            created = graph.set_scene_reference(self.db, "alice", "alice", {
                "project_id": "p1", "graph_revision": workspace["graph_revision"],
                "scene_key": scene_key, "source": "upload",
                "image_data": data_url, "filename": "scene.png", "prompt": prompt,
            })
        return graph.lock_scene_reference(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": created["graph_revision"],
            "scene_key": scene_key,
        })

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
            graph.__package__ + ".image": fake_image,
            graph.__package__ + ".cli_uploads": fake_uploads,
        }), mock.patch.object(
            sys.modules[graph.__package__], "image", fake_image, create=True,
        ), mock.patch.object(
            sys.modules[graph.__package__], "cli_uploads", fake_uploads, create=True,
        ):
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
            selected_reference = graph.locked_scene_reference(
                conn, "p1", "shot_001", scene["scene_key"],
            )
            missing_reference = graph.locked_scene_reference(
                conn, "p1", "shot_001", "scene-group:missing",
            )
        self.assertEqual("雨夜街道", reference["name"])
        self.assertTrue(reference["file"].startswith("short_drama_scene_uploads/scene_"))
        self.assertEqual(reference["scene_key"], selected_reference["scene_key"])
        self.assertIsNone(missing_reference)

    def test_scene_semantic_changes_retire_locked_references_until_reconfirmed(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.scene_workspace(self.db, "alice", "p1")
        system_scene = workspace["scenes"][0]
        locked_system = self._set_and_lock_scene_reference(
            workspace, system_scene["scene_key"], "rainy old street",
        )
        self.assertTrue(locked_system["scenes"][0]["locked"])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_shots SET scene_description=? WHERE project_id=? "
                "AND shot_key=?",
                ("sunny new rooftop", "p1", "shot_001"),
            )
            conn.commit()
        system_changed = graph.sync_foundation(
            self.db, "alice", "alice", "p1", locked_system["graph_revision"],
        )
        changed_system_scene = graph.scene_workspace(self.db, "alice", "p1")["scenes"][0]
        self.assertFalse(changed_system_scene["locked"])
        with closing(self.db()) as conn:
            self.assertIsNone(graph.locked_scene_reference(conn, "p1", "shot_001"))

        created = graph.create_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": system_changed["graph_revision"],
            "name": "Old terrace", "description": "quiet evening terrace",
            "shot_keys": ["shot_001"],
        })
        custom = next(item for item in created["scenes"] if item["custom"])
        locked_custom = self._set_and_lock_scene_reference(
            created, custom["scene_key"], "quiet evening terrace",
        )
        self.assertTrue(next(
            item for item in locked_custom["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )["locked"])

        renamed = graph.update_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": locked_custom["graph_revision"],
            "scene_key": custom["scene_key"], "name": "New terrace",
            "description": "quiet evening terrace", "shot_keys": ["shot_001"],
        })
        renamed_custom = next(
            item for item in renamed["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )
        self.assertFalse(renamed_custom["locked"])
        with closing(self.db()) as conn:
            self.assertIsNone(graph.locked_scene_reference(
                conn, "p1", "shot_001", custom["scene_key"],
            ))

        relocked = self._set_and_lock_scene_reference(
            renamed, custom["scene_key"], "new terrace confirmed",
        )
        relocked_custom = next(
            item for item in relocked["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )
        self.assertTrue(relocked_custom["locked"])
        self.assertEqual("new terrace confirmed", relocked_custom["preview"]["prompt"])

        redescribed = graph.update_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": relocked["graph_revision"],
            "scene_key": custom["scene_key"], "name": "New terrace",
            "description": "bright noon terrace", "shot_keys": ["shot_001"],
        })
        redescribed_custom = next(
            item for item in redescribed["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )
        self.assertFalse(redescribed_custom["locked"])
        with closing(self.db()) as conn:
            self.assertIsNone(graph.locked_scene_reference(
                conn, "p1", "shot_001", custom["scene_key"],
            ))

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
            graph.__package__ + ".image": fake_image,
            graph.__package__ + ".cli_uploads": fake_uploads,
        }), mock.patch.object(
            sys.modules[graph.__package__], "image", fake_image, create=True,
        ), mock.patch.object(
            sys.modules[graph.__package__], "cli_uploads", fake_uploads, create=True,
        ), mock.patch.object(
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
            graph.__package__ + ".image": fake_image,
        }), mock.patch.object(
            sys.modules[graph.__package__], "image", fake_image, create=True,
        ):
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

    def test_current_script_shots_merge_with_stale_legacy_rows_and_can_bind(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_conversations(project_id,current_version_id) "
                "VALUES ('p1','v-current')"
            )
            conn.execute(
                "INSERT INTO short_drama_script_snapshots(id,project_id,script_json) "
                "VALUES ('v-current','p1',?)",
                ('{"shots":[{"shot_key":"shot_01","sort_order":1,'
                 '"scene":"演播厅","character_keys":["hero"]}]}',),
            )
            conn.commit()
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.scene_workspace(self.db, "alice", "p1")
        current_scene = next(
            scene for scene in workspace["scenes"]
            if "shot_01" in [shot["shot_key"] for shot in scene["shots"]]
        )
        self.assertEqual("演播厅", current_scene["description"])
        self.assertFalse(any(
            shot["shot_key"] == "shot_001"
            for scene in workspace["scenes"] for shot in scene["shots"]
        ))
        bound = graph.bind_scene_to_shot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": workspace["graph_revision"],
            "shot_key": "shot_01", "scene_key": current_scene["scene_key"],
        })
        selected = next(
            scene for scene in bound["scenes"]
            if scene["scene_key"] == current_scene["scene_key"]
        )
        self.assertIn("shot_01", [shot["shot_key"] for shot in selected["shots"]])

    def test_current_user_shot_can_bind_and_build_immutable_snapshot(self):
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_conversations(project_id,current_version_id) "
                "VALUES ('p1','v-user-shot')"
            )
            conn.execute(
                "INSERT INTO short_drama_script_snapshots(id,project_id,script_json) "
                "VALUES ('v-user-shot','p1',?)",
                (json.dumps({"shots": [{
                    "shot_key": "shot_user_copy", "sort_order": 1,
                    "scene": "custom user scene", "camera": "close-up",
                    "visual": "a copied user shot", "character_keys": [],
                    "provider_prompt": "render the copied user shot",
                }]}),),
            )
            conn.commit()

        graph.sync_foundation(self.db, "alice", "alice", "p1")
        scenes = graph.scene_workspace(self.db, "alice", "p1")
        user_shot = scenes["scenes"][0]["shots"][0]
        workspace = graph.workspace(self.db, "alice", "p1")
        revision = self._lock_seeded(workspace)
        prop = graph.create_asset(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": revision,
            "asset_key": "prop:user-note", "asset_type": "prop", "name": "User note",
        })
        prop_version = next(
            entity for entity in graph.workspace(self.db, "alice", "p1")["entities"]
            if entity["id"] == prop["id"]
        )["versions"][0]
        locked = graph.lock_version(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": prop["graph_revision"],
            "version_id": prop_version["id"],
        })
        bound = graph.bind_asset(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": locked["graph_revision"],
            "shot_id": user_shot["id"], "relation_type": "uses",
            "entity_id": prop["id"],
        })
        snapshot = graph.build_snapshot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": bound["graph_revision"],
            "shot_id": user_shot["id"],
        })
        self.assertEqual("ready", snapshot["status"])
        self.assertEqual("shot_user_copy", snapshot["package"]["shot_key"])
        self.assertEqual("custom user scene", snapshot["package"]["shot"]["scene_description"])

    def test_deleted_script_shot_cleans_bindings_and_readds_as_fresh_draft(self):
        original_script = {
            "shots": [
                {"shot_key": "shot_001", "sort_order": 1,
                 "scene": "当前剧本雨夜街道", "character_keys": ["hero"]},
                {"shot_key": "shot_002", "sort_order": 2,
                 "scene": "车站月台", "character_keys": ["hero"]},
            ],
        }
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_conversations(project_id,current_version_id) "
                "VALUES ('p1','v-current')"
            )
            conn.execute(
                "INSERT INTO short_drama_script_snapshots(id,project_id,script_json) "
                "VALUES ('v-current','p1',?)", (json.dumps(original_script),),
            )
            conn.commit()
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        before = graph.scene_workspace(self.db, "alice", "p1")
        shot_002_id = next(
            shot["id"] for scene in before["scenes"] for shot in scene["shots"]
            if shot["shot_key"] == "shot_002"
        )
        authoritative_scene = next(
            scene for scene in before["scenes"]
            if any(shot["shot_key"] == "shot_001" for shot in scene["shots"])
        )
        self.assertEqual("当前剧本雨夜街道", authoritative_scene["description"])
        custom_result = graph.create_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": before["graph_revision"],
            "name": "自定义月台", "description": "夜间月台与冷色顶灯",
            "shot_keys": ["shot_002"],
        })
        custom = next(item for item in custom_result["scenes"] if item["custom"])
        graph_before_delete = graph.workspace(self.db, "alice", "p1")

        reduced_script = {"shots": [original_script["shots"][0]]}
        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_script_snapshots SET script_json=? WHERE id='v-current'",
                (json.dumps(reduced_script),),
            )
            conn.commit()
        with self.assertRaises(graph.AssetGraphError) as stale:
            graph.sync_foundation(
                self.db, "alice", "alice", "p1",
                graph_before_delete["graph_revision"] - 1,
            )
        self.assertEqual("graph_revision_conflict", stale.exception.code)
        self.assertEqual(graph_before_delete, graph.workspace(self.db, "alice", "p1"))

        cleaned = graph.sync_foundation(
            self.db, "alice", "alice", "p1", graph_before_delete["graph_revision"],
        )
        self.assertGreaterEqual(cleaned["removed_relations"], 1)
        self.assertEqual(1, cleaned["retired_entities"])
        with self.assertRaises(graph.AssetGraphError) as deleted:
            graph.build_snapshot(self.db, "alice", "alice", {
                "project_id": "p1", "graph_revision": cleaned["graph_revision"],
                "shot_id": shot_002_id,
            })
        self.assertEqual("shot_not_found", deleted.exception.code)
        reloaded = graph.scene_workspace(self.db, "alice", "p1")
        self.assertFalse(any(
            shot["shot_key"] == "shot_002"
            for scene in reloaded["scenes"] for shot in scene["shots"]
        ))
        retained_custom = next(
            item for item in reloaded["scenes"] if item["scene_key"] == custom["scene_key"]
        )
        self.assertEqual([], retained_custom["shots"])
        raw = graph.workspace(self.db, "alice", "p1")
        self.assertFalse(any(
            relation["source_id"] == "script:shot_002"
            or relation["metadata"].get("shot_key") == "shot_002"
            for relation in raw["relations"]
        ))
        retired = next(
            entity for entity in raw["entities"]
            if entity["asset_key"] == "scene:" + shot_002_id
        )
        self.assertEqual("retired", retired["status"])
        self.assertTrue(all(version["status"] == "retired" for version in retired["versions"]))
        with closing(self.db()) as conn:
            audit = conn.execute(
                "SELECT details_json FROM short_drama_graph_audit "
                "WHERE project_id='p1' AND action='sync_foundation' "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
        audit_details = json.loads(audit[0])
        self.assertIn("shot_002", audit_details["removed_shots"])
        self.assertIn(retired["id"], audit_details["retired_entity_ids"])

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_script_snapshots SET script_json=? WHERE id='v-current'",
                (json.dumps(original_script),),
            )
            conn.commit()
        restored = graph.sync_foundation(
            self.db, "alice", "alice", "p1", cleaned["graph_revision"],
        )
        self.assertEqual(1, restored["reactivated"])
        restored_workspace = graph.scene_workspace(self.db, "alice", "p1")
        restored_custom = next(
            item for item in restored_workspace["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )
        self.assertEqual([], restored_custom["shots"])
        raw_restored = graph.workspace(self.db, "alice", "p1")
        reactivated = next(
            entity for entity in raw_restored["entities"]
            if entity["asset_key"] == "scene:" + shot_002_id
        )
        restored_shot_002_id = next(
            shot["id"] for scene in restored_workspace["scenes"] for shot in scene["shots"]
            if shot["shot_key"] == "shot_002"
        )
        self.assertEqual(shot_002_id, restored_shot_002_id)
        self.assertEqual("active", reactivated["status"])
        self.assertEqual("draft", reactivated["versions"][0]["status"])
        self.assertTrue(reactivated["versions"][0]["attributes"]["reactivated"])
        self.assertTrue(all(
            version["status"] == "retired" for version in reactivated["versions"][1:]
        ))

    def test_script_deletion_unbinds_but_does_not_retire_a_custom_scene(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        scenes = graph.scene_workspace(self.db, "alice", "p1")
        created = graph.create_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": scenes["graph_revision"],
            "name": "Saved custom scene", "description": "A reusable custom location",
            "shot_keys": ["shot_001"],
        })
        custom = next(item for item in created["scenes"] if item["custom"])
        locked = self._set_and_lock_scene_reference(
            created, custom["scene_key"], "confirmed custom reference",
        )
        locked_custom = next(
            item for item in locked["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )

        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            graph.invalidate_script_mutation(
                conn, "p1", "alice",
                {"shots": [{"shot_key": "shot_001", "scene": "street"}]},
                {"shots": []},
            )
            conn.commit()

        remaining = graph.scene_workspace(self.db, "alice", "p1")
        retained = next(
            item for item in remaining["scenes"]
            if item["scene_key"] == custom["scene_key"]
        )
        self.assertEqual([], retained["shots"])
        self.assertTrue(retained["locked"])
        self.assertEqual(locked_custom["preview"]["file"], retained["preview"]["file"])

    def test_custom_scene_can_be_created_rebound_updated_and_deleted(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.scene_workspace(self.db, "alice", "p1")
        created = graph.create_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": workspace["graph_revision"],
            "name": "学校天台", "description": "傍晚的学校天台，暖色夕阳",
            "shot_keys": ["shot_001"],
        })
        custom = next(item for item in created["scenes"] if item["custom"])
        self.assertEqual("学校天台", custom["name"])
        self.assertEqual(["shot_001"], [item["shot_key"] for item in custom["shots"]])
        self.assertTrue(custom["scene_key"].startswith("scene-custom:"))

        graph.sync_foundation(self.db, "alice", "alice", "p1")
        resynced = graph.scene_workspace(self.db, "alice", "p1")
        rebound = next(item for item in resynced["scenes"] if item["custom"])
        self.assertEqual(["shot_001"], [item["shot_key"] for item in rebound["shots"]])

        updated = graph.update_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": resynced["graph_revision"],
            "scene_key": custom["scene_key"], "name": "教学楼天台",
            "description": "夜晚的教学楼天台，城市灯光",
            "shot_keys": [],
        })
        custom = next(item for item in updated["scenes"] if item["custom"])
        self.assertEqual("教学楼天台", custom["name"])
        self.assertEqual([], custom["shots"])
        deleted = graph.delete_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": updated["graph_revision"],
            "scene_key": custom["scene_key"],
        })
        self.assertFalse(any(item["custom"] for item in deleted["scenes"]))
        self.assertEqual(custom["scene_key"], deleted["deleted_scenes"][0]["scene_key"])
        restored = graph.restore_scene(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": deleted["graph_revision"],
            "scene_key": custom["scene_key"],
        })
        self.assertTrue(any(item["scene_key"] == custom["scene_key"]
                            for item in restored["scenes"]))
        self.assertEqual([], restored["deleted_scenes"])

    def test_shot_can_bind_and_unbind_existing_scene(self):
        graph.sync_foundation(self.db, "alice", "alice", "p1")
        workspace = graph.scene_workspace(self.db, "alice", "p1")
        scene = workspace["scenes"][0]
        bound = graph.bind_scene_to_shot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": workspace["graph_revision"],
            "shot_key": "shot_001", "scene_key": scene["scene_key"],
        })
        selected = next(item for item in bound["scenes"] if item["scene_key"] == scene["scene_key"])
        self.assertIn("shot_001", [item["shot_key"] for item in selected["shots"]])
        unbound = graph.bind_scene_to_shot(self.db, "alice", "alice", {
            "project_id": "p1", "graph_revision": bound["graph_revision"],
            "shot_key": "shot_001", "scene_key": "",
        })
        self.assertFalse(any(
            item["shot_key"] == "shot_001"
            for scene_item in unbound["scenes"] for item in scene_item["shots"]
        ))
        synced = graph.sync_foundation(
            self.db, "alice", "alice", "p1", unbound["graph_revision"],
        )
        reloaded = graph.scene_workspace(self.db, "alice", "p1")
        self.assertEqual(synced["graph_revision"], reloaded["graph_revision"])
        self.assertFalse(any(
            item["shot_key"] == "shot_001"
            for scene_item in reloaded["scenes"] for item in scene_item["shots"]
        ))


if __name__ == "__main__":
    unittest.main()
