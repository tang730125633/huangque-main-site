import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


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
             "文案编导", "文案成片", "短剧创作", "无限画布", "我的资产", "点数价格", "邀请中心",
             "教程视频", "通用设置"],
        )
        self.assertTrue(all(page["inventory_status"] == "verified" for page in pages))
        image = next(page for page in pages if page["key"] == "banana")
        self.assertEqual(
            [feature["name"] for feature in image["functions"]],
            ["纳米香蕉", "黄雀引擎 2", "黄雀引擎 1", "果肉生图"],
        )
        self.assertEqual(
            [mode["key"] for feature in image["functions"] for mode in feature["modes"]],
            [
                "image.banana.nb2.text", "image.banana.nb2.reference",
                "image.banana.pro.text", "image.banana.pro.reference",
                "image.openai.text", "image.openai.reference", "image.openai.inpaint",
                "image.seedream.std.text", "image.seedream.std.reference",
                "image.seedream.pro.text", "image.seedream.pro.reference",
                "image.xiaole.text", "image.xiaole.reference",
            ],
        )
        self.assertEqual(
            [item["name"] for item in image["auxiliary_actions"]],
            ["优化提示词", "反推提示词"],
        )
        self.assertNotIn(
            "zelong2",
            json.dumps(image, ensure_ascii=False),
        )
        audio = next(page for page in pages if page["key"] == "audio")
        self.assertEqual([feature["name"] for feature in audio["functions"]], ["AI 配音"])
        self.assertEqual(
            [mode["key"] for feature in audio["functions"] for mode in feature["modes"]],
            ["audio.tts.public", "audio.tts.personal"],
        )
        self.assertEqual(audio["auxiliary_actions"], [])
        self.assertNotIn(
            "/api/gen/audio/clone-vip",
            json.dumps(audio, ensure_ascii=False),
        )
        audio_tts = audio["functions"][0]
        dependency = audio_tts["dependencies"][0]
        self.assertEqual(
            (dependency["key"], dependency["credential_source"]),
            ("cosyvoice", "env"),
        )
        for mode in audio_tts["modes"]:
            self.assertEqual(mode["entrypoints"][0], {"method": "POST", "path": "/api/gen/audio"})
            self.assertEqual(mode["price_keys"], ["audio.tts"])
            self.assertEqual(
                set(mode["evidence_contract"]["not_applicable"]),
                {"provider_task", "balance"},
            )
            self.assertNotIn("billing", mode["evidence_contract"]["not_applicable"])
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
        collect = next(page for page in pages if page["key"] == "collect")
        self.assertEqual(
            [mode["key"] for feature in collect["functions"] for mode in feature["modes"]],
            ["collect.content.comments", "collect.content.video", "collect.content.transcript"],
        )
        self.assertEqual([item["name"] for item in collect["auxiliary_actions"]], ["关键词搜内容"])
        leads = next(page for page in pages if page["key"] == "leads")
        self.assertEqual(
            [mode["key"] for feature in leads["functions"] for mode in feature["modes"]],
            ["leads.keyword.search"],
        )
        self.assertEqual([item["name"] for item in leads["auxiliary_actions"]], ["线索跟进保存"])
        self.assertNotIn("HQ_E2E_COLLECT_URL", json.dumps(collect, ensure_ascii=False))
        self.assertEqual(
            self.admin.function_registry.e2e_runner("collect.content.comments")["prefill"]["url"],
            "@env/HQ_E2E_COLLECT_URL",
        )
        script = next(page for page in pages if page["key"] == "script")
        self.assertEqual(
            [item["name"] for item in script["functions"]],
            ["AI 写脚本", "拆解视频", "脚本结果生产"],
        )
        self.assertEqual(
            [mode["key"] for feature in script["functions"] for mode in feature["modes"]],
            ["script.write.spoken", "script.write.story", "script.write.recommend",
             "script.breakdown.scenes", "script.breakdown.reverse",
             "script.breakdown.local_image", "script.breakdown.local_video",
             "script.output.video.story", "script.output.video.spoken",
             "script.output.video.recommend", "script.output.remake.cinematic",
             "script.output.remake.grok", "script.output.remake.micro",
             "script.output.image"],
        )
        self.assertTrue(all(
            mode["validation"]["supported"]
            for feature in script["functions"][:2] for mode in feature["modes"]
        ))
        self.assertNotIn("evidence_gaps", script["functions"][1])
        output_modes = script["functions"][2]["modes"]
        self.assertEqual(
            [mode["key"] for mode in output_modes if mode["validation"]["supported"]],
            ["script.output.image"],
        )
        self.assertIn(
            "provider_task",
            output_modes[-1]["evidence_contract"]["not_applicable"],
        )
        text_video = next(page for page in pages if page["key"] == "text-video")
        self.assertEqual(
            [mode["key"] for mode in text_video["functions"][0]["modes"]],
            ["text_video.topic", "text_video.fixed"],
        )
        self.assertTrue(all(
            mode["validation"]["supported"]
            for mode in text_video["functions"][0]["modes"]
        ))
        assets = next(page for page in pages if page["key"] == "assets")
        self.assertEqual(assets["functions"][0]["modes"][0]["key"], "assets.audio.clone_vip")
        self.assertFalse(assets["functions"][0]["modes"][0]["validation"]["supported"])
        for key in {"inspiration", "script", "short-drama", "canvas", "assets",
                    "pricing", "invite", "tutorials", "settings"}:
            self.assertTrue(next(page for page in pages if page["key"] == key)["browser_journeys"])
        settings = next(page for page in pages if page["key"] == "settings")
        settings_endpoints = {
            (endpoint["method"], endpoint["path"])
            for journey in settings["browser_journeys"]
            for endpoint in journey["entrypoints"]
        }
        self.assertIn(("POST", "/api/auth/friend-requests/respond"), settings_endpoints)
        self.assertIn(("DELETE", "/api/auth/friends/{username}"), settings_endpoints)
        self.assertIn(("POST", "/api/auth/points/transfer"), settings_endpoints)
        self.assertIn(("POST", "/api/auth/change_password"), settings_endpoints)
        self.assertNotIn(
            "canvas.digital_presenter.project",
            {journey["key"] for journey in next(
                page for page in pages if page["key"] == "canvas"
            )["browser_journeys"]},
        )
        short_drama = next(page for page in pages if page["key"] == "short-drama")
        self.assertEqual([item["name"] for item in short_drama["functions"]], ["AI 真人短剧"])
        self.assertEqual(
            [mode["key"] for mode in short_drama["functions"][0]["modes"]],
            ["short_drama.live_action.script_planning",
             "short_drama.live_action.character_reference",
             "short_drama.live_action.shot_video",
             "short_drama.live_action.preview",
             "short_drama.live_action.delivery"],
        )
        short_drama_modes = short_drama["functions"][0]["modes"]
        self.assertTrue(all(
            mode["validation"]["supported"] for mode in short_drama_modes
        ))
        self.assertEqual(short_drama_modes[1]["dependencies"], [{
            "key": "gemini", "role": "主生成", "requirement": "required",
            "credential_source": "env",
        }])
        self.assertIn("provider_task", short_drama_modes[4]["evidence_contract"]["not_applicable"])
        self.assertEqual(
            self.admin.function_registry.e2e_runner(
                "short_drama.live_action.delivery"
            )["prefill"]["shot_count"],
            6,
        )
        self.assertNotIn("旧书店", json.dumps(short_drama, ensure_ascii=False))
        self.assertIn(
            "旧书店",
            self.admin.function_registry.e2e_runner(
                "short_drama.live_action.script_planning"
            )["prefill"]["source_text"],
        )
        self.assertEqual(
            self.admin.function_registry.e2e_runner(
                "short_drama.live_action.shot_video"
            )["prefill"]["character_contract"],
            self.admin.function_registry.QA_SHORT_DRAMA_CHARACTER_CONTRACT,
        )
        preview_runner = self.admin.function_registry.e2e_runner(
            "short_drama.live_action.preview"
        )
        self.assertEqual(preview_runner["prefill"]["shot_count"], 6)
        self.assertEqual(short_drama_modes[3]["dependencies"][0]["key"], "xai")
        canvas = next(page for page in pages if page["key"] == "canvas")
        self.assertEqual([item["name"] for item in canvas["functions"]], ["画布 Agent", "图片节点", "视频节点"])
        self.assertEqual(canvas["functions"][0]["modes"][0]["key"], "canvas.agent.plan")
        self.assertEqual(
            [item["name"] for item in canvas["auxiliary_actions"]],
            ["反推提示词", "本地画布编辑与协作同步"],
        )
        self.assertEqual(
            self.admin.function_registry.classify_task("image", {
                "source_page": "canvas", "provider": "banana", "model": "nb2",
            }),
            "canvas.image.banana.nb2",
        )
        canvas_image_modes = canvas["functions"][1]["modes"]
        self.assertTrue(all(mode["validation"]["supported"] for mode in canvas_image_modes))
        for mode in canvas_image_modes:
            runner = self.admin.function_registry.e2e_runner(mode["key"])
            self.assertEqual(runner["prefill"], self.admin.function_registry._image_validation()["prefill"])
            self.assertIn("provider_task", runner["evidence_contract"]["not_applicable"])
        self.assertEqual(
            self.admin.function_registry.classify_task("xiaole_video", {
                "source_page": "canvas", "channel": "grok", "operation": "generate",
            }),
            "canvas.video.grok",
        )
        classify = self.admin.function_registry.classify_task
        for metadata, operation in (
            ({"source_page": "canvas", "provider": "banana", "model": "pro"}, "canvas.image.banana.pro"),
            ({"source_page": "canvas", "provider": "openai"}, "canvas.image.openai"),
            ({"source_page": "canvas", "provider": "zelong"}, "canvas.image.zelong"),
        ):
            self.assertEqual(classify("image", metadata), operation)
        self.assertEqual(classify("xiaole_video", {"source_page": "canvas", "channel": "micro"}), "canvas.video.micro")
        canvas_video_modes = canvas["functions"][2]["modes"]
        self.assertTrue(all(mode["validation"]["supported"] for mode in canvas_video_modes))
        for style, operation in (("口播", "script.write.spoken"), ("剧情", "script.write.story"), ("种草", "script.write.recommend")):
            self.assertEqual(classify("copy", {"source_page": "script", "format": "script", "style": style}), operation)
        for metadata, operation in (
            ({"source_page": "script", "mode": "scenes"}, "script.breakdown.scenes"),
            ({"source_page": "script", "mode": "reverse_prompt", "source_type": ""}, "script.breakdown.reverse"),
            ({"source_page": "script", "source_type": "image"}, "script.breakdown.local_image"),
            ({"source_page": "script", "source_type": "video"}, "script.breakdown.local_video"),
        ):
            self.assertEqual(classify("breakdown", metadata), operation)
        for kind, metadata, operation in (
            ("script_to_video", {"source_page": "script", "style": "剧情"}, "script.output.video.story"),
            ("script_to_video", {"source_page": "script", "style": "口播"}, "script.output.video.spoken"),
            ("script_to_video", {"source_page": "script", "style": "种草"}, "script.output.video.recommend"),
            ("cinematic", {"source_page": "script", "cine_mode": "open"}, "script.output.remake.cinematic"),
            ("xiaole_video", {"source_page": "script", "channel": "grok"}, "script.output.remake.grok"),
            ("xiaole_video", {"source_page": "script", "channel": "micro"}, "script.output.remake.micro"),
            ("image", {"source_page": "script", "provider": "openai"}, "script.output.image"),
        ):
            self.assertEqual(classify(kind, metadata), operation)
        self.assertEqual(
            classify("script_to_video", {
                "source_page": "text-video", "pipeline": "pixelle", "mode": "generate",
            }),
            "text_video.topic",
        )
        self.assertEqual(
            classify("script_to_video", {
                "source_page": "text-video", "pipeline": "pixelle", "mode": "fixed",
            }),
            "text_video.fixed",
        )
        self.assertIsNone(classify("copy", {"source_page": "canvas", "format": "script", "style": "口播"}))
        self.assertIsNone(classify("breakdown", {"source_page": "video", "mode": "scenes"}))

        flags = self.admin.feature_flags.CATALOG_MAP
        routes = self.admin.KEY_GROUP_MAP
        prices = self.admin.pricing.CATALOG_MAP
        for feature in (image["functions"] + video["functions"] + audio["functions"]
                        + collect["functions"] + leads["functions"] + script["functions"]
                        + text_video["functions"] + short_drama["functions"]
                        + canvas["functions"] + assets["functions"]):
            self.assertTrue(feature["frontend_selector"])
            self.assertIn(feature["service"], {"content", "imggen", "leadgen"})
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
                    prefill.get("audio_url"), prefill.get("background_url"), prefill.get("mask_url"),
                    prefill.get("file_url"),
                ]
                assets += prefill.get("reference_images") or []
                for asset in filter(None, assets):
                    self.assertTrue(asset.startswith("@fixture/"))
                    self.assertTrue((Path(__file__).resolve().parents[1] / "server/qa_fixtures" / asset.removeprefix("@fixture/")).is_file())

        xiaole = next(item for item in image["functions"] if item["key"] == "xiaole")
        self.assertEqual(xiaole["flag_keys"], ["image_xiaole"])
        self.assertEqual(
            self.admin.function_registry.e2e_runner("image.xiaole.text")["flag_keys"],
            ["image_xiaole"],
        )

        modes = [mode for feature in video["functions"] for mode in feature["modes"]]
        self.assertEqual(sum(mode["validation"]["supported"] for mode in modes), 15)
        image_modes = [mode for feature in image["functions"] for mode in feature["modes"]]
        self.assertEqual(sum(mode["validation"]["supported"] for mode in image_modes), 13)
        for mode in image_modes:
            runner = self.admin.function_registry.e2e_runner(mode["key"])
            self.assertEqual(runner["prefill"]["quality"], "std")
            self.assertEqual(runner["prefill"]["count"], 1)
            self.assertEqual(runner["prefill"]["ratio"], "1:1")
            self.assertEqual(runner["endpoint"], mode["entrypoints"][0])
            public = json.dumps(mode["validation"], ensure_ascii=False)
            self.assertNotIn("qa-serum", public)
        self.assertEqual(
            self.admin.function_registry.e2e_runner("image.openai.inpaint")["prefill"]["mask_url"],
            "@fixture/qa-serum.png",
        )
        for operation in ("image.xiaole.text", "image.xiaole.reference"):
            self.assertNotIn(
                "provider_task",
                self.admin.function_registry.e2e_runner(operation)["evidence_contract"]["not_applicable"],
            )
        audio_modes = [mode for feature in audio["functions"] for mode in feature["modes"]]
        self.assertEqual(sum(mode["validation"]["supported"] for mode in audio_modes), 2)
        for mode in audio_modes:
            runner = self.admin.function_registry.e2e_runner(mode["key"])
            self.assertEqual(runner["prefill"]["voice_scope"], mode["key"].rsplit(".", 1)[-1])
            self.assertEqual(runner["endpoint"], {"method": "POST", "path": "/api/gen/audio"})
            self.assertIn("provider_task", runner["evidence_contract"]["not_applicable"])
            public = json.dumps(mode["validation"], ensure_ascii=False)
            self.assertNotIn("voice_key", public)
            self.assertNotIn("provider_voice", public)

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

        image_frontend = (root / "site/workbench/banana.html").read_text(encoding="utf-8")
        for marker in (
            'data-engine="banana"', 'data-engine="gpt"', 'data-engine="seedream"',
            'data-engine="xiaole"', "inpBtn.id='inpBtn'", "source_page:'banana'",
        ):
            self.assertIn(marker, image_frontend)
        imggen = (root / "server/imggen_api.py").read_text(encoding="utf-8")
        banana_route = imggen.split('if p == "/api/gen/banana":', 1)[1].split(
            'if p == "/api/gen/reverse":', 1
        )[0]
        self.assertNotIn('body["source_page"] =', banana_route)
        self.assertIn('body["provider"] = "banana"', banana_route)

        audio_frontend = (root / "site/workbench/audio.html").read_text(encoding="utf-8")
        self.assertEqual(audio_frontend.count('data-voice-tab="'), len(audio_modes))
        for marker in (
            'data-voice-tab="public"', 'data-voice-tab="personal"',
            "source_page:'audio'", "id=\"speedVal\"", "id=\"pitchVal\"",
            "id=\"volumeVal\"", "id=\"generateBtn\"",
        ):
            self.assertIn(marker, audio_frontend)

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
        self.assertEqual(
            classify("image", {"source_page": "banana", "provider": "banana", "model": "nb2"}),
            "image.banana.nb2.text",
        )
        self.assertEqual(
            classify("image", {"source_page": "banana", "provider": "openai", "image": "x", "mask": "x"}),
            "image.openai.inpaint",
        )
        self.assertEqual(
            classify("image", {"source_page": "banana", "provider": "seedream", "variant": "pro", "reference_count": 1}),
            "image.seedream.pro.reference",
        )
        self.assertIsNone(classify("image", {"provider": "openai"}))
        self.assertEqual(
            classify("image", {"source_page": "script", "provider": "openai"}),
            "script.output.image",
        )
        self.assertEqual(
            classify("audio", {"source_page": "audio", "provider": "cosyvoice", "voice_scope": "public"}),
            "audio.tts.public",
        )
        self.assertIsNone(classify("audio", {"provider": "cosyvoice", "voice_scope": "public"}))
        self.assertIsNone(classify("video", {"source_page": "audio", "mode": "text"}))
        self.assertIsNone(classify("image", {"source_page": "audio", "provider": "openai"}))
        self.assertEqual(
            classify("collect", {"source_page": "collect", "want": ["video"]}),
            "collect.content.video",
        )
        self.assertEqual(
            classify("collect", {"source_page": "collect", "collect_mode": "transcript"}),
            "collect.content.transcript",
        )
        self.assertEqual(classify("leads", {"source_page": "leads"}), "leads.keyword.search")

        filtered = self.admin.activity_logs(7, 20, q="video.grok.image", source="job")
        self.assertEqual(filtered["total"], 1)
        self.assertIn("video.grok.image", filtered["items"][0]["func"])

    def test_unused_compose_store_is_not_an_incident(self):
        self.admin.VIDEO_COMPOSE_DB.unlink()
        stats = self.admin.job_stats(7)
        self.assertNotIn("一键成片证据库不存在", stats["evidence_errors"])

    def test_character_reference_evidence_source_maps_to_customer_operation(self):
        now = int(time.time())
        (self.admin.CONTENT_OUT / "character.bin").write_bytes(b"generated-character")
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (7, "image", "qa", 35, "done",
                 json.dumps({"provider": "banana", "model": "nb2"}),
                 json.dumps({"file": "character.bin", "url": "https://cdn.example/character.bin"}),
                 "", now - 8, now - 2, 0),
            )
            connection.execute(
                """CREATE TABLE short_drama_character_reference_jobs(
                       job_id INTEGER,status TEXT,error TEXT,created_at INTEGER,updated_at INTEGER)"""
            )
            connection.execute(
                "INSERT INTO short_drama_character_reference_jobs VALUES(7,'done','',?,?)",
                (now - 8, now - 2),
            )
            connection.commit()

        stats = self.admin.job_stats(7)
        operation = next(
            item for item in stats["by_operation"]
            if item["operation"] == "short_drama.live_action.character_reference"
        )
        self.assertEqual((operation["done"], operation["error"]), (1, 0))
        self.assertEqual(operation["latest"]["job_id"], 7)
        self.assertEqual(operation["latest"]["route_provider"], "banana")
        self.assertTrue(operation["latest"]["delivery_verified"])
        self.assertEqual(operation["latest"]["artifact_check"], "file_exists")

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

    def test_image_page_jobs_are_classified_without_stealing_other_image_jobs(self):
        now = int(time.time())
        rows = [
            (20, {"source_page": "banana", "provider": "banana", "model": "pro",
                  "reference_images": ["ref"]}),
            (21, {"source_page": "banana", "prompt": "text only"}),
            (22, {"source_page": "banana", "provider": "seedream", "variant": "std",
                  "reference_images": ["ref"]}),
            (23, {"source_page": "banana", "provider": "xiaole", "image": "old-ref"}),
            (24, {"source_page": "script", "provider": "openai"}),
            (25, {"provider": "openai"}),
        ]
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(job_id, "image", "qa", 1, "done", json.dumps(payload),
                  (json.dumps({"urls": ["https://cdn.example/image.png"], "files": ["grok.bin"]})
                   if job_id == 20 else json.dumps({"provider_task_id": "xiaole-23"})
                   if job_id == 23 else "{}"), "",
                  now - job_id, now - job_id + 1, 0) for job_id, payload in rows],
            )
            connection.commit()

        operations = {item["id"]: item["operation"] for item in self.admin.call_logs(7, 50)["items"]}
        self.assertEqual(operations[20], "image.banana.pro.reference")
        self.assertEqual(operations[21], "image.openai.text")
        self.assertEqual(operations[22], "image.seedream.std.reference")
        self.assertEqual(operations[23], "image.xiaole.reference")
        self.assertEqual(operations[24], "script.output.image")
        self.assertIsNone(operations[25])

        stats = self.admin.job_stats(7)
        by_operation = {item["operation"]: item for item in stats["by_operation"]}
        self.assertIn("image.banana.pro.reference", by_operation)
        self.assertIn("image.openai.text", by_operation)
        self.assertIn("image.seedream.std.reference", by_operation)
        self.assertIn("image.xiaole.reference", by_operation)
        self.assertIn("script.output.image", by_operation)
        self.assertEqual(
            by_operation["image.xiaole.reference"]["latest"]["provider_task_id"],
            "xiaole-23",
        )
        banana = by_operation["image.banana.pro.reference"]["latest"]
        self.assertEqual(banana["result_url"], "https://cdn.example/image.png")
        self.assertEqual(banana["artifact_check"], "file_exists")
        self.assertTrue(banana["delivery_verified"])
        unmapped = {(item["page_key"], item["signature"]) for item in stats["unmapped"]}
        self.assertNotIn(("script", "image/openai"), unmapped)
        self.assertIn(("", "image/openai"), unmapped)

    def test_audio_page_jobs_join_assets_without_stealing_other_pages(self):
        now = int(time.time())
        rows = [
            (30, "audio", {"source_page": "audio", "provider": "cosyvoice", "voice_scope": "public"}),
            (31, "audio", {"source_page": "audio", "provider": "cosyvoice", "voice_scope": "personal"}),
            (32, "audio", {"provider": "cosyvoice", "voice_scope": "public"}),
            (33, "audio", {"source_page": "video", "provider": "cosyvoice", "voice_scope": "public"}),
            (34, "video", {"source_page": "audio", "mode": "text"}),
            (35, "image", {"source_page": "audio", "provider": "openai"}),
            (36, "canvas_agent", {"source_page": "audio"}),
            (37, "short_drama_sound_effect", {"source_page": "audio"}),
            (38, "audio", {"source_page": "canvas", "provider": "cosyvoice", "voice_scope": "personal"}),
        ]
        with closing(sqlite3.connect(self.admin.JOB_DB)) as connection:
            connection.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(job_id, kind, "qa", 10, "done", json.dumps(payload), "{}", "",
                  now - job_id, now - job_id + 1, 0) for job_id, kind, payload in rows],
            )
            connection.commit()
        (self.admin.CONTENT_OUT / "public.mp3").write_bytes(b"ID3-public")
        (self.admin.CONTENT_OUT / "personal.mp3").write_bytes(b"ID3-personal")
        with closing(sqlite3.connect(self.admin.ASSET_DB)) as connection:
            connection.execute(
                """CREATE TABLE audio_assets(
                       id INTEGER PRIMARY KEY,job_id INTEGER,file TEXT,url TEXT,
                       asset_kind TEXT,metadata_json TEXT,created_at INTEGER)"""
            )
            connection.executemany(
                "INSERT INTO audio_assets VALUES(?,?,?,?,?,?,?)",
                [
                    (1, 30, "public.mp3", "/api/gen/file/public.mp3", "voice", "{}", now - 29),
                    (2, 31, "personal.mp3", "/api/gen/file/personal.mp3", "voice", "{}", now - 30),
                ],
            )
            connection.commit()

        operations = {item["id"]: item["operation"] for item in self.admin.call_logs(7, 100)["items"]}
        self.assertEqual(operations[30], "audio.tts.public")
        self.assertEqual(operations[31], "audio.tts.personal")
        for job_id in range(32, 39):
            self.assertIsNone(operations[job_id])

        with mock.patch.object(self.admin.subprocess, "run") as ffprobe:
            ffprobe.return_value = mock.Mock(returncode=0, stdout=b"mp3\n", stderr=b"")
            stats = self.admin.job_stats(7)
        self.assertEqual(ffprobe.call_count, 2)
        for call in ffprobe.call_args_list:
            command = call.args[0]
            self.assertIn("a:0", command)
            self.assertNotIn("v:0", command)
        by_operation = {item["operation"]: item for item in stats["by_operation"]}
        for operation, asset_id in (("audio.tts.public", 1), ("audio.tts.personal", 2)):
            latest = by_operation[operation]["latest"]
            self.assertEqual(latest["asset_id"], asset_id)
            self.assertEqual(latest["asset_status"], "done")
            self.assertEqual(latest["asset_kind"], "voice")
            self.assertTrue(latest["delivery_verified"])
            self.assertEqual(latest["artifact_check"], "decodable")
            self.assertEqual(latest["billing_state"], "unverified")
            self.assertIsNone(latest["provider_task_id"])
        self.assertEqual(stats["evidence_errors"], [])

    def test_damaged_audio_artifact_is_not_delivery_verified_for_any_extension(self):
        for name in ("broken.bin", "broken.mp4"):
            (self.admin.CONTENT_OUT / name).write_bytes(b"not-audio")
        with mock.patch.object(self.admin.subprocess, "run") as ffprobe:
            ffprobe.return_value = mock.Mock(returncode=1, stdout=b"", stderr=b"invalid data")
            for name in ("broken.bin", "broken.mp4"):
                evidence = self.admin._verify_local_artifact({
                    "result_file": name,
                    "result_url": "/api/gen/file/" + name,
                    "delivery_verified": False,
                    "artifact_check": "not_recorded",
                    "_artifact_media_type": "audio",
                })
                self.assertEqual(evidence["artifact_check"], "decode_failed")
                self.assertFalse(evidence["delivery_verified"])
                self.assertNotIn("_artifact_media_type", evidence)
        self.assertEqual(ffprobe.call_count, 2)
        for call in ffprobe.call_args_list:
            command = call.args[0]
            self.assertIn("a:0", command)
            self.assertNotIn("v:0", command)


if __name__ == "__main__":
    unittest.main()
