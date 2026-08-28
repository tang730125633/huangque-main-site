import json
import inspect
import os
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
    core,
    pricing,
    short_drama,
    short_drama_autodraft,
    short_drama_formal_renderer,
    short_drama_native_audio,
    short_drama_refinement,
    video,
)


class Handler:
    def __init__(self, path, body=None, key="refinement-test-key", token="alice"):
        self.path = path
        self.body = body
        self.token = token
        self.headers = {"Idempotency-Key": key}
        self.response = None

    def _token(self):
        return self.token

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


class ShortDramaRefinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        self.free = mock.patch.dict(
            os.environ, {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "1",
                "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "demo",
                "CONTENT_OUT": self.tmp.name,
            }
        )
        self.free.start()
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db, "alice", {
                "title": "精修与交付测试",
                "synopsis": "朋友在公园发现一封来自未来的信。",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "电影感写实", "target_platform": "抖音",
                "point_budget": 0,
            },
        )
        now = int(time.time())
        shots = [
            {
                "shot_key": "shot_01", "sort_order": 1, "status": "ready",
                "start_ms": 0, "end_ms": 5000, "issue": None,
            },
            {
                "shot_key": "shot_02", "sort_order": 2, "status": "degraded",
                "start_ms": 5000, "end_ms": 10000,
                "issue": {"code": "safe_visual_fallback", "shot_key": "shot_02"},
            },
        ]
        issues = [{
            "code": "safe_visual_fallback", "shot_key": "shot_02",
            "message": "使用安全替代画面",
        }]
        manifest = {
            "resolution": "720p", "duration_ms": 10000,
            "shots": shots, "issues": issues,
            "media_contract": {
                "contract_version": "short-drama-locked-media-v1",
                "delivery_eligible": True,
                "audio_hash": "audio-hash",
                "subtitle_hash": "subtitle-hash",
                "timeline_hash": "timeline-hash",
                "material_hash": "material-hash",
                "subtitle_required": True,
            },
        }
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_autodraft_versions "
                "(id,project_id,job_id,version,plan_id,status,url,manifest_json,"
                "input_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "draft-v1", self.project["id"], "draft-job", 1, "plan-v1",
                    "degraded", "/assets/meiye_video.mp4",
                    json.dumps(manifest), "draft-hash", now,
                ),
            )
            for index, shot_key in enumerate(("shot_01", "shot_02"), 1):
                relative = "provider/%s-v2.mp4" % shot_key
                path = Path(self.tmp.name) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(("provider-media-%s" % shot_key).encode())
                job_id = "provider-job-%s" % shot_key
                version_id = "provider-version-%s" % shot_key
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_jobs "
                    "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                    "character_key,avatar_id,provider,provider_job_id,status,progress,"
                    "poll_count,input_hash,request_json,cost,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?, 'succeeded',100,1,?,'{}',0,?,?)",
                    (
                        job_id, self.project["id"], "alice", "alice", "plan-v1",
                        shot_key, "lead", "avatar-1", "test_provider",
                        "external-%s" % shot_key, "input-%s" % shot_key, now, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO short_drama_provider_shot_versions "
                    "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                    "status,file,url,input_hash,created_at) "
                    "VALUES (?,?,?,?,2,'test_provider',?,'ready',?,?,?,?)",
                    (
                        version_id, self.project["id"], job_id, shot_key,
                        "external-%s" % shot_key, relative,
                        "/api/gen/file/" + relative, "input-%s" % shot_key, now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        self.real_refinement_renderer = (
            short_drama_refinement._render_refinement_preview
        )
        self.refinement_renderer = mock.patch.object(
            short_drama_refinement, "_render_refinement_preview",
            side_effect=self._fake_refinement_preview,
        )
        self.refinement_renderer_mock = self.refinement_renderer.start()
        self.real_refinement_assembly_status = (
            short_drama_refinement._refinement_assembly_status
        )
        self.complete_assembly = mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            side_effect=self._refinement_assembly_status_fixture,
        )
        self.complete_assembly.start()

    def tearDown(self):
        self.complete_assembly.stop()
        self.refinement_renderer.stop()
        self.free.stop()
        self.tmp.cleanup()

    def _refinement_assembly_status_fixture(self, conn, project, refinement):
        staged = list(
            (refinement.get("media") or {}).get("staged_replacements") or []
        )
        if staged:
            return self.real_refinement_assembly_status(conn, project, refinement)
        return {
            "available": True,
            "reassembly_required": False,
            "staged_replacements": [],
            "staged_count": 0,
            "message": "complete preview fixture",
        }

    def _fake_refinement_preview(self, conn, job, source):
        shots, assets = [], []
        for original in source["shots"]:
            shot = dict(original)
            shot_key = str(shot["shot_key"])
            requested = (
                job["replacement_provider_version_id"]
                if shot_key == job["shot_key"]
                else str(shot.get("provider_version_id") or "")
            )
            asset = short_drama_refinement._provider_asset(
                conn, job["project_id"], shot_key, requested or None,
            )
            file_hash = short_drama_refinement._file_hash(
                Path(self.tmp.name) / asset["file"]
            )
            shot.update({
                "status": "ready", "issue": None,
                "visual_source": (
                    "provider_regeneration"
                    if shot_key == job["shot_key"] else "provider"
                ),
                "provider": asset["provider"],
                "provider_version_id": asset["id"],
                "provider_version": int(asset["version"]),
                "provider_job_id": asset["provider_job_id"],
                "file": asset["file"], "url": asset["url"],
                "file_hash": file_hash, "input_hash": asset["input_hash"],
                "native_media": asset.get("native_media") or {},
            })
            shots.append(shot)
            assets.append({
                "id": asset["id"], "shot_key": shot_key,
                "input_hash": asset["input_hash"], "file_hash": file_hash,
            })
        relative = "refinement/%s/preview-720p.mp4" % job["id"]
        output = Path(self.tmp.name) / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        source_url = str(source.get("url") or "")
        source_file = None
        if source_url.startswith("/api/gen/file/"):
            source_file = Path(self.tmp.name) / source_url.removeprefix(
                "/api/gen/file/"
            )
        if source_file and source_file.is_file():
            shutil.copyfile(source_file, output)
            with output.open("ab") as handle:
                handle.write(("\nrefinement:" + job["id"]).encode())
        else:
            output.write_bytes(("preview:" + job["id"]).encode())
        file_hash = short_drama_refinement._file_hash(output)
        manifest = json.loads(conn.execute(
            "SELECT manifest_json FROM short_drama_autodraft_versions WHERE id=?",
            (source["source_draft_version_id"],),
        ).fetchone()[0])
        media = dict(manifest.get("media_contract") or {})
        media["material_hash"] = short_drama_refinement._hash(assets)
        return {
            "url": "/api/gen/file/" + relative,
            "file": relative, "file_hash": file_hash,
            "probe": {"video": {"width": 1280, "height": 720}, "audio": {}},
            "media_contract": media, "shots": shots,
        }

    def add_provider_replacement(self, shot_key):
        conn = self.db()
        try:
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM "
                "short_drama_provider_shot_versions WHERE project_id=? AND shot_key=?",
                (self.project["id"], shot_key),
            ).fetchone()[0])
            now = int(time.time())
            job_id = "provider-job-%s-%d" % (shot_key, version)
            version_id = "provider-version-%s-%d" % (shot_key, version)
            relative = "provider/%s-v%d.mp4" % (shot_key, version)
            path = Path(self.tmp.name) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("provider-media-%s-v%d" % (shot_key, version)).encode())
            conn.execute(
                "INSERT INTO short_drama_provider_shot_jobs "
                "(id,project_id,owner_username,actor_username,plan_id,shot_key,"
                "character_key,avatar_id,provider,provider_job_id,status,progress,"
                "poll_count,input_hash,request_json,cost,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'succeeded',100,1,?,'{}',0,?,?)",
                (
                    job_id, self.project["id"], "alice", "alice", "plan-v1",
                    shot_key, "lead", "avatar-1", "test_provider",
                    "external-%s-%d" % (shot_key, version),
                    "input-%s-%d" % (shot_key, version), now, now,
                ),
            )
            conn.execute(
                "INSERT INTO short_drama_provider_shot_versions "
                "(id,project_id,job_id,shot_key,version,provider,provider_job_id,"
                "status,file,url,input_hash,created_at) "
                "VALUES (?,?,?,?,?,'test_provider',?,'ready',?,?,?,?)",
                (
                    version_id, self.project["id"], job_id, shot_key, version,
                    "external-%s-%d" % (shot_key, version), relative,
                    "/api/gen/file/" + relative,
                    "input-%s-%d" % (shot_key, version), now,
                ),
            )
            conn.commit()
            return version_id
        finally:
            conn.close()

    def repaired_version(self, key):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            key,
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        return version

    def confirmed_version(self, key):
        version = self.repaired_version(key)
        self.confirm_version(version)
        return version

    def install_mock_native_evidence(self):
        conn = self.db()
        raw_paths = []
        try:
            for shot_key in ("shot_01", "shot_02"):
                raw_relative = "video/%s-raw.mp4" % shot_key
                derived_relative = "video/%s-faststart.mp4" % shot_key
                raw = Path(self.tmp.name) / raw_relative
                derived = Path(self.tmp.name) / derived_relative
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_bytes(("native-raw-" + shot_key).encode())
                derived.write_bytes(("native-derived-" + shot_key).encode())
                raw_hash = short_drama_refinement._file_hash(raw)
                derived_hash = short_drama_refinement._file_hash(derived)
                evidence = {
                    "raw": {
                        "file": raw_relative, "sha256": raw_hash,
                        "size_bytes": raw.stat().st_size,
                    },
                    "derived": {
                        "file": derived_relative, "sha256": derived_hash,
                        "size_bytes": derived.stat().st_size,
                        "derived_from_sha256": raw_hash,
                    },
                    "resolution": {"width": 2560, "height": 1440},
                    "audio": {
                        "audible": True, "codec": "aac", "sample_rate": 48000,
                        "channels": 2, "mean_volume_dbfs": -21.0,
                        "max_volume_dbfs": -3.0,
                    },
                    "inspected_at": int(time.time()),
                }
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET provider='minimax_h3',"
                    "result_json=? WHERE shot_key=? AND project_id=?",
                    (json.dumps({"native_media": evidence}), shot_key, self.project["id"]),
                )
                conn.execute(
                    "UPDATE short_drama_provider_shot_versions SET provider='minimax_h3',"
                    "file=?,url=? WHERE shot_key=? AND project_id=?",
                    (
                        derived_relative, "/api/gen/file/" + derived_relative,
                        shot_key, self.project["id"],
                    ),
                )
                raw_paths.append(raw)
            conn.commit()
        finally:
            conn.close()
        return raw_paths

    def valid_native_inspection(self, path, expected_resolution="2K"):
        target = Path(path)
        return {
            "sha256": short_drama_refinement._file_hash(target),
            "size_bytes": target.stat().st_size,
            "resolution": {"width": 2560, "height": 1440},
            "audio": {
                "audible": True, "codec": "aac", "sample_rate": 48000,
                "channels": 2, "mean_volume_dbfs": -21.0,
                "max_volume_dbfs": -3.0,
            },
            "inspected_at": int(time.time()),
        }

    def create_stale_delivery_attempt(self, key, *, cost=80, state="accepted"):
        version = self.confirmed_version("repair-for-" + key)
        production = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "production", "adapter": "real_executor_test_double",
            "formal_cost": cost, "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        attempt_id = "attempt-" + key
        conn = self.db()
        try:
            conn.row_factory = sqlite3.Row
            quote_row = conn.execute(
                "SELECT * FROM short_drama_delivery_quotes WHERE token=?",
                (quote["quote_token"],),
            ).fetchone()
            request_hash = short_drama_refinement._hash({
                "project_id": self.project["id"],
                "quote_token": quote["quote_token"],
                "input_hash": quote_row["input_hash"],
            })
            stale = int(time.time()) - 600
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, "alice", self.project["id"], key, request_hash,
                    quote["quote_token"], cost, state, stale, stale,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return production, attempt_id, quote

    def acceptance_body(self, version):
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        return {
            "project_id": self.project["id"],
            "version_id": version["id"],
            "checklist": {
                key: True for key in short_drama_refinement.ACCEPTANCE_CHECKS
            },
            "source_hashes": workspace["acceptance_requirements"]["source_hashes"],
        }

    def confirm_version(self, version):
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={
                "available": True,
                "reassembly_required": False,
                "message": "complete preview",
            },
        ):
            return short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", self.acceptance_body(version)
            )

    def test_workspace_seeds_refinement_from_playable_draft(self):
        result = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("refining", result["state"])
        self.assertEqual(1, result["current_refinement"]["version"])
        self.assertEqual(1, len(result["current_refinement"]["issues"]))
        self.assertEqual("development_free", result["billing"]["mode"])
        self.assertTrue(result["billing"]["delivery_enabled"])
        self.assertFalse(result["billing"]["deliverable"])

    def test_local_ffmpeg_capability_enables_real_2k_delivery(self):
        process = mock.Mock(
            returncode=0, stdout=" V..... libx264 A..... aac ", stderr=""
        )
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "CONTENT_OUT": self.tmp.name,
        }), mock.patch.object(
            short_drama_refinement.subprocess, "run", return_value=process
        ):
            capability = short_drama_refinement._delivery_capability()
        self.assertTrue(capability["delivery_enabled"])
        self.assertTrue(capability["deliverable"])
        self.assertEqual("local_ffmpeg", capability["adapter"])
        self.assertEqual("local_2k_renderer", capability["reason"])

    def test_formal_2k_cost_uses_duration_tiers(self):
        prices = {
            "short_drama.delivery.2k.upto_60": 11,
            "short_drama.delivery.2k.upto_90": 16,
            "short_drama.delivery.2k.upto_120": 21,
        }
        cases = (
            (30, 11), (60, 11),
            (61, 16), (90, 16),
            (91, 21), (120, 21),
        )
        with mock.patch.object(
            pricing, "get_price", side_effect=lambda key: prices[key],
        ) as get_price:
            for duration, expected in cases:
                with self.subTest(duration=duration):
                    self.assertEqual(
                        expected,
                        short_drama_refinement._formal_2k_cost(duration),
                    )
        self.assertEqual(6, get_price.call_count)
        for key in prices:
            self.assertIn(key, pricing.CATALOG_MAP)

    def test_local_ffmpeg_rejects_quote_after_live_price_change(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("delivery-price-change")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch.object(pricing, "get_price", return_value=10):
            quote = short_drama_refinement.create_delivery_quote(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch.object(
            pricing, "get_price", return_value=12,
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                },
                "stale-live-price",
                deduct_points=deduct,
            )
        self.assertEqual("delivery_quote_stale", raised.exception.code)
        deduct.assert_not_called()

    def test_local_ffmpeg_quote_is_paid_2k_for_project_duration(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("paid-2k-quote")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
        self.assertEqual("2k", quote["resolution"])
        self.assertEqual(10, quote["cost"])

    def test_local_ffmpeg_quote_revalidates_raw_file_before_issuing_quote(self):
        raw_paths = self.install_mock_native_evidence()
        version = self.confirmed_version("quote-raw-revalidation")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        raw_paths[0].write_bytes(b"tampered-after-acceptance")
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.create_delivery_quote(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
        self.assertEqual("provider_native_media_changed", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_quotes"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_local_ffmpeg_quote_rejects_missing_downres_or_muted_raw_media(self):
        raw_paths = self.install_mock_native_evidence()
        version = self.confirmed_version("quote-raw-probe")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        cases = (
            ("provider_resolution_below_2k", "provider_native_media_invalid"),
            ("provider_audio_silent", "provider_native_audio_invalid"),
        )
        for native_code, expected_code in cases:
            with self.subTest(native_code=native_code), mock.patch.object(
                short_drama_refinement,
                "_delivery_capability",
                return_value=capability,
            ), mock.patch(
                "content_domains.short_drama_native_audio.inspect_native_media",
                side_effect=short_drama_native_audio.NativeAudioError(
                    native_code, "invalid raw media",
                ),
            ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.create_delivery_quote(
                    self.db,
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "version_id": version["id"],
                    },
                )
            self.assertEqual(expected_code, raised.exception.code)

        raw_paths[0].unlink()
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), self.assertRaises(short_drama_refinement.RefinementError) as missing:
            short_drama_refinement.create_delivery_quote(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
        self.assertEqual("provider_asset_missing", missing.exception.code)

    def test_local_ffmpeg_start_revalidates_raw_media_before_charge(self):
        raw_paths = self.install_mock_native_evidence()
        version = self.confirmed_version("charge-raw-revalidation")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
            raw_paths[1].write_bytes(b"changed-after-quote")
            deduct = mock.Mock()
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "raw-changed-before-charge",
                    deduct_points=deduct,
                )
        self.assertEqual("provider_native_media_changed", raised.exception.code)
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(
                (0, 0),
                (
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_delivery_attempts"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM short_drama_delivery_jobs"
                    ).fetchone()[0],
                ),
            )
        finally:
            conn.close()

    def test_delivery_raw_validation_does_not_hold_jobs_database_write_lock(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("raw-validation-write-lock")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }

        def validate_while_another_request_writes(_source, _capability):
            concurrent = sqlite3.connect(self.database, timeout=0.1)
            try:
                concurrent.execute(
                    "UPDATE short_drama_projects SET title=title WHERE id=?",
                    (self.project["id"],),
                )
                concurrent.commit()
            finally:
                concurrent.close()

        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch.object(
            short_drama_refinement,
            "_revalidate_delivery_native_sources",
            side_effect=validate_while_another_request_writes,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db,
                "alice",
                {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
            result = short_drama_refinement.start_delivery_job(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                },
                "raw-validation-write-lock",
                deduct_points=mock.Mock(),
            )
        self.assertEqual("queued", result["status"])

    def test_quote_snapshot_survives_raw_replacement_before_quote_insert(self):
        raw_paths = self.install_mock_native_evidence()
        version = self.confirmed_version("quote-snapshot-toctou")
        expected_hash = short_drama_refinement._file_hash(raw_paths[0])
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        real_copy = short_drama_refinement._copy_delivery_input_snapshot

        def copy_then_replace_raw(assembly, project_id, scope, key):
            snapshot = real_copy(assembly, project_id, scope, key)
            if scope == "quotes":
                raw_paths[0].write_bytes(b"replaced-after-validated-snapshot")
            return snapshot

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch.object(
            short_drama_refinement, "_copy_delivery_input_snapshot",
            side_effect=copy_then_replace_raw,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        snapshot = (
            short_drama_refinement._delivery_input_snapshot_dir(
                self.project["id"], "quotes", quote["input_hash"],
            ) / "source-001.mp4"
        )
        self.assertEqual(expected_hash, short_drama_refinement._file_hash(snapshot))
        self.assertNotEqual(expected_hash, short_drama_refinement._file_hash(raw_paths[0]))

    def test_repeated_quotes_share_one_content_addressed_native_snapshot(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("quote-snapshot-deduplication")
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ):
            first = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            second = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )

        self.assertNotEqual(first["quote_token"], second["quote_token"])
        self.assertEqual(first["input_hash"], second["input_hash"])
        quote_root = (
            Path(self.tmp.name) / "short_drama_delivery_inputs" /
            self.project["id"] / "quotes"
        )
        self.assertEqual([first["input_hash"]], sorted(
            item.name for item in quote_root.iterdir() if item.is_dir()
        ))

    def test_quote_creation_lock_blocks_reaper_before_database_reference(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("quote-snapshot-reaper-race")
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        real_copy = short_drama_refinement._copy_delivery_input_snapshot
        observed = {}

        def copy_then_reap(assembly, project_id, scope, key):
            snapshot = real_copy(assembly, project_id, scope, key)
            target = short_drama_refinement._delivery_input_snapshot_dir(
                project_id, scope, key,
            )
            old = int(time.time()) - 3600
            os.utime(target, (old, old))
            observed["reap"] = short_drama_refinement.reap_delivery_orphans(
                self.db, now=int(time.time()), grace_seconds=60,
            )
            observed["survived"] = target.is_dir()
            return snapshot

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch.object(
            short_drama_refinement, "_copy_delivery_input_snapshot",
            side_effect=copy_then_reap,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )

        target = short_drama_refinement._delivery_input_snapshot_dir(
            self.project["id"], "quotes", quote["input_hash"],
        )
        self.assertTrue(observed["survived"])
        self.assertTrue(target.is_dir())
        self.assertGreaterEqual(observed["reap"]["retained"], 1)

    def test_delivery_input_lock_is_advisory_and_old_release_cannot_unlock_new_owner(self):
        project_id = self.project["id"]
        key = "advisory-lock-contract"
        first = short_drama_refinement._acquire_delivery_input_lock(
            project_id, "quotes", key,
        )
        self.assertIsNotNone(first)
        lock_path = short_drama_refinement._delivery_input_lock_dir(
            project_id, "quotes", key,
        )
        self.assertTrue(lock_path.is_file())
        self.assertIsNone(short_drama_refinement._acquire_delivery_input_lock(
            project_id, "quotes", key,
        ))

        short_drama_refinement._release_delivery_input_lock(first)
        second = short_drama_refinement._acquire_delivery_input_lock(
            project_id, "quotes", key,
        )
        self.assertIsNotNone(second)
        short_drama_refinement._release_delivery_input_lock(first)
        self.assertIsNone(short_drama_refinement._acquire_delivery_input_lock(
            project_id, "quotes", key,
        ))
        short_drama_refinement._release_delivery_input_lock(second)

    def test_delivery_input_advisory_lock_files_are_bounded_by_shards(self):
        project_id = self.project["id"]
        lock_paths = set()
        for index in range(100):
            key = "attempt-%03d" % index
            handle = short_drama_refinement._acquire_delivery_input_lock(
                project_id, "attempts", key,
            )
            self.assertIsNotNone(handle)
            short_drama_refinement._release_delivery_input_lock(handle)
            lock_paths.add(short_drama_refinement._delivery_input_lock_dir(
                project_id, "attempts", key,
            ))
        self.assertLessEqual(len(lock_paths), 64)
        self.assertTrue(all(path.is_file() for path in lock_paths))

    def test_charge_uses_snapshot_when_raw_changes_after_validation(self):
        raw_paths = self.install_mock_native_evidence()
        version = self.confirmed_version("charge-snapshot-toctou")
        expected_hash = short_drama_refinement._file_hash(raw_paths[0])
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            real_copy = short_drama_refinement._copy_delivery_input_snapshot

            def copy_then_replace_raw(assembly, project_id, scope, key):
                snapshot = real_copy(assembly, project_id, scope, key)
                if scope == "attempts":
                    raw_paths[0].write_bytes(b"replaced-after-charge-validation")
                return snapshot

            deduct = mock.Mock()
            with mock.patch.object(
                short_drama_refinement, "_copy_delivery_input_snapshot",
                side_effect=copy_then_replace_raw,
            ):
                job = short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    }, "charge-snapshot-toctou", deduct_points=deduct,
                )
        deduct.assert_called_once()
        conn = self.db()
        try:
            attempt_id = conn.execute(
                "SELECT id FROM short_drama_delivery_attempts "
                "WHERE job_id=?", (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        snapshot = (
            short_drama_refinement._delivery_input_snapshot_dir(
                self.project["id"], "attempts", attempt_id,
            ) / "source-001.mp4"
        )
        self.assertEqual(expected_hash, short_drama_refinement._file_hash(snapshot))
        self.assertNotEqual(expected_hash, short_drama_refinement._file_hash(raw_paths[0]))

    def test_formal_2k_dimensions_follow_project_ratio(self):
        self.assertEqual(
            (2560, 1440),
            short_drama_refinement._dimensions("16:9", "2k"),
        )
        self.assertEqual(
            (1440, 2560),
            short_drama_refinement._dimensions("9:16", "2k"),
        )

    def test_formal_native_assembly_uses_ordered_raw_shots_not_preview(self):
        def native_media(name, raw_hash):
            return {
                "raw": {
                    "file": "video/%s-raw.mp4" % name,
                    "sha256": raw_hash,
                    "size_bytes": 123,
                },
                "derived": {
                    "file": "video/%s-faststart.mp4" % name,
                    "sha256": ("b" if raw_hash[0] == "a" else "d") * 64,
                    "size_bytes": 120,
                    "derived_from_sha256": raw_hash,
                },
                "resolution": {"width": 2560, "height": 1440},
                "audio": {
                    "audible": True, "codec": "aac", "sample_rate": 48000,
                    "channels": 2, "mean_volume_dbfs": -21.0,
                    "max_volume_dbfs": -3.0,
                },
                "inspected_at": 1,
            }

        source = {
            "url": "/api/gen/file/drafts/preview-1080p.mp4",
            "shots": [
                {
                    "shot_key": "shot_02", "provider": "minimax_h3",
                    "start_ms": 0, "end_ms": 2000,
                    "native_media": native_media("shot-02", "c" * 64),
                },
                {
                    "shot_key": "shot_01", "provider": "minimax_h3",
                    "start_ms": 2000, "end_ms": 3000,
                    "native_media": native_media("shot-01", "a" * 64),
                },
            ],
        }
        assembly = short_drama_refinement._formal_native_assembly(source)
        self.assertEqual(
            ["video/shot-02-raw.mp4", "video/shot-01-raw.mp4"],
            [item["file"] for item in assembly["shots"]],
        )
        self.assertEqual(
            [2000, 1000], [item["duration_ms"] for item in assembly["shots"]],
        )
        self.assertNotIn("preview-1080p.mp4", json.dumps(assembly))

    def test_formal_native_assembly_rejects_non_contiguous_timeline(self):
        evidence = {
            "raw": {"file": "video/raw.mp4", "sha256": "a" * 64,
                    "size_bytes": 123},
            "derived": {"file": "video/faststart.mp4", "sha256": "b" * 64,
                        "size_bytes": 120, "derived_from_sha256": "a" * 64},
            "resolution": {"width": 2560, "height": 1440},
            "audio": {"audible": True, "codec": "aac", "sample_rate": 48000,
                      "channels": 2, "mean_volume_dbfs": -21.0,
                      "max_volume_dbfs": -3.0},
            "inspected_at": 1,
        }
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement._formal_native_assembly({"shots": [{
                "shot_key": "shot_01", "provider": "minimax_h3",
                "start_ms": 500, "end_ms": 1500, "native_media": evidence,
            }]})
        self.assertEqual("delivery_locked_timeline_invalid", raised.exception.code)

    def test_paid_delivery_never_reads_1080p_preview_as_video_input(self):
        raw_paths = self.install_mock_native_evidence()
        version = self.repaired_version("native-source-contract")
        self.confirm_version(version)
        preview = Path(self.tmp.name) / version["media"]["preview_file"]
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        captured = {}
        published_without_db_lock = []
        real_publish = short_drama_formal_renderer.publish_validated_output

        def verify(
                assembly, snapshot_dir, *, require_locked_native_media=False):
            self.assertTrue(require_locked_native_media)
            captured["assembly"] = assembly
            snapshot_paths = [
                Path(self.tmp.name) / item["file"] for item in assembly["shots"]
            ]
            self.assertEqual(
                [short_drama_refinement._file_hash(path) for path in raw_paths],
                [short_drama_refinement._file_hash(path) for path in snapshot_paths],
            )
            return snapshot_paths

        def render(sources, ratio, duration_ms, media_contract, output, **kwargs):
            captured["sources"] = list(sources)
            output.write_bytes(b"formal-native-2k")
            return {
                "probe": {
                    "video": {"width": 2560, "height": 1440},
                    "audio": {"codec": "aac"}, "duration_ms": duration_ms,
                },
                "subtitle_streams": 0,
                "native_audio": {"audible": True},
                "sha256": short_drama_refinement._file_hash(output),
            }

        def publish(*args, **kwargs):
            concurrent = sqlite3.connect(self.database, timeout=0.1)
            try:
                concurrent.execute(
                    "UPDATE short_drama_projects SET title=title WHERE id=?",
                    (self.project["id"],),
                )
                concurrent.commit()
            finally:
                concurrent.close()
            published_without_db_lock.append(True)
            return real_publish(*args, **kwargs)

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch.object(
            short_drama_refinement, "_valid_acceptance",
            return_value={"snapshot": {"media_contract": {
                "delivery_eligible": True, "subtitle_required": False,
            }}},
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch(
            "content_domains.short_drama_autodraft._verified_native_assembly_sources",
            side_effect=verify,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.render_native_2k",
            side_effect=render,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.publish_validated_output",
            side_effect=publish,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "native-source-delivery", deduct_points=lambda *args: None,
                refund_points=lambda *args: None,
            )
            preview.unlink()
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"], job.get("error"))
        self.assertEqual(
            [short_drama_refinement._file_hash(path) for path in raw_paths],
            [short_drama_refinement._file_hash(path) for path in captured["sources"]],
        )
        self.assertNotIn("preview-1080p", json.dumps(captured["assembly"]))
        self.assertEqual([True], published_without_db_lock)

    def test_fresh_provider_draft_delivers_without_replacing_a_shot(self):
        raw_paths = self.install_mock_native_evidence()
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            cards = []
            for row in conn.execute(
                "SELECT v.*,j.result_json FROM short_drama_provider_shot_versions v "
                "JOIN short_drama_provider_shot_jobs j ON j.id=v.job_id "
                "WHERE v.project_id=? ORDER BY v.shot_key",
                (self.project["id"],),
            ):
                evidence = json.loads(row["result_json"])["native_media"]
                cards.append({
                    "shot_key": row["shot_key"], "sort_order": len(cards) + 1,
                    "status": "ready", "issue": None,
                    "provider": row["provider"],
                    "provider_version_id": row["id"],
                    "provider_version": row["version"],
                    "provider_job_id": row["provider_job_id"],
                    "file": row["file"], "url": row["url"],
                    "file_hash": evidence["derived"]["sha256"],
                    "native_media": evidence,
                })
            manifest = json.loads(conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()[0])
            locked_shots = {
                item["shot_key"]: item for item in manifest.get("shots") or []
            }
            for card in cards:
                locked = locked_shots[card["shot_key"]]
                card.update({
                    "start_ms": locked["start_ms"],
                    "end_ms": locked["end_ms"],
                })
            manifest.update({"resolution": "1080p", "shots": cards, "issues": []})
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET status='ready',"
                "manifest_json=? WHERE id='draft-v1'", (json.dumps(manifest),),
            )
            conn.commit()
        finally:
            conn.close()
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual([], version["issues"])
        self.assertTrue(all(shot.get("native_media") for shot in version["shots"]))
        self.confirm_version(version)
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        rendered_sources = []

        def render(sources, _ratio, duration_ms, _contract, output, **_kwargs):
            rendered_sources.extend(sources)
            output.write_bytes(b"fresh-provider-formal-2k")
            return {
                "probe": {
                    "video": {"width": 2560, "height": 1440},
                    "audio": {"codec": "aac"}, "duration_ms": duration_ms,
                },
                "subtitle_streams": 1, "native_audio": {"audible": True},
                "sha256": short_drama_refinement._file_hash(output),
            }

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch(
            "content_domains.short_drama_autodraft._verified_native_assembly_sources",
            return_value=raw_paths,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.render_native_2k",
            side_effect=render,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "fresh-provider-formal", deduct_points=lambda *_args: None,
            )
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"], job.get("error"))
        self.assertEqual(raw_paths, rendered_sources)

    def test_formal_renderer_receives_each_native_source_in_locked_order(self):
        source_a = Path(self.tmp.name) / "shot-a-raw.mp4"
        source_b = Path(self.tmp.name) / "shot-b-raw.mp4"
        source_a.write_bytes(b"a")
        source_b.write_bytes(b"b")
        output = Path(self.tmp.name) / "formal-2k.mp4"
        captured = {}

        def run(command, **kwargs):
            captured["command"] = list(command)
            output.write_bytes(b"native-formal-output")
            return subprocess.CompletedProcess(command, 0, "", "")

        first_probe = {
            "video": {"width": 2560, "height": 1440},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2},
            "duration_ms": 1200,
        }
        second_probe = dict(first_probe, duration_ms=1800)
        output_probe = dict(first_probe, duration_ms=3000)
        with mock.patch.object(
            short_drama_formal_renderer.media_plan,
            "probe_media", side_effect=[first_probe, second_probe, output_probe],
        ), mock.patch.object(
            short_drama_formal_renderer, "_run", side_effect=run,
        ), mock.patch.object(
            short_drama_formal_renderer.short_drama_native_audio,
            "inspect_native_audio", return_value={"audible": True},
        ), mock.patch.object(
            short_drama_formal_renderer.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            result = short_drama_formal_renderer.render_native_2k(
                [source_a, source_b], "16:9", 3000,
                {"subtitle_required": False}, output,
                shot_durations_ms=[1000, 2000],
            )
        command = captured["command"]
        inputs = [command[index + 1] for index, value in enumerate(command) if value == "-i"]
        self.assertEqual([str(source_a), str(source_b)], inputs)
        self.assertNotIn("preview-1080p", " ".join(command))
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("tpad=stop_mode=clone:stop_duration=1.000", filters)
        self.assertIn("trim=duration=1.000", filters)
        self.assertIn("apad=whole_dur=2.000", filters)
        self.assertIn("atrim=duration=2.000", filters)
        self.assertIn(
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]", filters,
        )
        self.assertEqual(1, filters.count("concat="))
        self.assertEqual(short_drama_refinement._file_hash(output), result["sha256"])

    def test_publish_rejects_render_output_replaced_after_validation(self):
        temp = Path(self.tmp.name) / ".job.owner.tmp"
        target = Path(self.tmp.name) / "job"
        temp.mkdir()
        rendered = temp / "final-2k.mp4"
        rendered.write_bytes(b"validated-formal-output")
        expected_hash = short_drama_refinement._file_hash(rendered)
        original_rename = Path.rename

        def replace_then_rename(path, destination):
            rendered.write_bytes(b"replacement-with-different-bytes")
            return original_rename(path, destination)

        with mock.patch.object(
            Path, "rename", autospec=True, side_effect=replace_then_rename,
        ):
            with self.assertRaises(
                short_drama_formal_renderer.FormalRenderError,
            ) as raised:
                short_drama_formal_renderer.publish_validated_output(
                    temp, target, "final-2k.mp4", expected_hash,
                )
        self.assertEqual(
            "delivery_output_identity_changed", raised.exception.code,
        )
        self.assertFalse(target.exists())

    def test_delivery_render_has_single_poll_owner(self):
        version = self.confirmed_version("single-render-owner")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        job = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "quote_token": quote["quote_token"],
            }, "single-render-owner",
        )
        for _ in range(3):
            job = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        calls = []

        def complete(owner_conn, row, **_kwargs):
            calls.append(row["id"])
            other = short_drama_refinement._connection(self.db)
            try:
                stale = int(time.time()) - (
                    short_drama_refinement._DELIVERY_RENDER_LEASE_SECONDS + 5
                )
                other.execute(
                    "UPDATE short_drama_delivery_jobs SET updated_at=? WHERE id=?",
                    (stale, row["id"]),
                )
                other.commit()
                deadline = time.time() + 2
                while time.time() < deadline:
                    refreshed = other.execute(
                        "SELECT updated_at FROM short_drama_delivery_jobs WHERE id=?",
                        (row["id"],),
                    ).fetchone()[0]
                    if refreshed > stale:
                        break
                    time.sleep(0.01)
                self.assertGreater(refreshed, stale)
                current = other.execute(
                    "SELECT * FROM short_drama_delivery_jobs WHERE id=?",
                    (row["id"],),
                ).fetchone()
                observed = short_drama_refinement._advance_delivery(
                    other, current, self.db,
                )
                other.commit()
            finally:
                other.close()
            self.assertTrue(observed["phase"].startswith("rendering:"))
            owner_conn.execute(
                "UPDATE short_drama_delivery_jobs SET status='succeeded',"
                "phase='completed',progress=100 WHERE id=?", (row["id"],),
            )
            return short_drama_refinement._job(owner_conn.execute(
                "SELECT * FROM short_drama_delivery_jobs WHERE id=?", (row["id"],),
            ).fetchone())

        with mock.patch.object(
            short_drama_refinement, "_DELIVERY_RENDER_HEARTBEAT_SECONDS", 0.01,
        ), mock.patch.object(
            short_drama_refinement, "_complete_delivery", side_effect=complete,
        ):
            completed = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual([job["id"]], calls)

    def test_stale_crashed_render_lease_is_reclaimed(self):
        version = self.confirmed_version("stale-render-takeover")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        job = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "quote_token": quote["quote_token"],
            }, "stale-render-takeover",
        )
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_delivery_jobs SET status='running',"
                "phase='rendering:crashed',poll_count=4,updated_at=? WHERE id=?",
                (
                    int(time.time())
                    - short_drama_refinement._DELIVERY_RENDER_LEASE_SECONDS - 1,
                    job["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        claimed_phases = []

        def complete(owner_conn, row, **_kwargs):
            claimed_phases.append(row["phase"])
            owner_conn.execute(
                "UPDATE short_drama_delivery_jobs SET status='succeeded',"
                "phase='completed',progress=100 WHERE id=?", (row["id"],),
            )
            return short_drama_refinement._job(owner_conn.execute(
                "SELECT * FROM short_drama_delivery_jobs WHERE id=?", (row["id"],),
            ).fetchone())

        with mock.patch.object(
            short_drama_refinement, "_complete_delivery", side_effect=complete,
        ):
            recovered = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("succeeded", recovered["status"])
        self.assertEqual(1, len(claimed_phases))
        self.assertNotEqual("rendering:crashed", claimed_phases[0])

    def test_lost_render_owner_cannot_delete_successor_files(self):
        root = Path(self.tmp.name)
        job = {"id": "shared-job", "project_id": self.project["id"]}
        old_phase = "rendering:old-owner"
        new_phase = "rendering:new-owner"
        old_temp, target = short_drama_refinement._delivery_render_paths(
            root, job, old_phase,
        )
        new_temp, successor_target = short_drama_refinement._delivery_render_paths(
            root, job, new_phase,
        )
        self.assertEqual(target, successor_target)
        old_temp.mkdir(parents=True)
        new_temp.mkdir(parents=True)
        target.mkdir(parents=True)
        (target / ".render-owner").write_text("new-owner", encoding="utf-8")
        (target / "final-2k.mp4").write_bytes(b"successor")

        short_drama_refinement._cleanup_delivery_render_files(
            root, job, old_phase, include_target=True,
        )

        self.assertFalse(old_temp.exists())
        self.assertTrue(new_temp.is_dir())
        self.assertEqual(b"successor", (target / "final-2k.mp4").read_bytes())

    def test_real_delivery_worker_uses_owner_scoped_temp_and_publish_marker(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("worker-owner-scoped-path")
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        rendered_parents = []

        def render(_sources, _ratio, duration_ms, _contract, output, **_kwargs):
            rendered_parents.append(output.parent.name)
            output.write_bytes(b"owner-scoped-formal-output")
            return {
                "probe": {
                    "video": {"width": 2560, "height": 1440},
                    "audio": {"codec": "aac"}, "duration_ms": duration_ms,
                },
                "subtitle_streams": 0, "native_audio": {"audible": True},
                "sha256": short_drama_refinement._file_hash(output),
            }

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.render_native_2k",
            side_effect=render,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "worker-owner-scoped-path", deduct_points=lambda *_args: None,
            )
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

        self.assertEqual("succeeded", job["status"], job.get("error"))
        self.assertEqual(1, len(rendered_parents))
        self.assertRegex(
            rendered_parents[0], r"^\.%s\.[0-9a-f]+\.tmp$" % job["id"],
        )
        owner = rendered_parents[0][len(".%s." % job["id"]):-4]
        target = (
            Path(self.tmp.name) / "short_drama_delivery" /
            self.project["id"] / job["id"]
        )
        self.assertEqual(
            owner, (target / ".render-owner").read_text(encoding="utf-8")
        )

    def test_real_delivery_worker_cleans_owned_publish_after_post_publish_lease_loss(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("post-publish-lease-loss")
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        cancel_event = {}
        real_publish = short_drama_formal_renderer.publish_validated_output

        def render(_sources, _ratio, duration_ms, _contract, output, **kwargs):
            cancel_event["value"] = kwargs["cancel_event"]
            output.write_bytes(b"lease-loss-formal-output")
            return {
                "probe": {
                    "video": {"width": 2560, "height": 1440},
                    "audio": {"codec": "aac"}, "duration_ms": duration_ms,
                },
                "subtitle_streams": 0, "native_audio": {"audible": True},
                "sha256": short_drama_refinement._file_hash(output),
            }

        def publish(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            cancel_event["value"].set()
            return result

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.render_native_2k",
            side_effect=render,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.publish_validated_output",
            side_effect=publish,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "post-publish-lease-loss", deduct_points=lambda *_args: None,
            )
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

        target = (
            Path(self.tmp.name) / "short_drama_delivery" /
            self.project["id"] / job["id"]
        )
        self.assertEqual("failed", job["status"])
        self.assertEqual("delivery_render_lease_lost", job["error"]["code"])
        self.assertFalse(target.exists())

    def test_real_delivery_takeover_keeps_successor_publish_and_cleans_old_temp(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("render-takeover-files")
        capability = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "local_ffmpeg", "adapter": "local_ffmpeg",
            "formal_cost": 0, "reason": "local_2k_renderer",
        }
        captured = {}

        def render(_sources, _ratio, duration_ms, _contract, output, **kwargs):
            captured["old_temp"] = output.parent
            owner_conn = self.db()
            try:
                owner_conn.execute(
                    "UPDATE short_drama_delivery_jobs SET phase=?,updated_at=? "
                    "WHERE id=?",
                    ("rendering:successor", int(time.time()), captured["job_id"]),
                )
                owner_conn.commit()
            finally:
                owner_conn.close()
            target = (
                Path(self.tmp.name) / "short_drama_delivery" /
                self.project["id"] / captured["job_id"]
            )
            target.mkdir(parents=True)
            (target / ".render-owner").write_text("successor", encoding="utf-8")
            (target / "final-2k.mp4").write_bytes(b"successor-output")
            self.assertTrue(kwargs["cancel_event"].wait(timeout=1))
            output.write_bytes(b"old-owner-output")
            return {
                "probe": {
                    "video": {"width": 2560, "height": 1440},
                    "audio": {"codec": "aac"}, "duration_ms": duration_ms,
                },
                "subtitle_streams": 0, "native_audio": {"audible": True},
                "sha256": short_drama_refinement._file_hash(output),
            }

        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch.object(
            short_drama_refinement, "_DELIVERY_RENDER_HEARTBEAT_SECONDS", 0.01,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.render_native_2k",
            side_effect=render,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "render-takeover-files", deduct_points=lambda *_args: None,
            )
            captured["job_id"] = job["id"]
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

        target = (
            Path(self.tmp.name) / "short_drama_delivery" /
            self.project["id"] / job["id"]
        )
        self.assertEqual("rendering:successor", job["phase"])
        self.assertFalse(captured["old_temp"].exists())
        self.assertEqual(
            b"successor-output", (target / "final-2k.mp4").read_bytes()
        )
        self.assertEqual(
            "successor", (target / ".render-owner").read_text(encoding="utf-8")
        )

    def test_reaper_retains_owner_scoped_temp_for_live_render(self):
        version = self.confirmed_version("live-owner-temp-reaper")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        job = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "quote_token": quote["quote_token"],
            }, "live-owner-temp-reaper",
        )
        phase = "rendering:live-owner"
        now = int(time.time())
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_delivery_jobs SET status='running',phase=?,"
                "poll_count=4,updated_at=? WHERE id=?", (phase, now, job["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        temp, _target = short_drama_refinement._delivery_render_paths(
            Path(self.tmp.name), job, phase,
        )
        old_temp, _target = short_drama_refinement._delivery_render_paths(
            Path(self.tmp.name), job, "rendering:old-owner",
        )
        temp.mkdir(parents=True)
        old_temp.mkdir(parents=True)
        old = now - short_drama_refinement._DELIVERY_RENDER_LEASE_SECONDS - 60
        os.utime(temp, (old, old))
        os.utime(old_temp, (old, old))

        result = short_drama_refinement.reap_delivery_orphans(
            self.db, now=now, grace_seconds=60,
        )

        self.assertTrue(temp.is_dir())
        self.assertFalse(old_temp.exists())
        self.assertEqual(1, result["retained"])
        self.assertEqual(1, result["removed"])

    def test_delivery_orphan_reaper_removes_crash_publish_but_keeps_version(self):
        root = Path(self.tmp.name) / "short_drama_delivery" / self.project["id"]
        referenced = root / "referenced-job"
        referenced_old_temp = root / ".referenced-job.old-owner.tmp"
        orphan = root / "orphan-job"
        temp_orphan = root / ".temp-orphan.tmp"
        for directory in (referenced, referenced_old_temp, orphan, temp_orphan):
            directory.mkdir(parents=True)
            (directory / "final-2k.mp4").write_bytes(directory.name.encode())
        now = int(time.time())
        old = now - 3600
        for directory in (referenced, referenced_old_temp, orphan, temp_orphan):
            os.utime(directory, (old, old))
        version = self.confirmed_version("delivery-orphan-reference")
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_delivery_versions "
                "(id,project_id,job_id,refinement_version_id,version,status,url,"
                "snapshot_json,input_hash,created_at) "
                "VALUES (?,?,?,?,1,'ready',?,?,?,?)",
                (
                    "delivery-version-reference", self.project["id"],
                    "referenced-job", version["id"],
                    "/api/gen/file/short_drama_delivery/referenced-job/final-2k.mp4",
                    "{}", "snapshot-hash", old,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        result = short_drama_refinement.reap_delivery_orphans(
            self.db, now=now, grace_seconds=60,
        )
        self.assertTrue(referenced.is_dir())
        self.assertFalse(referenced_old_temp.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse(temp_orphan.exists())
        self.assertEqual(3, result["removed"])
        self.assertEqual([], result["errors"])

    def test_delivery_input_reaper_retains_active_and_removes_terminal_snapshots(self):
        now = int(time.time())
        old = now - 3600
        root = (
            Path(self.tmp.name) / "short_drama_delivery_inputs" /
            self.project["id"]
        )
        paths = {
            "active_quote": root / "quotes" / "active-quote",
            "expired_quote": root / "quotes" / "expired-quote",
            "consumed_quote": root / "quotes" / "consumed-quote",
            "accepted_attempt": root / "attempts" / "accepted-attempt",
            "refund_attempt": root / "attempts" / "refund-attempt",
            "running_attempt": root / "attempts" / "running-attempt",
            "refunded_attempt": root / "attempts" / "refunded-attempt",
            "completed_attempt": root / "attempts" / "completed-attempt",
            "orphan_attempt": root / "attempts" / "orphan-attempt",
            "crashed_temp": root / "quotes" / ".crashed.copy.tmp",
        }
        for directory in paths.values():
            directory.mkdir(parents=True)
            (directory / "source-001.mp4").write_bytes(directory.name.encode())
            os.utime(directory, (old, old))

        conn = self.db()
        try:
            for token, expires_at, consumed_job_id in (
                ("active-quote", now + 300, None),
                ("expired-quote", now - 1, None),
                ("consumed-quote", now + 300, "completed-job"),
            ):
                conn.execute(
                    "INSERT INTO short_drama_delivery_quotes "
                    "(token,project_id,refinement_version_id,input_hash,cost,"
                    "expires_at,consumed_job_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        token, self.project["id"], "refinement-version", token,
                        10, expires_at, consumed_job_id, old,
                    ),
                )
            for job_id, status in (
                ("running-job", "running"), ("completed-job", "succeeded"),
            ):
                conn.execute(
                    "INSERT INTO short_drama_delivery_jobs "
                    "(id,project_id,refinement_version_id,actor_username,status,"
                    "phase,progress,poll_count,input_hash,cost,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'queued',0,0,'hash',10,?,?)",
                    (
                        job_id, self.project["id"], "refinement-version", "alice",
                        status, old, old,
                    ),
                )
            for attempt_id, state, job_id in (
                ("accepted-attempt", "accepted", None),
                ("refund-attempt", "refund_pending", None),
                ("running-attempt", "linked", "running-job"),
                ("refunded-attempt", "refunded", None),
                ("completed-attempt", "linked", "completed-job"),
            ):
                conn.execute(
                    "INSERT INTO short_drama_delivery_attempts "
                    "(id,actor_username,project_id,idempotency_key,request_hash,"
                    "quote_token,cost,state,job_id,created_at,updated_at) "
                    "VALUES (?,'alice',?,?,?,?,10,?,?,?,?)",
                    (
                        attempt_id, self.project["id"], "idem-" + attempt_id,
                        "request-" + attempt_id, "active-quote", state, job_id,
                        old, old,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        result = short_drama_refinement.reap_delivery_orphans(
            self.db, now=now, grace_seconds=60,
        )

        for name in (
            "active_quote", "accepted_attempt", "refund_attempt",
            "running_attempt",
        ):
            self.assertTrue(paths[name].is_dir(), name)
        for name in (
            "expired_quote", "consumed_quote", "refunded_attempt",
            "completed_attempt", "orphan_attempt", "crashed_temp",
        ):
            self.assertFalse(paths[name].exists(), name)
        self.assertEqual(4, result["retained"])
        self.assertEqual(6, result["removed"])
        self.assertEqual([], result["errors"])

    def test_delivery_orphan_reaper_fails_closed_on_database_error(self):
        orphan = (
            Path(self.tmp.name) / "short_drama_delivery" /
            self.project["id"] / "must-not-delete"
        )
        orphan.mkdir(parents=True)
        input_orphan = (
            Path(self.tmp.name) / "short_drama_delivery_inputs" /
            self.project["id"] / "quotes" / "must-not-delete"
        )
        input_orphan.mkdir(parents=True)
        old = int(time.time()) - 3600
        os.utime(orphan, (old, old))
        os.utime(input_orphan, (old, old))

        def unavailable():
            raise sqlite3.OperationalError("database unavailable")

        result = short_drama_refinement.reap_delivery_orphans(
            unavailable, now=int(time.time()), grace_seconds=60,
        )
        self.assertTrue(orphan.is_dir())
        self.assertTrue(input_orphan.is_dir())
        self.assertEqual(0, result["removed"])
        self.assertTrue(result["errors"])

    def test_worker_startup_reaps_native_orphans_before_pending_recovery(self):
        source = inspect.getsource(core.start_job_workers)
        self.assertLess(
            source.index("_reap_short_drama_native_media()"),
            source.index("threading.Thread"),
        )
        self.assertLess(
            source.index("_reap_short_drama_native_media()"),
            source.index("_recover_pending_jobs(_PENDING_RECOVERY_LIMIT)"),
        )
        self.assertLess(
            source.index("_reap_short_drama_delivery_media()"),
            source.index("_recover_pending_jobs(_PENDING_RECOVERY_LIMIT)"),
        )
        scanner = inspect.getsource(core._pending_job_scanner)
        self.assertIn("_run_short_drama_recovery", scanner)
        recovery = inspect.getsource(core._run_short_drama_recovery)
        self.assertIn("retry_delivery_attempt_recovery", recovery)

    def test_worker_startup_delivery_recovery_removes_stale_owner_artifacts(self):
        project_dir = (
            Path(self.tmp.name) / "short_drama_delivery" / self.project["id"]
        )
        stale_temp = project_dir / ".crashed-job.crashed-owner.tmp"
        stale_target = project_dir / "crashed-job"
        for directory in (stale_temp, stale_target):
            directory.mkdir(parents=True)
            (directory / ".render-owner").write_text(
                "crashed-owner", encoding="utf-8"
            )
            (directory / "final-2k.mp4").write_bytes(b"stale")
            old = int(time.time()) - (
                short_drama_refinement._DELIVERY_RENDER_LEASE_SECONDS + 60
            )
            os.utime(directory, (old, old))
        domain = mock.Mock()
        domain.short_drama_refinement = short_drama_refinement
        with mock.patch.dict(os.environ, {"CONTENT_OUT": self.tmp.name}), \
                mock.patch.object(core, "_short_drama_domain", return_value=domain), \
                mock.patch.object(core, "jdb", self.db):
            result = core._reap_short_drama_delivery_media()
        self.assertEqual(2, result["removed"])
        self.assertFalse(stale_temp.exists())
        self.assertFalse(stale_target.exists())

    def test_native_orphan_startup_failure_is_logged_and_nonfatal(self):
        with mock.patch.object(
            video, "reap_short_drama_native_orphans",
            side_effect=RuntimeError("reaper unavailable"),
        ), mock.patch("builtins.print") as logged:
            result = core._reap_short_drama_native_media()
        self.assertIsNone(result)
        self.assertIn(
            "[short-drama-native-media]",
            " ".join(str(value) for value in logged.call_args.args),
        )

    def _assert_real_formal_delivery(self, ratio, preview_size, expected_size):
        ffmpeg = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
        ffprobe = shutil.which(os.environ.get("FFPROBE_BIN", "ffprobe"))
        if not ffmpeg or not ffprobe:
            if os.environ.get("CI"):
                self.fail("CI must install FFmpeg and FFprobe for media contract tests")
            self.skipTest("real FFmpeg and FFprobe are not installed")
        # Keep every provider asset, refinement preview, and formal delivery
        # under the same controlled CONTENT_OUT root. Acceptance evidence binds
        # physical paths and hashes, so changing the root after confirmation
        # must (correctly) invalidate it.
        root = Path(self.tmp.name)
        source = root / "drafts" / "preview.mp4"
        source.parent.mkdir(parents=True)
        subtitle_file = root / "locked.srt"
        subtitle_file.write_text(
            "1\n00:00:00,000 --> 00:00:01,900\nlocked subtitle\n",
            encoding="utf-8",
        )
        generated = subprocess.run([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            "color=c=blue:size=%s:rate=25:duration=2" % preview_size,
            "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
            "-f", "srt", "-i", str(subtitle_file),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-c:s", "mov_text", "-t", "2", str(source),
        ], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, generated.returncode, generated.stderr)
        native_size = "2560x1440" if ratio == "16:9" else "1440x2560"
        native_evidence = {}
        for index, shot_key in enumerate(("shot_01", "shot_02"), 1):
            raw_relative = "video/%s-%s-raw.mp4" % (
                shot_key, ratio.replace(":", "-"),
            )
            derived_relative = "video/%s-%s-faststart.mp4" % (
                shot_key, ratio.replace(":", "-"),
            )
            raw = root / raw_relative
            raw.parent.mkdir(parents=True, exist_ok=True)
            native = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "color=c=%s:size=%s:rate=25:duration=2" % (
                    "red" if index == 1 else "green", native_size,
                ),
                "-f", "lavfi", "-i",
                "sine=frequency=%d:duration=2" % (440 + index * 110),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(raw),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, native.returncode, native.stderr)
            derived = root / derived_relative
            derived_result = subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(raw), "-t", "1", "-c", "copy", str(derived),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, derived_result.returncode, derived_result.stderr)
            raw_hash = short_drama_refinement._file_hash(raw)
            derived_hash = short_drama_refinement._file_hash(derived)
            native_evidence[shot_key] = {
                "raw": {
                    "file": raw_relative, "sha256": raw_hash,
                    "size_bytes": raw.stat().st_size,
                },
                "derived": {
                    "file": derived_relative, "sha256": derived_hash,
                    "size_bytes": derived.stat().st_size,
                    "derived_from_sha256": raw_hash,
                },
                "resolution": {
                    "width": expected_size[0], "height": expected_size[1],
                },
                "audio": {
                    "audible": True, "codec": "aac", "sample_rate": 44100,
                    "channels": 1, "mean_volume_dbfs": -21.0,
                    "max_volume_dbfs": -3.0,
                },
                "inspected_at": int(time.time()),
            }
        conn = self.db()
        try:
            manifest = json.loads(conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()[0])
            manifest["duration_ms"] = 2000
            manifest["shots"][0].update({"start_ms": 0, "end_ms": 1000})
            manifest["shots"][1].update({"start_ms": 1000, "end_ms": 2000})
            manifest["media_contract"].update({
                "subtitles": [{
                    "line_id": "locked-line", "start_ms": 0,
                    "end_ms": 1900, "text": "locked subtitle",
                }],
                "subtitle_required": True,
            })
            conn.execute(
                "UPDATE short_drama_projects SET ratio=? WHERE id=?",
                (ratio, self.project["id"]),
            )
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET url=?,manifest_json=? "
                "WHERE id='draft-v1'",
                (
                    "/api/gen/file/drafts/preview.mp4",
                    json.dumps(manifest),
                ),
            )
            for shot_key, evidence in native_evidence.items():
                conn.execute(
                    "UPDATE short_drama_provider_shot_jobs SET provider='minimax_h3',"
                    "result_json=? WHERE project_id=? AND shot_key=?",
                    (json.dumps({"native_media": evidence}), self.project["id"], shot_key),
                )
                conn.execute(
                    "UPDATE short_drama_provider_shot_versions SET provider='minimax_h3',"
                    "file=?,url=? WHERE project_id=? AND shot_key=?",
                    (
                        evidence["derived"]["file"],
                        "/api/gen/file/" + evidence["derived"]["file"],
                        self.project["id"], shot_key,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        version = self.repaired_version("real-delivery-" + ratio)
        self.confirm_version(version)
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "CONTENT_OUT": str(root), "FFMPEG_BIN": ffmpeg,
            "FFPROBE_BIN": ffprobe,
        }):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "real-delivery-job-" + ratio,
                deduct_points=lambda *args: None,
                refund_points=lambda *args: None,
            )
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"], job.get("error"))
        output = root / job["result"]["url"].removeprefix("/api/gen/file/")
        conn = self.db()
        try:
            snapshot = json.loads(conn.execute(
                "SELECT snapshot_json FROM short_drama_delivery_versions "
                "WHERE job_id=?", (job["id"],),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(
            [1000, 1000],
            [item["duration_ms"] for item in snapshot["native_inputs"]],
        )
        probe = short_drama_refinement.media_plan.probe_media(output)
        self.assertEqual(expected_size, (
            int(probe["video"]["width"]), int(probe["video"]["height"]),
        ))
        self.assertIsNotNone(probe["audio"])
        self.assertLessEqual(abs(int(probe["duration_ms"]) - 2000), 300)
        second_shot_pixel = subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "1.5",
            "-i", str(output), "-frames:v", "1", "-vf",
            "scale=1:1,format=rgb24", "-f", "rawvideo", "-",
        ], capture_output=True, timeout=30)
        self.assertEqual(0, second_shot_pixel.returncode, second_shot_pixel.stderr)
        self.assertGreaterEqual(len(second_shot_pixel.stdout), 3)
        red, green, _blue = second_shot_pixel.stdout[:3]
        self.assertGreater(green, red, "the locked second shot must start at 1s")
        subtitle = subprocess.run([
            ffprobe, "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index", "-of", "csv=p=0", str(output),
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, subtitle.returncode, subtitle.stderr)
        self.assertTrue(subtitle.stdout.strip())

    def test_real_ffmpeg_horizontal_formal_delivery_contract(self):
        self._assert_real_formal_delivery("16:9", "1280x720", (2560, 1440))

    def test_real_ffmpeg_vertical_formal_delivery_contract(self):
        self._assert_real_formal_delivery("9:16", "720x1280", (1440, 2560))

    def test_single_shot_job_creates_new_issue_free_version(self):
        preview = short_drama_refinement.preview_change(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
        )
        self.assertEqual(["shot_02"], preview["affected_shots"])
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "redo-shot-02",
        )
        replay = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "redo-shot-02",
        )
        self.assertEqual(job["id"], replay["id"])
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("succeeded", job["status"])
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual([], workspace["current_refinement"]["issues"])

    def test_candidate_adoption_defers_full_film_reassembly(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        job = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
            }, "adopt-shot-02-without-reassembly",
        )
        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

            self.assertEqual("succeeded", job["status"])
            self.assertTrue(job["result"]["candidate_adopted"])
            self.assertTrue(job["result"]["reassembly_required"])
            self.assertEqual(0, self.refinement_renderer_mock.call_count)

            current = short_drama_refinement.workspace(
                self.db, "alice", "alice", self.project["id"]
            )["current_refinement"]
            self.assertEqual(before["url"], current["url"])
            self.assertEqual(before["preview_file_hash"], current["preview_file_hash"])
            self.assertEqual([], current["issues"])
            target = next(
                shot for shot in current["shots"] if shot["shot_key"] == "shot_02"
            )
            self.assertEqual(replacement_id, target["provider_version_id"])
            self.assertEqual("provider_regeneration", target["visual_source"])
            self.assertEqual(
                replacement_id,
                current["media"]["staged_replacements"][0]["provider_version_id"],
            )
            self.assertTrue(current["assembly_status"]["reassembly_required"])
            self.assertEqual(1, current["assembly_status"]["staged_count"])

            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.confirm_refinement(
                    self.db, "alice", "alice", self.acceptance_body(current)
                )
            self.assertEqual(
                "refinement_reassembly_required", blocked.exception.code
            )

            from content_domains import short_drama_autodraft
            reassembly_calls = []
            with mock.patch.object(
                short_drama_autodraft,
                "_render_provider_preview",
                side_effect=self._reassembly_renderer(reassembly_calls),
            ):
                reassembled = short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": current["id"],
                    }, "reassemble-after-candidate-adoption",
                )
            self.assertEqual(1, len(reassembly_calls))
            self.assertNotEqual(current["id"], reassembled["id"])
            self.assertFalse(reassembled["assembly_status"]["reassembly_required"])
            self.assertEqual(0, reassembled["assembly_status"]["staged_count"])
            self.assertNotIn("staged_replacements", reassembled["media"])
            reassembled_target = next(
                shot for shot in reassembled["shots"]
                if shot["shot_key"] == "shot_02"
            )
            self.assertEqual(
                replacement_id, reassembled_target["provider_version_id"]
            )

    def test_candidate_adoption_endpoint_forces_deferred_reassembly(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        body = {
            "project_id": self.project["id"],
            "shot_key": "shot_02",
            "source_version_id": before["id"],
            "replacement_provider_version_id": replacement_id,
            "defer_reassembly": False,
        }
        job = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", body, "candidate-adoption"
        )
        replay = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", body, "candidate-adoption"
        )
        self.assertEqual(job["id"], replay["id"])
        self.assertTrue(job["defer_reassembly"])
        self.assertTrue(replay["defer_reassembly"])

        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )

        self.assertEqual("succeeded", job["status"])
        self.assertTrue(job["result"]["candidate_adopted"])
        self.assertEqual(0, self.refinement_renderer_mock.call_count)

    def test_candidate_adoption_idempotency_binds_the_source_version(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        body = {
            "project_id": self.project["id"],
            "shot_key": "shot_02",
            "source_version_id": source["id"],
            "replacement_provider_version_id": replacement_id,
        }
        short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", body, "candidate-source-bound-key"
        )

        changed_source = dict(body)
        changed_source["source_version_id"] = "different-source-version"
        with self.assertRaises(short_drama_refinement.RefinementError) as conflict:
            short_drama_refinement.adopt_refinement_candidate(
                self.db, "alice", "alice", changed_source,
                "candidate-source-bound-key",
            )
        self.assertEqual("idempotency_conflict", conflict.exception.code)

    def test_candidate_adoption_http_route_ignores_false_defer_flag(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        handler = Handler(
            "/api/gen/short-drama/refinement/candidates/adopt",
            body={
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
                "defer_reassembly": False,
            },
            key="candidate-adoption-http",
        )
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )

        self.assertTrue(short_drama.dispatch_http(handler, "POST", self.db, verify))
        self.assertEqual(200, handler.response[0])
        self.assertTrue(handler.response[1]["defer_reassembly"])

    def test_legacy_refinement_http_route_ignores_true_defer_flag(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        handler = Handler(
            "/api/gen/short-drama/refinement/jobs",
            body={
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
                "defer_reassembly": True,
            },
            key="legacy-refinement-cannot-defer",
        )
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )

        self.assertTrue(short_drama.dispatch_http(handler, "POST", self.db, verify))
        self.assertEqual(200, handler.response[0])
        self.assertFalse(handler.response[1]["defer_reassembly"])

    def test_candidate_adoption_requires_fixed_problem_shot_and_candidate(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        issue_replacement = self.add_provider_replacement("shot_02")

        for field in ("source_version_id", "replacement_provider_version_id"):
            body = {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": source["id"],
                "replacement_provider_version_id": issue_replacement,
            }
            body.pop(field)
            with self.subTest(missing=field):
                with self.assertRaises(
                    short_drama_refinement.RefinementError
                ) as invalid:
                    short_drama_refinement.adopt_refinement_candidate(
                        self.db, "alice", "alice", body,
                        "candidate-missing-" + field,
                    )
                self.assertEqual("refinement_candidate_invalid", invalid.exception.code)

        normal_replacement = self.add_provider_replacement("shot_01")
        with self.assertRaises(short_drama_refinement.RefinementError) as normal:
            short_drama_refinement.adopt_refinement_candidate(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "shot_key": "shot_01",
                    "source_version_id": source["id"],
                    "replacement_provider_version_id": normal_replacement,
                }, "candidate-normal-shot",
            )
        self.assertEqual("refinement_candidate_not_required", normal.exception.code)

        with self.assertRaises(short_drama_refinement.RefinementError) as stale:
            short_drama_refinement.adopt_refinement_candidate(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "shot_key": "shot_02",
                    "source_version_id": "stale-refinement-version",
                    "replacement_provider_version_id": issue_replacement,
                }, "candidate-stale-source",
            )
        self.assertEqual("refinement_source_stale", stale.exception.code)

        conn = self.db()
        try:
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_jobs"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, jobs)

    def test_staged_candidate_probe_failure_still_requires_reassembly(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        replacement_id = self.add_provider_replacement("shot_02")
        job = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": before["id"],
                "replacement_provider_version_id": replacement_id,
            }, "adopt-before-probe-failure",
        )
        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"])

        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            side_effect=short_drama_refinement.media_plan.MediaPlanError(
                "media_probe_failed", "candidate cannot be probed"
            ),
        ):
            current = short_drama_refinement.workspace(
                self.db, "alice", "alice", self.project["id"]
            )["current_refinement"]
            self.assertFalse(current["assembly_status"]["available"])
            self.assertTrue(current["assembly_status"]["reassembly_required"])
            self.assertEqual(1, current["assembly_status"]["staged_count"])
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.confirm_refinement(
                    self.db, "alice", "alice", self.acceptance_body(current)
                )
        self.assertEqual("refinement_reassembly_required", blocked.exception.code)

    def test_reassembly_rejects_remaining_issues_before_render(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        shots = [dict(item) for item in source["shots"]]
        extra_issue = {
            "code": "manual_review_required",
            "shot_key": "shot_01",
            "message": "first shot still needs review",
        }
        shots[0]["status"] = "degraded"
        shots[0]["issue"] = dict(extra_issue)
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_refinement_versions SET shots_json=?,issues_json=? "
                "WHERE id=?",
                (
                    json.dumps(shots),
                    json.dumps([extra_issue] + list(source["issues"])),
                    source["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        replacement_id = self.add_provider_replacement("shot_02")
        job = short_drama_refinement.adopt_refinement_candidate(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "shot_key": "shot_02",
                "source_version_id": source["id"],
                "replacement_provider_version_id": replacement_id,
            }, "stage-one-of-two-problem-shots",
        )
        with mock.patch.object(
            short_drama_refinement.media_plan,
            "probe_media",
            return_value={
                "duration_ms": 5000,
                "video": {"width": 1280, "height": 720},
                "audio": {},
            },
        ):
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"])
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual(["shot_01"], [
            item["shot_key"] for item in source["issues"]
        ])
        self.assertEqual(1, len(source["media"]["staged_replacements"]))
        from content_domains import short_drama_autodraft

        calls = []
        with mock.patch.object(
            short_drama_autodraft,
            "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "must-not-reassemble-with-open-issues",
                )
        self.assertEqual("refinement_issues_remaining", blocked.exception.code)
        self.assertEqual([], calls)

    def test_candidate_reassembly_rejects_issues_without_side_effects(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        conn = self.db()
        try:
            versions_before = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        from content_domains import short_drama_autodraft

        calls = []
        with mock.patch.object(
            short_drama_autodraft,
            "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "candidate-reassembly-with-issues",
                )
        self.assertEqual("refinement_issues_remaining", blocked.exception.code)
        self.assertEqual([], calls)

        conn = self.db()
        try:
            versions_after = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            operations = conn.execute(
                "SELECT COUNT(*) FROM short_drama_reassembly_operations "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            idempotency = conn.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE idem_key=?",
                ("candidate-reassembly-with-issues",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(versions_before, versions_after)
        self.assertEqual(0, operations)
        self.assertEqual(0, idempotency)

    def test_candidate_reassembly_rejects_legacy_result_with_open_issues(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        from content_domains import short_drama_autodraft

        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft,
            "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            legacy = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                }, "legacy-reassembly-with-issues",
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "candidate-must-not-reuse-legacy-result",
                )
        self.assertNotEqual(source["id"], legacy["id"])
        self.assertEqual("refinement_issues_remaining", blocked.exception.code)
        self.assertEqual(1, len(calls))

    def test_candidate_reassembly_rejects_stale_legacy_completed_result(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        shots = [dict(item, status="ready", issue=None) for item in source["shots"]]
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_refinement_versions SET shots_json=?,issues_json='[]' "
                "WHERE id=?", (json.dumps(shots), source["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        from content_domains import short_drama_autodraft

        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            completed = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                }, "legacy-before-new-issue",
            )
            short_drama_refinement.mark_issue(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": completed["id"],
                    "shot_key": "shot_01",
                    "issue_code": "continuity_error",
                    "message": "new issue after the legacy render",
                },
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as stale:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, "candidate-must-recheck-latest-source",
                )
        self.assertEqual("refinement_issues_remaining", stale.exception.code)
        self.assertEqual(1, len(calls))

    def test_candidate_reassembly_replay_revalidates_latest_source(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        shots = [dict(item, status="ready", issue=None) for item in source["shots"]]
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_refinement_versions SET shots_json=?,issues_json='[]' "
                "WHERE id=?", (json.dumps(shots), source["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        from content_domains import short_drama_autodraft

        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        key = "candidate-replay-must-recheck-latest"
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            completed = short_drama_refinement.reassemble_refinement_candidates(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": source["id"],
                }, key,
            )
            immediate_replay = (
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, key,
                )
            )
            self.assertEqual(completed["id"], immediate_replay["id"])
            short_drama_refinement.mark_issue(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": completed["id"],
                    "shot_key": "shot_01",
                    "issue_code": "continuity_error",
                    "message": "new issue after the strict render",
                },
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as stale:
                short_drama_refinement.reassemble_refinement_candidates(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "version_id": source["id"],
                    }, key,
                )
        self.assertEqual("refinement_version_stale", stale.exception.code)
        self.assertEqual(1, len(calls))

    def test_redo_publishes_new_preview_url_and_physical_hash(self):
        before = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        version = self.repaired_version("physical-redo")
        self.assertNotEqual(before["url"], version["url"])
        self.assertTrue(version["preview_file_hash"])
        target = next(
            shot for shot in version["shots"] if shot["shot_key"] == "shot_02"
        )
        self.assertEqual("provider_regeneration", target["visual_source"])
        self.assertTrue(target["file_hash"])
        self.assertEqual("provider-version-shot_02", target["provider_version_id"])
        self.assertGreaterEqual(self.refinement_renderer_mock.call_count, 1)
        output = Path(self.tmp.name) / version["media"]["preview_file"]
        self.assertTrue(output.is_file())
        self.assertEqual(
            version["preview_file_hash"],
            short_drama_refinement._file_hash(output),
        )

    def test_redo_failure_preserves_issue_and_is_not_reexecuted(self):
        failed_renderer = mock.Mock(side_effect=RuntimeError("renderer failed"))
        with mock.patch.object(
            short_drama_refinement, "_render_refinement_preview", failed_renderer,
        ):
            job = short_drama_refinement.start_refinement_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "shot_key": "shot_02",
                    "replacement_provider_version_id": "provider-version-shot_02",
                }, "failed-physical-redo",
            )
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
            repeated = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["error"]["issue_preserved"])
        self.assertEqual("failed", repeated["status"])
        self.assertEqual(1, failed_renderer.call_count)
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual("shot_02", current["issues"][0]["shot_key"])

    def test_issue_revision_change_while_job_runs_fails_closed(self):
        source = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_02",
                "source_version_id": source["id"],
                "replacement_provider_version_id": "provider-version-shot_02",
            }, "issue-revision-race",
        )
        marked = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "version_id": source["id"],
                "shot_key": "shot_02", "issue_code": "newer_review_issue",
                "message": "A newer issue revision supersedes the queued redo",
            },
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertEqual("refinement_source_stale", job["error"]["code"])
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual(marked["id"], current["id"])
        self.assertEqual("newer_review_issue", current["issues"][0]["code"])

    def test_visual_refinement_uses_preview_audio_fallback_without_locked_voice(self):
        output_relative = "rendered/real-refinement.mp4"
        output = Path(self.tmp.name) / output_relative
        output.parent.mkdir(parents=True, exist_ok=True)

        def assemble(_project_id, _job_id, assembly):
            self.assertEqual(2, len(assembly["shots"]))
            self.assertEqual(
                "provider_audio", assembly["media_contract"]["media_mode"]
            )
            self.assertEqual(
                "refinement_preview_audio_fallback",
                assembly["media_contract"]["evidence_source"],
            )
            self.assertTrue(assembly["media_contract"]["preview_only"])
            output.write_bytes(b"new-immutable-refinement-preview")
            return {
                "file": output_relative,
                "url": "/api/gen/file/" + output_relative,
                "probe": {
                    "duration_ms": 30000,
                    "video": {"width": 1280, "height": 720},
                    "audio": {"codec": "aac"},
                },
            }

        assembler = mock.Mock(side_effect=assemble)
        locked_media = {
            "contract_version": "short-drama-locked-media-v1",
            "delivery_eligible": False,
            "reason": "locked_voice_timeline_missing",
            "audio_hash": "", "subtitle_hash": "",
            "timeline_hash": "", "subtitle_required": False,
            "audio_tracks": [], "subtitles": [],
        }
        provider_probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        from content_domains import short_drama_autodraft
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()
            manifest = json.loads(row[0])
            manifest["media_contract"] = dict(locked_media)
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET manifest_json=? "
                "WHERE id='draft-v1'",
                (json.dumps(manifest),),
            )
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(
            short_drama_refinement, "_render_refinement_preview",
            side_effect=self.real_refinement_renderer,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media",
            return_value=provider_probe,
        ), mock.patch.object(
            short_drama_autodraft, "_locked_media_contract",
            return_value=locked_media,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview", assembler,
        ):
            job = short_drama_refinement.start_refinement_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "shot_key": "shot_02",
                    "replacement_provider_version_id": "provider-version-shot_02",
                }, "real-refinement-path",
            )
            for _ in range(4):
                job = short_drama_refinement.get_refinement_job(
                    self.db, "alice", self.project["id"], job["id"]
                )
        self.assertEqual("succeeded", job["status"], job.get("error"))
        self.assertEqual(1, assembler.call_count)
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        contract = version["media"]["media_contract"]
        self.assertTrue(contract["preview_only"])
        self.assertFalse(contract["delivery_eligible"])
        self.assertEqual("/api/gen/file/" + output_relative, version["url"])
        self.assertEqual(
            short_drama_refinement._file_hash(output),
            version["preview_file_hash"],
        )

        with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", self.acceptance_body(version)
            )
        self.assertEqual("refinement_preview_only_media", blocked.exception.code)

    def test_legacy_draft_without_media_contract_cannot_be_confirmed(self):
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()
            manifest = json.loads(row[0])
            manifest.pop("media_contract", None)
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET manifest_json=? "
                "WHERE id='draft-v1'",
                (json.dumps(manifest),),
            )
            media = dict(current.get("media") or {})
            media.pop("media_contract", None)
            shots = []
            for shot in current.get("shots") or []:
                item = dict(shot)
                item.pop("issue", None)
                item["status"] = "ready"
                shots.append(item)
            conn.execute(
                "UPDATE short_drama_refinement_versions SET media_json=?,"
                "shots_json=?,issues_json='[]' WHERE id=?",
                (json.dumps(media), json.dumps(shots), current["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", self.acceptance_body(current)
            )
        self.assertEqual("delivery_media_incomplete", blocked.exception.code)

    def test_preview_only_acceptance_cannot_be_quoted_or_started(self):
        version = self.repaired_version("preview-only-delivery-gates")
        self.confirm_version(version)
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )

        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            manifest = json.loads(conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()[0])
            contract = dict(manifest["media_contract"])
            contract.update({
                "delivery_eligible": False,
                "preview_only": True,
                "reason": "refinement_preview_audio_fallback",
            })
            manifest["media_contract"] = contract
            source = short_drama_refinement._refinement(conn.execute(
                "SELECT * FROM short_drama_refinement_versions WHERE id=?",
                (version["id"],),
            ).fetchone())
            media = dict(source["media"])
            media["media_contract"] = contract
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET manifest_json=? "
                "WHERE id='draft-v1'",
                (json.dumps(manifest),),
            )
            conn.execute(
                "UPDATE short_drama_refinement_versions SET media_json=? WHERE id=?",
                (json.dumps(media), version["id"]),
            )
            project = dict(conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone())
            source = short_drama_refinement._refinement(conn.execute(
                "SELECT * FROM short_drama_refinement_versions WHERE id=?",
                (version["id"],),
            ).fetchone())
            source_hashes, snapshot = short_drama_refinement._acceptance_evidence(
                conn, project, source
            )
            conn.execute(
                "UPDATE short_drama_refinement_acceptances SET "
                "source_hashes_json=?,snapshot_json=?,snapshot_hash=? "
                "WHERE refinement_version_id=?",
                (
                    json.dumps(source_hashes), json.dumps(snapshot),
                    short_drama_refinement._hash(snapshot), version["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
            short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_preview_only_media", blocked.exception.code)

        with self.assertRaises(short_drama_refinement.RefinementError) as blocked:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "preview-only-start-blocked",
            )
        self.assertEqual("refinement_preview_only_media", blocked.exception.code)

    def _reassembly_source(self):
        return short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]

    def _reassembly_renderer(self, calls=None, barrier=None, fail=False):
        from content_domains import short_drama_autodraft

        def render(project_id, render_id, assembly):
            if calls is not None:
                calls.append(render_id)
            if barrier is not None:
                barrier.wait(timeout=5)
            target = (
                Path(self.tmp.name) / "short_drama_autodraft" /
                project_id / render_id / "preview-720p.mp4"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"complete-reassembly-preview")
            if fail:
                raise short_drama_autodraft.AutodraftError(
                    "preview_render_failed", "render failed", 409
                )
            return {
                "file": target.relative_to(self.tmp.name).as_posix(),
                "url": "/api/gen/file/" + target.relative_to(
                    self.tmp.name
                ).as_posix(),
                "probe": {
                    "duration_ms": 10000,
                    "video": {"width": 1280, "height": 720},
                    "audio": {"codec": "aac"},
                },
                "duration_ms": 10000,
            }
        return render

    def test_reassembly_is_zero_cost_idempotent_and_preserves_history(self):
        source = self._reassembly_source()
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_refinement_acceptances "
                "(id,project_id,refinement_version_id,checklist_json,"
                "source_hashes_json,snapshot_json,snapshot_hash,accepted_by,"
                "accepted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "accept-before-reassembly", self.project["id"], source["id"],
                    "{}", "{}", "{}", "snapshot-before-reassembly", "alice",
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720},
            "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            first = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "version_id": source["id"],
                }, "reassembly-idempotent",
            )
            replay = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "version_id": source["id"],
                }, "reassembly-idempotent",
            )
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(1, len(calls))
        self.assertEqual(0, first["points_charged"])
        self.assertFalse(first["provider_called"])
        conn = self.db()
        try:
            versions = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            charges = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
            invalidation = conn.execute(
                "SELECT invalidation_reason FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (source["id"],),
            ).fetchone()[0]
            historical = conn.execute(
                "SELECT id FROM short_drama_refinement_versions WHERE id=?",
                (source["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(2, versions)
        self.assertEqual(0, charges)
        self.assertEqual("preview_reassembled", invalidation)
        self.assertEqual(source["id"], historical[0])

    def test_reassembly_rejects_missing_key_conflict_stale_and_invalid_media(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "Idempotency-Key"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, ""
            )
        idem_db = short_drama_refinement._idempotency_db_factory(self.db)
        short_drama_refinement.submission_idempotency.begin(
            idem_db, "alice", short_drama_refinement._REASSEMBLY_ENDPOINT,
            "reassembly-conflict", body,
        )
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "不同重新装配请求"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "version_id": "other"},
                "reassembly-conflict",
            )
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "正在处理中"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, "reassembly-conflict"
            )
        with self.assertRaisesRegex(short_drama_refinement.RefinementError, "预览版本已变化"):
            short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "version_id": "stale"},
                "reassembly-stale",
            )
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media",
            return_value={"duration_ms": 0, "video": None, "audio": None},
        ):
            with self.assertRaisesRegex(short_drama_refinement.RefinementError, "有效视频流"):
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "reassembly-invalid-media"
                )

    def test_reassembly_rejects_missing_and_uncontrolled_provider_files(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        conn = self.db()
        try:
            original = conn.execute(
                "SELECT file FROM short_drama_provider_shot_versions WHERE id=?",
                ("provider-version-shot_01",),
            ).fetchone()[0]
            for index, (provider_file, expected) in enumerate((
                ("provider/missing.mp4", "provider_asset_missing"),
                ("../outside.mp4", "provider_asset_path_invalid"),
            )):
                conn.execute(
                    "UPDATE short_drama_provider_shot_versions SET file=? WHERE id=?",
                    (provider_file, "provider-version-shot_01"),
                )
                conn.commit()
                with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                    short_drama_refinement.reassemble_refinement(
                        self.db, "alice", "alice", body,
                        "reassembly-invalid-file-%d" % index,
                    )
                self.assertEqual(expected, raised.exception.code)
            conn.execute(
                "UPDATE short_drama_provider_shot_versions SET file=? WHERE id=?",
                (original, "provider-version-shot_01"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_reassembly_failure_cleans_output_and_aborts_idempotency(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        from content_domains import short_drama_autodraft
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(fail=True),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "reassembly-failure"
                )
        root = Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"]
        self.assertFalse(any(root.glob("reassembly-*")) if root.exists() else False)
        conn = self.db()
        try:
            idem = conn.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE idem_key=?",
                ("reassembly-failure",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, idem)

    def test_concurrent_reassembly_creates_one_version_and_one_render(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        calls, results, errors = [], [], []
        render_started = threading.Event()
        release_render = threading.Event()
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft

        def worker(key):
            try:
                results.append(short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, key
                ))
            except Exception as error:
                errors.append(error)

        renderer = self._reassembly_renderer(calls)

        def blocking_renderer(project_id, render_id, assembly):
            render_started.set()
            release_render.wait(timeout=5)
            return renderer(project_id, render_id, assembly)

        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=blocking_renderer,
        ):
            first = threading.Thread(
                target=worker, args=("concurrent-reassembly-0",)
            )
            second = threading.Thread(
                target=worker, args=("concurrent-reassembly-1",)
            )
            first.start()
            self.assertTrue(render_started.wait(timeout=5))
            second.start()
            second.join(timeout=5)
            release_render.set()
            threads = [first, second]
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertEqual(
            "refinement_reassembly_in_progress", errors[0].code
        )
        self.assertEqual(1, len(calls))
        conn = self.db()
        try:
            versions = conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_versions "
                "WHERE project_id=?", (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(2, versions)

    def test_expired_reassembly_lease_is_taken_over_and_orphan_is_cleaned(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        orphan_id = "reassembly-crashed-worker"
        orphan = (
            Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"] /
            orphan_id / "preview-720p.mp4"
        )
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"incomplete-output")
        conn = self.db()
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO short_drama_reassembly_operations "
                "(id,project_id,source_version_id,status,lease_token,lease_owner,"
                "lease_expires_at,heartbeat_at,render_id,created_at,updated_at) "
                "VALUES (?,?,?,'processing',?,?,?,?,?,?,?)",
                (
                    "crashed-operation", self.project["id"], source["id"],
                    "dead-token", "dead-worker", now - 1, now - 60,
                    orphan_id, now - 60, now - 60,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(),
        ):
            result = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, "expired-lease-takeover"
            )
        self.assertFalse(orphan.parent.exists())
        conn = self.db()
        try:
            operation = conn.execute(
                "SELECT status,lease_token,refinement_version_id FROM "
                "short_drama_reassembly_operations WHERE id='crashed-operation'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("succeeded", operation[0])
        self.assertIsNone(operation[1])
        self.assertEqual(result["id"], operation[2])

    def test_reassembly_heartbeat_prevents_takeover_during_long_render(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        started = threading.Event()
        calls, result, errors = [], [], []
        renderer = self._reassembly_renderer(calls)

        def slow_renderer(project_id, render_id, assembly):
            started.set()
            time.sleep(4.2)
            return renderer(project_id, render_id, assembly)

        def first_worker():
            try:
                result.append(short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "heartbeat-owner"
                ))
            except Exception as error:
                errors.append(error)

        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 3,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=slow_renderer,
        ):
            worker = threading.Thread(target=first_worker)
            worker.start()
            self.assertTrue(started.wait(timeout=5))
            time.sleep(3.4)
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "heartbeat-contender"
                )
            self.assertEqual(
                "refinement_reassembly_in_progress", raised.exception.code
            )
            worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(1, len(result))
        self.assertEqual(1, len(calls))

    def test_reassembly_heartbeat_prevents_takeover_during_slow_preprocessing(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        preprocessing_started = threading.Event()
        release_preprocessing = threading.Event()
        calls, result, errors = [], [], []
        probe_calls = 0
        probe_lock = threading.Lock()
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft

        def slow_probe(_path):
            nonlocal probe_calls
            with probe_lock:
                probe_calls += 1
                first = probe_calls == 1
            if first:
                preprocessing_started.set()
                release_preprocessing.wait(timeout=8)
            return probe

        def first_worker():
            try:
                result.append(short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "slow-preprocess-owner"
                ))
            except Exception as error:
                errors.append(error)

        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 3,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", side_effect=slow_probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            worker = threading.Thread(target=first_worker)
            worker.start()
            self.assertTrue(preprocessing_started.wait(timeout=5))
            time.sleep(3.4)
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "slow-preprocess-contender"
                )
            self.assertEqual(
                "refinement_reassembly_in_progress", raised.exception.code
            )
            release_preprocessing.set()
            worker.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(1, len(result))
        self.assertEqual(1, len(calls))

    def test_reassembly_stops_before_render_after_heartbeat_loss(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        renewals = 0

        def renewal(*_args, **_kwargs):
            nonlocal renewals
            renewals += 1
            return renewals == 1

        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 1,
        ), mock.patch.object(
            short_drama_refinement, "_heartbeat_reassembly_operation",
            side_effect=renewal,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media",
            side_effect=lambda _path: (time.sleep(1.3), probe)[1],
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(calls),
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "heartbeat-loss"
                )
        self.assertEqual("refinement_reassembly_lease_lost", raised.exception.code)
        self.assertEqual([], calls)

    def test_reassembly_cancels_running_renderer_after_heartbeat_loss(self):
        source = self._reassembly_source()
        body = {"project_id": self.project["id"], "version_id": source["id"]}
        renderer_started = threading.Event()
        renderer_cancelled = threading.Event()
        successful_calls = []
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        renewals = 0

        def renewal(*_args, **_kwargs):
            nonlocal renewals
            renewals += 1
            return renewals == 1

        def cancellable_renderer(project_id, render_id, assembly):
            cancel_event = assembly["_cancel_event"]
            partial = (
                Path(self.tmp.name) / "short_drama_autodraft" / project_id /
                render_id / "preview-720p.mp4"
            )
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            renderer_started.set()
            self.assertTrue(cancel_event.wait(timeout=5))
            renderer_cancelled.set()
            raise short_drama_autodraft.AutodraftError(
                "preview_render_cancelled", "cancelled", 409,
            )

        with mock.patch.object(
            short_drama_refinement, "_REASSEMBLY_LEASE_SECONDS", 1,
        ), mock.patch.object(
            short_drama_refinement, "_heartbeat_reassembly_operation",
            side_effect=renewal,
        ), mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=cancellable_renderer,
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.reassemble_refinement(
                    self.db, "alice", "alice", body, "render-heartbeat-loss"
                )
        self.assertTrue(renderer_started.is_set())
        self.assertTrue(renderer_cancelled.is_set())
        self.assertEqual("refinement_reassembly_lease_lost", raised.exception.code)
        self.assertFalse(any(
            (Path(self.tmp.name) / "short_drama_autodraft" / self.project["id"]).glob(
                "reassembly-*"
            )
        ))

        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(successful_calls),
        ):
            result = short_drama_refinement.reassemble_refinement(
                self.db, "alice", "alice", body, "render-heartbeat-takeover"
            )
        self.assertEqual(1, len(successful_calls))
        conn = self.db()
        try:
            operation = conn.execute(
                "SELECT status,refinement_version_id FROM "
                "short_drama_reassembly_operations WHERE project_id=? "
                "AND source_version_id=?", (self.project["id"], source["id"]),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(("succeeded", result["id"]), tuple(operation))

    def test_reassembly_http_owner_editor_succeed_and_viewer_is_read_only(self):
        source = self._reassembly_source()
        board_id = "shared-refinement-board"
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET board_id=? WHERE id=?",
                (board_id, self.project["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        roles = {"alice": "owner", "bob": "editor", "eve": "viewer"}
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        access = lambda handler: {
            "board_id": board_id, "role": roles[handler._token()],
        }
        probe = {
            "duration_ms": 5000,
            "video": {"width": 1280, "height": 720}, "audio": None,
        }
        from content_domains import short_drama_autodraft
        with mock.patch.object(
            short_drama_refinement.media_plan, "probe_media", return_value=probe,
        ), mock.patch.object(
            short_drama_autodraft, "_render_provider_preview",
            side_effect=self._reassembly_renderer(),
        ):
            owner = Handler(
                "/api/gen/short-drama/refinement/reassemble",
                body={"project_id": self.project["id"], "version_id": source["id"]},
                key="owner-http-reassembly", token="alice",
            )
            self.assertTrue(short_drama.dispatch_http(
                owner, "POST", self.db, verify, canvas_access_resolver=access,
            ))
            self.assertEqual(200, owner.response[0])

            editor = Handler(
                "/api/gen/short-drama/refinement/reassemble",
                body={
                    "project_id": self.project["id"],
                    "version_id": owner.response[1]["id"],
                },
                key="editor-http-reassembly", token="bob",
            )
            self.assertTrue(short_drama.dispatch_http(
                editor, "POST", self.db, verify, canvas_access_resolver=access,
            ))
            self.assertEqual(200, editor.response[0])

            viewer = Handler(
                "/api/gen/short-drama/refinement/reassemble",
                body={
                    "project_id": self.project["id"],
                    "version_id": editor.response[1]["id"],
                },
                key="viewer-http-reassembly", token="eve",
            )
            self.assertTrue(short_drama.dispatch_http(
                viewer, "POST", self.db, verify, canvas_access_resolver=access,
            ))
        self.assertEqual(403, viewer.response[0])

    def test_confirm_quote_and_formal_delivery_snapshot(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "fix-for-delivery",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        version = workspace["current_refinement"]
        confirmed = self.confirm_version(version)
        self.assertEqual("confirmed", confirmed["status"])
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice",
            {"project_id": self.project["id"], "version_id": version["id"]},
        )
        self.assertEqual(0, quote["cost"])
        delivery = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "formal-delivery",
        )
        for _ in range(6):
            delivery = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], delivery["id"]
            )
        self.assertEqual("succeeded", delivery["status"])
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("demo_ready", workspace["state"])
        self.assertEqual("source", workspace["current_delivery"]["snapshot"]["resolution"])
        self.assertEqual(
            "demo_preview",
            workspace["current_delivery"]["snapshot"]["output_kind"],
        )
        self.assertFalse(workspace["current_delivery"]["snapshot"]["deliverable"])
        self.assertTrue(workspace["current_delivery"]["snapshot"]["immutable"])

    def test_delivery_quote_is_bound_to_exact_acceptance_snapshot(self):
        version = self.confirmed_version("quote-before-reacceptance")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            acceptance = short_drama_refinement._acceptance(conn.execute(
                "SELECT * FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone())
        finally:
            conn.close()
        changed = dict(acceptance)
        changed["snapshot"] = dict(acceptance["snapshot"])
        changed["snapshot"]["media_contract"] = dict(
            acceptance["snapshot"]["media_contract"]
        )
        changed["snapshot"]["media_contract"]["timeline_hash"] = "changed"
        changed["snapshot_hash"] = short_drama_refinement._hash(
            changed["snapshot"]
        )
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "_valid_acceptance", return_value=changed,
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "stale-acceptance-quote", deduct_points=deduct,
            )
        self.assertEqual("delivery_source_changed", raised.exception.code)
        deduct.assert_not_called()

    def test_provider_audio_preference_reuses_generated_shot_sound(self):
        from content_domains import short_drama_autodraft

        saved = short_drama_refinement.set_media_preference(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "mode": "provider_audio",
            },
        )
        self.assertEqual("provider_audio", saved["mode"])
        conn = self.db()
        conn.row_factory = sqlite3.Row
        try:
            project = conn.execute(
                "SELECT * FROM short_drama_projects WHERE id=?",
                (self.project["id"],),
            ).fetchone()
            contract = short_drama_autodraft._locked_media_contract(conn, project)
        finally:
            conn.close()
        self.assertTrue(contract["delivery_eligible"])
        self.assertEqual("provider_audio", contract["media_mode"])
        self.assertEqual(
            "explicit_provider_audio_confirmation",
            contract["evidence_source"],
        )
        self.assertFalse(contract["silent_confirmed"])
        self.assertEqual([], contract["audio_tracks"])

    def test_delivery_revalidates_provider_audio_with_project_shot_count(self):
        short_drama_refinement.set_media_preference(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "mode": "provider_audio",
            },
        )
        version = self.confirmed_version("provider-audio-formal-delivery")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        delivery = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "quote_token": quote["quote_token"],
            }, "provider-audio-formal-delivery-job",
        )
        with mock.patch.object(
            short_drama_refinement, "_valid_acceptance",
            wraps=short_drama_refinement._valid_acceptance,
        ) as validate_acceptance:
            for _ in range(4):
                delivery = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], delivery["id"]
                )
        completion_project = validate_acceptance.call_args.args[1]
        self.assertEqual(6, completion_project.get("shot_count"))
        self.assertEqual("succeeded", delivery["status"], delivery.get("error"))

    def test_production_delivery_is_closed_without_real_executor(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "close-paid-delivery",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        deduct = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_DEV_FREE": "0",
                "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "production",
                "HQ_SHORT_DRAMA_FORMAL_COST": "80",
            },
            clear=False,
        ):
            workspace = short_drama_refinement.workspace(
                self.db, "alice", "alice", self.project["id"]
            )
            self.assertEqual("disabled", workspace["billing"]["mode"])
            self.assertFalse(workspace["billing"]["delivery_enabled"])
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.create_delivery_quote(
                    self.db, "alice",
                    {"project_id": self.project["id"], "version_id": version["id"]},
                )
            with self.assertRaises(short_drama_refinement.RefinementError) as start_error:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": "must-not-create-anything",
                    },
                    "disabled-production-delivery",
                    deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("formal_delivery_unavailable", raised.exception.code)
        self.assertEqual(
            "formal_delivery_unavailable", start_error.exception.code
        )
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_quotes"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_attempts"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_delivery_jobs"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_unresolved_issues_block_confirmation(self):
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "version_id": workspace["current_refinement"]["id"],
                },
            )
        self.assertEqual("refinement_issues_remaining", raised.exception.code)

    def test_issue_free_confirmation_requires_complete_checklist_and_current_hashes(self):
        version = self.repaired_version("acceptance-contract")
        with self.assertRaises(short_drama_refinement.RefinementError) as missing:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_acceptance_incomplete", missing.exception.code)
        stale = self.acceptance_body(version)
        stale["source_hashes"] = dict(stale["source_hashes"], audio="changed")
        with self.assertRaises(short_drama_refinement.RefinementError) as rejected:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", stale,
            )
        self.assertEqual("refinement_acceptance_stale", rejected.exception.code)
        confirmed = self.confirm_version(version)
        self.assertTrue(confirmed["acceptance"]["valid"])
        self.assertEqual("alice", confirmed["acceptance"]["accepted_by"])

    def test_incomplete_assembly_blocks_confirmation_in_authoritative_transaction(self):
        version = self.repaired_version("incomplete-assembly-confirmation")
        body = self.acceptance_body(version)
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={
                "available": True,
                "reassembly_required": True,
                "message": "preview is missing shot duration",
            },
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.confirm_refinement(
                    self.db, "alice", "alice", body,
                )
        self.assertEqual("refinement_reassembly_required", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone()[0])
            self.assertEqual("draft", conn.execute(
                "SELECT status FROM short_drama_refinement_versions WHERE id=?",
                (version["id"],),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_unavailable_assembly_blocks_confirmation_without_side_effects(self):
        version = self.repaired_version("unavailable-assembly-confirmation")
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": False, "reassembly_required": False},
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.confirm_refinement(
                self.db, "alice", "alice", self.acceptance_body(version),
            )
        self.assertEqual("refinement_assembly_unavailable", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone()[0])
            self.assertEqual("draft", conn.execute(
                "SELECT status FROM short_drama_refinement_versions WHERE id=?",
                (version["id"],),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_incomplete_assembly_blocks_quote_without_creating_one(self):
        version = self.repaired_version("incomplete-assembly-quote")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={"available": False, "reassembly_required": False},
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.create_delivery_quote(
                    self.db, "alice", {
                        "project_id": self.project["id"],
                        "version_id": version["id"],
                    },
                )
        self.assertEqual("refinement_assembly_unavailable", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_quotes"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_reassembly_required_blocks_quote_without_creating_one(self):
        version = self.repaired_version("reassembly-required-quote")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": True},
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_reassembly_required", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_quotes"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_delivery_quote_rechecks_acceptance_after_concurrent_invalidation(self):
        version = self.repaired_version("quote-acceptance-race")
        self.confirm_version(version)

        def invalidate_during_media_validation(_source, _capability):
            conn = self.db()
            try:
                conn.execute(
                    "UPDATE short_drama_refinement_acceptances "
                    "SET invalidated_at=?,invalidation_reason='concurrent change' "
                    "WHERE refinement_version_id=?",
                    (int(time.time()), version["id"]),
                )
                conn.commit()
            finally:
                conn.close()

        with mock.patch.object(
            short_drama_refinement, "_revalidate_delivery_native_sources",
            side_effect=invalidate_during_media_validation,
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_acceptance_required", raised.exception.code)
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_quotes"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_incomplete_assembly_blocks_start_before_charge_or_job(self):
        version = self.repaired_version("incomplete-assembly-start")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": False},
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement,
            "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": True},
        ):
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    }, "incomplete-assembly-start", deduct_points=deduct,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("refinement_reassembly_required", raised.exception.code)
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_unavailable_assembly_blocks_start_before_charge_or_job(self):
        version = self.repaired_version("unavailable-assembly-start")
        self.confirm_version(version)
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": True, "reassembly_required": False},
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "_refinement_assembly_status",
            return_value={"available": False, "reassembly_required": False},
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "unavailable-assembly-start", deduct_points=deduct,
                project_usage=short_drama._project_point_usage,
            )
        self.assertEqual("refinement_assembly_unavailable", raised.exception.code)
        deduct.assert_not_called()
        conn = self.db()
        try:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_mark_issue_invalidates_acceptance_until_redo_and_reacceptance(self):
        version = self.confirmed_version("accept-before-issue")
        marked = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
                "shot_key": "shot_01", "issue_code": "continuity_error",
                "message": "人物动作不连续",
            },
        )
        self.assertEqual("draft", marked["status"])
        self.assertEqual("shot_01", marked["issues"][0]["shot_key"])
        conn = self.db()
        try:
            acceptance = conn.execute(
                "SELECT invalidation_reason FROM short_drama_refinement_acceptances "
                "WHERE refinement_version_id=?", (version["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("issue_reported", acceptance[0])
        replacement_id = self.add_provider_replacement("shot_01")
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_01",
                "source_version_id": marked["id"],
                "replacement_provider_version_id": replacement_id,
            }, "redo-reported-shot",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        current = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual([], current["current_refinement"]["issues"])
        self.assertIsNone(current["acceptance"])

    def test_marked_v3_rejects_historical_v2_until_new_v4_exists(self):
        current = self.confirmed_version("provider-monotonic-v2")
        marked_v2 = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "version_id": current["id"],
                "shot_key": "shot_02", "issue_code": "visual_regression",
                "message": "V2 needs a new Provider render",
            },
        )
        v3_id = self.add_provider_replacement("shot_02")
        v3_job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_02",
                "source_version_id": marked_v2["id"],
                "replacement_provider_version_id": v3_id,
            }, "provider-monotonic-v3",
        )
        for _ in range(4):
            v3_job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], v3_job["id"]
            )
        self.assertEqual("succeeded", v3_job["status"])
        current_v3 = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        target_v3 = next(
            item for item in current_v3["shots"]
            if item["shot_key"] == "shot_02"
        )
        self.assertEqual(3, target_v3["provider_version"])

        marked_v3 = short_drama_refinement.mark_issue(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "version_id": current_v3["id"], "shot_key": "shot_02",
                "issue_code": "visual_regression",
                "message": "V3 has a newly reported visual problem",
            },
        )
        issue = marked_v3["issues"][0]
        self.assertEqual(3, issue["provider_version_floor"])
        self.assertEqual(v3_id, issue["source_provider_version_id"])
        with self.assertRaises(short_drama_refinement.RefinementError) as old:
            short_drama_refinement.start_refinement_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"], "shot_key": "shot_02",
                    "source_version_id": marked_v3["id"],
                    "replacement_provider_version_id": "provider-version-shot_02",
                }, "provider-history-v2-must-fail",
            )
        self.assertEqual("refinement_new_provider_asset_required", old.exception.code)
        unchanged = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual(marked_v3["id"], unchanged["id"])
        self.assertEqual("shot_02", unchanged["issues"][0]["shot_key"])

        v4_id = self.add_provider_replacement("shot_02")
        v4_job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "shot_key": "shot_02",
                "source_version_id": marked_v3["id"],
                "replacement_provider_version_id": v4_id,
            }, "provider-monotonic-v4",
        )
        for _ in range(4):
            v4_job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], v4_job["id"]
            )
        self.assertEqual("succeeded", v4_job["status"])
        self.assertEqual(issue["issue_id"], v4_job["result"]["issue_revision"])
        self.assertEqual(
            v3_id, v4_job["result"]["source_provider_version_id"]
        )
        self.assertEqual(
            "provider-job-shot_02-4",
            v4_job["result"]["replacement_provider_job_id"],
        )
        repaired_v4 = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.assertEqual([], repaired_v4["issues"])
        target_v4 = next(
            item for item in repaired_v4["shots"]
            if item["shot_key"] == "shot_02"
        )
        self.assertEqual(4, target_v4["provider_version"])

    def test_locked_audio_change_invalidates_previous_acceptance(self):
        version = self.confirmed_version("accept-before-audio-change")
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT manifest_json FROM short_drama_autodraft_versions "
                "WHERE id='draft-v1'"
            ).fetchone()
            manifest = json.loads(row[0])
            manifest["media_contract"]["audio_hash"] = "new-audio-hash"
            conn.execute(
                "UPDATE short_drama_autodraft_versions SET manifest_json=? WHERE id='draft-v1'",
                (json.dumps(manifest),),
            )
            conn.commit()
        finally:
            conn.close()
        workspace = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertFalse(workspace["acceptance"]["valid"])
        self.assertEqual(
            "source_changed", workspace["acceptance"]["invalidation_reason"]
        )
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
        self.assertEqual("refinement_acceptance_required", raised.exception.code)

    def test_delivery_render_exception_is_persisted_as_terminal_failure(self):
        version = self.confirmed_version("terminal-delivery-failure")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice", {
                "project_id": self.project["id"], "version_id": version["id"],
            },
        )
        job = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice", {
                "project_id": self.project["id"], "quote_token": quote["quote_token"],
            }, "terminal-render-job",
        )
        for _ in range(3):
            job = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        with mock.patch.object(
            short_drama_refinement, "_complete_delivery",
            side_effect=OSError("renderer unavailable"),
        ) as render:
            job = short_drama_refinement.get_delivery_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        self.assertEqual("failed", job["status"])
        self.assertEqual("formal_render_failed", job["error"]["code"])
        replay = short_drama_refinement.get_delivery_job(
            self.db, "alice", self.project["id"], job["id"]
        )
        self.assertEqual("failed", replay["status"])
        self.assertEqual(job["poll_count"], replay["poll_count"])
        render.assert_called_once()

    def test_paid_2k_render_failure_refunds_the_delivery_charge(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("paid-2k-render-refund")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        deduct = mock.Mock()
        refund_spy = mock.Mock()

        def refund(*args, **kwargs):
            return refund_spy(*args, **kwargs)
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "paid-2k-render-refund-job",
                deduct_points=deduct,
                refund_points=refund,
            )
            for _ in range(3):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=refund,
                )
            with mock.patch.object(
                short_drama_refinement,
                "_complete_delivery",
                side_effect=OSError("2k renderer unavailable"),
            ):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=refund,
                )
        self.assertEqual("failed", job["status"])
        deduct.assert_called_once()
        refund_spy.assert_called_once()
        self.assertEqual(10, refund_spy.call_args.args[1])
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts WHERE job_id=?",
                (job["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)

    def test_real_delivery_revalidates_copied_attempt_snapshot_before_render(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("attempt-snapshot-render-toctou")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        real_copy = short_drama_autodraft.shutil.copyfileobj
        deduct = mock.Mock()
        refund = mock.Mock()
        render = mock.Mock()
        tampered = {}

        def render_tampered(_sources, _ratio, duration_ms, _contract, output, **_kwargs):
            output.write_bytes(b"rendered-from-tampered-attempt-snapshot")
            return {
                "probe": {
                    "video": {"width": 2560, "height": 1440},
                    "audio": {"codec": "aac"}, "duration_ms": duration_ms,
                },
                "subtitle_streams": 0,
                "native_audio": {"audible": True},
                "sha256": short_drama_refinement._file_hash(output),
            }

        render.side_effect = render_tampered

        def copy_then_replace(reader, writer, length):
            real_copy(reader, writer, length=length)
            destination = Path(writer.name)
            if destination.parent.name != ".sources":
                return
            writer.seek(0)
            writer.truncate()
            writer.write(b"tampered-during-delivery-render-copy")
            writer.flush()
            tampered["destination"] = destination

        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_autodraft.shutil.copyfileobj",
            side_effect=copy_then_replace,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ), mock.patch(
            "content_domains.short_drama_formal_renderer.render_native_2k",
            render,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
            )
            job = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "attempt-snapshot-render-toctou",
                deduct_points=deduct,
                refund_points=refund,
            )
            for _ in range(4):
                job = short_drama_refinement.get_delivery_job(
                    self.db, "alice", self.project["id"], job["id"],
                    refund_points=refund,
                )

        self.assertIn("destination", tampered)
        self.assertEqual("failed", job["status"])
        self.assertEqual("provider_native_media_changed", job["error"]["code"])
        self.assertEqual("refunded", job.get("refund_state"))
        deduct.assert_called_once()
        refund.assert_called_once()
        render.assert_not_called()
        target = (
            Path(self.tmp.name) / "short_drama_delivery" /
            self.project["id"] / job["id"]
        )
        self.assertFalse(target.exists())

    def test_local_ffmpeg_capability_reports_missing_tools(self):
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "FFMPEG_BIN": "missing-ffmpeg", "FFPROBE_BIN": "missing-ffprobe",
        }), mock.patch.object(
            short_drama_refinement.subprocess, "run",
            side_effect=FileNotFoundError("missing"),
        ):
            capability = short_drama_refinement._delivery_capability()
        self.assertFalse(capability["delivery_enabled"])
        self.assertFalse(capability["deliverable"])
        self.assertEqual("missing_ffmpeg", capability["reason"])

        project = {"target_duration": 60}
        refinement = {
            "media": {"media_validation": {"duration_ms": 64480}},
            "shots": [{"end_ms": 60000}],
        }
        self.assertEqual(
            60000,
            short_drama_refinement._confirmed_refinement_duration_ms(
                project, refinement
            ),
        )

    def test_local_ffmpeg_capability_rejects_each_required_dependency(self):
        def run_result(command, encoders=" V..... libx264 A..... aac "):
            if command[-1] == "-encoders":
                return mock.Mock(returncode=0, stdout=encoders, stderr="")
            return mock.Mock(returncode=0, stdout="available", stderr="")

        cases = (
            ("ffprobe", "missing_ffprobe", None),
            ("libx264", "missing_libx264", " A..... aac "),
            ("aac", "missing_aac", " V..... libx264 "),
            ("output_writable", "missing_output_writable", None),
        )
        for dependency, expected_reason, encoders in cases:
            with self.subTest(dependency=dependency), mock.patch.dict(
                os.environ,
                {"HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg"},
            ), mock.patch.object(
                short_drama_refinement.subprocess, "run",
                side_effect=lambda command, **kwargs: run_result(
                    command, encoders if encoders is not None
                    else " V..... libx264 A..... aac "
                ),
            ) as run, mock.patch.object(
                short_drama_refinement.tempfile, "NamedTemporaryFile",
                side_effect=(OSError("read only")
                             if dependency == "output_writable" else None),
                wraps=(None if dependency == "output_writable"
                       else tempfile.NamedTemporaryFile),
            ):
                if dependency == "ffprobe":
                    run.side_effect = lambda command, **kwargs: (
                        mock.Mock(returncode=1, stdout="", stderr="missing")
                        if command[0] == os.environ.get("FFPROBE_BIN", "ffprobe")
                        else run_result(command)
                    )
                capability = short_drama_refinement._delivery_capability()
            self.assertFalse(capability["delivery_enabled"])
            self.assertFalse(capability["deliverable"])
            self.assertFalse(capability["checks"][dependency])
            self.assertEqual(expected_reason, capability["reason"])

    def test_delivery_quote_is_single_use_across_new_idempotency_keys(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"}, "repair",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice",
            {"project_id": self.project["id"], "version_id": version["id"]},
        )
        short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "delivery-1",
        )
        with self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice",
                {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
                "delivery-2",
            )
        self.assertEqual("delivery_quote_consumed", raised.exception.code)

    def test_delivery_same_idempotency_key_replays_without_duplicate_charge(self):
        version = self.confirmed_version("repair-for-replay")
        quote = short_drama_refinement.create_delivery_quote(
            self.db, "alice",
            {"project_id": self.project["id"], "version_id": version["id"]},
        )
        deduct = mock.Mock()
        first = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "delivery-replay",
            deduct_points=deduct,
            project_usage=short_drama._project_point_usage,
        )
        replay = short_drama_refinement.start_delivery_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "quote_token": quote["quote_token"]},
            "delivery-replay",
            deduct_points=deduct,
            project_usage=short_drama._project_point_usage,
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        deduct.assert_not_called()

    def test_charge_then_job_link_failure_refunds_and_closes_attempt(self):
        version = self.confirmed_version("repair-for-refund")
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        refund_calls = []

        def refund(*args, **kwargs):
            conn = self.db()
            try:
                state = conn.execute(
                    "SELECT state FROM short_drama_delivery_attempts "
                    "WHERE idempotency_key='delivery-refund'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("refund_pending", state)
            refund_calls.append((args, kwargs))
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice",
                {"project_id": self.project["id"], "version_id": version["id"]},
            )

            def deduct_then_consume(*_args):
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_delivery_quotes "
                        "SET consumed_job_id='raced-job' WHERE token=?",
                        (quote["quote_token"],),
                    )
                    conn.commit()
                finally:
                    conn.close()

            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "delivery-refund",
                    deduct_points=deduct_then_consume,
                    refund_points=refund,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual(1, len(refund_calls))
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-refund'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refunded", state)

    def test_unknown_charge_result_stays_recoverable(self):
        version = self.confirmed_version("repair-for-unknown-charge")
        production = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "production", "adapter": "real_executor_test_double",
            "formal_cost": 80, "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    }, "delivery-unknown-charge",
                    deduct_points=mock.Mock(side_effect=TimeoutError("debit timeout")),
                    charge_lookup=mock.Mock(side_effect=TimeoutError("ledger timeout")),
                    refund_points=mock.Mock(),
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("delivery_recovery_pending", raised.exception.code)
        conn = self.db()
        try:
            state, job_id = conn.execute(
                "SELECT state,job_id FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-unknown-charge'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("accepted", state)
        self.assertIsNone(job_id)

    def test_unknown_charge_marker_failure_still_preserves_accepted_attempt(self):
        version = self.confirmed_version("unknown-charge-marker-failure")
        production = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "production", "adapter": "real_executor_test_double",
            "formal_cost": 80, "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ), mock.patch.object(
            short_drama_refinement, "_mark_delivery_charge_unknown",
            side_effect=sqlite3.OperationalError("marker commit failed"),
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": short_drama_refinement.create_delivery_quote(
                        self.db, "alice", {
                            "project_id": self.project["id"],
                            "version_id": version["id"],
                        },
                    )["quote_token"],
                }, "marker-write-failure",
                deduct_points=mock.Mock(side_effect=TimeoutError("debit timeout")),
                charge_lookup=mock.Mock(side_effect=TimeoutError("ledger timeout")),
            )
        self.assertEqual("delivery_recovery_pending", raised.exception.code)
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='marker-write-failure'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("accepted", state)

    def test_fresh_unknown_replay_with_no_ledger_stays_pending(self):
        version = self.confirmed_version("fresh-unknown-ledger-delay")
        production = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "production", "adapter": "real_executor_test_double",
            "formal_cost": 80, "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            request = {
                "project_id": self.project["id"],
                "quote_token": quote["quote_token"],
            }
            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", request, "delayed-ledger",
                    deduct_points=mock.Mock(side_effect=TimeoutError("debit timeout")),
                    charge_lookup=mock.Mock(side_effect=TimeoutError("ledger timeout")),
                )
            with self.assertRaises(short_drama_refinement.RefinementError) as replay:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", request, "delayed-ledger",
                    charge_lookup=mock.Mock(return_value=None),
                )
        self.assertEqual("delivery_recovery_pending", replay.exception.code)
        conn = self.db()
        try:
            state, error = conn.execute(
                "SELECT state,error FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delayed-ledger'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("accepted", state)
        self.assertTrue(error.startswith("charge_unknown_checked:"), error)

    def test_same_key_replay_immediately_recovers_unknown_charge(self):
        version = self.confirmed_version("replay-recovers-unknown-charge")
        production = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "production", "adapter": "real_executor_test_double",
            "formal_cost": 80, "reason": "",
        }
        body = {"project_id": self.project["id"]}
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", dict(body, version_id=version["id"]),
            )
            request = dict(body, quote_token=quote["quote_token"])
            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", request, "replay-charge",
                    deduct_points=mock.Mock(side_effect=TimeoutError("timeout")),
                    charge_lookup=mock.Mock(side_effect=TimeoutError("timeout")),
                )
            deduct = mock.Mock()
            recovered = short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", request, "replay-charge",
                deduct_points=deduct,
                charge_lookup=mock.Mock(return_value={
                    "username": "alice", "delta": -80,
                }),
            )
        self.assertTrue(recovered["replayed"])
        self.assertEqual("queued", recovered["status"])
        deduct.assert_not_called()

    def test_same_key_replay_reports_stable_failed_attempt(self):
        production, attempt_id, quote = self.create_stale_delivery_attempt(
            "terminal-replay", state="failed",
        )
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "terminal-replay",
            )
        self.assertEqual("delivery_attempt_failed", raised.exception.code)

    def test_same_key_replay_advances_refund_pending(self):
        production, _attempt_id, quote = self.create_stale_delivery_attempt(
            "refund-replay", state="refund_pending",
        )
        refund = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ), self.assertRaises(short_drama_refinement.RefinementError) as raised:
            short_drama_refinement.start_delivery_job(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "quote_token": quote["quote_token"],
                }, "refund-replay", refund_points=refund,
            )
        self.assertEqual("delivery_attempt_refunded", raised.exception.code)
        refund.assert_called_once()

    def test_refund_failure_is_persisted_for_recovery(self):
        version = self.confirmed_version("repair-for-refund-pending")
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice",
                {"project_id": self.project["id"], "version_id": version["id"]},
            )

            def deduct_then_consume(*_args):
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_delivery_quotes "
                        "SET consumed_job_id='raced-job' WHERE token=?",
                        (quote["quote_token"],),
                    )
                    conn.commit()
                finally:
                    conn.close()

            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "delivery-refund-pending",
                    deduct_points=deduct_then_consume,
                    refund_points=mock.Mock(
                        side_effect=RuntimeError("refund unavailable")
                    ),
                    project_usage=short_drama._project_point_usage,
                )
        conn = self.db()
        try:
            state = conn.execute(
                "SELECT state FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-refund-pending'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refund_pending", state)
        points_domain = mock.Mock()
        recovered = short_drama_refinement.retry_delivery_attempt_refunds(
            self.db, points_domain
        )
        self.assertEqual(1, recovered)
        points_domain.refund_points.assert_called_once()

    def test_delivery_refund_lease_blocks_concurrent_scanners(self):
        now = int(time.time())
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES('refund-race','alice',?,'refund-race','hash','quote',"
                "25,'refund_pending',?,?)", (self.project["id"], now, now),
            )
            conn.commit()
        finally:
            conn.close()
        started = threading.Event()
        release = threading.Event()
        points_domain = mock.Mock()
        points_domain.refund_points.side_effect = lambda *_args, **_kwargs: (
            started.set(), release.wait(5)
        )
        attempt = {"id": "refund-race", "state": "refund_pending"}
        first = threading.Thread(
            target=short_drama_refinement.reconcile_delivery_refund,
            args=(self.db, points_domain, attempt),
        )
        first.start()
        self.assertTrue(started.wait(5))
        second = short_drama_refinement.reconcile_delivery_refund(
            self.db, points_domain, attempt,
        )
        release.set()
        first.join(5)
        self.assertEqual("refund_pending", second["state"])
        points_domain.refund_points.assert_called_once()

    def test_delivery_refund_scanner_isolates_one_database_failure(self):
        now = int(time.time())
        conn = self.db()
        try:
            for attempt_id in ("refund-bad", "refund-good"):
                conn.execute(
                    "INSERT INTO short_drama_delivery_attempts "
                    "(id,actor_username,project_id,idempotency_key,request_hash,"
                    "quote_token,cost,state,created_at,updated_at) "
                    "VALUES(?, 'alice', ?, ?, 'hash', 'quote', 25,"
                    "'refund_pending',?,?)",
                    (attempt_id, self.project["id"], attempt_id, now, now),
                )
            conn.commit()
        finally:
            conn.close()
        original = short_drama_refinement.reconcile_delivery_refund
        def flaky(db_factory, points_domain, attempt):
            if attempt["id"] == "refund-bad":
                raise sqlite3.OperationalError("temporary database failure")
            return original(db_factory, points_domain, attempt)
        points_domain = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "reconcile_delivery_refund", side_effect=flaky,
        ):
            recovered = short_drama_refinement.retry_delivery_attempt_refunds(
                self.db, points_domain,
            )
        self.assertEqual(1, recovered)
        conn = self.db()
        try:
            states = dict(conn.execute(
                "SELECT id,state FROM short_drama_delivery_attempts "
                "WHERE id IN ('refund-bad','refund-good')"
            ).fetchall())
        finally:
            conn.close()
        self.assertEqual("refund_pending", states["refund-bad"])
        self.assertEqual("refunded", states["refund-good"])

    def test_formal_delivery_rechecks_project_budget_before_reserving_points(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "repair-for-budget",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=10 WHERE id=?",
                (self.project["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice",
                {"project_id": self.project["id"], "version_id": version["id"]},
            )
            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice",
                    {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    },
                    "budget-blocked-delivery",
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("point_budget_exceeded", raised.exception.code)

    def test_http_delivery_route_counts_real_spend_and_reservations_before_charge(self):
        job = short_drama_refinement.start_refinement_job(
            self.db, "alice", "alice",
            {"project_id": self.project["id"], "shot_key": "shot_02"},
            "repair-for-http-budget",
        )
        for _ in range(4):
            job = short_drama_refinement.get_refinement_job(
                self.db, "alice", self.project["id"], job["id"]
            )
        version = short_drama_refinement.workspace(
            self.db, "alice", "alice", self.project["id"]
        )["current_refinement"]
        self.confirm_version(version)
        now = int(time.time())
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_projects SET point_budget=100 WHERE id=?",
                (self.project["id"],),
            )
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES('charged-old','alice',?,'charged-old','hash-charged',"
                "'quote-charged',50,'charged',?,?)",
                (self.project["id"], now, now),
            )
            conn.execute(
                "INSERT INTO short_drama_delivery_attempts "
                "(id,actor_username,project_id,idempotency_key,request_hash,"
                "quote_token,cost,state,created_at,updated_at) "
                "VALUES('reserved-old','alice',?,'reserved-old','hash-old',"
                "'quote-old',10,'accepted',?,?)",
                (self.project["id"], now, now),
            )
            conn.commit()
        finally:
            conn.close()

        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        production = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "production",
            "adapter": "real_executor_test_double",
            "formal_cost": 80,
            "reason": "",
        }
        deduct = mock.Mock()
        with mock.patch.object(
            short_drama_refinement,
            "_delivery_capability",
            return_value=production,
        ):
            quote_handler = Handler(
                "/api/gen/short-drama/delivery/quote",
                body={
                    "project_id": self.project["id"],
                    "version_id": version["id"],
                },
                key="http-budget-quote",
            )
            self.assertTrue(short_drama.dispatch_http(
                quote_handler, "POST", self.db, verify
            ))
            self.assertEqual(200, quote_handler.response[0])
            job_handler = Handler(
                "/api/gen/short-drama/delivery/jobs",
                body={
                    "project_id": self.project["id"],
                    "quote_token": quote_handler.response[1]["quote_token"],
                },
                key="http-budget-job",
            )
            self.assertTrue(short_drama.dispatch_http(
                job_handler, "POST", self.db, verify, deduct_points=deduct
            ))
        self.assertEqual(409, job_handler.response[0])
        self.assertEqual("point_budget_exceeded", job_handler.response[1]["code"])
        deduct.assert_not_called()
        conn = self.db()
        try:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_attempts "
                "WHERE id NOT IN ('charged-old','reserved-old')"
            ).fetchone()[0]
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, attempts)
        self.assertEqual(0, jobs)

    def test_http_routes_expose_workspace_refinement_and_delivery_contracts(self):
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        workspace = Handler(
            "/api/gen/short-drama/refinement?project_id=" + self.project["id"]
        )
        self.assertTrue(short_drama.dispatch_http(workspace, "GET", self.db, verify))
        self.assertEqual(200, workspace.response[0])
        self.assertEqual("refining", workspace.response[1]["state"])

        preview = Handler(
            "/api/gen/short-drama/refinement/changes/preview",
            body={"project_id": self.project["id"], "shot_key": "shot_02"},
        )
        self.assertTrue(short_drama.dispatch_http(preview, "POST", self.db, verify))
        self.assertEqual(200, preview.response[0])
        self.assertEqual(["shot_02"], preview.response[1]["affected_shots"])

        rejected = Handler(
            "/api/gen/short-drama/refinement/confirm",
            body={
                "project_id": self.project["id"],
                "version_id": workspace.response[1]["current_refinement"]["id"],
            },
        )
        self.assertTrue(short_drama.dispatch_http(rejected, "POST", self.db, verify))
        self.assertEqual(409, rejected.response[0])
        self.assertEqual("refinement_issues_remaining", rejected.response[1]["code"])

        issue = Handler(
            "/api/gen/short-drama/refinement/issues",
            body={
                "project_id": self.project["id"],
                "version_id": workspace.response[1]["current_refinement"]["id"],
                "shot_key": "shot_01", "issue_code": "continuity_error",
                "message": "动作不连续",
            },
        )
        self.assertTrue(short_drama.dispatch_http(issue, "POST", self.db, verify))
        self.assertEqual(200, issue.response[0])
        self.assertEqual("shot_01", issue.response[1]["issues"][-1]["shot_key"])


    def test_local_ffmpeg_process_output_is_utf8_tolerant_on_windows(self):
        process = mock.Mock(
            returncode=0, stdout=" V..... libx264 A..... aac ", stderr=""
        )
        with mock.patch.dict(os.environ, {
            "HQ_SHORT_DRAMA_FORMAL_DELIVERY_MODE": "local_ffmpeg",
            "CONTENT_OUT": self.tmp.name,
        }), mock.patch.object(
            short_drama_refinement.subprocess, "run", return_value=process,
        ) as run:
            capability = short_drama_refinement._delivery_capability()
        self.assertTrue(capability["delivery_enabled"])
        self.assertTrue(run.call_args_list)
        for call in run.call_args_list:
            self.assertEqual("utf-8", call.kwargs.get("encoding"))
            self.assertEqual("replace", call.kwargs.get("errors"))


    def test_delivery_recovers_charge_after_process_exit(self):
        production, attempt_id, _quote = self.create_stale_delivery_attempt(
            "delivery-crash-recovery"
        )
        points = mock.Mock()
        lookup = mock.Mock(return_value={"username": "alice", "delta": -80})
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            first = short_drama_refinement.retry_delivery_attempt_recovery(
                self.db, points, lookup, stale_after=300,
            )
            second = short_drama_refinement.retry_delivery_attempt_recovery(
                self.db, points, lookup, stale_after=300,
            )
        self.assertEqual(1, first["linked"])
        self.assertEqual(0, second["linked"])
        conn = self.db()
        try:
            conn.row_factory = sqlite3.Row
            attempt = conn.execute(
                "SELECT state,job_id FROM short_drama_delivery_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("linked", attempt["state"])
        self.assertTrue(attempt["job_id"])
        self.assertEqual(1, jobs)
        points.refund_points.assert_not_called()

    def test_local_ffmpeg_recovery_revalidates_input_snapshot_before_linking_job(self):
        self.install_mock_native_evidence()
        version = self.confirmed_version("recovery-raw-revalidation")
        capability = {
            "delivery_enabled": True,
            "deliverable": True,
            "mode": "local_ffmpeg",
            "adapter": "local_ffmpeg",
            "formal_cost": 0,
            "reason": "local_2k_renderer",
        }
        lookup_unavailable = mock.Mock(side_effect=TimeoutError("ledger timeout"))
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=capability,
        ), mock.patch(
            "content_domains.short_drama_native_audio.inspect_native_media",
            side_effect=self.valid_native_inspection,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )
            with self.assertRaises(short_drama_refinement.RefinementError):
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    }, "recovery-raw-revalidation",
                    deduct_points=mock.Mock(side_effect=TimeoutError("debit timeout")),
                    charge_lookup=lookup_unavailable,
                )
            conn = self.db()
            try:
                attempt_id = conn.execute(
                    "SELECT id FROM short_drama_delivery_attempts "
                    "WHERE idempotency_key='recovery-raw-revalidation'"
                ).fetchone()[0]
                conn.execute(
                    "UPDATE short_drama_delivery_attempts SET updated_at=? "
                    "WHERE idempotency_key='recovery-raw-revalidation'",
                    (int(time.time()) - 600,),
                )
                conn.commit()
            finally:
                conn.close()
            snapshot = (
                short_drama_refinement._delivery_input_snapshot_dir(
                    self.project["id"], "attempts", attempt_id,
                ) / "source-001.mp4"
            )
            snapshot.chmod(0o644)
            snapshot.write_bytes(b"changed-after-debit")
            recovered = short_drama_refinement.retry_delivery_attempt_recovery(
                self.db, mock.Mock(),
                mock.Mock(return_value={"username": "alice", "delta": -10}),
                stale_after=300,
            )
        self.assertEqual(1, recovered["refund_pending"])
        conn = self.db()
        try:
            state, job_id = conn.execute(
                "SELECT state,job_id FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='recovery-raw-revalidation'"
            ).fetchone()
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("refund_pending", state)
        self.assertIsNone(job_id)
        self.assertEqual(0, jobs)

    def test_delivery_recovery_fails_without_matching_charge(self):
        production, attempt_id, _quote = self.create_stale_delivery_attempt(
            "delivery-no-charge"
        )
        points = mock.Mock()
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            result = short_drama_refinement.retry_delivery_attempt_recovery(
                self.db, points, mock.Mock(return_value=None), stale_after=300,
            )
        self.assertEqual(1, result["failed"])
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT state,job_id FROM short_drama_delivery_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(("failed", None), row)
        points.refund_points.assert_not_called()

    def test_delivery_recovery_refunds_stale_source_once(self):
        production, attempt_id, quote = self.create_stale_delivery_attempt(
            "delivery-stale-source"
        )
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_refinement_versions SET status='superseded' "
                "WHERE id=?", (quote["version_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        points = mock.Mock()
        ledger = mock.Mock(return_value={"username": "alice", "delta": -80})
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            recovered = short_drama_refinement.retry_delivery_attempt_recovery(
                self.db, points, ledger, stale_after=300,
            )
        self.assertEqual(1, recovered["refund_pending"])
        self.assertEqual(
            1, short_drama_refinement.retry_delivery_attempt_refunds(self.db, points)
        )
        self.assertEqual(
            0, short_drama_refinement.retry_delivery_attempt_refunds(self.db, points)
        )
        points.refund_points.assert_called_once()
        self.assertEqual(
            "short-drama-delivery-refund:" + attempt_id,
            points.refund_points.call_args.kwargs["transaction_key"],
        )

    def test_delivery_recovery_has_single_owner_against_online_link(self):
        version = self.confirmed_version("repair-for-recovery-owner-race")
        production = {
            "delivery_enabled": True, "deliverable": True,
            "mode": "production", "adapter": "real_executor_test_double",
            "formal_cost": 80, "reason": "",
        }
        refund = mock.Mock()
        lookup = mock.Mock(return_value={"username": "alice", "delta": -80})
        with mock.patch.object(
            short_drama_refinement, "_delivery_capability", return_value=production,
        ):
            quote = short_drama_refinement.create_delivery_quote(
                self.db, "alice", {
                    "project_id": self.project["id"], "version_id": version["id"],
                },
            )

            def deduct_then_recover(*_args):
                conn = self.db()
                try:
                    conn.execute(
                        "UPDATE short_drama_delivery_attempts SET updated_at=? "
                        "WHERE idempotency_key='delivery-owner-race'",
                        (int(time.time()) - 600,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                recovered = short_drama_refinement.retry_delivery_attempt_recovery(
                    self.db, mock.Mock(), lookup, stale_after=300,
                )
                self.assertEqual(1, recovered["linked"])

            with self.assertRaises(short_drama_refinement.RefinementError) as raised:
                short_drama_refinement.start_delivery_job(
                    self.db, "alice", "alice", {
                        "project_id": self.project["id"],
                        "quote_token": quote["quote_token"],
                    }, "delivery-owner-race", deduct_points=deduct_then_recover,
                    refund_points=refund,
                    project_usage=short_drama._project_point_usage,
                )
        self.assertEqual("delivery_recovery_pending", raised.exception.code)
        refund.assert_not_called()
        conn = self.db()
        try:
            state, job_id = conn.execute(
                "SELECT state,job_id FROM short_drama_delivery_attempts "
                "WHERE idempotency_key='delivery-owner-race'"
            ).fetchone()
            jobs = conn.execute(
                "SELECT COUNT(*) FROM short_drama_delivery_jobs WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("linked", state)
        self.assertTrue(job_id)
        self.assertEqual(1, jobs)


class ShortDramaRefinementSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "legacy-content.db")
        self.db = lambda: sqlite3.connect(self.database)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(
            self.db, "alice", {
                "title": "legacy preference migration",
                "synopsis": "preserve existing media preference rows",
                "ratio": "16:9", "target_duration": 30, "shot_count": 6,
                "visual_style": "cinematic", "target_platform": "test",
                "point_budget": 0,
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_media_preference_schema_upgrade_is_idempotent(self):
        conn = self.db()
        try:
            conn.execute("DROP TABLE short_drama_refinement_media_preferences")
            conn.execute(
                "CREATE TABLE short_drama_refinement_media_preferences ("
                "project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) "
                "ON DELETE CASCADE, mode TEXT NOT NULL CHECK(mode IN "
                "('voice_timeline','silent')), confirmed_by TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO short_drama_refinement_media_preferences "
                "(project_id,mode,confirmed_by,updated_at) VALUES(?,?,?,?)",
                (self.project["id"], "voice_timeline", "alice", 123),
            )
            conn.execute(
                "DELETE FROM short_drama_schema_migrations WHERE name=?",
                (short_drama_refinement._MEDIA_PREFERENCE_MIGRATION,),
            )
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_refinement_media_preferences'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT project_id,mode,confirmed_by,updated_at FROM "
                "short_drama_refinement_media_preferences"
            ).fetchone()
            legacy = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_refinement_media_preferences_legacy'"
            ).fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        self.assertIn("provider_audio", sql)
        self.assertEqual((self.project["id"], "voice_timeline", "alice", 123), row)
        self.assertEqual(0, legacy)
        self.assertEqual("ok", integrity)
        self.assertEqual([], foreign_keys)

    def test_legacy_media_preference_upgrade_resumes_after_rename_crash(self):
        conn = self.db()
        try:
            conn.execute("DROP TABLE short_drama_refinement_media_preferences")
            conn.execute(
                "CREATE TABLE short_drama_refinement_media_preferences_legacy ("
                "project_id TEXT PRIMARY KEY REFERENCES short_drama_projects(id) "
                "ON DELETE CASCADE, mode TEXT NOT NULL CHECK(mode IN "
                "('voice_timeline','silent')), confirmed_by TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO short_drama_refinement_media_preferences_legacy "
                "VALUES(?,?,?,?)",
                (self.project["id"], "voice_timeline", "alice", 123),
            )
            conn.execute(
                "DELETE FROM short_drama_schema_migrations WHERE name=?",
                (short_drama_refinement._MEDIA_PREFERENCE_MIGRATION,),
            )
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT project_id,mode,confirmed_by,updated_at FROM "
                "short_drama_refinement_media_preferences"
            ).fetchone()
            marker = conn.execute(
                "SELECT 1 FROM short_drama_schema_migrations WHERE name=?",
                (short_drama_refinement._MEDIA_PREFERENCE_MIGRATION,),
            ).fetchone()
            legacy = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_refinement_media_preferences_legacy'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual((self.project["id"], "voice_timeline", "alice", 123), row)
        self.assertIsNone(legacy)
        self.assertEqual((1,), marker)

    def test_reassembly_operation_schema_is_additive_idempotent_and_constrained(self):
        conn = self.db()
        try:
            conn.execute("DROP TABLE short_drama_reassembly_operations")
            counts_before = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (
                    "short_drama_projects",
                    "short_drama_refinement_jobs",
                    "short_drama_refinement_versions",
                    "short_drama_refinement_acceptances",
                    "short_drama_delivery_jobs",
                    "short_drama_delivery_versions",
                )
            }
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND "
                "name='short_drama_reassembly_operations'"
            ).fetchone()[0]
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(short_drama_reassembly_operations)"
                )
            }
            foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(short_drama_reassembly_operations)"
            ).fetchall()
            indexes = conn.execute(
                "PRAGMA index_list(short_drama_reassembly_operations)"
            ).fetchall()
            unique_columns = []
            for index in indexes:
                if index[2]:
                    unique_columns.append(tuple(
                        row[2] for row in conn.execute(
                            "PRAGMA index_info(%s)" % index[1]
                        )
                    ))
            counts_after = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in counts_before
            }
            operation_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_reassembly_operations"
            ).fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        self.assertIn("status IN ('processing','succeeded')", table_sql)
        self.assertTrue({
            "id", "project_id", "source_version_id", "status", "lease_token",
            "lease_owner", "lease_expires_at", "heartbeat_at", "render_id",
            "refinement_version_id", "created_at", "updated_at",
        }.issubset(columns))
        self.assertEqual(3, len(foreign_keys))
        self.assertIn(("project_id", "source_version_id"), unique_columns)
        self.assertEqual(counts_before, counts_after)
        self.assertEqual(0, operation_count)
        self.assertEqual("ok", integrity)
        self.assertEqual([], violations)

    def test_defer_reassembly_column_migrates_legacy_jobs_idempotently(self):
        job_id = "legacy-refinement-job"
        conn = self.db()
        try:
            conn.execute(
                "INSERT INTO short_drama_refinement_jobs "
                "(id,project_id,source_version_id,shot_key,actor_username,"
                "idempotency_key,request_hash,replacement_provider_version_id,"
                "defer_reassembly,status,progress,poll_count,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,'failed',100,1,?,?)",
                (
                    job_id, self.project["id"], "legacy-source-version",
                    "shot_01", "alice", "legacy-schema-job", "legacy-hash",
                    None, 100, 100,
                ),
            )
            conn.execute(
                "ALTER TABLE short_drama_refinement_jobs "
                "DROP COLUMN defer_reassembly"
            )
            counts_before = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (
                    "short_drama_projects",
                    "short_drama_refinement_jobs",
                    "short_drama_refinement_versions",
                    "short_drama_provider_shot_versions",
                )
            }
            conn.commit()
        finally:
            conn.close()

        short_drama_refinement.init_db(self.db)
        short_drama_refinement.init_db(self.db)

        conn = self.db()
        try:
            conn.row_factory = sqlite3.Row
            columns = {
                row[1]: row for row in conn.execute(
                    "PRAGMA table_info(short_drama_refinement_jobs)"
                )
            }
            migrated = short_drama_refinement._job(conn.execute(
                "SELECT * FROM short_drama_refinement_jobs WHERE id=?",
                (job_id,),
            ).fetchone())
            counts_after = {
                table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in counts_before
            }
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        self.assertIn("defer_reassembly", columns)
        self.assertEqual(1, columns["defer_reassembly"][3])
        self.assertEqual("0", columns["defer_reassembly"][4])
        self.assertFalse(migrated["defer_reassembly"])
        self.assertEqual(counts_before, counts_after)
        self.assertEqual("ok", integrity)
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
