# -*- coding: utf-8 -*-
"""剧情视频：多参考素材（视频 + 图片）+ 自动润色开关。

## 媒体预算是【和形象共用的】

官方文档原文：

    「Avatar looks and references share a combined media budget:
      at most 3 videos and 9 images total across avatar_id and references.」

不是各算各的。选了 3 个形象，参考图就只剩 6 张额度。

⚠️ 文档没明说「每个 avatar look 算不算一张图」。这里按【算】处理（保守）：
宁可少放，也别让 HeyGen 400 —— 那时视频已经提交、钱已经扣了（提交即计费），
报错对用户就是白扣一次。

## enhance_prompt

官方文档：「Auto-expand a short prompt into a richer description」。
默认【关】——它可能把用户的意图改跑偏，要不要开由用户决定。
"""
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")

PNG = "data:image/png;base64,iVBORw0KGgo="
MP4 = "data:video/mp4;base64,AAAAGGZ0eXA="


class MediaBudgetTests(unittest.TestCase):
    def test_avatars_eat_into_the_image_budget(self):
        """形象和参考图共用 9 张的额度 —— 不是各算各的。"""
        self.assertEqual(video.cinematic_ref_budget(1), (3, 8))
        self.assertEqual(video.cinematic_ref_budget(3), (3, 6))

    def test_videos_are_not_eaten_by_avatars(self):
        # 形象是图片，不占视频额度
        for n in (1, 2, 3):
            self.assertEqual(video.cinematic_ref_budget(n)[0], 3)


class ValidationTests(unittest.TestCase):
    """⚠️ 参考素材现在在【校验阶段】就落盘（按成片秒数计费，扣点前必须探测出时长），
    所以 cleaned payload 里是文件路径（reference_video_files / reference_image_files），
    不再是 base64 数组。base64 只在请求体里出现一次，落盘后就从 payload 里删掉。
    """

    def setUp(self):
        self._n = [0]

        def fake_save(data, prefix, exts):
            self._n[0] += 1
            return "video/%s_%d%s" % (prefix, self._n[0], exts[0])

        for x in (patch.object(video, "_save_data_file", fake_save),
                  patch.object(video, "_probe_video_duration", lambda f: 8.0)):
            x.start()
            self.addCleanup(x.stop)

    def _body(self, **kw):
        b = {"cine_mode": "open", "avatar_ids": [1], "prompt": "海边跳舞",
             "resolution": "720p", "ratio": "9:16"}
        b.update(kw)
        return b

    def test_multiple_reference_videos_and_images(self):
        out = video.validate_cinematic_payload(self._body(
            reference_videos=[MP4, MP4], reference_images=[PNG, PNG, PNG]))
        self.assertEqual(len(out["reference_video_files"]), 2)
        self.assertEqual(len(out["reference_image_files"]), 3)
        # base64 落盘后就从 payload 里删掉 —— 留着 jobs.payload 会被几十 MB 撑爆
        for k in ("reference_videos", "reference_images"):
            self.assertNotIn(k, out)

    def test_the_old_single_field_still_works(self):
        """老前端发的 reference_video_data 不能 400 —— 合进 reference_videos 再落盘。"""
        out = video.validate_cinematic_payload(self._body(reference_video_data=MP4))
        self.assertEqual(len(out["reference_video_files"]), 1)
        self.assertNotIn("reference_video_data", out, "别留两份，下游会重复上传")

    def test_video_cap_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "参考视频最多 3 个"):
            video.validate_cinematic_payload(self._body(reference_videos=[MP4] * 4))

    def test_image_cap_shrinks_with_more_avatars(self):
        """3 个形象 → 只剩 6 张图。报错要说清楚【为什么】只剩这么多，
        否则用户会以为是 bug（文档明明说 9 张）。"""
        body = self._body(avatar_ids=[1, 2, 3], reference_images=[PNG] * 7)
        with self.assertRaises(ValueError) as ctx:
            video.validate_cinematic_payload(body)
        msg = str(ctx.exception)
        self.assertIn("最多 6 张", msg)
        self.assertIn("共用", msg)
        self.assertIn("已选 3 个形象", msg)

    def test_one_avatar_leaves_eight_images(self):
        out = video.validate_cinematic_payload(self._body(reference_images=[PNG] * 8))
        self.assertEqual(len(out["reference_image_files"]), 8)

    def test_wrong_media_types_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "参考视频格式"):
            video.validate_cinematic_payload(self._body(reference_videos=[PNG]))
        with self.assertRaisesRegex(ValueError, "参考图片格式"):
            video.validate_cinematic_payload(self._body(reference_images=[MP4]))

    def test_enhance_prompt_defaults_off(self):
        self.assertFalse(video.validate_cinematic_payload(self._body())["enhance_prompt"])

    def test_enhance_prompt_can_be_turned_on(self):
        self.assertTrue(video.validate_cinematic_payload(self._body(enhance_prompt=True))["enhance_prompt"])


