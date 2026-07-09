# -*- coding: utf-8 -*-
"""统一 assets 表：写入幂等、stage 归类、meta 投影、读取过滤。

要点：
- record_asset 只对 image/copy/collect/leads 生效（audio/video 仍走各自的旧表）
- UNIQUE(kind, job_id) + INSERT OR IGNORE → 重复写不产生重复行（回填脚本可反复跑）
- meta 不复制大块数据：collect 的 comments、leads 的名单留在 jobs.result 里
"""
import importlib, os, sys, tempfile, unittest
from contextlib import closing
from pathlib import Path


class AssetsStoreTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CONTENT_ASSET_DB"] = os.path.join(self.tmp.name, "assets.db")
        # 每个用例都重新导入，让模块级 ASSET_DB / _initialized 跟着新临时库走
        self.store = importlib.reload(importlib.import_module("content_domains.assets_store"))
        self.store.init_assets()

    def tearDown(self):
        os.environ.pop("CONTENT_ASSET_DB", None)
        self.tmp.cleanup()

    def _count(self):
        with closing(self.store.adb()) as c:
            return c.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    # --- stage 默认归类，落地 DESIGN.md 的素材/作品/交付 ---
    def test_default_stage_per_kind(self):
        self.assertEqual(self.store.KIND_STAGE["collect"], self.store.MATERIAL)
        self.assertEqual(self.store.KIND_STAGE["image"], self.store.WORK)
        self.assertEqual(self.store.KIND_STAGE["copy"], self.store.WORK)
        self.assertEqual(self.store.KIND_STAGE["leads"], self.store.DELIVERY)

    def test_record_image_projects_fields(self):
        result = {"type": "image", "mode": "text2img", "provider": "xiaole", "count": 2,
                  "file": "img_a.png", "url": "https://cos/img_a.png", "ratio": "9:16",
                  "files": ["img_a.png", "img_b.png"], "urls": ["u1", "u2"],
                  "prompt": "科技焕肤 高级感美业海报"}
        self.assertTrue(self.store.record_asset(11, "u", "image", result))
        a = self.store.list_assets("u", kind="image")[0]
        self.assertEqual(a["stage"], "work")
        self.assertEqual(a["file"], "img_a.png")
        self.assertEqual(a["url"], "https://cos/img_a.png")
        self.assertEqual(a["title"], "科技焕肤 高级感美业海报")
        self.assertEqual(a["meta"]["provider"], "xiaole")
        self.assertEqual(a["meta"]["files"], ["img_a.png", "img_b.png"])

    def test_record_collect_prefers_play_url_and_drops_comments(self):
        result = {"type": "collect", "platform": "douyin", "source": "https://v.douyin.com/x",
                  "video": {"title": "标题", "author": "作者", "play_url": "https://cos/v.mp4",
                            "cover": "https://cdn/cover.jpg", "duration": 32},
                  "copy": {"desc": "描述", "tags": ["美业"]},
                  "transcript": {"text": "口播文案"},
                  "comments": [{"c": 1}] * 500,       # 大块数据，不该进 meta
                  "url": "https://cdn/cover.jpg"}
        self.assertTrue(self.store.record_asset(12, "u", "collect", result))
        a = self.store.list_assets("u", kind="collect")[0]
        self.assertEqual(a["stage"], "material")
        self.assertEqual(a["url"], "https://cos/v.mp4")     # 优先可播放直链而非封面
        self.assertEqual(a["title"], "标题")
        self.assertTrue(a["meta"]["has_transcript"])
        self.assertEqual(a["meta"]["comments_count"], 500)
        self.assertNotIn("comments", a["meta"])             # 名单/评论不复制一份

    def test_record_leads_drops_lead_list(self):
        result = {"type": "leads", "keyword": "美业获客", "platforms": ["douyin"],
                  "leads_count": 3, "spam": 7, "chat": 2, "total": 12,
                  "leads": [{"nickname": "a"}] * 3}
        self.assertTrue(self.store.record_asset(13, "u", "leads", result))
        a = self.store.list_assets("u", kind="leads")[0]
        self.assertEqual(a["stage"], "delivery")
        self.assertEqual(a["title"], "美业获客")
        self.assertEqual(a["meta"]["leads_count"], 3)
        self.assertNotIn("leads", a["meta"])

    def test_record_copy_keeps_text(self):
        result = {"type": "copy", "ctype": "朋友圈", "text": "文案正文", "prompt": "夏季促销"}
        self.assertTrue(self.store.record_asset(14, "u", "copy", result))
        a = self.store.list_assets("u", kind="copy")[0]
        self.assertEqual(a["title"], "夏季促销")
        self.assertEqual(a["meta"]["text"], "文案正文")
        self.assertIsNone(a["file"])

    # --- 幂等：回填脚本会反复跑 ---
    def test_record_is_idempotent(self):
        r = {"type": "copy", "text": "x", "prompt": "p"}
        self.assertTrue(self.store.record_asset(20, "u", "copy", r))
        self.assertFalse(self.store.record_asset(20, "u", "copy", r))   # 第二次不写
        self.assertFalse(self.store.record_asset(20, "u", "copy", r))
        self.assertEqual(self._count(), 1)

    # --- 同一个 job_id 不同 kind 互不冲突（UNIQUE 是复合键）---
    def test_same_job_id_different_kind(self):
        self.assertTrue(self.store.record_asset(30, "u", "copy", {"prompt": "a"}))
        self.assertTrue(self.store.record_asset(30, "u", "image", {"prompt": "b"}))
        self.assertEqual(self._count(), 2)

    # --- audio/video 不进这张表（仍走 audio_assets / video_assets）---
    def test_audio_video_not_recorded(self):
        self.assertFalse(self.store.record_asset(40, "u", "audio", {"file": "a.mp3"}))
        self.assertFalse(self.store.record_asset(41, "u", "video", {"file": "v.mp4"}))
        self.assertEqual(self._count(), 0)

    def test_no_username_not_recorded(self):
        self.assertFalse(self.store.record_asset(50, "", "copy", {"prompt": "x"}))
        self.assertEqual(self._count(), 0)

    # --- 读取：按 kind / stage 过滤，软删后不再返回，跨用户隔离 ---
    def test_list_filters_and_isolation(self):
        self.store.record_asset(60, "u", "copy", {"prompt": "c"})
        self.store.record_asset(61, "u", "collect", {"video": {"title": "t"}})
        self.store.record_asset(62, "other", "copy", {"prompt": "别人的"})
        self.assertEqual(len(self.store.list_assets("u")), 2)
        self.assertEqual(len(self.store.list_assets("u", kind="copy")), 1)
        self.assertEqual(len(self.store.list_assets("u", stage="material")), 1)
        self.assertEqual(len(self.store.list_assets("other")), 1)

    def test_soft_delete(self):
        self.store.record_asset(70, "u", "copy", {"prompt": "x"})
        aid = self.store.list_assets("u")[0]["id"]
        self.assertFalse(self.store.soft_delete("other", aid))   # 不是自己的删不掉
        self.assertTrue(self.store.soft_delete("u", aid))
        self.assertFalse(self.store.soft_delete("u", aid))       # 重复删返回 False
        self.assertEqual(self.store.list_assets("u"), [])

    def test_invalid_stage_falls_back_to_default(self):
        self.store.record_asset(80, "u", "copy", {"prompt": "x"}, stage="不存在的阶段")
        self.assertEqual(self.store.list_assets("u")[0]["stage"], "work")


if __name__ == "__main__":
    unittest.main()
