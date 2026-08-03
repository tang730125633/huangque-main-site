import base64
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import core, image, short_drama, short_drama_production, video


def valid_project(**changes):
    project = {
        "title": "雨夜来客", "synopsis": "陌生女孩敲开侦探的门",
        "ratio": "9:16", "target_duration": 30, "shot_count": 6,
        "visual_style": "电影写实", "point_budget": 1400,
    }
    project.update(changes)
    return project


def bound_planning_payload(project, **changes):
    payload = {
        "format": "short_drama",
        "project_id": project["id"],
        "project_revision": project["revision"],
        "prompt": project["synopsis"],
        "dur": "%ss" % project["target_duration"],
        "ratio": project["ratio"],
        "shot_count": project["shot_count"],
        "style": project["visual_style"],
        "platform": project["target_platform"],
    }
    payload.update(changes)
    return payload


def bound_planning_result(project, plan=None, **changes):
    result = {
        "type": "copy",
        "mode": "short_drama",
        "project_id": project["id"],
        "project_revision": project["revision"],
        "settings": {
            "ratio": project["ratio"],
            "target_duration": project["target_duration"],
            "shot_count": project["shot_count"],
        },
        "plan": plan or valid_raw_plan(),
        "prompt": project["synopsis"],
        "dur": "%ss" % project["target_duration"],
        "ratio": project["ratio"],
        "shot_count": project["shot_count"],
    }
    result.update(changes)
    return result


def valid_raw_plan():
    return {
        "title": "第一稿",
        "characters": [],
        "script": {"title": "第一稿", "dialogue_lines": []},
        "shots": [{
            "shot_key": "shot-%s" % index,
            "duration": 5,
            "scene_description": "场景",
            "camera_description": "镜头",
            "character_keys": [],
            "dialogue_line_ids": [],
            "image_prompt": "画面",
            "video_prompt": "视频",
        } for index in range(6)],
    }


def valid_editable_plan():
    characters = [{
        "character_key": "lin-mo",
        "name": "林默",
        "identity_text": "侦探",
        "personality": "冷静",
        "source_type": "ai_character",
        "avatar_id": None,
        "appearance_prompt": "黑色风衣",
        "wardrobe_prompt": "深色西装",
        "voice_key": "calm",
        "voice_settings": {"speed": 1},
    }, {
        "character_key": "su-qing",
        "name": "苏晴",
        "identity_text": "记者",
        "personality": "果断",
        "source_type": "ai_character",
        "avatar_id": None,
        "appearance_prompt": "米色大衣",
        "wardrobe_prompt": "浅色长裙",
        "voice_key": None,
        "voice_settings": {},
    }]
    dialogue_lines = [{
        "id": "line-1", "character_key": "lin-mo", "text": "我们得赶在天亮前。",
    }, {
        "id": "line-2", "character_key": "lin-mo", "text": "线索就在门后。",
    }]
    return {
        "title": "第一稿",
        "characters": characters,
        "script": {
            "title": "第一稿", "logline": "两人追查雨夜秘密",
            "hook": "门外有脚步声", "conflict_text": "线索即将被毁",
            "turn_text": "同伴隐瞒了真相", "ending": "门终于打开",
            "dialogue_lines": dialogue_lines,
        },
        "shots": [{
            "shot_key": "shot-%s" % index,
            "duration": 5,
            "scene_description": "雨夜门厅",
            "camera_description": "稳定推轨",
            "character_keys": [characters[index % 2]["character_key"]],
            "dialogue_line_ids": [dialogue_lines[index % 2]["id"]],
            "image_prompt": "潮湿门厅画面",
            "video_prompt": "人物走向木门",
        } for index in range(6)],
    }


class ShortDramaProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.path)
        self.next_job_id = 600
        short_drama.init_db(self.db)
        conn = self.db()
        try:
            conn.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, username TEXT NOT NULL,
                cost INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                refunded INTEGER NOT NULL DEFAULT 0
            )""")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, shot_count, duration=5):
        return {
            "characters": [],
            "script": {"title": "第一稿", "dialogue_lines": []},
            "shots": [{
                "shot_key": "shot-%s" % index,
                "duration": duration,
                "scene_description": "场景",
                "camera_description": "镜头",
                "character_keys": [],
                "dialogue_line_ids": [],
                "image_prompt": "画面",
                "video_prompt": "视频",
            } for index in range(shot_count)],
        }

    def insert_planning_job(self, project, job_id, cost=3, status="pending", refunded=0):
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO jobs(id,kind,username,cost,status,payload,refunded) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, "copy", "alice", cost, status,
                 json.dumps(bound_planning_payload(project), ensure_ascii=False), refunded),
            )
            conn.commit()
        finally:
            conn.close()

    def _assert_plan_rejected_without_side_effects(self, project, plan, job_id):
        before = short_drama.get_project(self.db, "alice", project["id"])
        with self.assertRaises(ValueError):
            short_drama.apply_plan(self.db, "alice", project["id"], before["revision"],
                                   plan, 140, job_id)
        after = short_drama.get_project(self.db, "alice", project["id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["stage"], before["stage"])
        self.assertEqual(after["spent_points"], before["spent_points"])
        conn = self.db()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM short_drama_applied_jobs WHERE job_id=?", (job_id,)
            ).fetchone())
        finally:
            conn.close()

    def applied_project(self):
        self.next_job_id += 1
        project = short_drama.create_project(self.db, "alice", valid_project())
        return short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_editable_plan(), planning_cost=3, planning_job_id=self.next_job_id,
        )

    def applied_project_with_two_characters_and_dialogue(self):
        return self.applied_project()

    def test_new_dialogue_gets_deterministic_server_id_and_snapshot(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1])
        script["dialogue_lines"] = list(script["dialogue_lines"]) + [{
            "client_token": "browser-request-1",
            "character_key": project["characters"][0]["character_key"],
            "text": "我们现在出发。",
            "subtitle_enabled": True,
            "voice_overrides": {"speed": 1.1},
        }]
        first = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], script
        )
        line = first["script_versions"][-1]["dialogue_lines"][-1]
        self.assertTrue(line["id"].startswith("line_"))
        self.assertEqual(project["characters"][0]["name"], line["speaker_name_snapshot"])
        self.assertEqual({"speed": 1.1}, line["voice_overrides"])
        self.assertEqual(
            line["id"], first["dialogue_token_receipts"]["browser-request-1"]
        )

        retried = short_drama.update_script(
            self.db, "alice", first["id"], first["revision"], script
        )
        self.assertEqual(
            line["id"], retried["script_versions"][-1]["dialogue_lines"][-1]["id"]
        )

    def test_lost_response_token_reconciles_with_saved_line(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1])
        token_line = {
            "client_token": "lost-response-token",
            "character_key": project["characters"][0]["character_key"],
            "text": "网络中断前已保存",
            "subtitle_enabled": True,
            "voice_overrides": {"speed": 1.05},
        }
        script["dialogue_lines"] = list(script["dialogue_lines"]) + [token_line]
        saved = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], script
        )
        saved_lines = saved["script_versions"][-1]["dialogue_lines"]
        generated_id = saved_lines[-1]["id"]

        replay = dict(saved["script_versions"][-1])
        replay["dialogue_lines"] = list(saved_lines) + [token_line]
        reconciled = short_drama.update_script(
            self.db, "alice", saved["id"], saved["revision"], replay
        )
        reconciled_lines = reconciled["script_versions"][-1]["dialogue_lines"]
        self.assertEqual(len(saved_lines), len(reconciled_lines))
        self.assertEqual(1, sum(line["id"] == generated_id for line in reconciled_lines))

    def test_lost_response_token_content_mismatch_is_atomic(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1])
        token_line = {
            "client_token": "mismatch-token",
            "character_key": project["characters"][0]["character_key"],
            "text": "已保存内容",
            "voice_overrides": {},
        }
        script["dialogue_lines"] = list(script["dialogue_lines"]) + [token_line]
        saved = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], script
        )
        replay = dict(saved["script_versions"][-1])
        replay["dialogue_lines"] = list(replay["dialogue_lines"]) + [
            dict(token_line, text="冲突内容")
        ]
        self._assert_content_rejected_without_side_effects(
            saved, lambda before: short_drama.update_script(
                self.db, "alice", saved["id"], before["revision"], replay
            )
        )

    def test_dialogue_id_tampering_is_rejected_atomically(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1])
        script["dialogue_lines"] = [
            dict(line, id="attacker_replaced_id")
            if line["id"] == "line-1" else line
            for line in script["dialogue_lines"]
        ]
        self._assert_content_rejected_without_side_effects(
            project, lambda before: short_drama.update_script(
                self.db, "alice", project["id"], before["revision"], script
            )
        )

    def test_deleted_dialogue_token_cannot_be_reused(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        original_script = dict(project["script_versions"][-1])
        created_script = dict(original_script)
        created_script["dialogue_lines"] = list(original_script["dialogue_lines"]) + [{
            "client_token": "retired-token",
            "character_key": project["characters"][0]["character_key"],
            "text": "临时台词",
            "voice_overrides": {},
        }]
        created = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], created_script
        )
        generated_id = created["script_versions"][-1]["dialogue_lines"][-1]["id"]
        deleted_script = dict(created["script_versions"][-1])
        deleted_script["dialogue_lines"] = [
            line for line in deleted_script["dialogue_lines"] if line["id"] != generated_id
        ]
        deleted = short_drama.update_script(
            self.db, "alice", created["id"], created["revision"], deleted_script
        )
        self.assertNotIn("retired-token", deleted["dialogue_token_receipts"])
        reused_script = dict(deleted["script_versions"][-1])
        reused_script["dialogue_lines"] = list(reused_script["dialogue_lines"]) + [{
            "client_token": "retired-token",
            "character_key": deleted["characters"][0]["character_key"],
            "text": "冒充的新台词",
            "voice_overrides": {},
        }]
        self._assert_content_rejected_without_side_effects(
            deleted, lambda before: short_drama.update_script(
                self.db, "alice", deleted["id"], before["revision"], reused_script
            )
        )

    def test_dialogue_client_token_is_project_scoped(self):
        ids = []
        for _index in range(2):
            project = self.applied_project()
            project = short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], "characters_review"
            )
            script = dict(project["script_versions"][-1])
            script["dialogue_lines"] = list(script["dialogue_lines"]) + [{
                "client_token": "same-browser-token",
                "character_key": project["characters"][0]["character_key"],
                "text": "项目内台词",
                "voice_overrides": {},
            }]
            updated = short_drama.update_script(
                self.db, "alice", project["id"], project["revision"], script
            )
            ids.append(updated["script_versions"][-1]["dialogue_lines"][-1]["id"])
            self.assertEqual(
                { "same-browser-token": ids[-1] },
                updated["dialogue_token_receipts"],
            )
        self.assertNotEqual(ids[0], ids[1])

    def test_dialogue_voice_overrides_requires_object_atomically(self):
        for invalid in ([], "", 1, None):
            with self.subTest(value=invalid):
                project = self.applied_project()
                project = short_drama.confirm_stage(
                    self.db, "alice", project["id"], project["revision"], "characters_review"
                )
                script = dict(project["script_versions"][-1])
                script["dialogue_lines"] = [
                    dict(script["dialogue_lines"][0], voice_overrides=invalid)
                ] + list(script["dialogue_lines"][1:])
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_script(
                        self.db, "alice", project["id"], before["revision"], script
                    )
                )

    def test_invalid_new_dialogue_rolls_back_token_allocation(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1])
        new_line = {
            "client_token": "rollback-token",
            "character_key": project["characters"][0]["character_key"],
            "text": "等待校验",
            "voice_overrides": [],
        }
        script["dialogue_lines"] = list(script["dialogue_lines"]) + [new_line]
        self._assert_content_rejected_without_side_effects(
            project, lambda before: short_drama.update_script(
                self.db, "alice", project["id"], before["revision"], script
            )
        )
        new_line["voice_overrides"] = {}
        saved = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], script
        )
        self.assertEqual(
            "等待校验", saved["script_versions"][-1]["dialogue_lines"][-1]["text"]
        )

    def test_narrator_is_valid_without_project_character(self):
        script = {
            "title": "第一稿", "logline": "一句话", "hook": "钩子",
            "conflict_text": "冲突", "turn_text": "反转", "ending": "结局",
            "dialogue_lines": [{
                "id": "line_narrator", "character_key": "narrator",
                "text": "夜幕降临。", "subtitle_enabled": False,
            }],
        }
        normalized = short_drama._normalize_script(script, set())
        line = normalized["dialogue_lines"][0]
        self.assertEqual("旁白", line["speaker_name_snapshot"])
        self.assertFalse(line["subtitle_enabled"])

    def _content_snapshot(self, project_id):
        project = short_drama.get_project(self.db, "alice", project_id)
        return {
            key: project[key]
            for key in (
                "revision", "stage", "characters", "script_versions", "shots", "spent_points",
            )
        }

    def _assert_content_rejected_without_side_effects(self, project, call, error=ValueError):
        before = self._content_snapshot(project["id"])
        with self.assertRaises(error):
            call(before)
        self.assertEqual(before, self._content_snapshot(project["id"]))

    def test_create_project_rejects_fifty_first_active_project(self):
        for index in range(50):
            short_drama.create_project(
                self.db, "alice", valid_project(title="短剧%02d" % index)
            )
        with self.assertRaises(short_drama.ProjectLimitExceeded):
            short_drama.create_project(
                self.db, "alice", valid_project(title="第51个")
            )

    def test_list_projects_returns_stable_page_metadata(self):
        for index in range(23):
            short_drama.create_project(
                self.db, "alice", valid_project(title="短剧%02d" % index)
            )
        result = short_drama.list_projects(
            self.db, "alice", page=2, page_size=20
        )
        self.assertEqual(
            (result["page"], result["page_size"], result["total"]),
            (2, 20, 23),
        )
        self.assertEqual(result["total_pages"], 2)
        self.assertEqual(len(result["items"]), 3)

    def test_list_projects_returns_summaries_and_aggregates_charged_points(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        self.insert_planning_job(project, job_id=39, cost=3, status="pending")
        self.insert_planning_job(project, job_id=40, cost=5, status="done")

        item = short_drama.list_projects(self.db, "alice")["items"][0]

        self.assertEqual(8, item["spent_points"])
        self.assertEqual(project["id"], item["id"])
        for detail_key in ("characters", "script_versions", "shots"):
            self.assertNotIn(detail_key, item)

    def test_soft_delete_hides_project_and_releases_capacity(self):
        projects = [
            short_drama.create_project(
                self.db, "alice", valid_project(title="短剧%02d" % index)
            )
            for index in range(50)
        ]
        deleted = short_drama.delete_project(
            self.db, "alice", projects[0]["id"], projects[0]["revision"]
        )
        self.assertTrue(deleted["deleted"])
        with self.assertRaises(LookupError):
            short_drama.get_project(self.db, "alice", projects[0]["id"])
        replacement = short_drama.create_project(
            self.db, "alice", valid_project(title="替补项目")
        )
        self.assertEqual(replacement["title"], "替补项目")

    def test_delete_rejects_unapplied_charged_job_but_allows_refunded_job(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        self.insert_planning_job(project, job_id=41, cost=3, status="running")

        with self.assertRaises(short_drama.ProjectHasUnappliedJobs):
            short_drama.delete_project(
                self.db, "alice", project["id"], project["revision"]
            )
        self.assertEqual(project["id"], short_drama.get_project(
            self.db, "alice", project["id"]
        )["id"])

        with closing(self.db()) as conn:
            conn.execute("UPDATE jobs SET refunded=1 WHERE id=41")
            conn.commit()
        deleted = short_drama.delete_project(
            self.db, "alice", project["id"], project["revision"]
        )
        self.assertTrue(deleted["deleted"])

    def test_delete_allows_charged_job_after_it_is_applied(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        self.insert_planning_job(project, job_id=42, cost=3, status="done")
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_editable_plan(), planning_cost=3, planning_job_id=42,
        )

        deleted = short_drama.delete_project(
            self.db, "alice", project["id"], project["revision"]
        )

        self.assertTrue(deleted["deleted"])

    def test_delete_blocks_collaborator_production_job_until_completed(self):
        project = self.applied_project()
        self.next_job_id += 1
        with closing(self.db()) as conn:
            short_drama_production.ensure_asset_slots(conn, project["id"])
            conn.execute(
                "INSERT INTO jobs(id,kind,username,cost,status,payload,refunded) "
                "VALUES(?,'image','bob',24,'error','{}',1)",
                (self.next_job_id,),
            )
            conn.execute(
                "INSERT INTO short_drama_production_jobs "
                "(id,username,project_id,shot_id,kind,job_id,idempotency_key,"
                "quoted_cost,status,created_at,updated_at) "
                "VALUES(?,'bob',?,?,'still',?,'collaborator-still',24,'pending',1,1)",
                ("collaborator-link", project["id"], project["shots"][0]["id"],
                 self.next_job_id),
            )
            conn.commit()

        with self.assertRaises(short_drama.ProjectHasUnappliedJobs):
            short_drama.delete_project(
                self.db, "alice", project["id"], project["revision"]
            )

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_production_jobs SET status='done' "
                "WHERE id='collaborator-link'"
            )
            conn.commit()
        self.assertTrue(short_drama.delete_project(
            self.db, "alice", project["id"], project["revision"]
        )["deleted"])

    def test_delete_blocks_orphan_production_link_until_reconciled(self):
        project = self.applied_project()
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_production_jobs "
                "(id,username,project_id,shot_id,kind,job_id,idempotency_key,"
                "quoted_cost,status,created_at,updated_at) "
                "VALUES('orphan-link','bob',?,?,'still',999999,"
                "'orphan-still',24,'running',1,1)",
                (project["id"], project["shots"][0]["id"]),
            )
            conn.commit()

        with self.assertRaises(short_drama.ProjectHasUnappliedJobs):
            short_drama.delete_project(
                self.db, "alice", project["id"], project["revision"]
            )

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_production_jobs SET status='failed' "
                "WHERE id='orphan-link'"
            )
            conn.commit()
        self.assertTrue(short_drama.delete_project(
            self.db, "alice", project["id"], project["revision"]
        )["deleted"])

    def test_delete_blocks_unsettled_charge_attempts_until_refunded(self):
        for index, state in enumerate(("accepted", "charged", "refund_pending")):
            with self.subTest(state=state):
                project = self.applied_project()
                shot_id = project["shots"][0]["id"]
                token = "delete-quote-%s" % index
                with closing(self.db()) as conn:
                    conn.execute(
                        "INSERT INTO short_drama_still_quotes "
                        "(token,username,project_id,shot_id,request_hash,cost,"
                        "expires_at,created_at) VALUES(?,'bob',?,?,?,24,9999999999,1)",
                        (token, project["id"], shot_id, "hash-%s" % index),
                    )
                    conn.execute(
                        "INSERT INTO short_drama_charge_attempts "
                        "(charge_key,refund_key,username,endpoint,idempotency_key,"
                        "request_hash,project_id,shot_id,quote_token,cost,"
                        "image_payload_json,state,created_at,updated_at) "
                        "VALUES(?,?,'bob','/api/gen/short-drama/generate-stills',"
                        "?,?,?,?,?,24,'{}',?,1,1)",
                        ("charge-%s" % index, "refund-%s" % index,
                         "delete-attempt-%s" % index, "hash-%s" % index,
                         project["id"], shot_id, token, state),
                    )
                    conn.commit()

                with self.assertRaises(short_drama.ProjectHasUnappliedJobs):
                    short_drama.delete_project(
                        self.db, "alice", project["id"], project["revision"]
                    )

                with closing(self.db()) as conn:
                    conn.execute(
                        "UPDATE short_drama_charge_attempts SET state='refunded' "
                        "WHERE charge_key=?",
                        ("charge-%s" % index,),
                    )
                    conn.commit()
                self.assertTrue(short_drama.delete_project(
                    self.db, "alice", project["id"], project["revision"]
                )["deleted"])

    def test_delete_blocks_unsettled_provider_shot_attempt_until_refunded(self):
        project = self.applied_project()
        shot_key = project["shots"][0]["id"]
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_provider_shot_quotes "
                "(token,actor_username,owner_username,project_id,plan_id,"
                "shot_key,character_key,"
                "avatar_id,request_hash,request_json,cost,expires_at,created_at) "
                "VALUES('provider-delete-quote','alice','alice',?,"
                "'plan-delete',?,"
                "'character-delete','avatar-delete','provider-delete-hash',"
                "'{}',10,9999999999,1)",
                (project["id"], shot_key),
            )
            conn.execute(
                "INSERT INTO short_drama_provider_shot_attempts "
                "(id,actor_username,owner_username,project_id,idempotency_key,"
                "request_hash,quote_token,cost,charge_key,refund_key,state,"
                "created_at,updated_at) "
                "VALUES('provider-delete-attempt','alice','alice',?,"
                "'provider-delete-key','provider-delete-hash',"
                "'provider-delete-quote',10,'provider-delete-charge',"
                "'provider-delete-refund','linked',1,1)",
                (project["id"],),
            )
            conn.commit()

        with self.assertRaises(short_drama.ProjectHasUnappliedJobs):
            short_drama.delete_project(
                self.db, "alice", project["id"], project["revision"]
            )

        with closing(self.db()) as conn:
            conn.execute(
                "UPDATE short_drama_provider_shot_attempts "
                "SET state='refunded' WHERE id='provider-delete-attempt'"
            )
            conn.commit()
        self.assertTrue(short_drama.delete_project(
            self.db, "alice", project["id"], project["revision"]
        )["deleted"])

    def test_create_project_rejects_impossible_duration_shot_pair(self):
        with self.assertRaisesRegex(ValueError, "时长与分镜数量不匹配"):
            short_drama.create_project(
                self.db, "alice",
                valid_project(target_duration=30, shot_count=10),
            )

    def test_partial_settings_update_validates_merged_pair(self):
        project = short_drama.create_project(
            self.db, "alice",
            valid_project(target_duration=45, shot_count=9),
        )
        with self.assertRaisesRegex(ValueError, "时长与分镜数量不匹配"):
            short_drama.update_project(
                self.db, "alice", project["id"], project["revision"],
                {"target_duration": 30},
            )

    def test_paid_planning_job_counts_before_apply(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        self.insert_planning_job(project, job_id=41, cost=3, status="pending")
        fetched = short_drama.get_project(self.db, "alice", project["id"])
        self.assertEqual(fetched["spent_points"], 3)

    def test_confirmed_refund_is_removed_from_spent_points(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        self.insert_planning_job(project, job_id=42, cost=3, status="running")
        self.insert_planning_job(
            project, job_id=43, cost=5, status="error", refunded=1,
        )
        fetched = short_drama.get_project(self.db, "alice", project["id"])
        self.assertEqual(fetched["spent_points"], 3)

    def test_apply_plan_does_not_double_count_paid_job(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        self.insert_planning_job(project, job_id=44, cost=3, status="done")
        applied = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_editable_plan(), planning_cost=3, planning_job_id=44,
        )
        self.assertEqual(applied["spent_points"], 3)
        conn = self.db()
        try:
            stored = conn.execute(
                "SELECT spent_points FROM short_drama_projects WHERE id=?",
                (project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stored, 0)

    def test_content_sections_save_only_in_their_review_stage(self):
        project = self.applied_project()
        edited = [dict(project["characters"][0], name="林默（新）")]
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"],
            {"characters": edited},
        )
        self.assertEqual(project["revision"], 3)
        self.assertEqual(project["stage"], "characters_review")
        self.assertEqual(project["characters"][0]["name"], "林默（新）")
        self.assertEqual(project, short_drama.get_project(self.db, "alice", project["id"]))

        with self.assertRaises(ValueError):
            short_drama.update_project(
                self.db, "alice", project["id"], project["revision"],
                {"script": dict(project["script_versions"][-1])},
            )

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1], ending="新的结尾")
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"script": script}
        )
        self.assertEqual(len(project["script_versions"]), 2)
        self.assertEqual(project["script_versions"][-1]["ending"], "新的结尾")
        self.assertTrue(all(
            shot["script_version"] == project["script_versions"][-1]["version"]
            for shot in project["shots"]
        ))

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "script_review"
        )
        shots = [dict(shot, scene_description="修改后的场景") for shot in project["shots"]]
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"shots": shots}
        )
        self.assertEqual(len(project["shots"]), 6)
        self.assertTrue(all(x["scene_description"] == "修改后的场景" for x in project["shots"]))
        self.assertEqual(project, short_drama.get_project(self.db, "alice", project["id"]))

    def test_confirmed_project_can_add_character_reference_without_editing_identity(self):
        project = self.applied_project()
        for stage in ("characters_review", "script_review", "storyboard_review"):
            project = short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], stage
            )
        conn = self.db()
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN result TEXT")
            conn.execute(
                "INSERT INTO jobs(id,kind,username,cost,status,payload,refunded,result) "
                "VALUES(901,'image','alice',35,'done',?,0,?)",
                (
                    json.dumps({"provider": "banana", "model": "nb2"}),
                    json.dumps({
                        "file": "character.png",
                        "url": "/api/gen/file/character.png",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        Path(self.tmp.name, "character.png").write_bytes(
            b"\x89PNG\r\n\x1a\nreference"
        )
        characters = [dict(item) for item in project["characters"]]
        characters[0].update({"reference_job_id": 901, "reference_locked": True})
        with patch.object(image, "OUT_DIR", Path(self.tmp.name)):
            updated = short_drama.update_characters(
                self.db, "alice", project["id"], project["revision"], characters
            )
        self.assertEqual("stills_review", updated["stage"])
        self.assertEqual(901, updated["characters"][0]["reference_job_id"])
        self.assertTrue(updated["characters"][0]["reference_locked"])

        changed = [dict(item) for item in updated["characters"]]
        changed[0]["name"] = "changed after confirmation"
        with self.assertRaisesRegex(ValueError, "只能补充或重新生成角色标准图"):
            short_drama.update_characters(
                self.db, "alice", updated["id"], updated["revision"], changed
            )

    def test_character_reference_generation_respects_project_budget(self):
        project = self.applied_project()
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=20 WHERE id=?",
                (project["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(short_drama.PointBudgetExceeded):
            short_drama.prepare_character_reference_submission(
                self.db, "alice", "alice", {
                    "project_id": project["id"],
                    "revision": project["revision"],
                    "character_key": project["characters"][0]["character_key"],
                }, "character-budget-1", lambda _kind, _payload: 35,
            )

    def test_character_reference_job_is_durable_counted_and_recovered(self):
        project = self.applied_project()
        character_key = project["characters"][0]["character_key"]
        request = {
            "project_id": project["id"],
            "revision": project["revision"],
            "character_key": character_key,
        }
        prepared = short_drama.prepare_character_reference_submission(
            self.db, "alice", "alice", request,
            "character-recovery-1", lambda _kind, _payload: 35,
        )
        short_drama.accept_character_reference_attempt(
            self.db, prepared, "alice"
        )
        short_drama.mark_character_reference_attempt_charged(
            self.db, "alice", "character-recovery-1", 65
        )
        conn = self.db()
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN result TEXT")
            conn.execute(
                "INSERT INTO jobs(id,kind,username,cost,status,payload,refunded,result) "
                "VALUES(902,'image','alice',35,'pending',?,0,NULL)",
                (json.dumps(prepared["payload"], ensure_ascii=False),),
            )
            short_drama.record_character_reference_job(
                conn, prepared, "alice", 902
            )
            conn.commit()
        finally:
            conn.close()

        recovered = short_drama.find_recoverable_character_reference(
            self.db, "alice", "alice", request, "new-browser-key"
        )
        self.assertEqual(902, recovered["job_id"])
        self.assertEqual(35, short_drama.get_project(
            self.db, "alice", project["id"]
        )["spent_points"])

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "character-902.png").write_bytes(
                b"\x89PNG\r\n\x1a\nreference"
            )
            conn = self.db()
            try:
                conn.execute(
                    "UPDATE jobs SET status='done',result=? WHERE id=902",
                    (json.dumps({
                        "file": "character-902.png",
                        "url": "/api/gen/file/character-902.png",
                    }),),
                )
                # Simulate an unrelated save/revision conflict after the paid job started.
                conn.execute(
                    "UPDATE short_drama_projects SET revision=revision+1 WHERE id=?",
                    (project["id"],),
                )
                conn.commit()
            finally:
                conn.close()
            with patch.object(image, "OUT_DIR", output):
                short_drama.reconcile_character_reference_job(
                    self.db, 902, "alice"
                )

        attached = short_drama.get_project(self.db, "alice", project["id"])
        character = next(
            item for item in attached["characters"]
            if item["character_key"] == character_key
        )
        self.assertEqual(902, character["reference_job_id"])
        self.assertEqual("character-902.png", character["reference_file"])
        self.assertTrue(character["reference_locked"])

    def test_character_and_script_edits_prune_only_invalid_unconfirmed_references(self):
        project = self.applied_project_with_two_characters_and_dialogue()
        kept_key = project["characters"][0]["character_key"]
        expected_character_refs = {
            shot["shot_key"]: [key for key in shot["character_keys"] if key == kept_key]
            for shot in project["shots"]
        }
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"],
            {"characters": [project["characters"][0]]},
        )
        self.assertEqual(expected_character_refs, {
            shot["shot_key"]: shot["character_keys"] for shot in project["shots"]
        })

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        historical_script = dict(project["script_versions"][-1])
        script = dict(project["script_versions"][-1])
        script["dialogue_lines"] = script["dialogue_lines"][:1]
        valid_dialogue = {line["id"] for line in script["dialogue_lines"]}
        expected_dialogue_refs = {
            shot["shot_key"]: [
                line_id for line_id in shot["dialogue_line_ids"] if line_id in valid_dialogue
            ]
            for shot in project["shots"]
        }
        project = short_drama.update_script(
            self.db, "alice", project["id"], project["revision"], script
        )
        self.assertEqual(expected_dialogue_refs, {
            shot["shot_key"]: shot["dialogue_line_ids"] for shot in project["shots"]
        })
        self.assertEqual(historical_script, project["script_versions"][0])

    def test_character_edits_require_complete_character_contract_atomically(self):
        def without(character, field):
            edited = dict(character)
            edited.pop(field, None)
            return edited

        cases = []
        for field in (
            "character_key", "name", "identity_text", "personality",
            "appearance_prompt", "wardrobe_prompt", "source_type",
        ):
            cases.extend((
                ("missing " + field, lambda character, field=field: without(character, field)),
                ("empty " + field, lambda character, field=field: dict(character, **{field: ""})),
            ))
        cases.extend((
            ("missing voice settings", lambda character: without(character, "voice_settings")),
            ("voice settings scalar", lambda character: dict(character, voice_settings="fast")),
            ("voice key container", lambda character: dict(character, voice_key=[])),
            ("avatar id scalar", lambda character: dict(character, avatar_id=123)),
            ("cinematic avatar id", lambda character: dict(
                character, source_type="cinematic_avatar", avatar_id=None
            )),
        ))
        for name, mutate in cases:
            with self.subTest(name=name):
                project = self.applied_project()
                characters = list(project["characters"])
                characters[0] = mutate(characters[0])
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_characters(
                        self.db, "alice", project["id"], before["revision"], characters
                    )
                )

    def test_character_edits_accept_complete_ai_and_cinematic_avatar_contracts(self):
        project = self.applied_project()
        characters = [dict(character) for character in project["characters"]]
        project = short_drama.update_characters(
            self.db, "alice", project["id"], project["revision"], characters,
            avatar_lookup=lambda username, avatar_id: {
                "username": username, "id": avatar_id, "status": "ready",
                "provider_avatar_id": "provider-ready-1",
            },
        )
        self.assertEqual("ai_character", project["characters"][0]["source_type"])
        self.assertIsNone(project["characters"][0]["avatar_id"])

        characters = [dict(character) for character in project["characters"]]
        characters[0].update({
            "source_type": "cinematic_avatar", "avatar_id": "cinematic-avatar-1",
        })
        project = short_drama.update_characters(
            self.db, "alice", project["id"], project["revision"], characters,
            avatar_lookup=lambda username, avatar_id: {
                "username": username, "id": avatar_id, "status": "ready",
                "provider_avatar_id": "provider-ready-1",
            },
        )
        self.assertEqual("cinematic_avatar", project["characters"][0]["source_type"])
        self.assertEqual("cinematic-avatar-1", project["characters"][0]["avatar_id"])

    def test_apply_plan_keeps_normalized_content_and_duplicate_job_id_atomic(self):
        plan = valid_editable_plan()
        project = short_drama.create_project(self.db, "alice", valid_project())
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"], plan,
            planning_cost=3, planning_job_id=777,
        )
        self.assertEqual(["lin-mo", "su-qing"], [
            character["character_key"] for character in project["characters"]
        ])
        self.assertEqual({"speed": 1}, project["characters"][0]["voice_settings"])
        self.assertEqual(plan["script"]["hook"], project["script_versions"][0]["hook"])
        self.assertEqual(["shot-%s" % index for index in range(6)], [
            shot["shot_key"] for shot in project["shots"]
        ])
        before = self._content_snapshot(project["id"])
        with self.assertRaises(short_drama.AppliedJobConflict):
            short_drama.apply_plan(
                self.db, "alice", project["id"], project["revision"], plan,
                planning_cost=3, planning_job_id=777,
            )
        self.assertEqual(before, self._content_snapshot(project["id"]))

    def test_content_update_access_dispatch_and_stage_rejections_are_atomic(self):
        cases = (
            ("cross owner", lambda p: short_drama.update_characters(
                self.db, "bob", p["id"], p["revision"], p["characters"]
            ), LookupError),
            ("stale revision", lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"] - 1, p["characters"]
            ), short_drama.RevisionConflict),
            ("wrong stage", lambda p: short_drama.update_script(
                self.db, "alice", p["id"], p["revision"], p["script_versions"][-1]
            ), ValueError),
            ("two sections", lambda p: short_drama.update_project(
                self.db, "alice", p["id"], p["revision"],
                {"characters": p["characters"], "script": p["script_versions"][-1]},
            ), ValueError),
            ("content plus title", lambda p: short_drama.update_project(
                self.db, "alice", p["id"], p["revision"],
                {"characters": p["characters"], "title": "混合更新"},
            ), ValueError),
            ("non-integer revision", lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], str(p["revision"]), p["characters"]
            ), ValueError),
        )
        for name, call, error in cases:
            with self.subTest(name=name):
                project = self.applied_project()
                self._assert_content_rejected_without_side_effects(
                    project, lambda _before: call(project), error
                )

    def test_malformed_content_and_duplicate_keys_are_rejected_atomically(self):
        cases = []
        project = self.applied_project()
        cases.extend((
            ("characters section", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"], {"not": "a list"}
            )),
            ("character field", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"],
                [dict(p["characters"][0], name=["not text"])]
            )),
            ("character voice settings", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"],
                [dict(p["characters"][0], voice_settings=[])]
            )),
            ("duplicate character key", project, lambda p: short_drama.update_characters(
                self.db, "alice", p["id"], p["revision"],
                [p["characters"][0], dict(p["characters"][0], name="重复角色")]
            )),
        ))
        for name, original, call in cases:
            with self.subTest(name=name):
                self._assert_content_rejected_without_side_effects(
                    original, lambda _before: call(short_drama.get_project(
                        self.db, "alice", original["id"]
                    ))
                )

        for name, mutate in (
            ("script section", lambda _script: []),
            ("script title", lambda script: dict(script, title={"not": "text"})),
            ("script dialogue container", lambda script: dict(script, dialogue_lines={})),
            ("duplicate dialogue id", lambda script: dict(
                script, dialogue_lines=[script["dialogue_lines"][0], script["dialogue_lines"][0]]
            )),
        ):
            with self.subTest(name=name):
                project = self.applied_project()
                project = short_drama.confirm_stage(
                    self.db, "alice", project["id"], project["revision"], "characters_review"
                )
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_script(
                        self.db, "alice", project["id"], before["revision"],
                        mutate(project["script_versions"][-1]),
                    )
                )

    def test_invalid_storyboard_updates_are_rejected_atomically(self):
        def storyboard_project():
            project = self.applied_project()
            project = short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], "characters_review"
            )
            return short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"], "script_review"
            )

        def replace(shots, index, **changes):
            edited = list(shots)
            edited[index] = dict(edited[index], **changes)
            return edited

        cases = (
            ("shots section", lambda _shots: {"not": "a list"}),
            ("shot field", lambda shots: replace(shots, 0, scene_description=[])),
            ("duplicate shot key", lambda shots: replace(
                shots, 1, shot_key=shots[0]["shot_key"]
            )),
            ("shot count", lambda shots: shots[:-1]),
            ("duration type", lambda shots: replace(shots, 0, duration="5")),
            ("duration value", lambda shots: replace(shots, 0, duration=7)),
            ("duration total", lambda shots: replace(shots, 0, duration=10)),
            ("character reference", lambda shots: replace(
                shots, 0, character_keys=["missing-character"]
            )),
            ("dialogue reference", lambda shots: replace(
                shots, 0, dialogue_line_ids=["missing-dialogue"]
            )),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                project = storyboard_project()
                self._assert_content_rejected_without_side_effects(
                    project, lambda before: short_drama.update_shots(
                        self.db, "alice", project["id"], before["revision"],
                        mutate(project["shots"]),
                    )
                )

    def test_null_content_sections_are_rejected_atomically_in_their_review_stage(self):
        cases = (
            ("characters", ()),
            ("script", ("characters_review",)),
            ("shots", ("characters_review", "script_review")),
        )
        for section, confirmations in cases:
            with self.subTest(section=section):
                project = self.applied_project()
                for stage in confirmations:
                    project = short_drama.confirm_stage(
                        self.db, "alice", project["id"], project["revision"], stage
                    )
                self._assert_content_rejected_without_side_effects(
                    project, lambda before, section=section: short_drama.update_project(
                        self.db, "alice", project["id"], before["revision"], {section: None}
                    )
                )

    def test_character_and_dialogue_limits_are_enforced_atomically(self):
        project = self.applied_project()
        characters = []
        for index in range(short_drama.MAX_CHARACTERS_PER_PROJECT + 1):
            character = dict(project["characters"][0])
            character.update({
                "character_key": "character-%s" % index,
                "name": "角色%s" % index,
                "source_type": "ai_character",
                "avatar_id": None,
            })
            characters.append(character)
        self._assert_content_rejected_without_side_effects(
            project, lambda before: short_drama.update_characters(
                self.db, "alice", project["id"], before["revision"], characters
            )
        )

        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        script = dict(project["script_versions"][-1])
        script["dialogue_lines"] = [{
            "id": "line-%s" % index,
            "character_key": project["characters"][0]["character_key"],
            "text": "台词%s" % index,
        } for index in range(short_drama.MAX_DIALOGUE_LINES_PER_SCRIPT + 1)]
        self._assert_content_rejected_without_side_effects(
            project, lambda before: short_drama.update_script(
                self.db, "alice", project["id"], before["revision"], script
            )
        )

    def test_script_version_limit_prevents_unbounded_history(self):
        project = self.applied_project()
        project = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        for index in range(1, short_drama.MAX_SCRIPT_VERSIONS_PER_PROJECT):
            script = dict(project["script_versions"][-1], title="版本%s" % (index + 1))
            project = short_drama.update_script(
                self.db, "alice", project["id"], project["revision"], script
            )
        self.assertEqual(
            short_drama.MAX_SCRIPT_VERSIONS_PER_PROJECT,
            len(project["script_versions"]),
        )

        script = dict(project["script_versions"][-1], title="超过上限")
        self._assert_content_rejected_without_side_effects(
            project, lambda before: short_drama.update_script(
                self.db, "alice", project["id"], before["revision"], script
            )
        )

    def test_create_get_and_list_are_owner_scoped(self):
        created = short_drama.create_project(self.db, "alice", {
            "title": "雨夜来客", "synopsis": "陌生女孩敲开侦探的门",
            "ratio": "9:16", "target_duration": 30, "shot_count": 6,
            "visual_style": "电影写实", "point_budget": 1400,
        })
        self.assertEqual(created["revision"], 1)
        self.assertEqual(short_drama.get_project(self.db, "alice", created["id"])["title"], "雨夜来客")
        listed = short_drama.list_projects(self.db, "alice")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(len(listed["items"]), 1)
        with self.assertRaises(LookupError):
            short_drama.get_project(self.db, "bob", created["id"])

    def test_update_rejects_stale_revision(self):
        project = short_drama.create_project(self.db, "alice", {
            "title": "旧标题", "synopsis": "足够长的故事梗概", "ratio": "16:9",
            "target_duration": 45, "shot_count": 8, "visual_style": "电影写实",
        })
        updated = short_drama.update_project(self.db, "alice", project["id"], 1, {"title": "新标题"})
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama.update_project(self.db, "alice", project["id"], 1, {"title": "冲突标题"})

    def test_mutations_are_owner_scoped(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        with self.assertRaises(LookupError):
            short_drama.update_project(self.db, "bob", project["id"], project["revision"], {"title": "夺取"})
        with self.assertRaises(LookupError):
            short_drama.apply_plan(
                self.db, "bob", project["id"], project["revision"],
                valid_raw_plan(), planning_cost=3, planning_job_id=100,
            )
        with self.assertRaises(LookupError):
            short_drama.confirm_stage(
                self.db, "bob", project["id"], project["revision"], "characters_review"
            )
        applied = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_raw_plan(), planning_cost=3, planning_job_id=101,
        )
        with self.assertRaises(LookupError):
            short_drama.apply_plan(
                self.db, "bob", project["id"], applied["revision"],
                valid_raw_plan(), planning_cost=3, planning_job_id=101,
            )

    def test_apply_and_confirm_reject_stale_revisions(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        project = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"title": "新版"}
        )
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama.apply_plan(
                self.db, "alice", project["id"], 1,
                valid_raw_plan(), planning_cost=3, planning_job_id=100,
            )
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_raw_plan(), planning_cost=3, planning_job_id=101,
        )
        with self.assertRaises(short_drama.RevisionConflict):
            short_drama.confirm_stage(
                self.db, "alice", project["id"], project["revision"] - 1, "characters_review"
            )

    def test_stage_transitions_cannot_skip_forward(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        project = short_drama.apply_plan(
            self.db, "alice", project["id"], project["revision"],
            valid_raw_plan(), planning_cost=3, planning_job_id=101,
        )
        assert project["stage"] == "characters_review"
        with self.assertRaisesRegex(ValueError, "不能跳过"):
            short_drama.confirm_stage(self.db, "alice", project["id"], project["revision"], "script_review")
        confirmed = short_drama.confirm_stage(
            self.db, "alice", project["id"], project["revision"], "characters_review"
        )
        self.assertEqual(confirmed["stage"], "script_review")

    def test_validation_rejects_unsupported_duration_ratio_and_shot_count(self):
        base = {"title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
                "target_duration": 30, "shot_count": 6, "visual_style": "写实"}
        for patch in ({"ratio": "1:1"}, {"target_duration": 20}, {"shot_count": 11}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                short_drama.create_project(self.db, "alice", dict(base, **patch))

    def test_validation_rejects_coercive_or_container_project_settings(self):
        cases = (
            {"ratio": []}, {"ratio": False},
            {"target_duration": []}, {"target_duration": True},
            {"target_duration": 30.0}, {"target_duration": "30"},
            {"shot_count": {}}, {"shot_count": True},
            {"shot_count": 6.0}, {"shot_count": "6"},
        )
        for patch in cases:
            with self.subTest(create=patch), self.assertRaises(ValueError):
                short_drama.create_project(self.db, "alice", valid_project(**patch))

        project = short_drama.create_project(self.db, "alice", valid_project())
        for patch in cases:
            with self.subTest(update=patch), self.assertRaises(ValueError):
                short_drama.update_project(
                    self.db, "alice", project["id"], project["revision"], patch
                )

    def test_concurrent_apply_plan_returns_one_success_and_one_domain_conflict(self):
        project = short_drama.create_project(self.db, "alice", valid_project())
        with closing(self.db()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        begin_barrier = threading.Barrier(2)
        read_barrier = threading.Barrier(2)

        class RacingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                normalized = " ".join(sql.split())
                cursor = super().execute(sql, parameters)
                if normalized == "BEGIN IMMEDIATE":
                    self.serialized = True
                elif normalized == "BEGIN":
                    begin_barrier.wait(timeout=3)
                elif (normalized.startswith(
                        "SELECT project_id, username FROM short_drama_applied_jobs")
                      and not getattr(self, "serialized", False)):
                    read_barrier.wait(timeout=3)
                return cursor

        def racing_db():
            return sqlite3.connect(self.path, timeout=5, factory=RacingConnection)

        outcomes = []

        def apply():
            try:
                outcomes.append(short_drama.apply_plan(
                    racing_db, "alice", project["id"], project["revision"],
                    valid_raw_plan(), planning_cost=3, planning_job_id=501,
                ))
            except Exception as error:
                outcomes.append(error)

        threads = [threading.Thread(target=apply) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        successes = [result for result in outcomes if isinstance(result, dict)]
        conflicts = [result for result in outcomes if isinstance(
            result, (short_drama.AppliedJobConflict, short_drama.RevisionConflict)
        )]
        raw_sqlite_errors = [result for result in outcomes if isinstance(result, sqlite3.Error)]
        self.assertEqual(1, len(successes), outcomes)
        self.assertEqual(1, len(conflicts), outcomes)
        self.assertEqual([], raw_sqlite_errors)

    def test_apply_plan_rejects_shot_counts_outside_project_limits(self):
        payload = {"title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
                   "target_duration": 30, "shot_count": 6, "visual_style": "写实"}
        for index, count in enumerate((5, 11), start=1):
            with self.subTest(count=count):
                project = short_drama.create_project(self.db, "alice", payload)
                self._assert_plan_rejected_without_side_effects(
                    project, self._plan(count), 900 + index
                )

    def test_apply_plan_rejects_duration_total_different_from_project_target(self):
        project = short_drama.create_project(self.db, "alice", {
            "title": "短剧", "synopsis": "足够长的故事梗概", "ratio": "9:16",
            "target_duration": 45, "shot_count": 6, "visual_style": "写实",
        })
        self._assert_plan_rejected_without_side_effects(project, self._plan(6), 999)

    def test_non_draft_projects_lock_planning_spec_settings_but_allow_budget_changes(self):
        locked_fields = {
            "synopsis": "修改后的故事梗概足够长",
            "ratio": "16:9",
            "target_duration": 45,
            "shot_count": 7,
            "visual_style": "赛博朋克",
            "target_platform": "视频号",
        }
        for field, value in locked_fields.items():
            with self.subTest(field=field):
                project = self.applied_project()
                with self.assertRaisesRegex(ValueError, "策划|阶段|修改"):
                    short_drama.update_project(
                        self.db, "alice", project["id"], project["revision"], {field: value}
                    )
        project = self.applied_project()
        updated = short_drama.update_project(
            self.db, "alice", project["id"], project["revision"], {"point_budget": 2200}
        )
        self.assertEqual(2200, updated["point_budget"])


class ShortDramaRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.originals = {
            "JOB_DB": core.JOB_DB,
            "AUDIO_DB": core.AUDIO_DB,
            "verify": core.verify,
            "_domains": core._domains,
            "HANDLERS": core.HANDLERS,
            "feature_init_db": core.feature_flags.init_db,
            "init_audio_db": core.init_audio_db,
            "canvas_access": core._short_drama_canvas_access,
        }
        core.JOB_DB = str(Path(self.tmp.name) / "content.db")
        core.AUDIO_DB = str(Path(self.tmp.name) / "audio.db")
        core.verify = lambda token: (
            {"username": token, "must_change": token == "locked"} if token else None
        )
        class FakePoints:
            class AuthPointsError(Exception):
                status = 402
                detail = "点数不足"

            def __init__(self):
                self.cost_calls = []
                self.cost_lock_states = []
                self.deduct_calls = []
                self.refund_calls = []
                self.refund_failures = 0

            def cost_of(self, kind, body):
                self.cost_calls.append((kind, dict(body)))
                self.cost_lock_states.append(core._submission_lock.locked())
                return 7

            def deduct_points(self, username, cost, reason, transaction_key=None):
                self.deduct_calls.append((username, cost, reason, transaction_key))
                return 100 - cost

            def refund_points(self, username, cost, reason, transaction_key=None):
                self.refund_calls.append((username, cost, reason, transaction_key))
                if self.refund_failures:
                    self.refund_failures -= 1
                    raise ConnectionError("refund response unavailable")
                return 100

        self.points = FakePoints()
        core._domains = lambda: (None, self.points, video)
        core.HANDLERS = dict(core.HANDLERS, copy=lambda payload: payload)
        core.feature_flags.init_db = lambda: None
        core.init_audio_db = lambda: None
        core.init_db()
        with closing(core.adb()) as db:
            db.execute("""CREATE TABLE avatars(
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, name TEXT NOT NULL,
                image_file TEXT NOT NULL, provider_avatar_id TEXT NOT NULL,
                provider_avatar_group_id TEXT, status TEXT NOT NULL DEFAULT 'ready',
                created_at INTEGER, updated_at INTEGER, UNIQUE(username, provider_avatar_id)
            )""")
            db.executemany(
                "INSERT INTO avatars(username,name,image_file,provider_avatar_id,status) VALUES(?,?,?,?,?)",
                (("alice", "我的形象", "alice.png", "look-alice", "ready"),
                 ("bob", "他人形象", "bob.png", "look-bob", "ready"),
                 ("alice", "已删除形象", "deleted.png", "look-deleted", "deleted")),
            )
            db.commit()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        core.JOB_DB = self.originals["JOB_DB"]
        core.AUDIO_DB = self.originals["AUDIO_DB"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["_domains"]
        core.HANDLERS = self.originals["HANDLERS"]
        core.feature_flags.init_db = self.originals["feature_init_db"]
        core.init_audio_db = self.originals["init_audio_db"]
        core._short_drama_canvas_access = self.originals["canvas_access"]
        self.tmp.cleanup()

    def request(self, method, path, username="alice", body=None, raw_body=None,
                board_id=None, idempotency_key=None):
        data = raw_body if raw_body is not None else (
            None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )
        headers = {"Content-Type": "application/json"}
        if username:
            headers["Authorization"] = "Bearer " + username
        if board_id:
            headers["X-Canvas-Board-Id"] = board_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def insert_job(self, username="alice", *, kind="copy", status="done", mode="short_drama", cost=3,
                   result_json=None, plan=None, project=None, payload=None, result_changes=None):
        if project is not None:
            payload = payload or bound_planning_payload(project)
            result_value = bound_planning_result(project, plan=plan)
            result_value["mode"] = mode
            result_value.update(result_changes or {})
        else:
            payload = payload or {}
            result_value = {"mode": mode, "plan": plan or valid_raw_plan()}
        result = result_json if result_json is not None else json.dumps(
            result_value, ensure_ascii=False
        )
        with closing(core.jdb()) as db:
            cursor = db.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,result,created_at,updated_at,owner) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, username, cost, status, json.dumps(payload, ensure_ascii=False), result,
                 1, 1, "content"),
            )
            db.commit()
            return cursor.lastrowid

    def applied_project(self):
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project()
        )
        self.assertEqual(200, status)
        job_id = self.insert_job(plan=valid_editable_plan(), project=project)
        status, project = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        self.assertEqual(200, status)
        return project

    def confirm(self, project, stage):
        status, confirmed = self.request("POST", "/api/gen/short-drama/confirm", body={
            "project_id": project["id"], "revision": project["revision"], "stage": stage,
        })
        self.assertEqual(200, status)
        return confirmed

    def project_path(self, project):
        return "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project["id"]})

    def enable_board_roles(self, roles):
        core._short_drama_canvas_access = lambda handler: ({
            "board_id": handler.headers.get("X-Canvas-Board-Id"),
            "role": roles.get(handler._token()),
        } if roles.get(handler._token()) else None)

    def finish_planning_job(self, job_id, project):
        with closing(core.jdb()) as db:
            db.execute(
                "UPDATE jobs SET status='done', result=? WHERE id=?",
                (json.dumps(bound_planning_result(
                    project, plan=valid_editable_plan()), ensure_ascii=False), job_id),
            )
            db.commit()

    def test_board_owner_and_editor_can_complete_planning_for_each_others_projects(self):
        roles = {"owner": "owner", "editor": "editor"}
        self.enable_board_roles(roles)

        for creator, operator in (("owner", "editor"), ("editor", "owner")):
            with self.subTest(creator=creator, operator=operator):
                board_id = "board-%s" % creator
                status, project = self.request(
                    "POST", "/api/gen/short-drama/projects", username=creator,
                    board_id=board_id, body=valid_project(board_id=board_id),
                )
                self.assertEqual(200, status)

                status, fetched = self.request(
                    "GET", self.project_path(project), username=operator, board_id=board_id,
                )
                self.assertEqual(200, status)
                self.assertEqual(creator, fetched["username"])

                status, accepted = self.request(
                    "POST", "/api/gen/copy", username=operator, board_id=board_id,
                    body=bound_planning_payload(project),
                )
                self.assertEqual(200, status, accepted)
                self.assertEqual(operator, self.points.deduct_calls[-1][0])
                self.finish_planning_job(accepted["job_id"], project)

                recovery_path = "/api/gen/short-drama/planning-job?" + urllib.parse.urlencode({
                    "project_id": project["id"],
                })
                status, recovered = self.request(
                    "GET", recovery_path, username=operator, board_id=board_id,
                )
                self.assertEqual(200, status)
                self.assertEqual(accepted["job_id"], recovered["job_id"])

                status, applied = self.request(
                    "POST", "/api/gen/short-drama/apply-plan", username=operator,
                    board_id=board_id, body={
                        "project_id": project["id"], "revision": project["revision"],
                        "job_id": accepted["job_id"],
                    },
                )
                self.assertEqual(200, status)
                status, confirmed = self.request(
                    "POST", "/api/gen/short-drama/confirm", username=operator,
                    board_id=board_id, body={
                        "project_id": project["id"], "revision": applied["revision"],
                        "stage": "characters_review",
                    },
                )
                self.assertEqual(200, status)
                self.assertEqual("script_review", confirmed["stage"])

    def test_planning_routes_recheck_viewer_demotion_and_removal(self):
        roles = {"owner": "owner", "editor": "editor", "viewer": "viewer"}
        self.enable_board_roles(roles)
        board_id = "board-access"
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", username="editor", board_id=board_id,
            body=valid_project(board_id=board_id),
        )
        self.assertEqual(200, status)
        recovery_path = "/api/gen/short-drama/planning-job?" + urllib.parse.urlencode({
            "project_id": project["id"],
        })

        viewer_read, _ = self.request(
            "GET", self.project_path(project), username="viewer", board_id=board_id,
        )
        viewer_recovery, _ = self.request(
            "GET", recovery_path, username="viewer", board_id=board_id,
        )
        viewer_submit, _ = self.request(
            "POST", "/api/gen/copy", username="viewer", board_id=board_id,
            body=bound_planning_payload(project),
        )
        viewer_job = self.insert_job(username="viewer", project=project)
        viewer_apply, _ = self.request(
            "POST", "/api/gen/short-drama/apply-plan", username="viewer", board_id=board_id,
            body={
                "project_id": project["id"], "revision": project["revision"],
                "job_id": viewer_job,
            },
        )
        viewer_confirm, _ = self.request(
            "POST", "/api/gen/short-drama/confirm", username="viewer", board_id=board_id,
            body={
                "project_id": project["id"], "revision": project["revision"],
                "stage": "characters_review",
            },
        )
        self.assertEqual(200, viewer_read)
        self.assertEqual(200, viewer_recovery)
        self.assertEqual(403, viewer_submit)
        self.assertEqual(403, viewer_apply)
        self.assertEqual(403, viewer_confirm)

        status, accepted = self.request(
            "POST", "/api/gen/copy", username="editor", board_id=board_id,
            body=bound_planning_payload(project),
        )
        self.assertEqual(200, status)
        self.finish_planning_job(accepted["job_id"], project)
        roles["editor"] = "viewer"
        demoted_submit, _ = self.request(
            "POST", "/api/gen/copy", username="editor", board_id=board_id,
            body=bound_planning_payload(project),
        )
        demoted_apply, _ = self.request(
            "POST", "/api/gen/short-drama/apply-plan", username="editor", board_id=board_id,
            body={
                "project_id": project["id"], "revision": project["revision"],
                "job_id": accepted["job_id"],
            },
        )
        demoted_confirm, _ = self.request(
            "POST", "/api/gen/short-drama/confirm", username="editor", board_id=board_id,
            body={
                "project_id": project["id"], "revision": project["revision"],
                "stage": "characters_review",
            },
        )
        self.assertEqual(403, demoted_submit)
        self.assertEqual(403, demoted_apply)
        self.assertEqual(403, demoted_confirm)

        roles.pop("editor")
        removed_read, _ = self.request(
            "GET", self.project_path(project), username="editor", board_id=board_id,
        )
        removed_recovery, _ = self.request(
            "GET", recovery_path, username="editor", board_id=board_id,
        )
        self.assertEqual(404, removed_read)
        self.assertEqual(404, removed_recovery)

    def test_review_content_puts_persist_without_jobs_or_points(self):
        project = self.applied_project()
        with closing(core.jdb()) as db:
            jobs_before = tuple(db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM jobs"
            ).fetchone())
        spent_before = project["spent_points"]
        path = self.project_path(project)

        characters = [dict(character, name=character["name"] + "（编辑）")
                      for character in project["characters"]]
        status, project = self.request("PUT", path, body={
            "revision": project["revision"], "characters": characters,
        })
        self.assertEqual(200, status)
        self.assertEqual(3, project["revision"])

        project = self.confirm(project, "characters_review")
        script = dict(project["script_versions"][-1], ending="HTTP 新结尾")
        status, project = self.request("PUT", path, body={
            "revision": project["revision"], "script": script,
        })
        self.assertEqual(200, status)
        self.assertEqual(5, project["revision"])

        project = self.confirm(project, "script_review")
        shots = [dict(shot, scene_description="HTTP 编辑场景") for shot in project["shots"]]
        status, project = self.request("PUT", path, body={
            "revision": project["revision"], "shots": shots,
        })
        self.assertEqual(200, status)
        self.assertEqual(7, project["revision"])

        status, fetched = self.request("GET", path)
        self.assertEqual(200, status)
        self.assertEqual(project, fetched)
        self.assertEqual(spent_before, fetched["spent_points"])
        with closing(core.jdb()) as db:
            jobs_after = tuple(db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM jobs"
            ).fetchone())
        self.assertEqual(jobs_before, jobs_after)

    def test_character_reference_route_persists_project_link_and_recovers(self):
        project = self.applied_project()
        character_key = project["characters"][0]["character_key"]
        body = {
            "project_id": project["id"],
            "revision": project["revision"],
            "character_key": character_key,
        }
        status, accepted = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            body=body, idempotency_key="character-http-1",
        )
        self.assertEqual(200, status)
        self.assertFalse(accepted["replayed"])
        with closing(core.jdb()) as db:
            link = db.execute(
                "SELECT project_id,character_key,job_id,cost,status "
                "FROM short_drama_character_reference_jobs"
            ).fetchone()
        self.assertEqual(
            (project["id"], character_key, accepted["job_id"], 7, "linked"),
            tuple(link),
        )

        status, recovered = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            body=body, idempotency_key="new-browser-key",
        )
        self.assertEqual(200, status)
        self.assertTrue(recovered["replayed"])
        self.assertEqual(accepted["job_id"], recovered["job_id"])
        self.assertEqual(1, len(self.points.deduct_calls))
        with closing(core.jdb()) as db:
            db.execute(
                "UPDATE jobs SET status='error',refunded=1 WHERE id=?",
                (accepted["job_id"],),
            )
            db.execute(
                "UPDATE short_drama_character_reference_jobs SET status='failed' "
                "WHERE job_id=?",
                (accepted["job_id"],),
            )
            db.commit()
        with core._job_queue_lock:
            core._queued_job_ids.discard(accepted["job_id"])

    def test_character_reference_is_globally_exclusive_across_collaborators(self):
        self.enable_board_roles({"alice": "owner", "bob": "editor"})
        board_id = "shared-character-board"
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", username="alice",
            board_id=board_id, body=valid_project(board_id=board_id),
        )
        self.assertEqual(200, status)
        plan_job = self.insert_job(username="alice", plan=valid_editable_plan(), project=project)
        status, project = self.request(
            "POST", "/api/gen/short-drama/apply-plan", username="alice",
            board_id=board_id, body={
                "project_id": project["id"], "revision": project["revision"],
                "job_id": plan_job,
            },
        )
        self.assertEqual(200, status)
        body = {
            "project_id": project["id"],
            "revision": project["revision"],
            "character_key": project["characters"][0]["character_key"],
        }
        status, accepted = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            username="alice", board_id=board_id, body=body,
            idempotency_key="alice-character-active",
        )
        self.assertEqual(200, status)
        status, blocked = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            username="bob", board_id=board_id, body=body,
            idempotency_key="bob-character-active",
        )
        self.assertEqual(409, status)
        self.assertEqual("character_reference_in_progress", blocked["code"])
        self.assertEqual(1, len(self.points.deduct_calls))
        with closing(core.jdb()) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM short_drama_character_reference_jobs "
                "WHERE project_id=? AND character_key=?",
                (project["id"], body["character_key"]),
            ).fetchone()[0])
            db.execute(
                "UPDATE jobs SET status='error',refunded=1 WHERE id=?",
                (accepted["job_id"],),
            )
            db.execute(
                "UPDATE short_drama_character_reference_jobs SET status='failed' "
                "WHERE job_id=?",
                (accepted["job_id"],),
            )
            db.execute(
                "UPDATE short_drama_character_reference_attempts SET state='refunded' "
                "WHERE job_id=?",
                (accepted["job_id"],),
            )
            db.commit()
        with core._job_queue_lock:
            core._queued_job_ids.discard(accepted["job_id"])

    def test_refunded_character_attempt_cannot_create_a_free_job_on_retry(self):
        project = self.applied_project()
        body = {
            "project_id": project["id"],
            "revision": project["revision"],
            "character_key": project["characters"][0]["character_key"],
        }
        with patch.object(
                core.jobs_store, "create_job_after_charge",
                side_effect=RuntimeError("association failed")):
            status, failed = self.request(
                "POST", "/api/gen/short-drama/generate-character-reference",
                body=body, idempotency_key="character-compensated",
            )
        self.assertEqual(500, status)
        self.assertEqual("character_reference_create_failed", failed["code"])
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        self.assertNotEqual(
            self.points.deduct_calls[0][3], self.points.refund_calls[0][3]
        )

        status, replay = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            body=body, idempotency_key="character-compensated",
        )
        self.assertEqual(409, status)
        self.assertEqual("character_reference_terminal", replay["code"])
        self.assertEqual(1, len(self.points.deduct_calls))
        self.assertEqual(1, len(self.points.refund_calls))
        with closing(core.jdb()) as db:
            attempt = db.execute(
                "SELECT state,job_id FROM short_drama_character_reference_attempts "
                "WHERE username='alice' AND idempotency_key='character-compensated'"
            ).fetchone()
            self.assertEqual(("refunded", None), tuple(attempt))
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM short_drama_character_reference_jobs "
                "WHERE project_id=? AND character_key=?",
                (project["id"], body["character_key"]),
            ).fetchone()[0])

    def test_character_reference_idempotency_binds_project_revision(self):
        project = self.applied_project()
        body = {
            "project_id": project["id"],
            "revision": project["revision"],
            "character_key": project["characters"][0]["character_key"],
        }
        status, accepted = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            body=body, idempotency_key="character-revision-bound",
        )
        self.assertEqual(200, status)
        status, conflict = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            body=dict(body, revision=body["revision"] + 1),
            idempotency_key="character-revision-bound",
        )
        self.assertEqual(409, status)
        self.assertEqual("idempotency_conflict", conflict["code"])
        self.assertEqual(1, len(self.points.deduct_calls))
        with closing(core.jdb()) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM short_drama_character_reference_jobs "
                "WHERE job_id=?",
                (accepted["job_id"],),
            ).fetchone()[0])
            db.execute(
                "UPDATE jobs SET status='error',refunded=1 WHERE id=?",
                (accepted["job_id"],),
            )
            db.execute(
                "UPDATE short_drama_character_reference_jobs SET status='failed' "
                "WHERE job_id=?",
                (accepted["job_id"],),
            )
            db.execute(
                "UPDATE short_drama_character_reference_attempts SET state='refunded' "
                "WHERE job_id=?",
                (accepted["job_id"],),
            )
            db.commit()
        with core._job_queue_lock:
            core._queued_job_ids.discard(accepted["job_id"])

    def test_generic_refund_scanner_excludes_character_attempt_owned_job(self):
        project = self.applied_project()
        body = {
            "project_id": project["id"],
            "revision": project["revision"],
            "character_key": project["characters"][0]["character_key"],
        }
        status, accepted = self.request(
            "POST", "/api/gen/short-drama/generate-character-reference",
            body=body, idempotency_key="character-refund-owner",
        )
        self.assertEqual(200, status)
        claimed = short_drama.fail_linked_character_reference_job(
            core.jdb, accepted["job_id"], "provider failed",
            from_states=("pending",),
        )
        self.assertTrue(claimed["claimed"])
        attempt = short_drama.get_character_reference_attempt(
            core.jdb, "alice", "character-refund-owner"
        )
        self.assertEqual("refund_pending", attempt["state"])

        self.points.refund_failures = 1
        attempt = short_drama.reconcile_character_reference_refund(
            core.jdb, self.points, attempt
        )
        self.assertEqual("refund_pending", attempt["state"])
        character_refund_key = attempt["refund_key"]
        self.assertEqual(character_refund_key, self.points.refund_calls[-1][3])

        generic_refunds = core.jobs_store.retry_failed_refunds(
            core.jdb, core._refund_once
        )
        self.assertEqual(0, generic_refunds)
        self.assertEqual(1, len(self.points.refund_calls))
        with closing(core.jdb()) as db:
            self.assertEqual(2, db.execute(
                "SELECT refunded FROM jobs WHERE id=?",
                (accepted["job_id"],),
            ).fetchone()[0])

        attempt = short_drama.reconcile_character_reference_refund(
            core.jdb, self.points, attempt
        )
        self.assertEqual("refunded", attempt["state"])
        self.assertEqual(
            [character_refund_key, character_refund_key],
            [call[3] for call in self.points.refund_calls],
        )
        self.assertFalse(any(
            str(call[3]).startswith("job-refund:")
            for call in self.points.refund_calls
        ))
        with closing(core.jdb()) as db:
            self.assertEqual(1, db.execute(
                "SELECT refunded FROM jobs WHERE id=?",
                (accepted["job_id"],),
            ).fetchone()[0])
        with core._job_queue_lock:
            core._queued_job_ids.discard(accepted["job_id"])

    def test_review_content_put_rejections_follow_http_contract(self):
        status, _ = self.request(
            "PUT", "/api/gen/short-drama/project?id=missing",
            username=None, raw_body=b"{malformed",
        )
        self.assertEqual(401, status)

        project = self.applied_project()
        path = self.project_path(project)
        status, _ = self.request("PUT", path, body={
            "revision": project["revision"], "script": project["script_versions"][-1],
        })
        self.assertEqual(400, status)

        status, conflict = self.request("PUT", path, body={
            "revision": project["revision"] - 1, "characters": project["characters"],
        })
        self.assertEqual(409, status)
        self.assertEqual("revision_conflict", conflict["code"])

        status, _ = self.request("PUT", path, body={
            "revision": project["revision"],
            "characters": project["characters"], "script": project["script_versions"][-1],
        })
        self.assertEqual(400, status)

    def test_review_content_put_rejects_null_sections_without_mutation(self):
        cases = (
            ("characters", ()),
            ("script", ("characters_review",)),
            ("shots", ("characters_review", "script_review")),
        )
        for section, confirmations in cases:
            with self.subTest(section=section):
                project = self.applied_project()
                for stage in confirmations:
                    project = self.confirm(project, stage)
                path = self.project_path(project)
                before = project
                status, _ = self.request("PUT", path, body={
                    "revision": before["revision"], section: None,
                })
                self.assertEqual(400, status)
                status, project = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertEqual(before, project)

    def test_core_dispatches_all_short_drama_routes_declared_by_domain(self):
        core_source = Path(core.__file__).read_text(encoding="utf-8")
        domain_source = Path(short_drama.__file__).read_text(encoding="utf-8")
        for route in (
            "/api/gen/short-drama/projects",
            "/api/gen/short-drama/project",
            "/api/gen/short-drama/apply-plan",
            "/api/gen/short-drama/confirm",
            "/api/gen/short-drama/planning-quote",
        ):
            with self.subTest(route=route):
                self.assertIn(route, core_source + domain_source)
        self.assertIn('_short_drama_domain().dispatch_http', core_source)
        self.assertNotIn('HANDLERS["short-drama"]', core_source)

    def test_planning_quote_is_authenticated_free_and_uses_copy_price_domain(self):
        status, _ = self.request("GET", "/api/gen/short-drama/planning-quote", username=None)
        self.assertEqual(401, status)

        status, quote = self.request("GET", "/api/gen/short-drama/planning-quote")
        self.assertEqual(200, status)
        self.assertEqual({"cost": 7}, quote)
        self.assertEqual([("copy", {"format": "short_drama"})], self.points.cost_calls)
        self.assertEqual([], self.points.deduct_calls)

    def test_short_drama_submission_requires_owned_draft_project_and_exact_snapshot_before_cost(self):
        status, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        self.assertEqual(200, status)
        invalid_payloads = []
        for missing in ("project_id", "project_revision"):
            payload = bound_planning_payload(project)
            payload.pop(missing)
            invalid_payloads.append(payload)
        invalid_payloads.extend((
            bound_planning_payload(project, ratio="16:9"),
            bound_planning_payload(project, dur="45s"),
            bound_planning_payload(project, shot_count=7),
            bound_planning_payload(project, prompt="另一个足够长的故事梗概"),
        ))
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, _ = self.request("POST", "/api/gen/copy", body=payload)
                self.assertEqual(400, status)

        other_status, _ = self.request(
            "POST", "/api/gen/copy", username="bob", body=bound_planning_payload(project)
        )
        self.assertEqual(404, other_status)
        self.assertEqual([], self.points.cost_calls)
        self.assertEqual([], self.points.deduct_calls)
        with closing(core.jdb()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_short_drama_budget_check_is_locked_and_counts_spend_and_outstanding_jobs(self):
        status, limited = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project(point_budget=6)
        )
        self.assertEqual(200, status)
        status, rejected = self.request(
            "POST", "/api/gen/copy", body=bound_planning_payload(limited)
        )
        self.assertEqual(400, status)
        self.assertEqual("point_budget_exceeded", rejected["code"])
        self.assertEqual([True], self.points.cost_lock_states)
        self.assertEqual([], self.points.deduct_calls)
        with closing(core.jdb()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

        self.points.cost_calls.clear()
        self.points.cost_lock_states.clear()
        status, unlimited = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project(point_budget=0)
        )
        self.assertEqual(200, status)
        status, accepted = self.request(
            "POST", "/api/gen/copy", body=bound_planning_payload(unlimited)
        )
        self.assertEqual(200, status)
        self.assertEqual(7, accepted["cost"])
        self.assertEqual([True], self.points.cost_lock_states)

        status, outstanding = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project(point_budget=10)
        )
        self.assertEqual(200, status)
        self.insert_job(
            project=outstanding, status="running", cost=7,
            payload=bound_planning_payload(outstanding, prompt="先前不同的规划快照"),
        )
        deductions_before = list(self.points.deduct_calls)
        with closing(core.jdb()) as db:
            jobs_before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        status, rejected = self.request(
            "POST", "/api/gen/copy", body=bound_planning_payload(outstanding)
        )
        self.assertEqual(400, status)
        self.assertEqual("point_budget_exceeded", rejected["code"])
        self.assertEqual(deductions_before, self.points.deduct_calls)
        with closing(core.jdb()) as db:
            self.assertEqual(jobs_before, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_concurrent_identical_planning_posts_replay_one_paid_job_even_with_unlimited_budget(self):
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project(point_budget=0)
        )
        self.assertEqual(200, status)
        payload = bound_planning_payload(project)
        start = threading.Barrier(3)
        outcomes = []

        def submit():
            start.wait(timeout=3)
            outcomes.append(self.request("POST", "/api/gen/copy", body=payload))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual([200, 200], sorted(status for status, _body in outcomes))
        self.assertEqual(1, len({body["job_id"] for _status, body in outcomes}))
        self.assertEqual(1, len(self.points.deduct_calls))
        with closing(core.jdb()) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM jobs WHERE username='alice' AND kind='copy'"
            ).fetchone()[0])

    def test_planning_settings_put_cannot_enter_paid_submission_critical_section(self):
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project(point_budget=0)
        )
        self.assertEqual(200, status)
        entered_cost = threading.Event()
        release_cost = threading.Event()
        put_done = threading.Event()
        outcomes = {}
        original_cost_of = self.points.cost_of

        def blocking_cost(kind, body):
            if kind == "copy" and body.get("format") == "short_drama":
                entered_cost.set()
                if not release_cost.wait(timeout=3):
                    raise AssertionError("test did not release planning cost gate")
            return original_cost_of(kind, body)

        def submit():
            outcomes["post"] = self.request(
                "POST", "/api/gen/copy", body=bound_planning_payload(project)
            )

        def update():
            outcomes["put"] = self.request("PUT", self.project_path(project), body={
                "revision": project["revision"], "synopsis": "锁后更新的全新故事梗概",
            })
            put_done.set()

        self.points.cost_of = blocking_cost
        post_thread = threading.Thread(target=submit)
        put_thread = threading.Thread(target=update)
        try:
            post_thread.start()
            self.assertTrue(entered_cost.wait(timeout=3))
            put_thread.start()
            put_entered_critical_section = put_done.wait(timeout=0.2)
        finally:
            release_cost.set()
            post_thread.join(timeout=5)
            put_thread.join(timeout=5)
            self.points.cost_of = original_cost_of

        self.assertFalse(put_entered_critical_section)
        self.assertEqual(200, outcomes["post"][0])
        self.assertEqual(200, outcomes["put"][0])
        with closing(core.jdb()) as db:
            job_payload = json.loads(db.execute(
                "SELECT payload FROM jobs WHERE id=?", (outcomes["post"][1]["job_id"],)
            ).fetchone()[0])
        self.assertEqual(project["synopsis"], job_payload["prompt"])
        self.assertEqual("锁后更新的全新故事梗概", outcomes["put"][1]["synopsis"])

    def test_paid_planning_job_is_discoverable_after_conflict_and_reapplies_without_new_job(self):
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project()
        )
        self.assertEqual(200, status)
        job_id = self.insert_job(project=project, plan=valid_editable_plan(), cost=7)
        path = self.project_path(project)
        status, changed = self.request("PUT", path, body={
            "revision": project["revision"], "title": "刷新后的标题",
        })
        self.assertEqual(200, status)

        status, conflict = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        self.assertEqual(409, status)
        self.assertEqual("revision_conflict", conflict["code"])
        recovery_path = "/api/gen/short-drama/planning-job?" + urllib.parse.urlencode({
            "project_id": project["id"],
        })
        status, recovered = self.request("GET", recovery_path)
        self.assertEqual(200, status)
        self.assertEqual(job_id, recovered["job_id"])
        status, applied = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": changed["revision"], "job_id": job_id,
        })
        self.assertEqual(200, status)
        self.assertEqual("characters_review", applied["stage"])
        with closing(core.jdb()) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_planning_job_route_rejects_every_current_project_snapshot_drift_then_post_creates_new_job(self):
        changes = (
            ("synopsis", "变化后的另一个完整故事梗概"),
            ("visual_style", "赛博朋克"),
            ("target_platform", "快手"),
            ("ratio", "16:9"),
            ("target_duration", 30),
            ("shot_count", 7),
        )
        for field, value in changes:
            with self.subTest(field=field):
                status, project = self.request(
                    "POST", "/api/gen/short-drama/projects",
                    body=valid_project(target_duration=45, shot_count=6),
                )
                self.assertEqual(200, status)
                old_job_id = self.insert_job(project=project, status="done", cost=7)
                status, changed = self.request("PUT", self.project_path(project), body={
                    "revision": project["revision"], field: value,
                })
                self.assertEqual(200, status)
                recovery_path = "/api/gen/short-drama/planning-job?" + urllib.parse.urlencode({
                    "project_id": project["id"],
                })
                status, recovered = self.request("GET", recovery_path)
                self.assertEqual(200, status)
                self.assertEqual({"job_id": None}, recovered)
                status, accepted = self.request(
                    "POST", "/api/gen/copy", body=bound_planning_payload(changed)
                )
                self.assertEqual(200, status)
                self.assertNotEqual(old_job_id, accepted["job_id"])
                with closing(core.jdb()) as db:
                    db.execute("UPDATE jobs SET status='error' WHERE id=?", (accepted["job_id"],))
                    db.commit()

    def test_apply_rejects_job_project_or_result_snapshot_mismatch_without_mutation(self):
        mismatch_changes = (
            {"project_id": "different-project"},
            {"settings": {"ratio": "16:9", "target_duration": 30, "shot_count": 6}},
            {"project_revision": 999},
            {"ratio": "16:9"},
        )
        for changes in mismatch_changes:
            with self.subTest(changes=changes):
                _, project = self.request(
                    "POST", "/api/gen/short-drama/projects", body=valid_project()
                )
                job_id = self.insert_job(
                    project=project, plan=valid_editable_plan(), result_changes=changes
                )
                status, _ = self.request("POST", "/api/gen/short-drama/apply-plan", body={
                    "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
                })
                self.assertEqual(400, status)
                status, fetched = self.request("GET", self.project_path(project))
                self.assertEqual(200, status)
                self.assertEqual("draft", fetched["stage"])
                # The paid job remains unrefunded even though its result cannot be
                # applied, so the project must show the points already deducted.
                self.assertEqual(3, fetched["spent_points"])

    def test_character_update_rejects_cross_owner_missing_and_deleted_cinematic_avatars(self):
        for avatar_id in (2, 999, 3):
            with self.subTest(avatar_id=avatar_id):
                project = self.applied_project()
                characters = [dict(character) for character in project["characters"]]
                characters[0].update({
                    "source_type": "cinematic_avatar", "avatar_id": str(avatar_id),
                })
                status, _ = self.request("PUT", self.project_path(project), body={
                    "revision": project["revision"], "characters": characters,
                })
                self.assertEqual(400, status)
        project = self.applied_project()
        characters = [dict(character) for character in project["characters"]]
        characters[0].update({"source_type": "cinematic_avatar", "avatar_id": "1"})
        status, saved = self.request("PUT", self.project_path(project), body={
            "revision": project["revision"], "characters": characters,
        })
        self.assertEqual(200, status)
        self.assertEqual("1", saved["characters"][0]["avatar_id"])

    def test_character_update_requires_ready_avatar_and_clears_ai_binding(self):
        with closing(core.adb()) as db:
            db.execute(
                "INSERT INTO avatars(username,name,image_file,provider_avatar_id,status) "
                "VALUES(?,?,?,?,?)",
                ("alice", "处理中形象", "pending.png", "", "pending"),
            )
            pending_id = db.execute(
                "SELECT id FROM avatars WHERE username='alice' AND name='处理中形象'"
            ).fetchone()[0]
            db.commit()

        project = self.applied_project()
        characters = [dict(character) for character in project["characters"]]
        characters[0].update({
            "source_type": "cinematic_avatar",
            "avatar_id": str(pending_id),
        })
        status, response = self.request(
            "PUT", self.project_path(project), body={
                "revision": project["revision"], "characters": characters,
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("avatar_not_ready", response["code"])

        characters[0].update({
            "source_type": "ai_character",
            "avatar_id": "1",
        })
        status, saved = self.request(
            "PUT", self.project_path(project), body={
                "revision": project["revision"], "characters": characters,
            },
        )
        self.assertEqual(200, status)
        self.assertIsNone(saved["characters"][0]["avatar_id"])

    def test_project_creation_missing_required_settings_returns_http_400(self):
        for missing in ("ratio", "target_duration", "shot_count"):
            with self.subTest(missing=missing):
                body = valid_project()
                body.pop(missing)
                status, _ = self.request("POST", "/api/gen/short-drama/projects", body=body)
                self.assertEqual(400, status)

    def test_cinematic_quote_is_authenticated_free_and_uses_complete_payload(self):
        payload = {
            "cine_mode": "open", "avatar_ids": [1], "prompt": "雨中奔跑",
            "ratio": "9:16", "duration": 5, "resolution": "1080p",
        }
        status, _ = self.request(
            "POST", "/api/gen/cinematic/quote", username=None, body=payload
        )
        self.assertEqual(401, status)
        status, quote = self.request("POST", "/api/gen/cinematic/quote", body=payload)
        self.assertEqual(200, status)
        self.assertEqual({"cost": 7}, quote)
        self.assertEqual([], self.points.deduct_calls)
        self.assertEqual("cinematic", self.points.cost_calls[-1][0])
        quoted_payload = self.points.cost_calls[-1][1]
        self.assertEqual("720p", quoted_payload["resolution"])
        self.assertTrue(all(
            quoted_payload[key] == value
            for key, value in payload.items()
            if key != "resolution"
        ))
        with closing(core.jdb()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_cinematic_quote_cleans_reference_files_on_success_failure_and_cancel_but_paid_keeps_them(self):
        output = Path(self.tmp.name) / "content_out"
        image = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"x" * 32
        ).decode("ascii")
        payload = {
            "cine_mode": "open", "avatar_ids": [1], "prompt": "雨中奔跑",
            "ratio": "9:16", "duration": 5, "resolution": "1080p",
            "reference_images": [image],
        }

        def out_path(relative):
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            return path

        def files():
            return [path for path in output.rglob("*") if path.is_file()]

        class CancelledHandler:
            path = "/api/gen/cinematic/quote"
            def _token(self): return "alice"
            def _json_body_strict(self): return payload
            def _send(self, *_args): raise ConnectionAbortedError("client cancelled")

        with patch.object(video, "_out_path", side_effect=out_path):
            status, _quote = self.request("POST", "/api/gen/cinematic/quote", body=payload)
            self.assertEqual(200, status)
            self.assertEqual([], files())

            original_cost_of = self.points.cost_of
            self.points.cost_of = lambda *_args: (_ for _ in ()).throw(ValueError("quote failed"))
            try:
                status, _error = self.request("POST", "/api/gen/cinematic/quote", body=payload)
            finally:
                self.points.cost_of = original_cost_of
            self.assertEqual(400, status)
            self.assertEqual([], files())

            with self.assertRaises(ConnectionAbortedError):
                video.dispatch_cinematic_quote(CancelledHandler(), core.verify, original_cost_of)
            self.assertEqual([], files())

            original_handlers = core.HANDLERS
            core.HANDLERS = dict(core.HANDLERS, cinematic=lambda value: value)
            try:
                with patch.object(video, "record_video_pending_asset"):
                    status, accepted = self.request("POST", "/api/gen/cinematic", body=payload)
            finally:
                core.HANDLERS = original_handlers
            self.assertEqual(200, status, accepted)
            with closing(core.jdb()) as db:
                persisted = json.loads(db.execute(
                    "SELECT payload FROM jobs WHERE id=?", (accepted["job_id"],)
                ).fetchone()[0])
            self.assertEqual(1, len(persisted["reference_image_files"]))
            self.assertEqual(1, len(files()))

    def test_invalid_short_drama_copy_payload_is_rejected_before_deduct_or_job_insert(self):
        invalid_changes = (
            {"dur": "31s"},
            {"ratio": "1:1"},
            {"shot_count": 5},
            {"dur": "45s", "shot_count": 10},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                body = {
                    "format": "short_drama", "prompt": "足够长的短剧故事需求",
                    "dur": "30s", "ratio": "9:16", "shot_count": 6,
                }
                body.update(changes)
                status, _ = self.request("POST", "/api/gen/copy", body=body)
                self.assertEqual(400, status)

        self.assertEqual([], self.points.deduct_calls)
        with closing(core.jdb()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_project_crud_routes_are_authenticated_and_owner_scoped(self):
        status, _ = self.request("GET", "/api/gen/short-drama/projects", username=None)
        self.assertEqual(401, status)
        status, created = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        self.assertEqual(200, status)
        project_id = created["id"]
        status, listed = self.request("GET", "/api/gen/short-drama/projects")
        self.assertEqual([project_id], [item["id"] for item in listed["items"]])
        status, fetched = self.request(
            "GET", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id})
        )
        self.assertEqual(200, status)
        self.assertEqual(project_id, fetched["id"])
        status, _ = self.request(
            "GET", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id}), username="bob"
        )
        self.assertEqual(404, status)
        status, updated = self.request(
            "PUT", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id}),
            body={"revision": created["revision"], "title": "新版标题"},
        )
        self.assertEqual(200, status)
        self.assertEqual("新版标题", updated["title"])
        status, conflict = self.request(
            "PUT", "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project_id}),
            body={"revision": created["revision"], "title": "过期标题"},
        )
        self.assertEqual(409, status)
        self.assertEqual("revision_conflict", conflict["code"])

    def test_project_routes_enforce_pagination_limit_and_soft_delete(self):
        for query in ("page=0", "page=", "page=abc", "page_size=0", "page_size=51"):
            with self.subTest(query=query):
                status, _ = self.request(
                    "GET", "/api/gen/short-drama/projects?" + query
                )
                self.assertEqual(400, status)

        with patch.dict(os.environ, {"HQ_SHORT_DRAMA_MAX_PROJECTS_PER_USER": "1"}):
            status, created = self.request(
                "POST", "/api/gen/short-drama/projects", body=valid_project()
            )
            self.assertEqual(200, status)
            status, limited = self.request(
                "POST", "/api/gen/short-drama/projects",
                body=valid_project(title="超过上限"),
            )
            self.assertEqual(429, status)
            self.assertEqual("short_drama_project_cap", limited["code"])
            self.assertEqual(1, limited["max_projects"])

            delete_body = {
                "project_id": created["id"], "revision": created["revision"],
            }
            status, _ = self.request(
                "POST", "/api/gen/short-drama/project/delete",
                username="bob", body=delete_body,
            )
            self.assertEqual(404, status)
            status, conflict = self.request(
                "POST", "/api/gen/short-drama/project/delete",
                body={"project_id": created["id"], "revision": created["revision"] + 1},
            )
            self.assertEqual(409, status)
            self.assertEqual("revision_conflict", conflict["code"])
            status, deleted = self.request(
                "POST", "/api/gen/short-drama/project/delete", body=delete_body,
            )
            self.assertEqual(200, status)
            self.assertTrue(deleted["deleted"])
            status, listed = self.request("GET", "/api/gen/short-drama/projects")
            self.assertEqual(200, status)
            self.assertEqual(0, listed["total"])

            status, replacement = self.request(
                "POST", "/api/gen/short-drama/projects",
                body=valid_project(title="释放名额后的项目"),
            )
            self.assertEqual(200, status)
            self.assertEqual("释放名额后的项目", replacement["title"])

    def test_delete_route_serializes_with_submission_and_blocks_unapplied_paid_job(self):
        status, project = self.request(
            "POST", "/api/gen/short-drama/projects", body=valid_project()
        )
        self.assertEqual(200, status)
        job_id = self.insert_job(status="running", project=project)
        lock_states = []
        original_delete = short_drama.delete_project

        def observed_delete(*args, **kwargs):
            lock_states.append(core._submission_lock.locked())
            return original_delete(*args, **kwargs)

        with patch.object(short_drama, "delete_project", side_effect=observed_delete):
            status, blocked = self.request(
                "POST", "/api/gen/short-drama/project/delete",
                body={"project_id": project["id"], "revision": project["revision"]},
            )
        self.assertEqual(409, status)
        self.assertEqual("short_drama_unapplied_paid_job", blocked["code"])
        self.assertEqual([True], lock_states)

        with closing(core.jdb()) as db:
            db.execute("UPDATE jobs SET refunded=1 WHERE id=?", (job_id,))
            db.commit()
        status, deleted = self.request(
            "POST", "/api/gen/short-drama/project/delete",
            body={"project_id": project["id"], "revision": project["revision"]},
        )
        self.assertEqual(200, status)
        self.assertTrue(deleted["deleted"])

    def test_project_routes_reject_malformed_settings_with_http_400(self):
        for patch in ({"ratio": []}, {"target_duration": []}, {"target_duration": "30"},
                      {"shot_count": {}}, {"shot_count": 6.0}):
            with self.subTest(create=patch):
                status, _ = self.request(
                    "POST", "/api/gen/short-drama/projects", body=valid_project(**patch)
                )
                self.assertEqual(400, status)

        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        path = "/api/gen/short-drama/project?" + urllib.parse.urlencode({"id": project["id"]})
        for patch in ({"ratio": []}, {"target_duration": []}, {"target_duration": 30.0},
                      {"shot_count": {}}, {"shot_count": "6"}):
            with self.subTest(update=patch):
                status, _ = self.request(
                    "PUT", path, body={"revision": project["revision"], **patch}
                )
                self.assertEqual(400, status)

    def test_short_drama_routes_authenticate_before_parsing_malformed_json(self):
        cases = (
            ("POST", "/api/gen/short-drama/projects"),
            ("PUT", "/api/gen/short-drama/project?id=missing"),
            ("POST", "/api/gen/short-drama/apply-plan"),
            ("POST", "/api/gen/short-drama/confirm"),
        )
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, _ = self.request(
                    method, path, username=None, raw_body=b"{malformed"
                )
                self.assertEqual(401, status)

    def test_short_drama_routes_require_initial_password_change_before_dispatch(self):
        cases = (
            ("GET", "/api/gen/short-drama/projects", None),
            ("POST", "/api/gen/short-drama/projects", b"{malformed"),
            ("GET", "/api/gen/short-drama/project?id=missing", None),
            ("PUT", "/api/gen/short-drama/project?id=missing", b"{malformed"),
            ("POST", "/api/gen/short-drama/apply-plan", b"{malformed"),
            ("POST", "/api/gen/short-drama/confirm", b"{malformed"),
            ("GET", "/api/gen/short-drama/planning-quote", None),
            ("GET", "/api/gen/short-drama/planning-job?project_id=missing", None),
        )
        for method, path, raw_body in cases:
            with self.subTest(method=method, path=path):
                status, response = self.request(
                    method, path, username="locked", raw_body=raw_body
                )
                self.assertEqual(403, status)
                self.assertIn("修改初始密码", response["detail"])

        self.assertEqual([], self.points.cost_calls)
        self.assertEqual([], self.points.deduct_calls)
        with closing(core.jdb()) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM short_drama_projects"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM short_drama_applied_jobs"
            ).fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_apply_plan_uses_only_owned_completed_copy_job_data(self):
        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        job_id = self.insert_job(cost=3, project=project)
        status, rejected = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
            "plan": {"shots": []}, "cost": 999,
        })
        self.assertEqual(400, status)
        self.assertIn("字段", rejected["detail"])
        status, applied = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        self.assertEqual(200, status)
        self.assertEqual("characters_review", applied["stage"])
        self.assertEqual(3, applied["spent_points"])
        status, stale_duplicate = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        self.assertEqual(409, status)
        self.assertEqual("job_already_applied", stale_duplicate["code"])
        status, duplicate = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": applied["revision"], "job_id": job_id,
        })
        self.assertEqual(409, status)
        self.assertEqual("job_already_applied", duplicate["code"])

        other_job_id = self.insert_job(username="bob", project=project)
        status, _ = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": applied["revision"], "job_id": other_job_id,
        })
        self.assertEqual(404, status)

    def test_apply_plan_rejects_untrusted_job_kind_status_mode_and_result(self):
        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        cases = (
            {"kind": "image"},
            {"status": "running"},
            {"mode": "copy"},
            {"result_json": "{malformed"},
        )
        for options in cases:
            with self.subTest(options=options):
                job_id = self.insert_job(project=project, **options)
                status, _ = self.request("POST", "/api/gen/short-drama/apply-plan", body={
                    "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
                })
                self.assertEqual(400, status)

    def test_confirm_route_enforces_owner_and_stage_order(self):
        _, project = self.request("POST", "/api/gen/short-drama/projects", body=valid_project())
        job_id = self.insert_job(project=project)
        _, project = self.request("POST", "/api/gen/short-drama/apply-plan", body={
            "project_id": project["id"], "revision": project["revision"], "job_id": job_id,
        })
        status, _ = self.request("POST", "/api/gen/short-drama/confirm", username="bob", body={
            "project_id": project["id"], "revision": project["revision"], "stage": "characters_review",
        })
        self.assertEqual(404, status)
        status, skipped = self.request("POST", "/api/gen/short-drama/confirm", body={
            "project_id": project["id"], "revision": project["revision"], "stage": "script_review",
        })
        self.assertEqual(400, status)
        self.assertIn("不能跳过", skipped["detail"])
        status, confirmed = self.request("POST", "/api/gen/short-drama/confirm", body={
            "project_id": project["id"], "revision": project["revision"], "stage": "characters_review",
        })
        self.assertEqual(200, status)
        self.assertEqual("script_review", confirmed["stage"])

    def test_legacy_stage_completion_remains_available_until_d6_is_enabled(self):
        project = self.applied_project()
        with closing(core.jdb()) as db:
            db.execute(
                "UPDATE short_drama_projects SET stage='assembly_review' "
                "WHERE id=?",
                (project["id"],),
            )
            db.commit()
            revision = db.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (project["id"],),
            ).fetchone()[0]
        stage_body = {
            "project_id": project["id"],
            "revision": revision,
            "stage": "assembly_review",
        }
        with patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "0"},
        ):
            status, result = self.request(
                "POST", "/api/gen/short-drama/confirm", body=stage_body,
            )
        self.assertEqual(200, status)
        self.assertEqual("completed", result["stage"])
        with closing(core.jdb()) as db:
            row = db.execute(
                "SELECT stage,completion_id FROM short_drama_projects "
                "WHERE id=?",
                (project["id"],),
            ).fetchone()
        self.assertEqual(("completed", None), tuple(row))

        blocked_project = self.applied_project()
        with closing(core.jdb()) as db:
            db.execute(
                "UPDATE short_drama_projects SET stage='assembly_review' "
                "WHERE id=?", (blocked_project["id"],),
            )
            db.commit()
            blocked_revision = db.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (blocked_project["id"],),
            ).fetchone()[0]
        with patch.dict(
            os.environ, {"HQ_SHORT_DRAMA_COMPLETION_ENABLED": "1"},
        ):
            status, result = self.request(
                "POST", "/api/gen/short-drama/confirm", body={
                    "project_id": blocked_project["id"],
                    "revision": blocked_revision,
                    "stage": "assembly_review",
                },
            )
        self.assertEqual(409, status)
        self.assertEqual("completion_required", result["code"])
