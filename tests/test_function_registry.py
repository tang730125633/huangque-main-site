import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path


class FunctionRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server.admin_api as admin_api

        cls.admin = admin_api

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.old_paths = (
            self.admin.JOB_DB, self.admin.ASSET_DB, self.admin.VIDEO_COMPOSE_DB,
            self.admin.CONTENT_OUT,
        )
        self.admin.JOB_DB = base / "jobs.db"
        self.admin.ASSET_DB = base / "assets.db"
        self.admin.VIDEO_COMPOSE_DB = base / "compose.db"
        self.admin.CONTENT_OUT = base / "content_out"
        self.admin.CONTENT_OUT.mkdir()
        (self.admin.CONTENT_OUT / "grok.bin").write_bytes(b"video")
        now = int(time.time())
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute(
                """CREATE TABLE jobs(
                       id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,
                       status TEXT,payload TEXT,result TEXT,error TEXT,
                       created_at INTEGER,updated_at INTEGER,refunded INTEGER DEFAULT 0)"""
            )
            connection.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (1, "xiaole_video", "qa", 60, "done",
                     json.dumps({"channel": "grok", "operation": "generate",
                                 "reference_images": ["cos-key://qa/ref"]}),
                     json.dumps({"video_url": "https://cdn.example/grok.mp4"}), "",
                     now - 30, now - 20, 0),
                    (2, "cinematic", "qa", 100, "error",
                     json.dumps({"cine_mode": "motion"}), None, "上游未接单",
                     now - 20, now - 10, 1),
                    (3, "xiaole_video", "qa", 10, "done",
                     json.dumps({"channel": "legacy"}), None, "",
                     now - 10, now - 5, 0),
                    (4, "cinematic", "qa", 100, "done",
                     json.dumps({"cine_mode": "open", "_short_drama_video": {
                         "project_id": "drama-1", "shot_id": "shot-1",
                     }}), None, "", now - 8, now - 4, 0),
                    (5, "avatar", "qa", 20, "done", "{}",
                     json.dumps({"provider_avatar_id": "avatar-1",
                                 "image_url": "https://cdn.example/avatar.jpg"}), "",
                     now - 6, now - 3, 0),
                    (6, "xiaole_video", "qa", 10, "error", "{}", None,
                     "旧任务缺少渠道", now - 5, now - 2, 1),
                ],
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute(
                """CREATE TABLE video_assets(
                       job_id INTEGER,phase TEXT,provider_video_id TEXT,video_file TEXT,
                       video_url TEXT,status TEXT,error TEXT,updated_at INTEGER)"""
            )
            connection.execute(
                "INSERT INTO video_assets VALUES(1,'completed','provider-1','grok.bin',"
                "'https://cdn.example/grok.mp4','done','',?)",
                (now - 20,),
            )
            connection.commit()
        with closing(sqlite3.connect(self.admin.VIDEO_COMPOSE_DB)) as connection:
            connection.execute(
                """CREATE TABLE video_compose_projects(
                       id TEXT,status TEXT,output_file TEXT,output_asset_id INTEGER,
                       created_at INTEGER,updated_at INTEGER)"""
            )
            connection.execute(
                "INSERT INTO video_compose_projects VALUES('compose-1','completed',"
                "'compose.mp4',9,?,?)",
                (now - 40, now - 15),
            )
            connection.commit()

    def tearDown(self):
        self.admin.JOB_DB, self.admin.ASSET_DB, self.admin.VIDEO_COMPOSE_DB, self.admin.CONTENT_OUT = self.old_paths
        self.tmp.cleanup()

    def test_customer_tree_is_the_registry_root(self):
        pages = self.admin.function_registry.list_pages()
        self.assertEqual(
            [page["name"] for page in pages],
            ["灵感设计", "平台获客", "内容爬取", "图片生成", "视频生成", "音频生成",
             "文案编导", "短剧创作", "无限画布", "我的资产", "点数价格", "邀请中心",
             "教程视频", "通用设置"],
        )
        self.assertEqual([page["inventory_status"] for page in pages].count("verified"), 1)
        video = next(page for page in pages if page["key"] == "video")
        self.assertEqual(
            [feature["name"] for feature in video["functions"]],
            ["一键成片", "数字化 IP", "电影化身", "换装换背景", "果肉视频生成",
             "Sora 2", "麦克视频", "Omni 视频", "Seedance 视频"],
        )
        cinematic = next(item for item in video["functions"] if item["key"] == "cinematic")
        self.assertEqual([item["name"] for item in cinematic["modes"]], ["动作模仿", "开放式生成"])
        self.assertEqual(cinematic["shared_steps"][0]["name"], "创建或选择形象")
        self.assertTrue(cinematic["modes"][1]["validation"]["supported"])
        self.assertNotIn("xiaole_video", [item["key"] for item in video["functions"]])
        one_click = video["functions"][0]["modes"][0]
        self.assertEqual(one_click["evidence_contract"]["acceptance_id_type"], "project_id")
        self.assertIn("provider_task", one_click["evidence_contract"]["not_applicable"])
        self.assertNotIn("balance", one_click["evidence_contract"]["not_applicable"])
        self.assertIn(
            {"method": "GET", "path": "/api/gen/video-compose/projects/{project_id}"},
            one_click["entrypoints"],
        )
        self.assertEqual(one_click["dependencies"][0]["credential_source"], "env")
        digital_text = video["functions"][1]["modes"][0]
        cosyvoice = next(item for item in digital_text["dependencies"] if item["key"] == "cosyvoice")
        self.assertEqual(cosyvoice["requirement"], "required")
        tryon_fast = video["functions"][3]["modes"][0]
        self.assertEqual(next(item for item in tryon_fast["dependencies"] if item["key"] == "cos")["requirement"], "required")

        flags = self.admin.feature_flags.CATALOG_MAP
        routes = self.admin.KEY_GROUP_MAP
        prices = self.admin.pricing.CATALOG_MAP
        for feature in video["functions"]:
            self.assertTrue(feature["frontend_selector"])
            self.assertEqual(feature["service"], "content")
            for flag in feature.get("flag_keys", []):
                self.assertIn(flag, flags)
            leaves = feature.get("shared_steps", []) + feature.get("modes", [])
            for leaf in leaves:
                self.assertTrue(leaf["entrypoints"])
                self.assertTrue(leaf.get("task_match") or leaf.get("evidence_source"))
                for flag in leaf.get("flag_keys", []):
                    self.assertIn(flag, flags)
                for dependency in feature.get("dependencies", []) + leaf.get("dependencies", []):
                    self.assertIn(dependency["key"], routes)
                    self.assertIn(dependency["requirement"], {"required", "optional", "alternative"})
                    self.assertIn(dependency.get("credential_source"), {None, "env", "pool"})
                    if dependency["requirement"] == "alternative":
                        self.assertTrue(dependency.get("alternative_group"))
                for price in leaf.get("price_keys", []):
                    self.assertIn(price, prices)
            for mode in feature["modes"]:
                self.assertTrue(mode["smoke_inputs"])
                self.assertIn("price_keys", mode)
                public = mode["validation"]
                self.assertNotIn("target_path", public)
                self.assertNotIn("prefill", public)
                self.assertNotIn("@fixture/", json.dumps(public, ensure_ascii=False))
                runner = self.admin.function_registry.e2e_runner(mode["key"])
                self.assertIsNotNone(runner)
                self.assertEqual(public["supported"], runner["supported"])
                prefill = runner["prefill"]
                assets = [
                    prefill.get("image_url"), prefill.get("reference_video_url"),
                    prefill.get("audio_url"), prefill.get("background_url"),
                ]
                assets += prefill.get("reference_images") or []
                for asset in filter(None, assets):
                    self.assertTrue(asset.startswith("@fixture/"))
                    self.assertTrue((Path(__file__).resolve().parents[1] / "server/qa_fixtures" / asset.removeprefix("@fixture/")).is_file())

        modes = [mode for feature in video["functions"] for mode in feature["modes"]]
        self.assertEqual(sum(mode["validation"]["supported"] for mode in modes), 14)

        root = Path(__file__).resolve().parents[1]
        for page in pages:
            self.assertTrue((root / "site" / page["path"].lstrip("/")).is_file())
        frontend = (root / "site/workbench/video.html").read_text(encoding="utf-8")
        for marker in (
            'href="one-click-video.html"', 'data-function="talking"',
            'data-function="cinematic"', 'data-cine-mode="motion"',
            'data-cine-mode="open"', 'data-function="tryon"', 'data-line="2"',
            'data-line="1"', 'data-function="grok"', 'data-function="sora"',
            'data-function="minimax"', 'data-function="omni"', 'data-function="micro"',
            "xiaoleRefRenderers",
        ):
            self.assertIn(marker, frontend)

    def test_runtime_visibility_and_task_evidence_stay_separate(self):
        pages = self.admin.load_function_registry([{
            "key": "content", "online": True,
            "detail": {"sora_video_enabled": False, "minimax_h3_video_enabled": False,
                       "omni_video_enabled": True, "seedance_video_enabled": True},
        }])
        video = next(page for page in pages if page["key"] == "video")
        visible = [item["key"] for item in video["functions"] if item["runtime_visible"]]
        self.assertEqual(
            visible,
            ["one_click", "digital_ip", "cinematic", "tryon", "grok", "sora", "omni", "seedance"],
        )
        sora = next(item for item in video["functions"] if item["key"] == "sora")
        self.assertTrue(sora["runtime_visible"])
        self.assertFalse(sora["acceptance_health"])
        grok = next(item for item in video["functions"] if item["key"] == "grok")
        self.assertEqual(grok["selected_alternatives"]["grok_provider"], "xai")
        self.assertEqual(
            {item.get("selection_value") for item in grok["dependencies"] if item.get("alternative_group")},
            {"xai", "xiaole"},
        )

        stats = self.admin.job_stats(7)
        operations = {item["operation"]: item for item in stats["by_operation"]}
        grok = operations["video.grok.image"]
        self.assertEqual((grok["done"], grok["error"]), (1, 0))
        self.assertEqual(grok["latest"]["provider_task_id"], "provider-1")
        self.assertTrue(grok["latest"]["output_reference_present"])
        self.assertTrue(grok["latest"]["delivery_verified"])
        self.assertEqual(grok["latest"]["artifact_check"], "file_exists")
        self.assertEqual(grok["latest"]["billing_state"], "unverified")
        motion = operations["video.cinematic.motion"]
        self.assertEqual(motion["latest"]["billing_state"], "refunded")
        avatar = operations["video.cinematic.avatar"]["latest"]
        self.assertEqual(avatar["result_url"], "https://cdn.example/avatar.jpg")
        self.assertEqual(avatar["provider_task_id"], "avatar-1")
        self.assertIn("video.one_click.compose", operations)
        compose = operations["video.one_click.compose"]["latest"]
        self.assertEqual(compose["business_id_type"], "project_id")
        self.assertEqual(compose["provider_task_state"], "not_applicable")
        self.assertEqual(compose["billing_state"], "not_applicable")
        unmapped = {(item["page_key"], item["signature"]) for item in stats["unmapped"]}
        self.assertIn(("video", "xiaole_video/legacy"), unmapped)
        self.assertIn(("video", "xiaole_video"), unmapped)
        self.assertIn(("short-drama", "cinematic/open"), unmapped)
        self.assertEqual(stats["evidence_errors"], [])

        classify = self.admin.function_registry.classify_task
        self.assertEqual(classify("video", {"mode": "text", "batch_id": "b"}), "video.digital_ip.text.batch")
        self.assertEqual(classify("video", {"mode": "audio"}), "video.digital_ip.audio")
        self.assertEqual(classify("avatar"), "video.cinematic.avatar")
        self.assertIsNone(classify("cinematic", {}))
        self.assertIsNone(classify("cinematic", {"cine_mode": "open", "source_page": "short-drama"}))
        self.assertEqual(classify("tryon", {"line": "2"}), "video.tryon.fast")
        self.assertEqual(classify("sora_video", {"reference_count": 0}), "video.sora.text")
        self.assertEqual(classify("xiaole_video", {"channel": "omni", "reference_count": 1}), "video.omni.image")
        self.assertEqual(classify("xiaole_video", {"channel": "grok"}), "video.grok.text")
        self.assertIsNone(classify("xiaole_video", {}))
        self.assertIsNone(classify("xiaole_video", {"channel": "grok", "operation": "edit"}))

        filtered = self.admin.activity_logs(7, 20, q="video.grok.image", source="job")
        self.assertEqual(filtered["total"], 1)
        self.assertIn("video.grok.image", filtered["items"][0]["func"])

    def test_unused_compose_store_is_not_an_incident(self):
        self.admin.VIDEO_COMPOSE_DB.unlink()
        stats = self.admin.job_stats(7)
        self.assertNotIn("一键成片证据库不存在", stats["evidence_errors"])

    def test_task_drilldown_classifies_large_payloads_from_full_json(self):
        now = int(time.time())
        large = "x" * 10000
        rows = [
            (7, "xiaole_video", {"image": large, "channel": "grok", "operation": "generate",
                                  "reference_images": ["cos-key://qa/ref"]}),
            (8, "xiaole_video", {"image": large, "channel": "omni",
                                  "reference_images": ["cos-key://qa/ref"]}),
            (9, "cinematic", {"image": large, "cine_mode": "motion"}),
            (10, "video", {"image": large, "mode": "text", "batch_id": "batch-1"}),
        ]
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(job_id, kind, "qa", 1, "done", json.dumps(payload), "{}", "",
                  now - job_id, now - job_id + 1, 0) for job_id, kind, payload in rows],
            )
            connection.commit()
        operations = {item["id"]: item["operation"] for item in self.admin.call_logs(7, 20)["items"]}
        self.assertEqual(operations[7], "video.grok.image")
        self.assertEqual(operations[8], "video.omni.image")
        self.assertEqual(operations[9], "video.cinematic.motion")
        self.assertEqual(operations[10], "video.digital_ip.text.batch")


if __name__ == "__main__":
    unittest.main()
