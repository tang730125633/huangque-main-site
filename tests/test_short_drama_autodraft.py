import os
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import (
    feature_flags,
    points,
    provider_keys,
    short_drama,
    short_drama_autodraft,
    short_drama_conversation,
    short_drama_native_audio,
    short_drama_preflight,
)
from providers.short_drama_visual.heygen_cinematic import HeyGenCinematicShotProvider
from providers.short_drama_visual.base import VisualProviderError


class Handler:
    def __init__(self, path, body=None, key="autodraft-route-key"):
        self.path = path
        self.body = body
        self.headers = {"Idempotency-Key": key}
        self.response = None

    def _token(self):
        return "alice"

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaAutodraftTests(unittest.TestCase):
    @staticmethod
    def _native_media_evidence(
            raw_hash="a" * 64, derived_hash="b" * 64,
            raw_file="video/minimax_h3_raw_result.mp4",
            derived_file="video/minimax_h3_result.mp4",
            width=2560, height=1440):
        audio = {
            "audible": True, "codec": "aac", "sample_rate": 48000,
            "channels": 2, "mean_volume_dbfs": -24.3,
            "max_volume_dbfs": -3.1,
        }
        return {
            "raw": {
                "file": raw_file,
                "sha256": raw_hash,
                "size_bytes": 12345,
            },
            "derived": {
                "file": derived_file,
                "sha256": derived_hash,
                "size_bytes": 12000,
                "derived_from_sha256": raw_hash,
            },
            "resolution": {"width": width, "height": height},
            "audio": audio,
            "inspected_at": 1700000000,
        }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        self.free = mock.patch.dict(
            os.environ, {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "1",
                "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "1",
                "CONTENT_OUT": self.tmp.name,
            }
        )
        self.free.start()
        self.content_out_path = mock.patch(
            "content_domains.core._out_path",
            side_effect=lambda relative: Path(self.tmp.name) / relative,
        )
        self.content_out_path.start()
        self.pending_video_asset = mock.patch(
            "content_domains.video.record_video_pending_asset"
        )
        self.pending_video_asset.start()
        short_drama.init_db(self.db)

        self.project = short_drama.create_project(
            self.db,
            "alice",
            {
                "title": "自动草稿测试",
                "synopsis": "两个朋友在公园找到一封来自未来的信，并做出不同选择。",
                "ratio": "16:9",
                "target_duration": 30,
                "shot_count": 6,
                "visual_style": "电影感写实",
                "target_platform": "抖音",
                "point_budget": 0,
            },
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
            "autodraft-select-direction",
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
            "autodraft-confirm-direction",
        )
        generated = short_drama_conversation.generate_script(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "温暖反转",
            },
            "generate-key",
        )
        locked = short_drama_conversation.lock_script(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "lock-key",
        )
        prepared = short_drama_preflight.generate_plan(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": locked["conversation"]["revision"],
                "quality_route": "quick_draft",
            },
            "prepare-key",
        )
        current = prepared["current_plan"]
        short_drama_preflight.confirm_plan(
            self.db, "alice", "alice",
            {
                "project_id": self.project["id"],
                "plan_id": current["id"],
                "plan_version": current["version"],
                "accepted_issue_keys": current["plan"]["required_acceptance"],
            },
            "confirm-key",
        )
        self.plan_id = current["id"]

    def test_heygen_provider_forces_legacy_1080p_requests_to_720p(self):
        request = HeyGenCinematicShotProvider().validate_request({
            "provider_avatar_id": "look-1",
            "prompt": "镜头缓慢推进",
            "ratio": "16:9",
            "resolution": "1080p",
            "duration_seconds": 5,
        })
        self.assertEqual("720p", request["resolution"])

    def tearDown(self):
        self.content_out_path.stop()
        self.pending_video_asset.stop()
        self.free.stop()
        self.tmp.cleanup()

    def _start(self, key="autodraft-key"):
        return short_drama_autodraft.start_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "plan_id": self.plan_id},
            key,
        )

    def _provider_avatar(self):
        return {
            "id": "avatar-local-1",
            "username": "alice",
            "name": "记者林夏",
            "status": "ready",
            "provider_avatar_id": "heygen-avatar-1",
            "image_url": "https://cdn.example/reference.png",
        }

    def _lock_project_character_references(self):
        from PIL import Image

        conn = self.db()
        try:
            plan_row = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()
            plan = json.loads(plan_row[0])
            required_keys = []
            for shot in plan.get("material_plan") or []:
                for dialogue in shot.get("dialogue") or []:
                    key = str(dialogue.get("character_key") or "")
                    if key and key not in required_keys:
                        required_keys.append(key)
                for value in shot.get("character_keys") or []:
                    key = str(value or "")
                    if key and key not in required_keys:
                        required_keys.append(key)
            for index, key in enumerate(required_keys, 1):
                conn.execute(
                    "INSERT OR IGNORE INTO short_drama_characters "
                    "(id,project_id,character_key,name,source_type,sort_order) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        "character-minimax-%d" % index,
                        self.project["id"], key, "角色 %d" % index,
                        "ai_character", index,
                    ),
                )
            rows = conn.execute(
                "SELECT character_key FROM short_drama_characters "
                "WHERE project_id=? ORDER BY sort_order",
                (self.project["id"],),
            ).fetchall()
            self.assertTrue(rows)
            for index, row in enumerate(rows, 1):
                relative = "short_drama_refs/character-%d.png" % index
                reference_path = Path(self.tmp.name) / relative
                reference_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (256, 256), (30, 80, 120)).save(
                    reference_path, "PNG"
                )
                conn.execute(
                    "UPDATE short_drama_characters SET reference_file=?,"
                    "reference_url=?,reference_version=1,reference_locked=1 "
                    "WHERE project_id=? AND character_key=?",
                    (
                        relative,
                        "https://cdn.example/short-drama/character-%d.png" % index,
                        self.project["id"], row[0],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _persist_bound_locked_scene_reference(self, *, file_value, url_value):
        self._lock_project_character_references()
        short_drama_autodraft.short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = next(
                item for item in plan["material_plan"]
                if item.get("character_keys")
            )
            scene = conn.execute(
                "SELECT entity.id,entity.current_version_id "
                "FROM short_drama_graph_entities entity "
                "JOIN short_drama_graph_relations relation "
                "ON relation.entity_id=entity.id "
                "JOIN short_drama_shots shot ON shot.id=relation.source_id "
                "WHERE entity.project_id=? AND entity.asset_type='scene' "
                "AND entity.status='active' AND relation.source_scope='shot' "
                "AND relation.relation_type='located_in' AND shot.shot_key=? "
                "LIMIT 1",
                (self.project["id"], shot["shot_key"]),
            ).fetchone()
            self.assertIsNotNone(scene)
            version_id = "legacy-scene-reference-" + shot["shot_key"]
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 "
                "FROM short_drama_graph_versions WHERE entity_id=?",
                (scene[0],),
            ).fetchone()[0])
            conn.execute(
                "UPDATE short_drama_graph_versions SET status='retired' "
                "WHERE entity_id=? AND status='locked'",
                (scene[0],),
            )
            conn.execute(
                "INSERT INTO short_drama_graph_versions "
                "(id,entity_id,version,parent_id,status,prompt,negative_prompt,"
                "references_json,attributes_json,valid_from,valid_to,content_hash,"
                "created_by,created_at,locked_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    scene[0],
                    version,
                    scene[1],
                    "locked",
                    "雨中的纪念广场",
                    "",
                    json.dumps([{
                        "file": file_value,
                        "url": url_value,
                        "name": "历史锁定场景图",
                    }], ensure_ascii=False),
                    json.dumps({
                        "source": "legacy",
                        "scene_operation_id": version_id,
                    }, ensure_ascii=False),
                    "",
                    "",
                    "legacy-scene-reference-content-hash",
                    "alice",
                    1,
                    1,
                ),
            )
            conn.execute(
                "UPDATE short_drama_graph_entities "
                "SET current_version_id=?,updated_at=? WHERE id=?",
                (version_id, 1, scene[0]),
            )
            conn.commit()
            return shot
        finally:
            conn.close()

    def _claim_legacy_scene_reference(
        self, shot, *, reference_project_id, operation_id,
    ):
        conn = self.db()
        try:
            conn.row_factory = sqlite3.Row
            scene = conn.execute(
                "SELECT entity.id,entity.current_version_id "
                "FROM short_drama_graph_entities entity "
                "JOIN short_drama_graph_relations relation "
                "ON relation.entity_id=entity.id "
                "JOIN short_drama_shots shot ON shot.id=relation.source_id "
                "WHERE entity.project_id=? AND entity.asset_type='scene' "
                "AND relation.source_scope='shot' "
                "AND relation.relation_type='located_in' AND shot.shot_key=? "
                "LIMIT 1",
                (self.project["id"], shot["shot_key"]),
            ).fetchone()
            scene_row = next(
                item for item in short_drama_autodraft.short_drama_asset_graph._scene_rows(
                    conn, self.project["id"],
                )
                if item["id"] == scene[0]
            )
            scene_key = short_drama_autodraft.short_drama_asset_graph._scene_key(
                scene_row
            )
            conn.execute(
                "UPDATE short_drama_graph_versions SET attributes_json=? WHERE id=?",
                (
                    json.dumps({
                        "source": "upload",
                        "scene_operation_id": operation_id,
                        "scene_reference_owner": "alice",
                        "scene_reference_actor": "alice",
                        "scene_reference_project_id": reference_project_id,
                    }),
                    scene[1],
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_graph_audit"
                "(id,project_id,actor,action,target_id,details_json,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    operation_id,
                    self.project["id"],
                    "alice",
                    "set_scene_reference",
                    scene_key,
                    json.dumps({
                        "operation_id": operation_id,
                        "source": "upload",
                    }),
                    1,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_controlled_scene_reference(self):
        import base64
        import io
        from PIL import Image
        from content_domains import image as image_domain

        self._lock_project_character_references()
        asset_graph = short_drama_autodraft.short_drama_asset_graph
        asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        scenes = asset_graph.scene_workspace(
            self.db, "alice", self.project["id"],
        )
        scene = next(
            item for item in scenes["scenes"] if item.get("shots")
        )
        raw = io.BytesIO()
        Image.new("RGB", (256, 256), (45, 85, 125)).save(raw, "PNG")
        data_url = "data:image/png;base64," + base64.b64encode(
            raw.getvalue()
        ).decode("ascii")
        with mock.patch.object(image_domain, "OUT_DIR", Path(self.tmp.name)):
            created = asset_graph.set_scene_reference(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "graph_revision": scenes["graph_revision"],
                    "scene_key": scene["scene_key"],
                    "source": "upload",
                    "image_data": data_url,
                    "filename": "controlled-scene.png",
                    "prompt": "雨中的纪念广场",
                },
            )
        asset_graph.lock_scene_reference(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "graph_revision": created["graph_revision"],
                "scene_key": scene["scene_key"],
            },
        )
        return scene["shots"][0]

    def _provider_quote(self):
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        shot = workspace["provider_poc"]["shots"][0]
        avatar = self._provider_avatar()
        return short_drama_autodraft.create_provider_quote(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "plan_id": self.plan_id,
                "shot_key": shot["shot_key"],
                "avatar_id": avatar["id"],
            },
            avatar_lookup=lambda _username, _avatar_id: avatar,
        )

    def _running_provider_job(self, key):
        charged = []
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
            "HEYGEN_API_KEY": "configured-for-test",
            "HQ_SHORT_DRAMA_PROVIDER_SHOT_POINTS_PER_SECOND": "10",
        }):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, key,
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda user, cost, reason, charge_key: charged.append(
                    (user, cost, reason, charge_key)
                ),
                project_usage=short_drama._project_point_usage,
            )
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='running',"
                "provider_job_id='provider-timeout-job',created_at=100,"
                "updated_at=100 WHERE id=?",
                (job["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertGreater(quote["cost"], 0)
        self.assertEqual(1, len(charged))
        return job, quote

    def test_provider_shot_cost_uses_shared_dynamic_model_pricing(self):
        rates = {
            "video.grok.v1.480p": 10,
            "video.grok.v1.720p": 12,
            "video.grok.v1_5.480p": 15,
            "video.grok.v1_5.720p": 25,
            "video.grok.v1_5.1080p": 44,
        }

        def price(key):
            return rates[key]

        def cost(model, resolution, duration=5):
            return short_drama_autodraft._provider_shot_cost({
                "provider": "grok",
                "model": model,
                "resolution": resolution,
                "duration_seconds": duration,
            })

        with mock.patch(
            "content_domains.points.pricing.get_price", side_effect=price
        ), mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_PROVIDER_SHOT_POINTS_PER_SECOND": "999"
        }):
            self.assertEqual(50, cost("grok-imagine-video", "480p"))
            self.assertEqual(60, cost("grok-imagine-video", "720p"))
            self.assertEqual(125, cost("grok-imagine-video-1.5", "720p"))
            self.assertEqual(220, cost("grok-imagine-video-1.5", "1080p"))
            rates["video.grok.v1.720p"] = 17
            self.assertEqual(85, cost("grok-imagine-video", "720p"))
            rates["video.cinematic.open"] = 9
            self.assertEqual(45, short_drama_autodraft._provider_shot_cost({
                "provider": "heygen_cinematic",
                "duration_seconds": 5,
            }))

    def test_provider_quote_request_errors_are_readable_utf8(self):
        cases = [
            ({}, "Provider 规范化请求缺少有效渠道"),
            (
                {"provider": "grok", "resolution": "720p", "duration_seconds": 5},
                "Grok 规范化请求缺少必要计费参数",
            ),
            (
                {
                    "provider": "grok",
                    "model": "grok-imagine-video-1.5",
                    "duration_seconds": 5,
                },
                "Grok 规范化请求缺少必要计费参数",
            ),
            (
                {
                    "provider": "grok",
                    "model": "grok-imagine-video-1.5",
                    "resolution": "720p",
                    "duration_seconds": 0,
                },
                "Grok 规范化请求缺少必要计费参数",
            ),
        ]
        for request, expected_detail in cases:
            with self.subTest(request=request):
                with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                    short_drama_autodraft._provider_shot_cost(request)
                self.assertEqual("provider_quote_request_invalid", raised.exception.code)
                self.assertEqual(500, raised.exception.status)
                self.assertEqual(expected_detail, str(raised.exception))
                self.assertEqual(
                    expected_detail,
                    str(raised.exception).encode("utf-8").decode("utf-8"),
                )

    def test_grok_15_quote_uses_the_normalized_persisted_request(self):
        avatar = self._provider_avatar()
        rates = {"video.grok.v1_5.720p": 25}
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()
            plan = json.loads(row[0])
            target_shot = plan["material_plan"][0]
            target_shot["duration_ms"] = 5000
            target_shot["input_hash"] = short_drama_autodraft._hash(target_shot)
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "grok",
            "HQ_SHORT_DRAMA_GROK_MODEL": "grok-imagine-video-1.5",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True
        ), mock.patch(
            "content_domains.points.pricing.get_price",
            side_effect=lambda key: rates[key],
        ):
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"],
                avatar_list=lambda _username, _limit: [avatar],
            )
            shot = next(
                item for item in workspace["provider_poc"]["shots"]
                if item["shot_key"] == target_shot["shot_key"]
            )
            body = {
                "project_id": self.project["id"],
                "plan_id": self.plan_id,
                "shot_key": shot["shot_key"],
                "avatar_id": avatar["id"],
            }
            quote = short_drama_autodraft.create_provider_quote(
                self.db, "alice", "alice", body,
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )
            self.assertEqual("grok-imagine-video-1.5", quote["request"]["model"])
            self.assertEqual(125, quote["cost"])

            conn = self.db()
            try:
                row = conn.execute(
                    "SELECT request_json,cost FROM short_drama_provider_shot_quotes "
                    "WHERE token=?", (quote["quote_token"],),
                ).fetchone()
            finally:
                conn.close()
            persisted_request = json.loads(row[0])
            self.assertEqual("grok", persisted_request["provider"])
            self.assertEqual("grok-imagine-video-1.5", persisted_request["model"])
            self.assertEqual("720p", persisted_request["resolution"])
            self.assertEqual(5, persisted_request["duration_seconds"])
            self.assertEqual(125, row[1])

            deduct = mock.Mock()
            with mock.patch.object(
                provider_keys,
                "claim_candidate",
                return_value={"id": "grok-15-key", "secret": "test-secret"},
            ):
                job = short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "grok-15-normalized-submit",
                    avatar_lookup=lambda _username, _avatar_id: avatar,
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
            self.assertEqual(125, job["cost"])
            self.assertEqual("grok-imagine-video-1.5", job["request"]["model"])
            self.assertEqual("720p", job["request"]["resolution"])
            self.assertEqual(5, job["request"]["duration_seconds"])
            deduct.assert_called_once()

            rates["video.grok.v1_5.720p"] = 31
            repriced = short_drama_autodraft.create_provider_quote(
                self.db, "alice", "alice", body,
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )
            self.assertEqual(155, repriced["cost"])

    def test_confirmed_plan_starts_free_local_pollable_job(self):
        job = self._start()
        self.assertEqual("queued", job["status"])
        self.assertEqual(0, job["cost"])
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("producing", workspace["state"])
        self.assertEqual("development_free", workspace["billing"]["mode"])
        self.assertEqual("demo", workspace["production"]["mode"])

    def test_completion_exception_is_terminal_and_not_rendered_again(self):
        job = self._start("terminal-preview-failure")
        temp = (
            Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"] /
            (".%s.tmp" % job["id"])
        )
        temp.mkdir(parents=True)
        (temp / "partial.mp4").write_bytes(b"partial")
        renderer = mock.Mock(side_effect=RuntimeError("ffmpeg timed out"))
        with mock.patch.object(short_drama_autodraft, "_complete", renderer):
            for _ in range(6):
                job = short_drama_autodraft.get_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
            repeated = short_drama_autodraft.get_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertEqual("failed", job["phase"])
        self.assertEqual(100, job["progress"])
        self.assertTrue(job["error"]["retryable"])
        self.assertTrue(job["error"]["temporary_output_cleaned"])
        self.assertFalse(temp.exists())
        self.assertEqual("failed", repeated["status"])
        self.assertEqual(1, renderer.call_count)
        conn = self.db()
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM short_drama_autodraft_jobs WHERE project_id=? "
                "AND status IN ('queued','running')", (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, active)

    def test_http_start_route_accepts_project_usage_contract(self):
        handler = Handler(
            "/api/gen/short-drama/autodraft/jobs",
            body={"project_id": self.project["id"], "plan_id": self.plan_id},
        )
        verify = lambda token: {
            "username": token,
            "must_change": False,
        } if token else None
        self.assertTrue(
            short_drama.dispatch_http(handler, "POST", self.db, verify)
        )
        self.assertEqual(200, handler.response[0])
        self.assertEqual("queued", handler.response[1]["status"])

    def test_polling_finishes_with_playable_degraded_draft(self):
        job = self._start()
        result = job
        for _ in range(8):
            result = short_drama_autodraft.get_job(
                self.db, "alice", self.project["id"], job["id"]
            )
            if result["status"] not in short_drama_autodraft.ACTIVE:
                break
        self.assertEqual("degraded", result["status"])
        self.assertEqual(100, result["progress"])
        self.assertEqual("/assets/meiye_video.mp4", result["result"]["url"])
        self.assertEqual(6, len(result["result"]["shot_cards"]))
        self.assertEqual(1, len(result["result"]["issues"]))

        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("draft_ready", workspace["state"])
        self.assertEqual("degraded", workspace["current_version"]["status"])
        self.assertTrue(workspace["current_version"]["is_demo"])

    def test_real_start_is_blocked_before_attempt_when_provider_is_unavailable(self):
        with mock.patch.dict(
            os.environ, {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "0",
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "",
            }
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                self._start("provider-unavailable-key")
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"]
            )
        self.assertEqual("autodraft_provider_unavailable", raised.exception.code)
        self.assertEqual(503, raised.exception.status)
        self.assertFalse(workspace["production"]["ready"])
        self.assertEqual(
            "provider_not_selected",
            workspace["production"]["provider"]["code"],
        )
        conn = self.db()
        try:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM short_drama_autodraft_attempts "
                "WHERE idempotency_key='provider-unavailable-key'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, attempts)

    def test_provider_preflight_compiles_one_shot_without_external_submission(self):
        avatar = {
            "id": "avatar-local-1",
            "username": "alice",
            "name": "记者林夏",
            "status": "ready",
            "provider_avatar_id": "heygen-avatar-1",
        }
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "0",
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-preflight-only",
            },
        ):
            workspace = short_drama_autodraft.workspace(
                self.db,
                "alice",
                "alice",
                self.project["id"],
                avatar_list=lambda _username, _limit: [avatar],
            )
            shot = workspace["provider_poc"]["shots"][0]
            with mock.patch(
                "providers.short_drama_visual.heygen_cinematic."
                "HeyGenCinematicShotProvider.create_job"
            ) as create_job:
                result = short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "avatar_id": avatar["id"],
                    },
                    avatar_lookup=lambda _username, _avatar_id: avatar,
                )
        self.assertTrue(result["ready"])
        self.assertFalse(result["billable"])
        self.assertFalse(result["external_submission"])
        self.assertEqual("[已绑定]", result["request"]["provider_avatar"])
        self.assertNotIn("heygen-avatar-1", json.dumps(result, ensure_ascii=False))
        conversation = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        source_shot = conversation["current_script"]["script"]["shots"][0]
        self.assertTrue(
            result["request"]["prompt"].startswith(
                source_shot["provider_prompt"].rstrip("。；; ")
            )
        )
        self.assertIn(
            "禁止项：%s" % source_shot["negative_prompt"].rstrip("。；; "),
            result["request"]["prompt"],
        )
        self.assertEqual(64, len(result["request_hash"]))
        create_job.assert_not_called()

    def test_grok_preflight_uses_avatar_reference_without_heygen_binding(self):
        avatar = {
            "id": "avatar-grok-1",
            "username": "alice",
            "name": "记者林夏",
            "status": "ready",
            "image_url": "https://cdn.example/avatar-grok-1.png",
            "image_file": "avatar/avatar-grok-1.png",
        }
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "grok",
                "XAI_API_KEY": "configured-for-preflight-only",
            },
        ), mock.patch.object(provider_keys, "has_candidate", return_value=True):
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"],
                avatar_list=lambda _username, _limit: [avatar],
            )
            shot = workspace["provider_poc"]["shots"][0]
            result = short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": avatar["id"],
                },
                avatar_lookup=lambda _username, _avatar_id: avatar,
                include_private=True,
            )
        self.assertEqual("grok", workspace["provider_poc"]["provider"])
        self.assertEqual(1, len(workspace["provider_poc"]["avatars"]))
        self.assertTrue(result["ready"])
        self.assertEqual("grok", result["provider"])
        self.assertEqual(
            "https://cdn.example/avatar-grok-1.png",
            result["_provider_request"]["reference_image_url"],
        )
        self.assertNotIn("provider_avatar_id", result["request"])

    def test_grok_preflight_uses_locked_ai_character_reference_directly(self):
        conn = self.db()
        try:
            plan_row = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()
            plan = json.loads(plan_row[0])
            character_key = next(
                str(key)
                for shot in plan["material_plan"]
                for key in shot.get("character_keys") or []
                if str(key)
            )
            conn.execute(
                "INSERT INTO short_drama_characters "
                "(id,project_id,character_key,name,source_type,reference_file,"
                "reference_locked,sort_order) VALUES (?,?,?,?,?,?,1,1)",
                (
                    "character-grok-direct",
                    self.project["id"],
                    character_key,
                    "直接参考图角色",
                    "ai_character",
                    "image/locked-character.png",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "grok",
                "XAI_API_KEY": "configured-for-preflight-only",
            },
        ), mock.patch.object(provider_keys, "has_candidate", return_value=True), \
             mock.patch.object(
                 provider_keys,
                 "claim_candidate",
                 return_value={"id": "grok-test-key", "secret": "test-only-secret"},
             ):
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"],
                avatar_list=lambda _username, _limit: [],
            )
            shot = next(
                item for item in workspace["provider_poc"]["shots"]
                if item["primary_character_key"] == character_key
            )
            self.assertEqual(
                "character:" + character_key, shot["primary_avatar_id"]
            )
            result = short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "character_key": character_key,
                    "avatar_id": shot["primary_avatar_id"],
                },
                include_private=True,
            )
            quote = short_drama_autodraft.create_provider_quote(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "character_key": character_key,
                    "avatar_id": shot["primary_avatar_id"],
                },
            )
            deduct = mock.Mock()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "grok-direct-reference-start",
                deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(
            "image/locked-character.png",
            result["_provider_request"]["reference_image_file"],
        )
        self.assertEqual("grok", job["provider"])
        self.assertEqual(1, deduct.call_count)

    def test_provider_preflight_uses_locked_provider_prompt_as_source_of_truth(self):
        conn = self.db()
        try:
            raw = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0]
            plan = json.loads(raw)
            shot = plan["material_plan"][0]
            shot["provider_prompt"] = "唯一真实提示词：雨夜车站，女儿回头看向母亲"
            shot["negative_prompt"] = "字幕，水印，额外人物"
            shot["visual_prompt"] = "这段旧画面描述不能进入真实请求"
            shot["input_hash"] = short_drama_autodraft._hash(shot)
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        avatar = self._provider_avatar()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-preflight-only",
            },
        ):
            result = short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": avatar["id"],
                },
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )

        self.assertTrue(result["request"]["prompt"].startswith(
            "唯一真实提示词：雨夜车站，女儿回头看向母亲 "
            "禁止项：字幕，水印，额外人物。"
        ))
        self.assertIn("全片统一视觉基线", result["request"]["prompt"])
        self.assertNotIn("旧画面描述", result["request"]["prompt"])

    def test_provider_preflight_persists_generation_only_execution_override(self):
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
        finally:
            conn.close()
        avatar = self._provider_avatar()
        execution = {
            "visual": "男孩把糖果递给女孩",
            "camera": "中近景，缓慢推近",
            "performance": "先犹豫，再露出真诚微笑",
            "scene": "傍晚的小区长椅",
            "lighting": "暖色夕阳",
            "composition_style": "电影感写实",
            "continuity": "服装和糖果袋与上一镜一致",
            "sound_design": "长椅旁有晚风和远处孩童玩耍声",
            "negative_prompt": "字幕，水印，人物变脸",
            "provider_prompt": "傍晚长椅，中近景缓慢推近，男孩真诚分享糖果",
        }
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
            "HEYGEN_API_KEY": "configured-for-preflight-only",
        }):
            result = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"], "avatar_id": avatar["id"],
                    "execution": execution,
                }, avatar_lookup=lambda _username, _avatar_id: avatar,
            )
            repeated = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"], "avatar_id": avatar["id"],
                }, avatar_lookup=lambda _username, _avatar_id: avatar,
            )
        self.assertIn(execution["provider_prompt"], result["request"]["prompt"])
        self.assertIn(
            "景别与运镜：中近景，缓慢推近",
            result["request"]["prompt"],
        )
        self.assertIn(
            "画面与人物动作：男孩把糖果递给女孩",
            result["request"]["prompt"],
        )
        self.assertIn(
            "补充生成要求：" + execution["provider_prompt"],
            result["request"]["prompt"],
        )
        self.assertEqual(result["request"]["prompt"], repeated["request"]["prompt"])
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"],
        )
        self.assertEqual(
            execution["provider_prompt"],
            workspace["provider_execution_overrides"][shot["shot_key"]]["provider_prompt"],
        )
        self.assertEqual(
            execution["sound_design"],
            workspace["provider_execution_overrides"][shot["shot_key"]]["sound_design"],
        )

    def test_structured_execution_fields_are_valid_without_manual_prompt(self):
        execution = short_drama_autodraft._clean_execution({
            "visual": "顾承川停在纪念墙前",
            "camera": "固定中近景，对焦顾承川，背景虚化",
            "continuity": "服装和人物位置承接上一镜头",
        })
        self.assertEqual("", execution["provider_prompt"])
        prompt = short_drama_autodraft._execution_visual_prompt(execution)
        self.assertIn("画面与人物动作：顾承川停在纪念墙前", prompt)
        self.assertIn("景别与运镜：固定中近景，对焦顾承川，背景虚化", prompt)
        self.assertIn("连续性要求：服装和人物位置承接上一镜头", prompt)

    def test_legacy_synthesized_prompt_is_not_appended_twice(self):
        legacy = {
            "visual": "男孩把糖果递给女孩",
            "camera": "中近景缓慢推近",
            "performance": "先犹豫再微笑",
            "scene": "傍晚长椅",
            "lighting": "暖色夕阳",
            "composition_style": "电影感写实",
            "continuity": "服装和糖果袋承接上一镜头",
        }
        legacy["provider_prompt"] = short_drama_autodraft._legacy_execution_prompt(
            legacy
        )
        cleaned = short_drama_autodraft._clean_execution(legacy)
        self.assertEqual("", cleaned["provider_prompt"])
        self.assertEqual(
            short_drama_autodraft._EXECUTION_PROMPT_SEMANTICS,
            cleaned["prompt_semantics"],
        )
        compiled = short_drama_autodraft._execution_visual_prompt(cleaned)
        self.assertEqual(1, compiled.count(legacy["visual"]))
        self.assertNotIn("补充生成要求：", compiled)

    def test_free_preflight_rejects_aggregate_prompt_over_provider_limit(self):
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
        finally:
            conn.close()
        avatar = self._provider_avatar()
        execution = {
            "visual": "动" * 600,
            "camera": "镜" * 300,
            "performance": "演" * 300,
            "scene": "景" * 160,
            "lighting": "光" * 240,
            "composition_style": "构" * 240,
            "continuity": "连" * 360,
            "sound_design": "",
            "negative_prompt": "",
            "provider_prompt": "",
            "character_keys": [shot["character_keys"][0]],
            "prompt_semantics": short_drama_autodraft._EXECUTION_PROMPT_SEMANTICS,
        }
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
            "HEYGEN_API_KEY": "configured-for-preflight-only",
        }):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "avatar_id": avatar["id"],
                        "execution": execution,
                    }, avatar_lookup=lambda _username, _avatar_id: avatar,
                )
        self.assertEqual("visual_prompt_too_long", raised.exception.code)
        self.assertEqual(422, raised.exception.status)

    def test_sensitive_failure_cannot_disable_continuity_or_scene_references(self):
        execution = short_drama_autodraft._clean_execution({
            "provider_prompt": "校园教室里，学生收拾书包",
            "include_continuity_reference": False,
            "include_scene_reference": True,
            "scene_key": "scene-group:locked-classroom",
        })
        self.assertTrue(execution["include_continuity_reference"])
        self.assertTrue(execution["include_scene_reference"])
        self.assertEqual("scene-group:locked-classroom", execution["scene_key"])
        error = short_drama_autodraft._provider_failure_error({
            "failure": {
                "code": "1026",
                "message": "input new_sensitive, input text sensitive",
            }
        })
        self.assertEqual("1026", error.provider_code)
        self.assertIn("输入内容未通过审核", str(error))
        self.assertNotIn("input text sensitive", str(error))

    def test_execution_override_accepts_deduplicated_character_binding(self):
        execution = short_drama_autodraft._clean_execution({
            "provider_prompt": "陈宇走进教室",
            "character_keys": ["character_2", "character_2", "character_1"],
        })
        self.assertEqual(
            ["character_2", "character_1"], execution["character_keys"]
        )
        with self.assertRaises(short_drama_autodraft.AutodraftError):
            short_drama_autodraft._clean_execution({
                "provider_prompt": "空镜头", "character_keys": [],
            })

    def test_legacy_plan_recovers_prompt_from_its_locked_script_snapshot(self):
        conversation = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        source_shot = conversation["current_script"]["script"]["shots"][0]
        conn = self.db()
        try:
            raw = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0]
            plan = json.loads(raw)
            shot = plan["material_plan"][0]
            shot.pop("provider_prompt", None)
            shot.pop("negative_prompt", None)
            shot["visual_prompt"] = "旧计划中的简略画面说明"
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        avatar = self._provider_avatar()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-preflight-only",
            },
        ):
            result = short_drama_autodraft.preview_provider_request(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": avatar["id"],
                },
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )

        self.assertTrue(
            result["request"]["prompt"].startswith(
                source_shot["provider_prompt"].rstrip("。；; ")
            )
        )
        self.assertIn(
            source_shot["negative_prompt"].rstrip("。；; "),
            result["request"]["prompt"],
        )
        self.assertNotIn("旧计划中的简略画面说明", result["request"]["prompt"])

    def test_provider_preflight_rejects_unowned_avatar(self):
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        shot = workspace["provider_poc"]["shots"][0]
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
        }):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "avatar_id": "avatar-bob",
                    },
                    avatar_lookup=lambda _username, _avatar_id: {
                        "id": "avatar-bob",
                        "username": "bob",
                        "status": "ready",
                        "provider_avatar_id": "provider-bob",
                    },
                )
        self.assertEqual("provider_avatar_forbidden", raised.exception.code)
        self.assertEqual(403, raised.exception.status)

    def test_provider_preflight_http_route_is_non_billable(self):
        self._lock_project_character_references()
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        handler = Handler(
            "/api/gen/short-drama/autodraft/provider-preflight",
            body={
                "project_id": self.project["id"],
                "plan_id": self.plan_id,
                "shot_key": workspace["provider_poc"]["shots"][0]["shot_key"],
                "avatar_id": "foreign-avatar-must-be-ignored",
            },
        )
        verify = lambda token: {
            "username": token,
            "must_change": False,
        } if token else None
        avatar_lookup = mock.Mock(side_effect=AssertionError(
            "MiniMax preflight must not resolve a client supplied avatar_id"
        ))
        with mock.patch(
            "content_domains.core._out_path",
            side_effect=lambda relative: Path(self.tmp.name) / relative,
        ), mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            self.assertTrue(short_drama.dispatch_http(
                handler,
                "POST",
                self.db,
                verify,
                avatar_lookup=avatar_lookup,
            ))
        self.assertEqual(200, handler.response[0], handler.response[1])
        self.assertEqual("minimax_h3", handler.response[1]["provider"])
        self.assertFalse(handler.response[1]["billable"])
        self.assertFalse(handler.response[1]["external_submission"])
        avatar_lookup.assert_not_called()

    def test_minimax_url_only_locked_character_is_not_reported_ready(self):
        self._lock_project_character_references()
        conn = self.db()
        try:

            conn.execute(
                "UPDATE short_drama_characters SET reference_file='' "
                "WHERE project_id=?",
                (self.project["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"],
                avatar_list=lambda _username, _limit: [],
            )
            provider_poc = workspace["provider_poc"]
            self.assertFalse(provider_poc["all_roles_bound"])
            self.assertTrue(any(
                not character["binding_ready"]
                for character in provider_poc["characters"]
            ))
            shot = next(
                item for item in provider_poc["shots"]
                if item["character_keys"]
            )

            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "character_key": shot["primary_character_key"],
                    },
                    include_private=True,
                )
        self.assertEqual("provider_avatar_not_ready", raised.exception.code)

    def test_minimax_omits_url_only_optional_references(self):
        self._lock_project_character_references()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch(
            "content_domains.core._out_path",
            side_effect=lambda relative: Path(self.tmp.name) / relative,
        ):
            workspace = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"],
                avatar_list=lambda _username, _limit: [],
            )
            shots = [
                item for item in workspace["provider_poc"]["shots"]
                if item["character_keys"]
            ]
            cases = (
                (
                    "scene",
                    shots[0],
                    None,
                    {
                        "scene_key": "scene-legacy", "name": "旧场景",
                        "file": "", "url": "https://cdn.example/scene.png",
                    },
                    "__scene_reference__",
                ),
                (
                    "continuity",
                    shots[1],
                    {
                        "shot_key": shots[0]["shot_key"],
                        "file": "", "url": "https://cdn.example/tail.png",
                    },
                    None,
                    "__continuity_tail__",
                ),
            )
            for label, shot, previous, scene, forbidden_key in cases:
                with self.subTest(optional_reference=label), mock.patch.object(
                    short_drama_autodraft,
                    "_previous_shot_reference",
                    return_value=previous,
                ), mock.patch.object(
                    short_drama_autodraft.short_drama_asset_graph,
                    "locked_scene_reference",
                    return_value=scene,
                ):
                    result = short_drama_autodraft.preview_provider_request(
                        self.db,
                        "alice",
                        "alice",
                        {
                            "project_id": self.project["id"],
                            "plan_id": self.plan_id,
                            "shot_key": shot["shot_key"],
                            "character_key": shot["primary_character_key"],
                        },
                        include_private=True,
                    )
                    reference_keys = {
                        item["character_key"]
                        for item in result["_provider_request"]["reference_images"]
                    }
                    self.assertNotIn(forbidden_key, reference_keys)

    def test_start_is_idempotent_and_rejects_changed_request(self):
        first = self._start("same-key")
        replay = self._start("same-key")
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.start_job(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "plan_id": "different"},
                "same-key",
            )
        self.assertIn(
            raised.exception.code,
            {"confirmed_plan_required", "idempotency_conflict"},
        )

    def test_only_one_active_job_per_project(self):
        self._start("first-key")
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            self._start("second-key")
        self.assertEqual("active_autodraft_job", raised.exception.code)

    def test_provider_assembly_requires_all_shots_and_pins_latest_versions(self):
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot_keys = [item["shot_key"] for item in plan["material_plan"]]
            now = 1700000000
            for index, shot_key in enumerate(shot_keys[:-1]):
                for version in (1, 2) if index == 0 else (1,):
                    job_id = "job-%s-%s" % (shot_key, version)
                    conn.execute(
                        "INSERT INTO short_drama_provider_shot_jobs "
                        "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                        "character_key,avatar_id,provider,provider_job_id,status,progress,"
                        "poll_count,input_hash,request_json,result_json,error_json,cost,"
                        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,100,1,?,?,?,?,0,?,?)",
                        (
                            job_id, self.project["id"], "alice", "alice",
                            self.plan_id, shot_key, "character_1", "avatar_1",
                            "heygen_cinematic", "provider-" + job_id, "succeeded",
                            "hash-%s-%s" % (shot_key, version), "{}", "{}", None,
                            now + version, now + version,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO short_drama_provider_shot_versions "
                        "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                        "status,file,url,input_hash,created_at) "
                        "VALUES (?,?,?,?,?,'heygen_cinematic',?,'ready',?,?,?,?)",
                        (
                            "version-%s-%s" % (shot_key, version), self.project["id"],
                            job_id, shot_key, version, "provider-" + job_id,
                            "video/%s-v%s.mp4" % (shot_key, version),
                            "/api/gen/file/video/%s-v%s.mp4" % (shot_key, version),
                            "hash-%s-%s" % (shot_key, version), now + version,
                        ),
                    )
            partial = short_drama_autodraft._provider_assembly_snapshot(
                conn, self.project["id"], plan,
            )
            self.assertFalse(partial["all_ready"])
            self.assertEqual([shot_keys[-1]], partial["missing_shot_keys"])
            self.assertEqual(
                "version-%s-2" % shot_keys[0], partial["shots"][0]["id"]
            )
            conn.commit()
            short_drama_autodraft.select_provider_version(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "shot_key": shot_keys[0],
                    "version_id": "version-%s-1" % shot_keys[0],
                },
            )
            selected = short_drama_autodraft._provider_assembly_snapshot(
                conn, self.project["id"], plan,
            )
            self.assertEqual(
                "version-%s-1" % shot_keys[0], selected["shots"][0]["id"]
            )
        finally:
            conn.close()

    def test_minimax_assembly_requires_native_2k_versions(self):
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot_keys = [item["shot_key"] for item in plan["material_plan"]]
            now = 1700000000
            for index, shot_key in enumerate(shot_keys):
                job_id = "minimax-job-" + shot_key
                resolution = "768p" if index == 0 else "2k"
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_jobs "
                    "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                    "character_key,avatar_id,provider,provider_job_id,status,progress,"
                    "poll_count,input_hash,request_json,result_json,error_json,cost,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,100,1,?,?,?,?,0,?,?)",
                    (
                        job_id, self.project["id"], "alice", "alice", self.plan_id,
                        shot_key, "character_1", "avatar_1", "minimax_h3",
                        "provider-" + job_id, "succeeded", "hash-" + shot_key,
                        json.dumps({"resolution": resolution, "duration_seconds": 5}),
                        json.dumps({
                            "native_media": self._native_media_evidence(
                                raw_file="video/%s-raw.mp4" % shot_key,
                                derived_file="video/%s.mp4" % shot_key,
                                width=1920 if index == 0 else 2560,
                                height=1080 if index == 0 else 1440,
                            ),
                        }), None, now + index, now + index,
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions "
                    "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                    "status,file,url,input_hash,created_at) "
                    "VALUES (?,?,?,?,1,'minimax_h3',?,'ready',?,?,?,?)",
                    (
                        "minimax-version-" + shot_key, self.project["id"], job_id,
                        shot_key, "provider-" + job_id, "video/%s.mp4" % shot_key,
                        "/api/gen/file/video/%s.mp4" % shot_key,
                        "hash-" + shot_key, now + index,
                    ),
                )
            snapshot = short_drama_autodraft._provider_assembly_snapshot(
                conn, self.project["id"], plan, "minimax_h3",
            )
            self.assertTrue(snapshot["assets_ready"])
            self.assertFalse(snapshot["quality_ready"])
            self.assertFalse(snapshot["all_ready"])
            self.assertEqual([shot_keys[0]], snapshot["low_resolution_shot_keys"])
        finally:
            conn.close()

    def test_provider_preview_rejects_changed_selected_native_file_before_ffmpeg(self):
        root = Path(self.tmp.name) / "native-changed"
        source_relative = "video/selected-shot.mp4"
        source = root / source_relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"changed-native-file")
        assembly = {
            "ratio": "16:9",
            "duration_ms": 5000,
            "media_contract": {"media_mode": "provider_audio"},
            "shots": [{
                "id": "version-shot-02",
                "shot_key": "shot_02",
                "provider": "minimax_h3",
                "file": source_relative,
                "native_media": self._native_media_evidence(
                    raw_file="video/selected-shot-raw.mp4",
                    derived_file=source_relative,
                ),
            }],
        }
        source_probe = {
            "duration_ms": 5000,
            "video": {"width": 2560, "height": 1440},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
        }
        output_probe = {
            **source_probe,
            "video": {"width": 1920, "height": 1080},
        }
        current = {
            "sha256": "c" * 64,
            "size_bytes": len(b"changed-native-file"),
            "resolution": {"width": 2560, "height": 1440},
            "audio": {"audible": True, "codec": "aac"},
            "inspected_at": 1700000001,
        }

        def render(command, **_kwargs):
            Path(command[-1]).write_bytes(b"preview")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"CONTENT_OUT": str(root)}), \
                mock.patch.object(
                    short_drama_autodraft.media_plan,
                    "probe_media",
                    side_effect=lambda path: (
                        output_probe if Path(path).name == "preview-1080p.mp4"
                        else source_probe
                    ),
                ), mock.patch.object(
                    short_drama_native_audio,
                    "inspect_native_media",
                    return_value=current,
                ), mock.patch.object(
                    short_drama_autodraft,
                    "_run_preview_process",
                    side_effect=render,
                ) as runner:
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft._render_provider_preview(
                    self.project["id"], "changed-native", assembly
                )
        self.assertEqual("provider_native_media_changed", raised.exception.code)
        self.assertIn("shot_02", str(raised.exception))
        runner.assert_not_called()

    def test_provider_preview_rejects_silent_selected_native_file_before_ffmpeg(self):
        root = Path(self.tmp.name) / "native-silent"
        source_relative = "video/selected-shot.mp4"
        source = root / source_relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"silent-native-file")
        assembly = {
            "ratio": "16:9",
            "duration_ms": 5000,
            "media_contract": {"media_mode": "provider_audio"},
            "shots": [{
                "id": "version-shot-03",
                "shot_key": "shot_03",
                "provider": "minimax_h3",
                "file": source_relative,
                "native_media": self._native_media_evidence(
                    raw_file="video/selected-shot-raw.mp4",
                    derived_file=source_relative,
                ),
            }],
        }
        source_probe = {
            "duration_ms": 5000,
            "video": {"width": 2560, "height": 1440},
            "audio": {"codec": "aac"},
        }
        output_probe = {
            **source_probe,
            "video": {"width": 1920, "height": 1080},
        }

        def render(command, **_kwargs):
            Path(command[-1]).write_bytes(b"preview")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"CONTENT_OUT": str(root)}), \
                mock.patch.object(
                    short_drama_autodraft.media_plan,
                    "probe_media",
                    side_effect=lambda path: (
                        output_probe if Path(path).name == "preview-1080p.mp4"
                        else source_probe
                    ),
                ), mock.patch.object(
                    short_drama_native_audio,
                    "inspect_native_media",
                    side_effect=short_drama_native_audio.NativeAudioError(
                        "provider_audio_silent", "声音不可听"
                    ),
                ), mock.patch.object(
                    short_drama_autodraft,
                    "_run_preview_process",
                    side_effect=render,
                ) as runner:
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft._render_provider_preview(
                    self.project["id"], "silent-native", assembly
                )
        self.assertEqual("provider_native_audio_invalid", raised.exception.code)
        self.assertIn("shot_03", str(raised.exception))
        runner.assert_not_called()

    def test_vertical_provider_preview_keeps_audio_and_builds_1080p(self):
        root = Path(self.tmp.name) / "content-out"
        source = root / "provider" / "shot.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"provider-video")
        commands = []

        def render(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"rendered-preview")
            return mock.Mock(returncode=0, stdout="", stderr="")

        source_probe = {
            "duration_ms": 5000,
            "video": {
                "codec": "h264", "width": 720, "height": 1280,
                "fps": 25, "pix_fmt": "yuv420p", "sar": "1:1", "rotation": 0,
            },
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
        }
        output_probe = {
            **source_probe,
            "video": {
                **source_probe["video"], "width": 1080, "height": 1920,
            },
        }
        with mock.patch.dict(os.environ, {"CONTENT_OUT": str(root)}), \
                mock.patch.object(
                    short_drama_autodraft, "_run_preview_process",
                    side_effect=render,
                ), mock.patch.object(
                    short_drama_autodraft.media_plan, "probe_media",
                    side_effect=[source_probe, output_probe],
                ):
            result = short_drama_autodraft._render_provider_preview(
                "project", "job", {
                    "shots": [{"file": "provider/shot.mp4"}],
                    "ratio": "9:16", "duration_ms": 5000,
                    "media_contract": {
                        "media_mode": "provider_audio",
                        "audio_tracks": [], "subtitles": [],
                    },
                },
            )
        command = commands[0]
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1080:1920", filters)
        self.assertIn("force_original_aspect_ratio=increase:flags=lanczos", filters)
        self.assertIn("crop=1080:1920", filters)
        self.assertEqual("medium", command[command.index("-preset") + 1])
        self.assertEqual("18", command[command.index("-crf") + 1])
        self.assertNotIn("-an", command)
        self.assertIn("[outa]", command)
        self.assertIn("[0:a:0]aresample=48000", filters)
        self.assertNotIn("anullsrc", filters)
        self.assertEqual(
            "/api/gen/file/short_drama_autodraft/project/job/preview-1080p.mp4",
            result["url"],
        )

    def test_provider_assembly_manifest_records_1080p_preview(self):
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            assets = [
                {
                    "id": "version-" + shot["shot_key"],
                    "shot_key": shot["shot_key"],
                    "version": 1,
                    "provider": "minimax_h3",
                    "file": "video/%s.mp4" % shot["shot_key"],
                    "url": "/api/gen/file/video/%s.mp4" % shot["shot_key"],
                    "input_hash": "hash-" + shot["shot_key"],
                    "provider_job_id": "provider-job-" + shot["shot_key"],
                    "native_media": {
                        "raw": {
                            "file": "video/%s-raw.mp4" % shot["shot_key"],
                            "sha256": "a" * 64, "size_bytes": 101,
                        },
                        "derived": {
                            "file": "video/%s.mp4" % shot["shot_key"],
                            "sha256": "b" * 64, "size_bytes": 99,
                            "derived_from_sha256": "a" * 64,
                        },
                        "resolution": {"width": 2560, "height": 1440},
                        "audio": {
                            "audible": True, "codec": "aac",
                            "sample_rate": 48000, "channels": 2,
                            "mean_volume_dbfs": -21.0,
                            "max_volume_dbfs": -3.0,
                        },
                        "inspected_at": 1,
                    },
                }
                for shot in plan["material_plan"]
            ]
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_autodraft_jobs "
                "(id,project_id,owner_username,actor_username,plan_id,status,phase,"
                "progress,poll_count,input_hash,request_json,cost,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'running','assembling',95,3,?,?,0,?,?)",
                (
                    "provider-assembly-1080p", self.project["id"], "alice",
                    "alice", self.plan_id, "assembly-input-hash",
                    json.dumps({
                        "production_mode": "provider_assembly",
                        "provider_assets": assets,
                    }), now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM short_drama_autodraft_jobs WHERE id=?",
                ("provider-assembly-1080p",),
            ).fetchone()
            rendered = {
                "file": "short_drama_autodraft/project/job/preview-1080p.mp4",
                "url": (
                    "/api/gen/file/short_drama_autodraft/project/job/"
                    "preview-1080p.mp4"
                ),
                "probe": {
                    "duration_ms": 30000,
                    "video": {"width": 1080, "height": 1920},
                    "audio": {"codec": "aac"},
                },
                "duration_ms": 30000,
            }
            with mock.patch.object(
                short_drama_autodraft, "_render_provider_preview",
                return_value=rendered,
            ):
                short_drama_autodraft._complete(conn, row)
            manifest = json.loads(conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE job_id=?", ("provider-assembly-1080p",),
            ).fetchone()[0])
        finally:
            conn.close()

        self.assertEqual("1080p", manifest["resolution"])
        self.assertTrue(manifest["playback_file"].endswith("preview-1080p.mp4"))
        self.assertTrue(all(item["native_media"] for item in manifest["shots"]))
        self.assertEqual("a" * 64, manifest["shots"][0]["native_media"]["raw"]["sha256"])
        self.assertEqual("b" * 64, manifest["shots"][0]["file_hash"])
        self.assertTrue(manifest["shots"][0]["provider_job_id"])

    def test_preview_process_is_terminated_and_reaped_when_cancelled(self):
        process = mock.Mock()
        process.communicate.return_value = ("", "cancelled")
        cancel = mock.Mock()
        cancel.is_set.return_value = True
        with mock.patch.object(
            short_drama_autodraft.subprocess, "Popen", return_value=process,
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft._run_preview_process(
                    ["ffmpeg", "-version"], cancel_event=cancel,
                )
        self.assertEqual("preview_render_cancelled", raised.exception.code)
        process.terminate.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_preview_process_kills_and_reaps_when_terminate_does_not_exit(self):
        process = mock.Mock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("ffmpeg", 5), ("", "killed"),
        ]
        short_drama_autodraft._stop_preview_process(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            [mock.call(timeout=5), mock.call()],
            process.communicate.call_args_list,
        )

    def test_real_ffmpeg_preview_preserves_ratio_audio_duration_and_subtitles(self):
        ffmpeg = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
        ffprobe = shutil.which(os.environ.get("FFPROBE_BIN", "ffprobe"))
        if not ffmpeg or not ffprobe:
            if os.environ.get("CI"):
                self.fail("CI must install FFmpeg and FFprobe for media contract tests")
            self.skipTest("real FFmpeg and FFprobe are not installed")
        root = Path(self.tmp.name) / "real-media"
        source_dir = root / "provider"
        source_dir.mkdir(parents=True)
        for ratio, source_size, expected in (
            ("16:9", "320x180", (1920, 1080)),
            ("9:16", "180x320", (1080, 1920)),
        ):
            source = source_dir / ("shot-" + ratio.replace(":", "-") + ".mp4")
            generated = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "testsrc2=size=%s:rate=25:duration=1" % source_size,
                "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, generated.returncode, generated.stderr)
            with mock.patch.dict(os.environ, {
                "CONTENT_OUT": str(root), "FFMPEG_BIN": ffmpeg,
                "FFPROBE_BIN": ffprobe,
            }):
                result = short_drama_autodraft._render_provider_preview(
                    "project-" + ratio.replace(":", "-"),
                    "job-" + ratio.replace(":", "-"),
                    {
                        "shots": [{"file": source.relative_to(root).as_posix()}],
                        "ratio": ratio, "duration_ms": 1000,
                        "media_contract": {
                            "audio_tracks": [],
                            "subtitles": [{
                                "start_ms": 0, "end_ms": 900,
                                "text": "real subtitle",
                            }],
                        },
                    },
                )
            probe = result["probe"]
            self.assertEqual(expected, (
                int(probe["video"]["width"]), int(probe["video"]["height"]),
            ))
            self.assertIsNotNone(probe["audio"])
            self.assertLessEqual(abs(int(probe["duration_ms"]) - 1000), 200)
            output = root / result["file"]
            subtitle = subprocess.run([
                ffprobe, "-v", "error", "-select_streams", "s",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(output),
            ], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, subtitle.returncode, subtitle.stderr)
            self.assertTrue(subtitle.stdout.strip())

    def test_unconfirmed_project_cannot_start(self):
        other = short_drama.create_project(
            self.db, "alice",
            {
                "title": "未准备项目", "synopsis": "一个尚未完成准备的短剧项目。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "写实", "target_platform": "抖音",
                "point_budget": 0,
            },
        )
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.start_job(
                self.db, "alice", "alice",
                {"project_id": other["id"], "plan_id": ""},
                "unconfirmed-key",
            )
        self.assertEqual("confirmed_plan_required", raised.exception.code)

    def test_cancel_then_retry_preserves_same_task_identity(self):
        job = self._start()
        canceled = short_drama_autodraft.cancel_job(
            self.db, "alice",
            {"project_id": self.project["id"], "job_id": job["id"]},
        )
        self.assertEqual("canceled", canceled["status"])
        retried = short_drama_autodraft.retry_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "job_id": job["id"]},
        )
        self.assertEqual(job["id"], retried["id"])
        self.assertEqual("queued", retried["status"])

    def test_lost_charge_response_is_recovered_from_ledger(self):
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "0"}
        ):
            conn = self.db()
            try:
                raw = conn.execute(
                    "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                    (self.plan_id,),
                ).fetchone()[0]
                plan = json.loads(raw)
                plan.setdefault("estimate", {})["points"] = 7
                conn.execute(
                    "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                    (json.dumps(plan), self.plan_id),
                )
                conn.commit()
            finally:
                conn.close()

            def lost_response(*_args):
                raise TimeoutError("charge response lost")

            job = short_drama_autodraft.start_job(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "plan_id": self.plan_id},
                "recovered-charge-key",
                deduct_points=lost_response,
                charge_lookup=lambda _key: {
                    "username": "alice", "delta": -7,
                },
            )
        self.assertEqual("queued", job["status"])
        self.assertEqual(7, job["cost"])
        conn = self.db()
        try:
            usage = short_drama._project_point_usage(
                conn, self.project["id"]
            )
        finally:
            conn.close()
        self.assertEqual(7, usage["spent_points"])
        self.assertEqual(0, usage["reserved_points"])

    def test_start_rechecks_unified_project_budget_before_charging(self):
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=5 WHERE id=?",
                (self.project["id"],),
            )
            raw = conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0]
            plan = json.loads(raw)
            plan.setdefault("estimate", {})["points"] = 7
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        deduct = mock.Mock()
        with mock.patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "0"}
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_job(
                    self.db,
                    "alice",
                    "alice",
                    {"project_id": self.project["id"], "plan_id": self.plan_id},
                    "budget-blocked-key",
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("point_budget_exceeded", raised.exception.code)
        deduct.assert_not_called()

    def test_single_shot_quote_charge_submit_poll_and_archive(self):
        class FakeProvider:
            name = "heygen_cinematic"
            configured = True

            def create_job(self, request):
                self.request = request
                return {"provider_job_id": "provider-job-1"}

            def get_job(self, provider_job_id):
                return {
                    "status": "completed",
                    "result_url": "https://provider.example/result.mp4",
                }

            def fetch_result(self, provider_job_id, result_url):
                return {
                    "provider_job_id": provider_job_id,
                    "file": "video/provider-job-1.mp4",
                    "url": "/api/files/video/provider-job-1.mp4",
                }

        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
                "HQ_SHORT_DRAMA_PROVIDER_SHOT_POINTS_PER_SECOND": "10",
            },
        ):
            quote = self._provider_quote()
            charged = []
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]},
                "provider-shot-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda user, cost, reason, key: charged.append(
                    (user, cost, reason, key)
                ),
                project_usage=short_drama._project_point_usage,
            )
            provider = FakeProvider()
            with mock.patch(
                "content_domains.short_drama_autodraft.load_by_name",
                return_value=provider,
            ):
                completed = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual(quote["cost"], charged[0][1])
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual("/api/files/video/provider-job-1.mp4", completed["result"]["url"])
        self.assertEqual("heygen-avatar-1", provider.request["provider_avatar_id"])
        conn = self.db()
        try:
            version = conn.execute(
                "SELECT status,url FROM short_drama_provider_shot_versions "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()
            attempt = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(("ready", "/api/files/video/provider-job-1.mp4"), version)
        self.assertEqual("done", attempt[0])

    def test_running_job_polls_bound_key_when_provider_has_no_active_candidate(self):
        class RecoveringProvider:
            name = "heygen_cinematic"

            def __init__(self):
                self.active = True
                self.polls = 0

            @property
            def configured(self):
                return self.active

            def create_job(self, request):
                return {"provider_job_id": "bound-retired-key-job"}

            def get_job(self, provider_job_id):
                self.polls += 1
                if self.polls == 1:
                    return {"status": "pending"}
                return {
                    "status": "completed",
                    "result_url": "https://provider.example/retired-result.mp4",
                }

            def fetch_result(self, provider_job_id, result_url):
                return {
                    "provider_job_id": provider_job_id,
                    "file": "video/retired-result.mp4",
                    "url": "/api/files/video/retired-result.mp4",
                }

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
            "HEYGEN_API_KEY": "configured-for-test",
        }):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice", {"quote_token": quote["quote_token"]},
                "retired-key-recovery", deduct_points=mock.Mock(),
                avatar_lookup=lambda *_args: self._provider_avatar(),
                project_usage=short_drama._project_point_usage,
            )
        provider = RecoveringProvider()
        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ):
            running = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"]
            )
            provider.active = False
            completed = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("running", running["status"])
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual(2, provider.polls)

    def test_missing_bound_key_enters_observable_recovery_state(self):
        class MissingKeyProvider:
            name = "heygen_cinematic"
            configured = True

            def create_job(self, request):
                return {"provider_job_id": "missing-bound-key-job"}

            def get_job(self, provider_job_id):
                raise VisualProviderError(
                    "provider_key_unavailable",
                    "任务绑定的密钥快照不可用",
                    submitted=True,
                )

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
            "HEYGEN_API_KEY": "configured-for-test",
        }):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice", {"quote_token": quote["quote_token"]},
                "missing-key-recovery", deduct_points=mock.Mock(),
                avatar_lookup=lambda *_args: self._provider_avatar(),
                project_usage=short_drama._project_point_usage,
            )
        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=MissingKeyProvider(),
        ):
            result = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("submit_unknown", result["status"])
        self.assertEqual("provider_key_unavailable", result["error"]["code"])
        self.assertFalse(result["error"]["retryable"])
        self.assertTrue(result["error"]["requires_reconciliation"])

    def test_non_cancelable_shot_deadline_stays_reconcilable_without_refund(self):
        class PendingProvider:
            def __init__(self):
                self.polls = 0

            capability = type("Capability", (), {"supports_cancel": False})()

            def get_job(self, _provider_job_id):
                self.polls += 1
                return {"status": "pending"}

        job, quote = self._running_provider_job("pending-timeout")
        provider = PendingProvider()
        refunds = []
        refund = lambda user, cost, reason, key: refunds.append(
            (user, cost, reason, key)
        )
        with mock.patch.object(
            short_drama_autodraft, "PROVIDER_SHOT_DEADLINE_SECONDS", 10
        ), mock.patch.object(
            short_drama_autodraft, "PROVIDER_SHOT_MAX_POLLS", 99
        ), mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ):
            with mock.patch(
                "content_domains.short_drama_autodraft.time.time",
                return_value=105,
            ):
                running = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=refund,
                )
            with mock.patch(
                "content_domains.short_drama_autodraft.time.time",
                return_value=111,
            ):
                failed = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=refund,
                )
                replay = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=refund,
                )
        self.assertEqual("running", running["status"])
        self.assertEqual("running", failed["status"])
        self.assertEqual("provider_reconciliation_pending", failed["error"]["code"])
        self.assertEqual("deadline", failed["error"]["timeout_reason"])
        self.assertEqual("running", replay["status"])
        self.assertGreaterEqual(provider.polls, 2)
        self.assertEqual([], refunds)
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?", (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("linked", state)

    def test_non_cancelable_shot_poll_failures_do_not_refund(self):
        class FailingProvider:
            def __init__(self):
                self.polls = 0

            capability = type("Capability", (), {"supports_cancel": False})()

            def get_job(self, _provider_job_id):
                self.polls += 1
                raise RuntimeError("temporary provider network failure")

        job, quote = self._running_provider_job("poll-failure-timeout")
        provider = FailingProvider()
        refunds = []
        with mock.patch.object(
            short_drama_autodraft, "PROVIDER_SHOT_DEADLINE_SECONDS", 9999
        ), mock.patch.object(
            short_drama_autodraft, "PROVIDER_SHOT_MAX_POLLS", 2
        ), mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ), mock.patch(
            "content_domains.short_drama_autodraft.time.time",
            return_value=105,
        ):
            running = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
                refund_points=lambda user, cost, reason, key: refunds.append(
                    (user, cost, reason, key)
                ),
            )
            failed = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
                refund_points=lambda user, cost, reason, key: refunds.append(
                    (user, cost, reason, key)
                ),
            )
        self.assertEqual("running", running["status"])
        self.assertEqual("running", failed["status"])
        self.assertEqual("poll_limit", failed["error"]["timeout_reason"])
        self.assertEqual(2, failed["error"]["poll_count"])
        self.assertEqual(2, provider.polls)
        self.assertEqual([], refunds)

    def test_non_cancelable_timeout_never_calls_refund_recovery(self):
        job, quote = self._running_provider_job("timeout-refund-recovery")
        calls = []

        def flaky_refund(user, cost, reason, key):
            calls.append((user, cost, reason, key))
            if len(calls) == 1:
                raise RuntimeError("points service unavailable")

        with mock.patch.object(
            short_drama_autodraft, "PROVIDER_SHOT_DEADLINE_SECONDS", 10
        ), mock.patch(
            "content_domains.short_drama_autodraft.time.time",
            return_value=111,
        ):
            failed = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
                refund_points=flaky_refund,
            )
            recovered = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
                refund_points=flaky_refund,
            )
            short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
                refund_points=flaky_refund,
            )
        self.assertEqual("running", failed["status"])
        self.assertEqual("running", recovered["status"])
        self.assertEqual([], calls)

    def test_background_sweeper_retries_refund_without_workspace_access(self):
        job, _quote = self._running_provider_job("background-refund-recovery")
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='failed' "
                "WHERE id=?", (job["id"],),
            )
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts SET "
                "state='refund_pending',refund_retry_count=1,refund_retry_at=120 "
                "WHERE job_id=?", (job["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        refunds = mock.Mock()
        recovered = short_drama_autodraft.retry_provider_refunds(
            self.db, mock.Mock(refund_points=refunds), now=120,
        )
        replay = short_drama_autodraft.retry_provider_refunds(
            self.db, mock.Mock(refund_points=refunds), now=500,
        )
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?", (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(1, recovered)
        self.assertEqual(0, replay)
        self.assertEqual("refunded", state)
        refunds.assert_called_once()

    def test_workspace_keeps_active_paid_job_after_provider_switch(self):
        job, _quote = self._running_provider_job("provider-switch-recovery")
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_DEMO_FALLBACK": "0",
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "grok",
            "XAI_API_KEY": "configured-for-test",
        }), mock.patch.object(provider_keys, "has_candidate", return_value=True):
            result = short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"]
            )
        self.assertEqual(job["id"], result["provider_job"]["id"])
        self.assertEqual("heygen_cinematic", result["provider_job"]["provider"])
        self.assertEqual("grok", result["production"]["provider"]["selected"])

    def test_workspace_returns_every_active_provider_shot_job(self):
        first, _quote = self._running_provider_job("parallel-shot-one")
        conn = self.db()
        try:
            now = int(time.time()) + 1
            conn.execute(
                "INSERT INTO short_drama_provider_shot_jobs "
                "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                "character_key,avatar_id,provider,status,progress,poll_count,"
                "input_hash,request_json,cost,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'queued',5,0,?,?,0,?,?)",
                (
                    "parallel-shot-two", self.project["id"], "alice", "alice",
                    self.plan_id, "shot_02", "character_1", "avatar-1",
                    "heygen_cinematic", "parallel-hash-two", "{}", now, now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"],
        )

        self.assertEqual(
            {first["id"], "parallel-shot-two"},
            {item["id"] for item in result["provider_jobs"]},
        )

    def test_workspace_restores_latest_terminal_job_for_each_shot(self):
        first, _quote = self._running_provider_job("terminal-shot-one")
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs "
                "SET status='failed',created_at=100,updated_at=100 WHERE id=?",
                (first["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_provider_shot_jobs "
                "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                "character_key,avatar_id,provider,status,progress,poll_count,"
                "input_hash,request_json,error_json,cost,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'failed',0,0,?,?,?,0,101,101)",
                (
                    "terminal-shot-two", self.project["id"], "alice", "alice",
                    self.plan_id, "shot_02", "character_1", "avatar-1",
                    "heygen_cinematic", "terminal-hash-two", "{}",
                    json.dumps({"code": "provider_failed"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"],
        )

        self.assertEqual(
            {first["id"], "terminal-shot-two"},
            {item["id"] for item in result["provider_jobs"]},
        )

    def test_provider_failure_reason_and_refund_recover_on_workspace_refresh(self):
        class RejectedProvider:
            def get_job(self, _provider_job_id):
                return {
                    "status": "failed",
                    "failure": {
                        "code": "content_risk",
                        "message": "reference image did not pass review",
                    },
                }

        job, quote = self._running_provider_job("provider-rejection-recovery")
        calls = []

        def recovering_refund(user, cost, reason, key):
            calls.append((user, cost, reason, key))
            if len(calls) < 3:
                raise RuntimeError("points service temporarily unavailable")

        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=RejectedProvider(),
        ), mock.patch(
            "content_domains.short_drama_autodraft.time.time",
            return_value=105,
        ):
            failed = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
                refund_points=recovering_refund,
            )
        self.assertEqual("failed", failed["status"])
        self.assertIn("provider_code", failed["error"], failed)
        self.assertEqual("content_risk", failed["error"]["provider_code"])
        self.assertIn(
            "reference image did not pass review", failed["error"]["detail"]
        )
        self.assertTrue(failed["error"]["retryable"])
        self.assertTrue(failed["billing_recovery"]["refund_pending"])

        short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"],
            avatar_list=lambda *_args: [], refund_points=recovering_refund,
        )
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?", (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)
        self.assertEqual(quote["cost"], calls[-1][1])
        self.assertEqual(calls[0][3], calls[-1][3])

    def test_grok_vault_failure_blocks_before_charge_and_submission(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "grok",
                "XAI_API_KEY": "late-environment-key",
                "HQ_SHORT_DRAMA_PROVIDER_SHOT_POINTS_PER_SECOND": "10",
            },
        ), mock.patch.object(provider_keys, "has_candidate", return_value=True):
            quote = self._provider_quote()
            deduct = mock.Mock()
            with mock.patch.object(
                provider_keys,
                "claim_candidate",
                side_effect=provider_keys.KeyStoreUnavailable(
                    "视频密钥保险箱未配置，已停止新付费任务"
                ),
            ), mock.patch("content_domains.video_xai._create") as create:
                with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                    short_drama_autodraft.start_provider_job(
                        self.db,
                        "alice",
                        "alice",
                        {"quote_token": quote["quote_token"]},
                        "grok-vault-blocked-before-charge",
                        avatar_lookup=lambda *_args: self._provider_avatar(),
                        deduct_points=deduct,
                    )
        self.assertEqual("provider_not_configured", raised.exception.code)
        deduct.assert_not_called()
        create.assert_not_called()

    def test_single_shot_idempotency_replay_does_not_charge_twice(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            deduct = mock.Mock()
            first = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, "same-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
            replay = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, "same-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, deduct.call_count)

    def test_single_shot_start_rejects_quote_after_avatar_binding_changes(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            changed_avatar = dict(self._provider_avatar())
            changed_avatar["provider_avatar_id"] = "heygen-avatar-replaced"
            deduct = mock.Mock()
            with self.assertRaises(
                short_drama_autodraft.AutodraftError
            ) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "stale-avatar-provider-key",
                    avatar_lookup=lambda *_args: changed_avatar,
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("provider_quote_stale", raised.exception.code)
        deduct.assert_not_called()

    def test_single_shot_billing_recovery_requires_two_empty_ledger_checks(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]},
                "billing-recovery-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda *_args: None,
                project_usage=short_drama._project_point_usage,
            )
            conn = self.db()
            try:
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs "
                    "SET status='billing',created_at=100,error_json=NULL "
                    "WHERE id=?",
                    (job["id"],),
                )
                conn.execute(
                    "UPDATE short_drama_provider_shot_attempts "
                    "SET state='accepted' WHERE job_id=?",
                    (job["id"],),
                )
                conn.commit()
            finally:
                conn.close()
            with mock.patch(
                "content_domains.short_drama_autodraft.time.time",
                return_value=1000,
            ):
                first = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    charge_lookup=lambda _key: None,
                )
            with mock.patch(
                "content_domains.short_drama_autodraft.time.time",
                return_value=1061,
            ):
                second = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    charge_lookup=lambda _key: None,
                )
        self.assertEqual("billing", first["status"])
        self.assertEqual("failed", second["status"])
        self.assertEqual("billing_not_committed", second["error"]["code"])

    def test_single_shot_provider_rejection_refunds_once(self):
        class RejectingProvider:
            name = "heygen_cinematic"
            configured = True

            def create_job(self, request):
                raise RuntimeError("provider rejected request")

        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic",
                "HEYGEN_API_KEY": "configured-for-test",
            },
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]}, "refund-provider-key",
                avatar_lookup=lambda *_args: self._provider_avatar(),
                deduct_points=lambda *_args: None,
                project_usage=short_drama._project_point_usage,
            )
            refunds = []
            with mock.patch(
                "content_domains.short_drama_autodraft.load_by_name",
                return_value=RejectingProvider(),
            ):
                failed = short_drama_autodraft.reconcile_provider_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=lambda user, cost, reason, key: refunds.append(
                        (user, cost, reason, key)
                    ),
                )
        self.assertEqual("failed", failed["status"])
        self.assertEqual(1, len(refunds))
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)

    def test_provider_finalization_lease_prevents_duplicate_download(self):
        job, _quote = self._running_provider_job("finalization-lease")
        started = threading.Event()
        release = threading.Event()

        class CompletedProvider:
            def __init__(self):
                self.fetches = 0

            def fetch_result(self, _job_id, _result_url):
                self.fetches += 1
                started.set()
                release.wait(5)
                return {"file": "provider/final.mp4", "url": "/api/gen/file/provider/final.mp4"}

        provider = CompletedProvider()
        conn = self.db()
        try:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?", (job["id"],)
            ).fetchone())
        finally:
            conn.close()
        with mock.patch.object(short_drama_autodraft, "_extract_tail_reference", return_value=None):
            first = threading.Thread(target=short_drama_autodraft._finish_provider_job,
                args=(self.db, row, provider, {"result_url": "https://example.test/final"}))
            first.start()
            self.assertTrue(started.wait(5))
            short_drama_autodraft._finish_provider_job(
                self.db, row, provider, {"result_url": "https://example.test/final"}
            )
            release.set()
            first.join(5)
        self.assertEqual(1, provider.fetches)

    def test_admin_can_bind_submit_unknown_and_editor_cannot(self):
        job, _quote = self._running_provider_job("submit-unknown-reconcile")
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='submit_unknown',"
                "provider_job_id=NULL WHERE id=?", (job["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        body = {
            "project_id": self.project["id"], "action": "bind_provider_job",
            "provider_job_id": "upstream-job-1",
        }
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.reconcile_unknown_provider_submission(
                self.db, "alice", "editor", "editor", job["id"], body,
            )
        self.assertEqual("provider_reconciliation_forbidden", raised.exception.code)
        result = short_drama_autodraft.reconcile_unknown_provider_submission(
            self.db, "alice", "admin", "admin", job["id"], body,
        )
        self.assertEqual("running", result["status"])
        self.assertEqual("upstream-job-1", result["provider_job_id"])

    def test_submit_unknown_binding_is_normalized_before_the_next_poll(self):
        job, _quote = self._running_provider_job("submit-unknown-normalized")
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='submit_unknown',"
                "provider_job_id=NULL WHERE id=?", (job["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        class Provider:
            def __init__(self):
                self.polled = None

            def bind_reconciled_job_id(self, provider_job_id, request):
                self.assert_request = request
                return "bound:" + provider_job_id

            def get_job(self, provider_job_id):
                self.polled = provider_job_id
                return {"status": "running"}

        provider = Provider()
        with mock.patch.object(
            short_drama_autodraft, "load_by_name", return_value=provider,
        ):
            bound = short_drama_autodraft.reconcile_unknown_provider_submission(
                self.db, "alice", "admin", "admin", job["id"], {
                    "project_id": self.project["id"],
                    "action": "bind_provider_job",
                    "provider_job_id": "raw-upstream-task",
                },
            )
            short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"],
            )
        self.assertEqual("bound:raw-upstream-task", bound["provider_job_id"])
        self.assertEqual("bound:raw-upstream-task", provider.polled)

    def test_reconcile_route_dispatches_to_admin_recovery(self):
        job, _quote = self._running_provider_job("submit-unknown-http")
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='submit_unknown',"
                "provider_job_id=NULL WHERE id=?", (job["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        handler = Handler(
            "/api/gen/short-drama/autodraft/provider-jobs/%s/reconcile" % job["id"],
            body={
                "project_id": self.project["id"], "action": "bind_provider_job",
                "provider_job_id": "upstream-http-1",
            },
        )
        verify = lambda _token: {
            "username": "alice", "role": "admin", "must_change": False,
        }
        self.assertTrue(short_drama.dispatch_http(handler, "POST", self.db, verify))
        self.assertEqual(200, handler.response[0])
        self.assertEqual("running", handler.response[1]["status"])


    def _init_shared_jobs_table(self):
        conn = self.db()
        try:
            conn.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT,
                error TEXT, created_at INTEGER, updated_at INTEGER,
                deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT
            )""")
            conn.commit()
        finally:
            conn.close()

    def _numeric_legacy_minimax_job(self, key, numeric_id="70001"):
        job, quote = self._running_provider_job(key)
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET id=?,provider=? "
                "WHERE id=?",
                (numeric_id, "minimax_h3", job["id"]),
            )
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts SET job_id=? "
                "WHERE job_id=?",
                (numeric_id, job["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        job["id"] = numeric_id
        job["provider"] = "minimax_h3"
        return job, quote

    def test_minimax_quote_creates_one_shared_xiaole_paid_job(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()

        charged = []
        refunded = []
        enqueued = []

        def deduct(username, cost, reason, transaction_key=""):
            charged.append((username, cost, reason, transaction_key))
            return 100 - cost

        def refund(username, cost, reason, transaction_key=""):
            refunded.append((username, cost, reason, transaction_key))
            return 100

        def enqueue(job_id, kind=None, mode=None):
            enqueued.append((job_id, kind, mode))
            return True

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-shared-job-1",
                deduct_points=deduct,
                refund_points=refund,
                enqueue_job=enqueue,
                project_usage=short_drama._project_point_usage,
            )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            shared = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (int(job["id"]),)
            ).fetchone()
            projected = conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs WHERE id=?",
                (str(job["id"]),),
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(shared)
        self.assertEqual("xiaole_video", shared["kind"])
        self.assertEqual("alice", shared["username"])
        self.assertEqual(quote["cost"], shared["cost"])
        self.assertEqual("pending", shared["status"])
        payload = json.loads(shared["payload"])
        self.assertEqual("minimax", payload["channel"])
        self.assertEqual("MiniMax-H3", payload["model"])
        self.assertEqual("2k", payload["resolution"])
        self.assertEqual(str(shared["id"]), projected["id"])
        self.assertEqual("minimax_h3", projected["provider"])
        self.assertEqual("queued", projected["status"])
        self.assertIsNone(projected["provider_job_id"])
        self.assertEqual(1, len(charged))
        self.assertEqual([], refunded)
        self.assertEqual([(shared["id"], "xiaole_video", None)], enqueued)

    def test_minimax_shared_job_respects_existing_xiaole_active_limit(self):
        from content_domains import core

        self._lock_project_character_references()
        self._init_shared_jobs_table()
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                "VALUES ('xiaole_video','alice',1,'pending','{}',1,1,'content')"
            )
            conn.commit()
        finally:
            conn.close()
        charged = []
        refunded = []
        enqueued = []

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ), mock.patch.object(
            core, "MAX_USER_ACTIVE_XIAOLE_VIDEO", 1,
        ), mock.patch.object(
            core, "MAX_USER_ACTIVE_JOBS", 10,
        ):
            quote = self._provider_quote()
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-active-cap-1",
                    deduct_points=lambda *args: charged.append(args) or 99,
                    refund_points=lambda *args: refunded.append(args) or 100,
                    enqueue_job=lambda *args: enqueued.append(args) or True,
                    project_usage=short_drama._project_point_usage,
                    shared_video_submission_limit=(
                        core._short_drama_xiaole_submission_limit
                    ),
                )

        self.assertEqual("xiaole_active_cap", raised.exception.code)
        self.assertEqual([], charged)
        self.assertEqual([], refunded)
        self.assertEqual([], enqueued)

    def test_minimax_shared_job_rechecks_active_limit_after_charge(self):
        from content_domains import core

        self._lock_project_character_references()
        self._init_shared_jobs_table()
        charged = []
        refunded = []
        enqueued = []

        def deduct(username, cost, reason, transaction_key=""):
            charged.append((username, cost, reason, transaction_key))
            conn = self.db()
            try:
                conn.execute(
                    "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                    "VALUES ('xiaole_video',?,1,'pending','{}',1,1,'content')",
                    (username,),
                )
                conn.commit()
            finally:
                conn.close()
            return 100 - cost

        def refund(username, cost, reason, transaction_key=""):
            refunded.append((username, cost, reason, transaction_key))
            return 100

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ), mock.patch.object(
            core, "MAX_USER_ACTIVE_XIAOLE_VIDEO", 1,
        ), mock.patch.object(
            core, "MAX_USER_ACTIVE_JOBS", 10,
        ):
            quote = self._provider_quote()
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-active-cap-race-1",
                    deduct_points=deduct,
                    refund_points=refund,
                    enqueue_job=lambda *args: enqueued.append(args) or True,
                    project_usage=short_drama._project_point_usage,
                    shared_video_submission_limit=(
                        core._short_drama_xiaole_submission_limit
                    ),
                )

        conn = self.db()
        try:
            active_count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE username='alice' "
                "AND status IN ('pending','running')"
            ).fetchone()[0]
            projected_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_jobs"
            ).fetchone()[0]
            consumed = conn.execute(
                "SELECT consumed_job_id FROM short_drama_provider_shot_quotes "
                "WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual("xiaole_active_cap", raised.exception.code)
        self.assertEqual(1, active_count)
        self.assertEqual(0, projected_count)
        self.assertIsNone(consumed)
        self.assertEqual(1, len(charged))
        self.assertEqual(1, len(refunded))
        self.assertEqual([], enqueued)

    def test_minimax_shared_job_respects_total_active_job_limit(self):
        from content_domains import core

        self._lock_project_character_references()
        self._init_shared_jobs_table()
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                "VALUES ('image','alice',1,'running','{}',1,1,'content')"
            )
            conn.commit()
        finally:
            conn.close()
        charged = []

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ), mock.patch.object(
            core, "MAX_USER_ACTIVE_XIAOLE_VIDEO", 10,
        ), mock.patch.object(
            core, "MAX_USER_ACTIVE_JOBS", 1,
        ):
            quote = self._provider_quote()
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-total-active-cap-1",
                    deduct_points=lambda *args: charged.append(args) or 99,
                    refund_points=lambda *_args: 100,
                    enqueue_job=lambda *_args: True,
                    project_usage=short_drama._project_point_usage,
                    shared_video_submission_limit=(
                        core._short_drama_xiaole_submission_limit
                    ),
                )

        self.assertEqual("active_job_cap", raised.exception.code)
        self.assertEqual([], charged)

    def test_minimax_shared_job_resolves_local_reference_only_in_worker_memory(self):
        import io
        from PIL import Image

        self._lock_project_character_references()
        self._init_shared_jobs_table()
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_characters SET reference_url="
                "'/api/gen/file/short_drama_refs/character.png' "
                "WHERE project_id=?",
                (self.project["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        image_path = mock.Mock()
        image_path.is_file.return_value = True
        png = io.BytesIO()
        Image.new("RGB", (256, 256), (40, 80, 120)).save(png, "PNG")
        image_path.stat.return_value.st_size = len(png.getvalue())
        image_path.read_bytes.return_value = png.getvalue()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ), mock.patch(
            "content_domains.core._out_path", return_value=image_path,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-local-reference-1",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda _job_id, _kind, _mode: True,
                project_usage=short_drama._project_point_usage,
            )

        conn = self.db()
        try:
            payload = json.loads(conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (int(job["id"]),)
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual([], payload["reference_images"])
        self.assertNotIn("data:image", json.dumps(payload))
        with mock.patch(
            "content_domains.core._out_path", return_value=image_path,
        ):
            resolved = short_drama_autodraft.resolve_shared_xiaole_payload(
                self.db, int(job["id"]), "alice", payload,
            )
        self.assertTrue(all(
            value.startswith("data:image/png;base64,")
            for value in resolved["reference_images"]
        ))

        conn = self.db()
        try:
            persisted = conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (int(job["id"]),)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn("data:image", persisted)

    def test_minimax_worker_revalidates_and_resolves_controlled_scene_reference(self):
        shot = self._set_controlled_scene_reference()
        self._init_shared_jobs_table()
        avatar = self._provider_avatar()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = short_drama_autodraft.create_provider_quote(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "avatar_id": avatar["id"],
                },
                avatar_lookup=lambda _username, _avatar_id: avatar,
            )
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-worker-controlled-scene-1",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda _job_id, _kind, _mode: True,
                project_usage=short_drama._project_point_usage,
            )

        conn = self.db()
        try:
            request = json.loads(conn.execute(
                "SELECT request_json FROM short_drama_provider_shot_jobs WHERE id=?",
                (job["id"],),
            ).fetchone()[0])
            payload = json.loads(conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (int(job["id"]),)
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(1, len([
            item for item in request["reference_images"]
            if item.get("character_key") == "__scene_reference__"
        ]))
        resolved = short_drama_autodraft.resolve_shared_xiaole_payload(
            self.db, int(job["id"]), "alice", payload,
        )
        self.assertTrue(resolved["reference_images"])
        self.assertTrue(all(
            value.startswith("data:image/png;base64,")
            for value in resolved["reference_images"]
        ))

        for mutation in ("delete", "replace"):
            tampered = json.loads(json.dumps(request))
            if mutation == "delete":
                tampered["reference_images"] = [
                    item for item in tampered["reference_images"]
                    if item.get("character_key") != "__scene_reference__"
                ]
            else:
                scene_item = next(
                    item for item in tampered["reference_images"]
                    if item.get("character_key") == "__scene_reference__"
                )
                scene_item["scene_key"] = "scene-group:same-project-substitution"
            conn = self.db()
            try:
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET request_json=? "
                    "WHERE id=?",
                    (json.dumps(tampered), job["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            provider = mock.Mock()
            with mock.patch(
                "content_domains.short_drama_autodraft.load_by_name",
                return_value=provider,
            ):
                with self.assertRaises(
                    short_drama_autodraft.AutodraftError
                ) as raised:
                    short_drama_autodraft.resolve_shared_xiaole_payload(
                        self.db, int(job["id"]), "alice", payload,
                    )
            self.assertEqual(422, raised.exception.status)
            self.assertEqual(
                "provider_scene_reference_required", raised.exception.code,
            )
            provider.resolve_reference_values.assert_not_called()

        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET request_json=? WHERE id=?",
                (json.dumps(request), job["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        asset_graph = short_drama_autodraft.short_drama_asset_graph
        scenes = asset_graph.scene_workspace(
            self.db, "alice", self.project["id"],
        )
        asset_graph.bind_scene_to_shot(self.db, "alice", "alice", {
            "project_id": self.project["id"],
            "graph_revision": scenes["graph_revision"],
            "shot_key": shot["shot_key"],
            "scene_key": "",
        })
        provider = mock.Mock()
        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.resolve_shared_xiaole_payload(
                    self.db, int(job["id"]), "alice", payload,
                )
        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)
        provider.resolve_reference_values.assert_not_called()

    def test_minimax_worker_rejects_forged_scene_reference_before_file_resolution(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-worker-forged-scene-1",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda _job_id, _kind, _mode: True,
                project_usage=short_drama._project_point_usage,
            )

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT request_json FROM short_drama_provider_shot_jobs WHERE id=?",
                (job["id"],),
            ).fetchone()
            request = json.loads(row[0])
            request["reference_images"].append({
                "character_key": "__scene_reference__",
                "scene_key": "scene-group:forged",
                "scene_version_id": "forged-version",
                "scene_reference_identity": "forged-identity",
                "file": "other_user/private_scene.png",
                "url": "/api/gen/file/other_user/private_scene.png",
            })
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET request_json=? WHERE id=?",
                (json.dumps(request), job["id"]),
            )
            payload = json.loads(conn.execute(
                "SELECT payload FROM jobs WHERE id=?", (int(job["id"]),)
            ).fetchone()[0])
            conn.commit()
        finally:
            conn.close()

        provider = mock.Mock()
        provider.resolve_reference_values.return_value = [
            "data:image/png;base64,Zm9yZ2Vk"
        ]
        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.resolve_shared_xiaole_payload(
                    self.db, int(job["id"]), "alice", payload,
                )

        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)
        provider.resolve_reference_values.assert_not_called()

    def test_minimax_existing_short_drama_http_route_enqueues_shared_job(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        enqueued = []
        verify = lambda token: {
            "username": token,
            "must_change": False,
        } if token else None

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            handler = Handler(
                "/api/gen/short-drama/autodraft/provider-jobs",
                body={
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                },
                key="minimax-existing-http-route-1",
            )
            handled = short_drama.dispatch_http(
                handler,
                "POST",
                self.db,
                verify,
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda *args: enqueued.append(args) or True,
            )

        self.assertTrue(handled)
        self.assertEqual(200, handler.response[0], handler.response[1])
        self.assertEqual(
            [(int(handler.response[1]["id"]), "xiaole_video", None)],
            enqueued,
        )

    def test_minimax_shared_job_success_projects_version_without_second_provider_call(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()

        refunded = []

        def deduct(_username, cost, _reason, transaction_key=""):
            self.assertTrue(transaction_key)
            return 100 - cost

        def refund(username, cost, reason, transaction_key=""):
            refunded.append((username, cost, reason, transaction_key))
            return 100

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-shared-success-1",
                deduct_points=deduct,
                refund_points=refund,
                enqueue_job=lambda _job_id, _kind, _mode: True,
                project_usage=short_drama._project_point_usage,
            )

        shared_result = {
            "type": "video",
            "status": "done",
            "mode": "minimax",
            "model": "MiniMax-H3",
            "provider": "minimax_h3_cn",
            "provider_video_id": "h3-upstream-task-1",
            "video_file": "video/minimax_h3_result.mp4",
            "raw_video_file": "video/minimax_h3_raw_result.mp4",
            "video_url": "/api/gen/file/video/minimax_h3_result.mp4",
            "generate_audio": True,
            "native_audio": {
                "audible": True, "codec": "aac", "sample_rate": 48000,
                "channels": 2, "mean_volume_dbfs": -24.3,
                "max_volume_dbfs": -3.1,
            },
            "native_resolution": {"width": 2560, "height": 1440},
            "native_media": self._native_media_evidence(),
            "phase": "done",
        }
        conn = self.db()
        try:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (json.dumps(shared_result, ensure_ascii=False), int(job["id"])),
            )
            conn.commit()
        finally:
            conn.close()

        forbidden_provider = mock.Mock(configured=True)
        forbidden_provider.create_job.side_effect = AssertionError(
            "shared MiniMax completion must not submit a second provider job"
        )
        completed = short_drama_autodraft.reconcile_shared_xiaole_job(
            self.db, int(job["id"])
        )
        with mock.patch.object(
            short_drama_autodraft, "load_by_name", return_value=forbidden_provider,
        ):
            replay = short_drama_autodraft.reconcile_provider_job(
                self.db,
                "alice",
                self.project["id"],
                str(job["id"]),
                refund_points=refund,
            )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            versions = conn.execute(
                "SELECT * FROM short_drama_provider_shot_versions WHERE job_id=?",
                (str(job["id"]),),
            ).fetchall()
            attempt = conn.execute(
                "SELECT * FROM short_drama_provider_shot_attempts WHERE job_id=?",
                (str(job["id"]),),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual("succeeded", completed["status"])
        self.assertEqual("succeeded", replay["status"])
        self.assertEqual("done", completed["billing_recovery"]["state"])
        self.assertEqual("done", replay["billing_recovery"]["state"])
        self.assertEqual("h3-upstream-task-1", completed["provider_job_id"])
        self.assertEqual(1, len(versions))
        self.assertEqual(shared_result["video_file"], versions[0]["file"])
        self.assertEqual(shared_result["video_url"], versions[0]["url"])
        projected_version = next(
            item for item in short_drama_autodraft.workspace(
                self.db, "alice", "alice", self.project["id"]
            )["provider_versions"]
            if item["id"] == versions[0]["id"]
        )
        self.assertEqual(
            shared_result["native_audio"], projected_version["native_audio"]
        )
        self.assertEqual(
            "a" * 64, projected_version["native_media"]["raw"]["sha256"]
        )
        self.assertEqual(
            projected_version["native_media"]["raw"]["sha256"],
            projected_version["native_media"]["derived"]["derived_from_sha256"],
        )
        self.assertEqual("done", attempt["state"])
        self.assertEqual([], refunded)
        forbidden_provider.create_job.assert_not_called()

    def test_minimax_queue_rejection_uses_shared_refund_owner(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        charged = []
        refunded = []

        def deduct(username, cost, reason, transaction_key=""):
            charged.append((username, cost, reason, transaction_key))
            return 100 - cost

        def refund(username, cost, reason, transaction_key=""):
            refunded.append((username, cost, reason, transaction_key))
            return 100

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-queue-full-1",
                    deduct_points=deduct,
                    refund_points=refund,
                    enqueue_job=lambda _job_id, _kind, _mode: False,
                    project_usage=short_drama._project_point_usage,
                )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            shared = conn.execute("SELECT * FROM jobs").fetchone()
            projected = conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs"
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM short_drama_provider_shot_attempts"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual("provider_queue_full", raised.exception.code)
        self.assertEqual("error", shared["status"])
        self.assertEqual(1, shared["refunded"])
        self.assertEqual("failed", projected["status"])
        self.assertEqual("refunded", attempt["state"])
        self.assertEqual(1, len(charged))
        self.assertEqual(1, len(refunded))
        self.assertEqual(
            "job-refund:alice:%s" % shared["id"], refunded[0][3]
        )

    def test_minimax_failure_waits_for_shared_refund_without_double_refund(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        direct_refunds = []

        def refund(username, cost, reason, transaction_key=""):
            direct_refunds.append((username, cost, reason, transaction_key))
            return 100

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-shared-failure-1",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=refund,
                enqueue_job=lambda _job_id, _kind, _mode: True,
                project_usage=short_drama._project_point_usage,
            )

        conn = self.db()
        try:
            conn.execute(
                "UPDATE jobs SET status='error',error='provider rejected',refunded=2 "
                "WHERE id=?",
                (int(job["id"]),),
            )
            conn.commit()
        finally:
            conn.close()
        failed = short_drama_autodraft.reconcile_shared_xiaole_job(
            self.db, int(job["id"])
        )
        short_drama_autodraft.retry_provider_refunds(
            self.db,
            mock.Mock(refund_points=refund),
            10,
        )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            pending_attempt = conn.execute(
                "SELECT * FROM short_drama_provider_shot_attempts WHERE job_id=?",
                (str(job["id"]),),
            ).fetchone()
            conn.execute(
                "UPDATE jobs SET refunded=1 WHERE id=?", (int(job["id"]),)
            )
            conn.commit()
        finally:
            conn.close()
        settled = short_drama_autodraft.reconcile_shared_xiaole_job(
            self.db, int(job["id"])
        )
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            settled_attempt = conn.execute(
                "SELECT * FROM short_drama_provider_shot_attempts WHERE job_id=?",
                (str(job["id"]),),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual("failed", failed["status"])
        self.assertEqual("failed", settled["status"])
        self.assertTrue(failed["billing_recovery"]["refund_pending"])
        self.assertTrue(settled["billing_recovery"]["refunded"])
        self.assertEqual("refund_pending", pending_attempt["state"])
        self.assertEqual("refunded", settled_attempt["state"])
        self.assertEqual([], direct_refunds)

    def test_minimax_quote_change_between_validation_and_insert_fails_closed(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        charged = []
        refunded = []
        enqueued = []

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()

            def deduct(username, cost, reason, transaction_key=""):
                charged.append((username, cost, reason, transaction_key))
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_provider_shot_quotes "
                        "SET request_hash='changed-after-validation' WHERE token=?",
                        (quote["quote_token"],),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return 100 - cost

            def refund(username, cost, reason, transaction_key=""):
                refunded.append((username, cost, reason, transaction_key))
                return 100

            with self.assertRaises(
                short_drama_autodraft.AutodraftError
            ) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-quote-race-1",
                    deduct_points=deduct,
                    refund_points=refund,
                    enqueue_job=lambda *args: enqueued.append(args) or True,
                    project_usage=short_drama._project_point_usage,
                )

        conn = self.db()
        try:
            shared_count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='xiaole_video' "
                "AND status IN ('pending','running','done')"
            ).fetchone()[0]
            projected_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_jobs"
            ).fetchone()[0]
            consumed = conn.execute(
                "SELECT consumed_job_id FROM short_drama_provider_shot_quotes "
                "WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual("provider_quote_changed", raised.exception.code)
        self.assertEqual(0, shared_count)
        self.assertEqual(0, projected_count)
        self.assertIsNone(consumed)
        self.assertEqual(1, len(charged))
        self.assertEqual(1, len(refunded))
        self.assertEqual([], enqueued)

    def test_minimax_character_binding_change_during_charge_fails_closed(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        charged = []
        refunded = []
        enqueued = []

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()

            def deduct(username, cost, reason, transaction_key=""):
                charged.append((username, cost, reason, transaction_key))
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_characters "
                        "SET reference_version=reference_version+1 "
                        "WHERE project_id=? AND character_key=("
                        "SELECT character_key FROM short_drama_characters "
                        "WHERE project_id=? ORDER BY sort_order LIMIT 1)",
                        (self.project["id"], self.project["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return 100 - cost

            def refund(username, cost, reason, transaction_key=""):
                refunded.append((username, cost, reason, transaction_key))
                return 100

            with self.assertRaises(
                short_drama_autodraft.AutodraftError
            ) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-character-race-1",
                    deduct_points=deduct,
                    refund_points=refund,
                    enqueue_job=lambda *args: enqueued.append(args) or True,
                    project_usage=short_drama._project_point_usage,
                )

        conn = self.db()
        try:
            active_shared = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='xiaole_video' "
                "AND status IN ('pending','running','done')"
            ).fetchone()[0]
            projected_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_jobs"
            ).fetchone()[0]
            consumed = conn.execute(
                "SELECT consumed_job_id FROM short_drama_provider_shot_quotes "
                "WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual("provider_quote_stale", raised.exception.code)
        self.assertEqual(0, active_shared)
        self.assertEqual(0, projected_count)
        self.assertIsNone(consumed)
        self.assertEqual(1, len(charged))
        self.assertEqual(1, len(refunded))
        self.assertEqual([], enqueued)

    def test_numeric_legacy_minimax_job_uses_legacy_provider_reconciliation(self):
        class PendingLegacyProvider:
            def __init__(self):
                self.polls = []

            def get_job(self, provider_job_id):
                self.polls.append(provider_job_id)
                return {"status": "pending"}

        job, _quote = self._numeric_legacy_minimax_job(
            "numeric-legacy-minimax-query"
        )
        provider = PendingLegacyProvider()
        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ), mock.patch(
            "content_domains.short_drama_autodraft.time.time",
            return_value=105,
        ):
            result = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"]
            )

        self.assertEqual("running", result["status"])
        self.assertEqual(["provider-timeout-job"], provider.polls)

    def test_numeric_legacy_minimax_refund_pending_uses_legacy_refund_owner(self):
        job, quote = self._numeric_legacy_minimax_job(
            "numeric-legacy-minimax-refund"
        )
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET status='failed' "
                "WHERE id=?",
                (job["id"],),
            )
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts "
                "SET state='refund_pending' WHERE job_id=?",
                (job["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        refunds = []

        short_drama_autodraft.retry_provider_refunds(
            self.db,
            mock.Mock(
                refund_points=lambda user, cost, reason, key: refunds.append(
                    (user, cost, reason, key)
                )
            ),
            10,
        )

        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_provider_shot_attempts "
                "WHERE job_id=?",
                (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)
        self.assertEqual(1, len(refunds))
        self.assertEqual(quote["cost"], refunds[0][1])

    def test_numeric_legacy_minimax_ignores_unrelated_shared_job_id(self):
        self._init_shared_jobs_table()
        job, _quote = self._numeric_legacy_minimax_job(
            "numeric-legacy-minimax-shared-collision"
        )
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO jobs(id,kind,username,status,payload,refunded) "
                "VALUES (?,?,?,?,?,0)",
                (
                    int(job["id"]),
                    "xiaole_video",
                    "alice",
                    "running",
                    json.dumps({
                        "channel": "minimax",
                        "_short_drama_provider_binding": {
                            "project_id": "another-project",
                            "plan_id": self.plan_id,
                            "shot_key": "shot_01",
                            "request_hash": "unrelated-request",
                        },
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        provider = mock.Mock()
        provider.get_job.return_value = {"status": "pending"}

        with mock.patch(
            "content_domains.short_drama_autodraft.load_by_name",
            return_value=provider,
        ), mock.patch(
            "content_domains.short_drama_autodraft.time.time",
            return_value=105,
        ):
            result = short_drama_autodraft.reconcile_provider_job(
                self.db, "alice", self.project["id"], job["id"]
            )

        self.assertEqual("running", result["status"])
        provider.get_job.assert_called_once_with("provider-timeout-job")

    def test_minimax_prompt_contains_native_dialogue_and_sound_design(self):
        from PIL import Image

        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
            shot["character_keys"] = ["lead", "friend"]
            shot["dialogue"] = [
                {
                    "character_key": "lead", "text": "别回头",
                    "speech_rate": 1.0, "timing_mode": "sequential",
                },
                {
                    "character_key": "friend", "text": "快走",
                    "speech_rate": 1.0,
                    "timing_mode": "simultaneous_with_previous",
                },
            ]
            shot["sound_design"] = "远处金属碰撞声"
            shot["provider_prompt"] = "两人在废墟中快速撤离"
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            for index, (key, name) in enumerate(
                (("lead", "顾承川"), ("friend", "许安")), 1
            ):
                conn.execute(
                    "INSERT INTO short_drama_characters "
                    "(id,project_id,character_key,name,source_type,reference_file,"
                    "reference_url,reference_version,reference_locked,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(project_id,character_key) DO UPDATE SET "
                    "name=excluded.name,reference_file=excluded.reference_file,"
                    "reference_url=excluded.reference_url,reference_version=1,"
                    "reference_locked=1",
                    (
                        "native-audio-character-%d" % index,
                        self.project["id"], key, name, "ai_character",
                        "short_drama_refs/native-audio-%d.png" % index,
                        "https://cdn.example/native-audio-%d.png" % index,
                        1, index,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        for index in (1, 2):
            reference_path = (
                Path(self.tmp.name) /
                ("short_drama_refs/native-audio-%d.png" % index)
            )
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (256, 256), (30, 80, 120)).save(
                reference_path, "PNG"
            )

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            result = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                },
            )

        prompt = result["request"]["prompt"]
        self.assertIn("顾承川：别回头", prompt)
        self.assertIn("许安（与上一条同时说）：快走", prompt)
        self.assertIn("远处金属碰撞声", prompt)
        self.assertIn("原生双声道", prompt)

    def test_minimax_sound_design_changes_request_hash(self):
        self._lock_project_character_references()
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
            shot["sound_design"] = "雨声逐渐增强"
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        body = {
            "project_id": self.project["id"], "plan_id": self.plan_id,
            "shot_key": shot["shot_key"],
        }
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            first = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", body,
            )
            conn = self.db()
            try:
                plan = json.loads(conn.execute(
                    "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                    (self.plan_id,),
                ).fetchone()[0])
                plan["material_plan"][0]["sound_design"] = "风声突然停止"
                conn.execute(
                    "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                    (json.dumps(plan, ensure_ascii=False), self.plan_id),
                )
                conn.commit()
            finally:
                conn.close()
            changed = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", body,
            )

        self.assertNotEqual(first["request_hash"], changed["request_hash"])

    def test_minimax_runtime_queue_phase_is_exposed_without_fake_progress(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db,
                "alice",
                "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-runtime-queue-phase-1",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda _job_id, _kind, _mode: True,
                project_usage=short_drama._project_point_usage,
            )

        conn = self.db()
        try:
            conn.execute(
                "UPDATE jobs SET status='running' WHERE id=?", (int(job["id"]),)
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch(
            "content_domains.video.get_video_job_diagnostics",
            return_value={
                "phase": "minimax_queued",
                "provider_video_id": "minimax-upstream-queued-1",
            },
        ):
            projected = short_drama_autodraft.reconcile_shared_xiaole_job(
                self.db, int(job["id"])
            )

        self.assertEqual("queued", projected["status"])
        self.assertEqual("minimax_queued", projected["phase"])
        self.assertTrue(projected["progress_indeterminate"])
        self.assertEqual(
            "minimax-upstream-queued-1", projected["provider_job_id"]
        )

    def test_minimax_pending_asset_failure_refunds_before_enqueue(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        charged = []
        refunded = []
        enqueued = []

        def deduct(username, cost, reason, transaction_key=""):
            charged.append((username, cost, reason, transaction_key))
            return 100 - cost

        def refund(username, cost, reason, transaction_key=""):
            refunded.append((username, cost, reason, transaction_key))
            return 100

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-asset-registration-failed-1",
                    deduct_points=deduct,
                    refund_points=refund,
                    enqueue_job=lambda *args: enqueued.append(args) or True,
                    project_usage=short_drama._project_point_usage,
                    video_asset_recorder=lambda *_args: (_ for _ in ()).throw(
                        RuntimeError("asset database unavailable")
                    ),
                )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            shared = conn.execute("SELECT * FROM jobs").fetchone()
            projected = conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs"
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM short_drama_provider_shot_attempts"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(
            "video_asset_register_failed", raised.exception.code
        )
        self.assertEqual("error", shared["status"])
        self.assertEqual(1, shared["refunded"])
        self.assertEqual("failed", projected["status"])
        self.assertEqual("refunded", attempt["state"])
        self.assertEqual(1, len(charged))
        self.assertEqual(1, len(refunded))
        self.assertEqual([], enqueued)

    def test_minimax_asset_registration_failure_refunds_before_enqueue(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        charged = []
        refunded = []
        enqueued = []

        def deduct(username, cost, reason, transaction_key=""):
            charged.append((username, cost, reason, transaction_key))
            return 100 - cost

        def refund(username, cost, reason, transaction_key=""):
            refunded.append((username, cost, reason, transaction_key))
            return 100

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-asset-registration-failure-1",
                    deduct_points=deduct,
                    refund_points=refund,
                    video_asset_recorder=lambda *_args: (_ for _ in ()).throw(
                        RuntimeError("asset db unavailable")
                    ),
                    enqueue_job=lambda *args: enqueued.append(args) or True,
                    project_usage=short_drama._project_point_usage,
                )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            shared = conn.execute("SELECT * FROM jobs").fetchone()
            projected = conn.execute(
                "SELECT * FROM short_drama_provider_shot_jobs"
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM short_drama_provider_shot_attempts"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual("video_asset_register_failed", raised.exception.code)
        self.assertEqual("error", shared["status"])
        self.assertEqual(1, shared["refunded"])
        self.assertEqual("failed", projected["status"])
        self.assertEqual("refunded", attempt["state"])
        self.assertEqual(1, len(charged))
        self.assertEqual(1, len(refunded))
        self.assertEqual([], enqueued)

    def test_all_validated_minimax_versions_default_to_provider_audio(self):
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot_keys = [
                str(item["shot_key"]) for item in plan["material_plan"]
            ]
            now = int(time.time())
            audio = {
                "audible": True, "codec": "aac", "sample_rate": 48000,
                "channels": 2, "mean_volume_dbfs": -20.0,
                "max_volume_dbfs": -2.0,
            }
            for index, shot_key in enumerate(shot_keys, 1):
                job_id = "native-contract-job-%d" % index
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_jobs "
                    "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                    "character_key,avatar_id,provider,provider_job_id,status,progress,"
                    "poll_count,input_hash,request_json,result_json,cost,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,'succeeded',100,1,?,?,?,0,?,?)",
                    (
                        job_id, self.project["id"], "alice", "alice", self.plan_id,
                        shot_key, "lead", "character:lead", "minimax_h3",
                        "upstream-%d" % index, "input-%d" % index,
                        json.dumps({"duration_seconds": 5}),
                        json.dumps({
                            "native_audio": audio,
                            "native_media": self._native_media_evidence(
                                raw_file="video/native-raw-%d.mp4" % index,
                                derived_file="video/native-%d.mp4" % index,
                            ),
                        }), now, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions "
                    "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                    "status,file,url,input_hash,created_at) "
                    "VALUES (?,?,?,?,1,'minimax_h3',?,'ready',?,?,?,?)",
                    (
                        "native-contract-version-%d" % index,
                        self.project["id"], job_id, shot_key,
                        "upstream-%d" % index, "video/native-%d.mp4" % index,
                        "/api/gen/file/video/native-%d.mp4" % index,
                        "input-%d" % index, now,
                    ),
                )
            conn.commit()
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            contract = short_drama_autodraft._locked_media_contract(conn, project)
            self.assertTrue(contract["delivery_eligible"])
            self.assertEqual("provider_audio", contract["media_mode"])
            self.assertEqual(
                "validated_provider_native_audio", contract["evidence_source"]
            )
            self.assertEqual([], contract["audio_tracks"])

            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET result_json=? WHERE id=?",
                (json.dumps({"native_audio": {"audible": False}}),
                 "native-contract-job-1"),
            )
            conn.commit()
            incomplete = short_drama_autodraft._locked_media_contract(conn, project)
            self.assertFalse(incomplete["delivery_eligible"])
            self.assertEqual("provider_native_audio_incomplete", incomplete["reason"])
            self.assertEqual([shot_keys[0]], incomplete["invalid_shot_keys"])

            conn.execute(
                "UPDATE short_drama_provider_shot_jobs SET result_json=? WHERE id=?",
                (json.dumps({
                    "native_audio": audio,
                    "native_media": self._native_media_evidence(
                        raw_file="video/native-raw-1.mp4",
                        derived_file="video/native-1.mp4",
                    ),
                }), "native-contract-job-1"),
            )
            conn.execute(
                "UPDATE short_drama_provider_shot_versions SET shot_key=? WHERE id=?",
                ("stale_shot", "native-contract-version-%d" % len(shot_keys)),
            )
            conn.commit()
            stale = short_drama_autodraft._automatic_native_audio_contract(
                conn, project, len(shot_keys)
            )
            self.assertIsNone(stale)
        finally:
            conn.close()

    def test_minimax_shared_completion_without_audio_evidence_is_not_ready(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-missing-native-audio",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda *_args: True,
                project_usage=short_drama._project_point_usage,
            )
        result_without_audio = {
            "type": "video", "status": "done", "mode": "minimax",
            "provider_video_id": "h3-without-audio-evidence",
            "video_file": "video/no-audio-evidence.mp4",
            "video_url": "/api/gen/file/video/no-audio-evidence.mp4",
        }
        conn = self.db()
        try:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (json.dumps(result_without_audio), int(job["id"])),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.reconcile_shared_xiaole_job(
                self.db, int(job["id"])
            )
        self.assertEqual("shared_video_result_incomplete", raised.exception.code)


    def test_minimax_shared_completion_without_media_lineage_is_not_ready(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            job = short_drama_autodraft.start_provider_job(
                self.db, "alice", "alice",
                {"quote_token": quote["quote_token"]},
                "minimax-missing-native-audio",
                deduct_points=lambda _user, cost, _reason, _key: 100 - cost,
                refund_points=lambda _user, _cost, _reason, _key: 100,
                enqueue_job=lambda *_args: True,
                project_usage=short_drama._project_point_usage,
            )
        result_without_lineage = {
            "type": "video", "status": "done", "mode": "minimax",
            "provider_video_id": "h3-without-media-lineage",
            "video_file": "video/no-audio-evidence.mp4",
            "video_url": "/api/gen/file/video/no-audio-evidence.mp4",
            "native_audio": {
                "audible": True, "codec": "aac", "sample_rate": 48000,
                "channels": 2, "mean_volume_dbfs": -24.3,
                "max_volume_dbfs": -3.1,
            },
        }
        conn = self.db()
        try:
            conn.execute(
                "UPDATE jobs SET status='done',result=? WHERE id=?",
                (json.dumps(result_without_lineage), int(job["id"])),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft.reconcile_shared_xiaole_job(
                self.db, int(job["id"])
            )
        self.assertEqual("shared_video_result_incomplete", raised.exception.code)

    def test_minimax_native_media_sanitizer_rejects_mismatched_lineage(self):
        valid = self._native_media_evidence()
        sanitized = short_drama_autodraft._sanitized_native_media(valid)
        self.assertEqual("a" * 64, sanitized["raw"]["sha256"])
        broken = self._native_media_evidence()
        broken["derived"]["derived_from_sha256"] = "c" * 64
        self.assertEqual({}, short_drama_autodraft._sanitized_native_media(broken))


    def _preview_second_minimax_shot_with_scene_references(
        self, current_scene, previous_scene, recorded_previous_identity=True,
    ):
        from PIL import Image

        self._lock_project_character_references()
        for relative in [
            (current_scene or {}).get("file"),
            (previous_scene or {}).get("file"),
            "short_drama_refs/previous-tail.png",
        ]:
            if not relative:
                continue
            path = Path(self.tmp.name) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (256, 256), (40, 80, 120)).save(path, "PNG")
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shots = plan["material_plan"]
            self.assertGreaterEqual(len(shots), 2)
            current_shot = shots[1]
            previous_shot = shots[0]
        finally:
            conn.close()

        def locked_scene(_conn, _project_id, shot_key, scene_key=None):
            if scene_key:
                return current_scene if scene_key == current_scene["scene_key"] else None
            if shot_key == current_shot["shot_key"]:
                return current_scene
            if shot_key == previous_shot["shot_key"]:
                return previous_scene
            return None

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "locked_scene_reference", side_effect=locked_scene,
        ), mock.patch.object(
            short_drama_autodraft, "_previous_shot_reference",
            return_value={
                "shot_key": previous_shot["shot_key"],
                "file": "short_drama_refs/previous-tail.png",
                "url": "https://cdn.example/previous-tail.jpg",
                "scene_key": (
                    previous_scene.get("scene_key")
                    if previous_scene and recorded_previous_identity else ""
                ),
                "scene_version_id": (
                    previous_scene.get("version_id") if previous_scene else ""
                ),
                "scene_reference_identity": (
                    previous_scene.get("reference_identity")
                    if previous_scene and recorded_previous_identity else ""
                ),
            },
        ):
            return short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": current_shot["shot_key"],
                }, include_private=True,
            )

    def test_minimax_same_scene_submits_scene_reference_and_previous_tail(self):
        scene = {
            "scene_key": "scene:memorial-square",
            "version_id": "scene-version-1",
            "reference_identity": "scene-operation-1",
            "name": "阵亡者纪念广场",
            "prompt": "雨中的纪念墙",
            "file": "short_drama_scenes/memorial-square.png",
            "url": "https://cdn.example/memorial-square.png",
        }
        result = self._preview_second_minimax_shot_with_scene_references(
            scene, dict(scene, version_id="per-shot-version-2"),
        )

        reference_types = [
            item["type"] for item in result["request"]["reference_inputs"]
        ]
        self.assertIn("scene", reference_types)
        self.assertIn("continuity", reference_types)
        self.assertIn("本镜头必须直接承接上一镜头", result["request"]["prompt"])

    def test_minimax_scene_change_submits_scene_reference_without_previous_tail(self):
        current_scene = {
            "scene_key": "scene:memorial-square",
            "version_id": "memorial-version-1",
            "reference_identity": "memorial-operation-1",
            "name": "阵亡者纪念广场",
            "prompt": "雨中的纪念墙",
            "file": "short_drama_scenes/memorial-square.png",
            "url": "https://cdn.example/memorial-square.png",
        }
        previous_scene = {
            "scene_key": "scene:ruined-street",
            "version_id": "ruined-street-version-1",
            "reference_identity": "ruined-street-operation-1",
            "name": "旧城区废墟街道",
            "prompt": "坍塌的街道",
            "file": "short_drama_scenes/ruined-street.png",
            "url": "https://cdn.example/ruined-street.png",
        }
        result = self._preview_second_minimax_shot_with_scene_references(
            current_scene, previous_scene,
        )

        reference_types = [
            item["type"] for item in result["request"]["reference_inputs"]
        ]
        self.assertIn("scene", reference_types)
        self.assertNotIn("continuity", reference_types)
        self.assertNotIn("本镜头必须直接承接上一镜头", result["request"]["prompt"])

    def test_minimax_same_scene_rejects_four_characters_to_reserve_tail(self):
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][1]
            shot["dialogue"] = []
            shot["character_keys"] = ["limit_%d" % index for index in range(1, 5)]
            shot["provider_prompt"] = "四名队员走入同一座纪念广场"
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            for index, key in enumerate(shot["character_keys"], 1):
                conn.execute(
                    "INSERT INTO short_drama_characters "
                    "(id,project_id,character_key,name,source_type,reference_file,"
                    "reference_url,reference_version,reference_locked,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?)",
                    (
                        "limit-character-%d" % index, self.project["id"], key,
                        "队员%d" % index, "ai_character",
                        "short_drama_refs/limit-%d.png" % index,
                        "https://cdn.example/limit-%d.png" % index, 1, index,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        scene = {
            "scene_key": "scene:memorial-square",
            "version_id": "scene-version-1",
            "reference_identity": "scene-operation-1",
            "name": "阵亡者纪念广场",
            "prompt": "雨中的纪念墙",
            "file": "short_drama_scenes/memorial-square.png",
            "url": "https://cdn.example/memorial-square.png",
        }
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            self._preview_second_minimax_shot_with_scene_references(
                scene, dict(scene),
            )

        self.assertEqual("provider_reference_limit_exceeded", raised.exception.code)
        self.assertIn("上一镜头尾帧", str(raised.exception))

    def test_minimax_same_scene_key_with_changed_reference_version_skips_tail(self):
        current_scene = {
            "scene_key": "scene:memorial-square",
            "version_id": "scene-version-2",
            "reference_identity": "scene-operation-2",
            "name": "阵亡者纪念广场",
            "prompt": "更新后的雨中纪念墙",
            "file": "short_drama_scenes/memorial-square-v2.png",
            "url": "https://cdn.example/memorial-square-v2.png",
        }
        previous_scene = dict(
            current_scene,
            version_id="scene-version-1",
            reference_identity="scene-operation-1",
        )
        result = self._preview_second_minimax_shot_with_scene_references(
            current_scene, previous_scene,
        )

        reference_types = [
            item["type"] for item in result["request"]["reference_inputs"]
        ]
        self.assertIn("scene", reference_types)
        self.assertNotIn("continuity", reference_types)

    def test_minimax_bound_url_only_locked_scene_is_rejected_during_preflight(self):
        shot = self._persist_bound_locked_scene_reference(
            file_value="",
            url_value="https://cdn.example/legacy-locked-scene.png",
        )

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                    },
                    include_private=True,
                )

        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)

    def test_minimax_bound_missing_locked_scene_file_is_rejected_during_preflight(self):
        shot = self._persist_bound_locked_scene_reference(
            file_value="short_drama_scenes/missing-locked-scene.png",
            url_value="/api/gen/file/short_drama_scenes/missing-locked-scene.png",
        )

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                    },
                    include_private=True,
                )

        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)

    def test_minimax_bound_cross_user_scene_file_is_rejected_before_provider(self):
        from PIL import Image
        from providers.short_drama_visual.minimax_h3 import MiniMaxH3ShotProvider

        relative = "other_user/private_scene.png"
        path = Path(self.tmp.name) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (256, 256), (40, 80, 120)).save(path, "PNG")
        shot = self._persist_bound_locked_scene_reference(
            file_value=relative,
            url_value="/api/gen/file/" + relative,
        )
        self._claim_legacy_scene_reference(
            shot,
            reference_project_id=self.project["id"],
            operation_id="cross-user-path-operation",
        )

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch.object(
            MiniMaxH3ShotProvider,
            "resolve_reference_values",
            side_effect=AssertionError("untrusted scene must not be encoded"),
        ) as resolve_references, mock.patch(
            "content_domains.core._out_path",
            side_effect=AssertionError("untrusted scene file must not be read"),
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                    },
                    include_private=True,
                )

        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)
        resolve_references.assert_not_called()

    def test_minimax_cross_project_scene_is_rejected_without_quote_or_file_read(self):
        relative = (
            short_drama_autodraft.short_drama_asset_graph._scene_upload_prefix(
                "alice", self.project["id"],
            )
            + "scene_claimed_from_other_project.png"
        )
        shot = self._persist_bound_locked_scene_reference(
            file_value=relative,
            url_value="/api/gen/file/" + relative,
        )
        self._claim_legacy_scene_reference(
            shot,
            reference_project_id="another-project",
            operation_id="cross-project-operation",
        )
        avatar = self._provider_avatar()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch(
            "content_domains.core._out_path",
            side_effect=AssertionError("untrusted scene file must not be read"),
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.create_provider_quote(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "avatar_id": avatar["id"],
                    },
                    avatar_lookup=lambda _username, _avatar_id: avatar,
                )

        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_quotes"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_jobs"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_minimax_forged_scene_after_quote_is_rejected_without_charge_or_job(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()

        prefix = short_drama_autodraft.short_drama_asset_graph._scene_upload_prefix(
            "alice", self.project["id"],
        )
        forged = prefix + "../other_user/private_scene.png"
        shot = self._persist_bound_locked_scene_reference(
            file_value=forged,
            url_value="/api/gen/file/" + forged,
        )
        self._claim_legacy_scene_reference(
            shot,
            reference_project_id=self.project["id"],
            operation_id="forged-path-operation",
        )
        charged = []

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ), mock.patch(
            "content_domains.core._out_path",
            side_effect=AssertionError("untrusted scene file must not be read"),
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.start_provider_job(
                    self.db,
                    "alice",
                    "alice",
                    {"quote_token": quote["quote_token"]},
                    "minimax-forged-scene-after-quote-1",
                    deduct_points=lambda *args: charged.append(args) or 99,
                    refund_points=lambda *_args: 100,
                    enqueue_job=lambda *_args: True,
                    project_usage=short_drama._project_point_usage,
                )

        self.assertEqual(422, raised.exception.status)
        self.assertEqual("provider_scene_reference_required", raised.exception.code)
        self.assertEqual([], charged)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_jobs"
            ).fetchone()[0])
            consumed = conn.execute(
                "SELECT consumed_job_id FROM short_drama_provider_shot_quotes "
                "WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0]
            self.assertIsNone(consumed)
        finally:
            conn.close()

    def test_minimax_bound_scene_without_locked_reference_is_rejected(self):
        self._lock_project_character_references()
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "locked_scene_reference", return_value=None,
        ), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "bound_scene_key", return_value="scene:memorial-square",
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "execution": {
                            "provider_prompt": "人物站在纪念墙前",
                            "include_scene_reference": True,
                        },
                    },
                )

        self.assertEqual("provider_scene_reference_required", raised.exception.code)

    def test_minimax_http_route_returns_payment_required_without_creating_job(self):
        self._lock_project_character_references()
        self._init_shared_jobs_table()
        verify = lambda token: {
            "username": token,
            "must_change": False,
        } if token else None

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
        }), mock.patch.object(
            provider_keys, "has_candidate", return_value=True,
        ), mock.patch.object(
            feature_flags, "is_enabled", return_value=True,
        ):
            quote = self._provider_quote()
            handler = Handler(
                "/api/gen/short-drama/autodraft/provider-jobs",
                body={
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                },
                key="minimax-insufficient-points-route-1",
            )

            handled = short_drama.dispatch_http(
                handler,
                "POST",
                self.db,
                verify,
                deduct_points=lambda *_args: (_ for _ in ()).throw(
                    points.AuthPointsError(
                        402,
                        "点数不足",
                        {"need": quote["cost"]},
                    )
                ),
                refund_points=lambda *_args: None,
                enqueue_job=lambda *_args: True,
            )

        self.assertTrue(handled)
        self.assertEqual(402, handler.response[0])
        self.assertEqual("点数不足", handler.response[1]["detail"])
        self.assertEqual("charge_rejected", handler.response[1]["code"])
        self.assertEqual(quote["cost"], handler.response[1]["need"])
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_provider_shot_jobs"
            ).fetchone()[0])
            self.assertIsNone(conn.execute(
                "SELECT consumed_job_id FROM short_drama_provider_shot_quotes "
                "WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()[0])
        finally:
            conn.close()


    def test_minimax_legacy_previous_shot_without_scene_identity_skips_tail(self):
        scene = {
            "scene_key": "scene:memorial-square",
            "version_id": "scene-version-current",
            "reference_identity": "scene-operation-current",
            "name": "Memorial square",
            "prompt": "Rainy memorial wall",
            "file": "short_drama_scenes/memorial-square.png",
            "url": "https://cdn.example/memorial-square.png",
        }
        result = self._preview_second_minimax_shot_with_scene_references(
            scene, dict(scene), recorded_previous_identity=False,
        )

        self.assertNotIn(
            "continuity",
            [item["type"] for item in result["request"]["reference_inputs"]],
        )

    def test_minimax_legacy_execution_override_keeps_bound_scene_reference(self):
        from PIL import Image

        self._lock_project_character_references()
        scene = {
            "scene_key": "scene:memorial-square",
            "version_id": "scene-version-legacy",
            "reference_identity": "scene-operation-legacy",
            "name": "阵亡者纪念广场",
            "prompt": "雨中的纪念墙",
            "file": "short_drama_scenes/memorial-square-legacy.png",
            "url": "https://cdn.example/memorial-square-legacy.png",
        }
        scene_path = Path(self.tmp.name) / scene["file"]
        scene_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (256, 256), (40, 80, 120)).save(scene_path, "PNG")
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
            conn.execute(
                "INSERT INTO short_drama_provider_shot_execution_overrides "
                "(project_id,shot_key,execution_json,updated_at) VALUES (?,?,?,?)",
                (
                    self.project["id"],
                    shot["shot_key"],
                    json.dumps({
                        "provider_prompt": "人物站在纪念墙前",
                        "include_scene_reference": False,
                        "include_continuity_reference": False,
                    }, ensure_ascii=False),
                    1,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "locked_scene_reference", return_value=scene,
        ), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "bound_scene_key", return_value=scene["scene_key"],
        ):
            result = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                }, include_private=True,
            )

        self.assertTrue(result["scene_reference"]["locked"])
        self.assertIn(
            "scene",
            [item["type"] for item in result["request"]["reference_inputs"]],
        )
        self.assertTrue(result["execution"]["include_scene_reference"])
        self.assertTrue(result["execution"]["include_continuity_reference"])
        workspace = short_drama_autodraft.workspace(
            self.db, "alice", "alice", self.project["id"],
        )
        persisted = workspace["provider_execution_overrides"][shot["shot_key"]]
        self.assertTrue(persisted["include_scene_reference"])
        self.assertTrue(persisted["include_continuity_reference"])

    def test_minimax_bound_scene_cannot_disable_locked_reference_requirement(self):
        self._lock_project_character_references()
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "locked_scene_reference", return_value=None,
        ), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "bound_scene_key", return_value="scene:memorial-square",
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "execution": {
                            "provider_prompt": "Actor stands before the memorial wall",
                            "include_scene_reference": False,
                        },
                    },
                )

        self.assertEqual("provider_scene_reference_required", raised.exception.code)

    def test_minimax_unknown_explicit_scene_key_is_rejected(self):
        self._lock_project_character_references()
        conn = self.db()
        try:
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "locked_scene_reference", return_value=None,
        ), mock.patch.object(
            short_drama_autodraft.short_drama_asset_graph,
            "bound_scene_key", return_value="",
        ):
            with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
                short_drama_autodraft.preview_provider_request(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "plan_id": self.plan_id,
                        "shot_key": shot["shot_key"],
                        "execution": {
                            "provider_prompt": "Actor stands before the memorial wall",
                            "scene_key": "scene:does-not-exist",
                        },
                    },
                )

        self.assertEqual("provider_scene_reference_required", raised.exception.code)

    def test_minimax_without_scene_accepts_five_character_references(self):
        from PIL import Image

        character_keys = ["ensemble_%d" % index for index in range(1, 6)]
        conn = self.db()
        try:
            for index, key in enumerate(character_keys, 1):
                relative = "short_drama_refs/ensemble-%d.png" % index
                path = Path(self.tmp.name) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (256, 256), (40, 80, 120)).save(path, "PNG")
                conn.execute(
                    "INSERT INTO short_drama_characters "
                    "(id,project_id,character_key,name,source_type,reference_file,"
                    "reference_url,reference_version,reference_locked,sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?)",
                    (
                        "ensemble-character-%d" % index, self.project["id"], key,
                        "Ensemble %d" % index, "ai_character",
                        relative,
                        "https://cdn.example/ensemble-%d.png" % index, 1, index,
                    ),
                )
            plan = json.loads(conn.execute(
                "SELECT plan_json FROM short_drama_production_plans WHERE id=?",
                (self.plan_id,),
            ).fetchone()[0])
            shot = plan["material_plan"][0]
            shot["dialogue"] = []
            shot["character_keys"] = list(character_keys)
            conn.execute(
                "UPDATE short_drama_production_plans SET plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), self.plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "minimax_h3",
            "MINIMAX_API_KEY": "configured-for-preflight-only",
        }):
            result = short_drama_autodraft.preview_provider_request(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "plan_id": self.plan_id,
                    "shot_key": shot["shot_key"],
                    "execution": {
                        "provider_prompt": "Five actors enter an empty hall together",
                        "character_keys": character_keys,
                    },
                }, include_private=True,
            )

        self.assertEqual(5, len(result["character_keys"]))
        self.assertEqual(5, result["request"]["reference_count"])


class ShortDramaContinuityChainTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE short_drama_provider_shot_jobs (
          id TEXT PRIMARY KEY, result_json TEXT
        );
        CREATE TABLE short_drama_provider_shot_versions (
          id TEXT PRIMARY KEY, project_id TEXT, job_id TEXT, shot_key TEXT,
          version INTEGER, status TEXT, file TEXT, url TEXT
        );
        CREATE TABLE short_drama_provider_shot_selections (
          project_id TEXT, shot_key TEXT, version_id TEXT
        );
        """)
        self.shots = [
            {"shot_key": "shot_01", "sort_order": 1},
            {"shot_key": "shot_02", "sort_order": 2},
        ]

    def tearDown(self):
        self.conn.close()

    def test_later_shot_waits_for_previous_completed_version(self):
        with self.assertRaises(short_drama_autodraft.AutodraftError) as raised:
            short_drama_autodraft._previous_shot_reference(
                self.conn, "project-1", self.shots, "shot_02"
            )
        self.assertEqual("provider_previous_shot_required", raised.exception.code)

    def test_later_shot_uses_previous_tail_reference(self):
        self.conn.execute(
            "INSERT INTO short_drama_provider_shot_jobs(id,result_json) VALUES (?,?)",
            ("job-1", json.dumps({
                "continuity_tail_file": "video/shot_01_tail.jpg",
                "continuity_tail_url": "/api/gen/file/video/shot_01_tail.jpg",
            })),
        )
        self.conn.execute(
            "INSERT INTO short_drama_provider_shot_versions "
            "(id,project_id,job_id,shot_key,version,status,file,url) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("version-1", "project-1", "job-1", "shot_01", 1, "ready",
             "video/shot_01.mp4", "/api/gen/file/video/shot_01.mp4"),
        )
        result = short_drama_autodraft._previous_shot_reference(
            self.conn, "project-1", self.shots, "shot_02"
        )
        self.assertEqual("shot_01", result["shot_key"])
        self.assertEqual("video/shot_01_tail.jpg", result["file"])


if __name__ == "__main__":
    unittest.main()