class RequestBodyTests(unittest.TestCase):
    def _capture(self, **kw):
        seen = {}

        def fake(method, path, body, headers, timeout=90, direct=False):
            seen.update(json.loads(body))
            return {"data": {"video_id": "v1"}}

        with patch.object(video, "_heygen_request_json", fake):
            video._heygen_create_cinematic_video(direct=True, **kw)
        return seen

    def _base(self, **kw):
        b = {"avatar_item_id": ["look1"], "reference_asset_id": None,
             "ratio": "9:16", "resolution": "720p", "duration": 10, "prompt": "跳舞"}
        b.update(kw)
        return b

    def test_all_references_go_into_the_array(self):
        body = self._capture(**self._base(reference_asset_id=["a1", "a2", "a3"]))
        self.assertEqual(body["references"],
                         [{"type": "asset_id", "asset_id": a} for a in ("a1", "a2", "a3")])

    def test_a_single_asset_id_still_works(self):
        # 兼容老调用：动作模仿那条路径传的就是单个（不是列表）
        body = self._capture(**self._base(reference_asset_id="a1"))
        self.assertEqual(body["references"], [{"type": "asset_id", "asset_id": "a1"}])

    def test_no_references_key_when_there_are_none(self):
        body = self._capture(**self._base())
        self.assertNotIn("references", body, "不能发一个空的 references")

    def test_enhance_prompt_is_forwarded(self):
        self.assertTrue(self._capture(**self._base(enhance_prompt=True))["enhance_prompt"])
        self.assertFalse(self._capture(**self._base())["enhance_prompt"], "默认必须是关的")


class UiTests(unittest.TestCase):
    def test_enhance_toggle_exists_and_warns(self):
        self.assertIn('id="cineEnhance"', HTML)
        self.assertIn("可能偏离你的原意", HTML, "要告诉用户开了会有什么代价")
        self.assertIn("enhance_prompt:!!($('cineEnhance')&&$('cineEnhance').checked)", HTML)

    def test_both_reference_kinds_can_be_uploaded_multiple(self):
        self.assertIn('id="cineVideoFile"', HTML)
        self.assertIn('id="cineImageFile"', HTML)
        self.assertEqual(HTML.count('multiple hidden'), 3, "批量形象 + 参考视频 + 参考图片")

    def test_the_shared_budget_is_shown(self):
        """预算随形象数变化，必须显示 —— 否则用户会以为「怎么只能传 6 张，文档说 9 张」。"""
        self.assertIn('id="cineRefBudget"', HTML)
        self.assertIn("cineMaxRefImages()", HTML)
        self.assertIn("CINE_MAX_MEDIA_IMAGES-cineSelectedAvatarIds.length", HTML)

    def test_only_the_base64_is_sent_not_the_display_name(self):
        # store 里存的是 {data, name}，name 只用于界面。发上去的必须是 base64 本身。
        self.assertIn("cineRefVideos.map(function(x){return x.data;})", HTML)
        self.assertIn("cineRefImages.map(function(x){return x.data;})", HTML)


if __name__ == "__main__":
    unittest.main()
