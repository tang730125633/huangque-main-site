# -*- coding: utf-8 -*-
"""把动作模仿拆成两步：建形象 / 生成视频。

## 为什么拆

原来建 avatar 混在动作模仿任务里。用户传的照片若检测不到人脸，HeyGen 报
「No face detected in the image」——整个任务失败，20 点虽然会退，但用户白等了几分钟。
线上失败样本里这一类占了相当比例。

拆开之后：
  * 建形象  独立一步，5 点，实测 25 秒，失败当场暴露。建好可反复用，是长期资产。
  * 剧情视频 只做生成：选 1~3 个【已建好】的形象 + 自己的提示词 + 720p/1080p。
    不再传人物图、不再建 avatar、不再等 avatar 就绪 —— 这条精简路径实测 10 路并发
    无 429、生成不降速。

## 动作模仿去线路化

原来 motion 有两条线路（HeyGen / WaveSpeed）。HeyGen 那条的能力（多形象、自定义提示词、
1080p）已经拆成独立的「AI 剧情视频」，所以动作模仿只留 WaveSpeed，做它最擅长的一件事：
人物图 + 驱动视频 → 照着跳，固定 720p。

## HeyGen 的接口契约（查文档 + 实测确认）

  avatar_id 是 1~3 个 look 的【数组】。多个 look = 同一个镜头里出现多个人，
  不是生成多条视频 —— 所以 3 个形象仍然只扣 1 条视频的钱。
  resolution: 720p / 1080p；duration: 4~15 秒（扁平计价，与时长无关）
  aspect_ratio: 16:9 / 9:16 / 1:1
  references（参考视频）是【可选】的：只给提示词也能生成。
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

core = importlib.import_module("content_domains.core")
video = importlib.import_module("content_domains.video")
points = importlib.import_module("content_domains.points")
registry = importlib.import_module("content_domains.registry")
feature_flags = importlib.import_module("content_domains.feature_flags")


class PipelineWiringTests(unittest.TestCase):
    """新 kind 的管线是「一环都不能少」的：少接一环就是静默故障。"""

    def test_handlers_expose_both_new_kinds(self):
        # 路由是自动的：/api/gen/<kind> 由 HANDLERS 派生
        self.assertIn("avatar", registry.HANDLERS)
        self.assertIn("cinematic", registry.HANDLERS)

    def test_costs_are_registered(self):
        """⚠️ cost_of() 回落到 COST.get(kind, 0) —— 新 kind 忘了登记就是【免费】。"""
        self.assertEqual(points.cost_of("avatar", {}), 5)
        # cinematic 改成按成片秒数计费了（见 test_cinematic_billing），这里只守住底线：
        # 无论 payload 多空、玩法认不认得出来，都不能算出 0 点 —— 那是白送一条 $7 的视频。
        for kind in ("avatar", "cinematic"):
            self.assertGreater(points.cost_of(kind, {}), 0, "%s 免费了 —— COST 里漏登记" % kind)

    def test_reaper_grace_is_long_enough(self):
        # cinematic 实测生成 339~511s，加上传下载；avatar 实测 25s
        self.assertGreaterEqual(core.KIND_GRACE["cinematic"], 1800)
        self.assertGreaterEqual(core.KIND_GRACE["avatar"], 120)

    def test_each_kind_lands_in_its_own_pool(self):
        self.assertIs(core._pick_job_queue("cinematic"), core._cinematic_job_queue)
        self.assertIs(core._pick_job_queue("avatar"), core._avatar_job_queue)
        # 别掉进快池——那是给秒级任务的，8 分钟的视频会把它堵死
        self.assertIsNot(core._pick_job_queue("cinematic"), core._fast_job_queue)
        self.assertIsNot(core._pick_job_queue("avatar"), core._fast_job_queue)

    def test_avatar_pool_is_serial(self):
        self.assertEqual(core.AVATAR_JOB_WORKERS, 1, "建形象要求串行")

    def test_the_gate_does_not_throttle_the_workers_it_is_supposed_to_serve(self):
        """并发闸必须【容得下】所有 worker，否则 worker 数就白设了。

        闸是【削峰】用的，不是挡并发的。20 路并发实测（10 口播 + 10 剧情视频同时生成）：
        20/20 全部成功、零降速（口播 114s vs 单条基线 104s）—— HeyGen 的渲染容量远大于 20，
        官方文档说的「Max Concurrent Video Jobs = 10」不是硬限制。

        所以闸只需要 ≥ 所有打 HeyGen 的 worker 之和；配小了，worker 会卡在信号量上排队，
        「口播 10 + 剧情 10」就变成了实际只有 10 个在跑。
        """
        heygen_workers = core.TALKING_JOB_WORKERS + core.CINEMATIC_JOB_WORKERS + core.AVATAR_JOB_WORKERS
        self.assertGreaterEqual(video.HEYGEN_MAX_CONCURRENCY, heygen_workers,
                                "闸(%d) 小于打 HeyGen 的 worker 总数(%d) —— worker 数白设了"
                                % (video.HEYGEN_MAX_CONCURRENCY, heygen_workers))
        self.assertEqual(video._heygen_gen_sem._initial_value, video.HEYGEN_MAX_CONCURRENCY)

    def test_cinematic_gets_a_real_pool_now_that_20_way_is_proven(self):
        # 20 路并发实测通过 → 剧情视频不再需要「份额上限」，给满 10 个
        self.assertEqual(core.CINEMATIC_JOB_WORKERS, 10)

    def test_every_heygen_generation_path_takes_a_slot(self):
        """漏掉任何一条路径，那条就绕过了闸 —— 包括中转（泽龙转发的是同一个账号）。"""
        src = Path(video.__file__).read_text(encoding="utf-8")
        for label in ("口播直连", "口播中转", "剧情视频"):
            self.assertIn('heygen_slot("%s")' % label, src)

    def test_handlers_receive_username_and_job_id(self):
        """gen_avatar 要 _username 才能记形象归属；gen_cinematic 要它才能查到用户的形象。

        漏掉一个 kind，handler 就拿不到 —— 而且是静默的：形象建出来了但不属于任何人。
        """
        src = Path(core.__file__).read_text(encoding="utf-8")
        block = src.split('payload["_username"] = username')[0].rsplit("if kind in", 1)[-1]
        self.assertIn('"cinematic"', block)
        self.assertIn('"avatar"', block)

    def test_cinematic_results_reach_the_video_asset_library(self):
        # 剧情视频是视频，必须进视频资产库，否则用户在资产库里看不到它
        src = Path(core.__file__).read_text(encoding="utf-8")
        self.assertIn('{"video", "tryon", "xiaole_video", "cinematic"}', src)

    def test_both_features_are_toggleable_from_admin(self):
        self.assertIn("avatar", feature_flags.CATALOG_MAP)
        self.assertIn("cinematic", feature_flags.CATALOG_MAP)


class AvatarValidationTests(unittest.TestCase):
    def test_requires_an_image(self):
        with self.assertRaises(ValueError):
            video.validate_avatar_payload({})

    def test_rejects_non_image_data_url(self):
        with self.assertRaises(ValueError):
            video.validate_avatar_payload({"image_data": "data:video/mp4;base64,AAAA"})

    def test_name_is_trimmed(self):
        body = video.validate_avatar_payload({
            "image_data": "data:image/png;base64,iVBORw0KGgo=", "name": "x" * 99})
        self.assertEqual(len(body["name"]), 40)

    def test_no_face_detected_becomes_a_human_message(self):
        """HeyGen 返回的是英文报文，原样丢给用户毫无意义 —— 这是线上最常见的失败。"""
        with patch.object(video, "_save_data_file", return_value="video/x.jpg"), \
             patch.object(video, "_resolve_out_file", return_value=Path("/tmp/x.jpg")), \
             patch.object(video, "_ensure_heygen_image_jpg", return_value=Path("/tmp/x.jpg")), \
             patch.object(video, "_heygen_upload_asset",
                          side_effect=RuntimeError('HeyGen接口失败: HTTP 400 {"error":{"code":"invalid_parameter",'
                                                   '"message":"No face detected in the image image/abc/original.jpg"}}')):
            with self.assertRaises(ValueError) as ctx:
                video.gen_avatar({"_username": "kongli", "image_data": "x"})
        self.assertIn("人脸", str(ctx.exception))


class CinematicValidationTests(unittest.TestCase):
    PNG = "data:image/png;base64,iVBORw0KGgo="

    def _body(self, **kw):
        b = {"avatar_ids": [1], "prompt": "在海边跳舞，夕阳背光", "resolution": "720p",
             "ratio": "9:16", "duration": 10}
        b.update(kw)
        return b

    def test_accepts_up_to_three_avatars(self):
        out = video.validate_cinematic_payload(self._body(avatar_ids=[1, 2, 3]))
        self.assertEqual(out["avatar_ids"], [1, 2, 3])

    def test_rejects_more_than_three(self):
        # HeyGen 的硬上限：avatar_id 数组最多 3 个
        with self.assertRaises(ValueError):
            video.validate_cinematic_payload(self._body(avatar_ids=[1, 2, 3, 4]))

    def test_requires_at_least_one_avatar(self):
        with self.assertRaises(ValueError):
            video.validate_cinematic_payload(self._body(avatar_ids=[]))

    def test_dedupes_avatar_ids(self):
        out = video.validate_cinematic_payload(self._body(avatar_ids=[2, 2, 2]))
        self.assertEqual(out["avatar_ids"], [2])

    def test_avatar_ownership_is_checked(self):
        """别人的形象 id 不能借用 —— get_video_avatar 只认本人的。"""
        called = []
        with patch.object(video, "get_video_avatar", side_effect=lambda u, a: called.append((u, a))):
            video.validate_cinematic_payload(self._body(avatar_ids=[7, 8]), username="kongli")
        self.assertEqual(called, [("kongli", 7), ("kongli", 8)])

    def test_prompt_is_required(self):
        with self.assertRaises(ValueError):
            video.validate_cinematic_payload(self._body(prompt="   "))

    def test_both_resolutions_are_allowed(self):
        for r in ("720p", "1080p"):
            self.assertEqual(video.validate_cinematic_payload(self._body(resolution=r))["resolution"], r)

    def test_rejects_unsupported_resolution(self):
        with self.assertRaises(ValueError):
            video.validate_cinematic_payload(self._body(resolution="4k"))

    def test_rejects_ratio_heygen_cannot_do(self):
        # cinematic_avatar 只收 16:9/9:16/1:1；4:5 会被 HeyGen 判 invalid_parameter 400
        with self.assertRaises(ValueError):
            video.validate_cinematic_payload(self._body(ratio="4:5"))

    def test_duration_must_be_within_heygen_range(self):
        for bad in (3, 16):
            with self.assertRaises(ValueError):
                video.validate_cinematic_payload(self._body(duration=bad))

    def test_reference_video_is_optional(self):
        # 文档：references 用来 steer 风格/动作/构图，非必填 —— 只给提示词也能生成
        out = video.validate_cinematic_payload(self._body())
        self.assertNotIn("reference_video_data", out)


class CinematicRequestBodyTests(unittest.TestCase):
    """发给 HeyGen 的请求体。"""

    def _capture(self, **kw):
        seen = {}

        def fake_req(method, path, body, headers, timeout=90, direct=False):
            import json
            seen.update(json.loads(body))
            return {"data": {"video_id": "vid123"}}

        with patch.object(video, "_heygen_request_json", fake_req):
            video._heygen_create_cinematic_video(direct=True, **kw)
        return seen

    def test_multiple_avatars_go_into_one_video(self):
        body = self._capture(avatar_item_id=["look1", "look2", "look3"], reference_asset_id="ref1",
                             ratio="9:16", resolution="1080p", duration=10, prompt="海边跳舞")
        self.assertEqual(body["avatar_id"], ["look1", "look2", "look3"])
        self.assertEqual(body["resolution"], "1080p")

    def test_single_avatar_still_works(self):
        # 动作模仿那条老路径传的是单个 id（不是列表），不能因为改成数组就坏掉
        body = self._capture(avatar_item_id="look1", reference_asset_id="ref1",
                             ratio="9:16", resolution="720p", duration=10)
        self.assertEqual(body["avatar_id"], ["look1"])

    def test_references_omitted_when_there_is_no_reference_video(self):
        body = self._capture(avatar_item_id=["look1"], reference_asset_id=None,
                             ratio="9:16", resolution="720p", duration=10, prompt="海边跳舞")
        self.assertNotIn("references", body, "没有参考视频时不能发空的 references")

    def test_identity_guard_is_always_appended(self):
        """用户写的是创意，身份约束不由用户把关 —— 少了它，HeyGen 会把参考视频里的人抄进来。"""
        prompt = "在海边跳舞" + video.CINEMATIC_IDENTITY_GUARD
        body = self._capture(avatar_item_id=["look1"], reference_asset_id="r", ratio="9:16",
                             resolution="720p", duration=10, prompt=prompt)
        self.assertIn("Do NOT copy any person's appearance", body["prompt"])
        self.assertIn("在海边跳舞", body["prompt"])


class MotionIsWavespeedOnlyTests(unittest.TestCase):
    """动作模仿不再有线路之分。"""

    def test_line_is_stripped_from_payload(self):
        body = video.validate_video_payload({
            "mode": "motion", "image_data": "data:image/png;base64,iVBORw0KGgo=",
            "reference_video_data": "data:video/mp4;base64,AAAA", "line": "1",
        })
        self.assertNotIn("line", body, "老前端传来的 line 不能写进 payload，会混淆历史记录")

    def test_motion_is_locked_to_720p(self):
        with self.assertRaises(ValueError) as ctx:
            video.validate_video_payload({
                "mode": "motion", "image_data": "data:image/png;base64,iVBORw0KGgo=",
                "reference_video_data": "data:video/mp4;base64,AAAA", "resolution": "1080p",
            })
        self.assertIn("剧情视频", str(ctx.exception), "要告诉用户 1080p 去哪儿找")

    def test_motion_defaults_to_720p(self):
        body = video.validate_video_payload({
            "mode": "motion", "image_data": "data:image/png;base64,iVBORw0KGgo=",
            "reference_video_data": "data:video/mp4;base64,AAAA",
        })
        self.assertEqual(body["resolution"], "720p")

    def test_motion_is_not_hostage_to_the_heygen_key(self):
        """HeyGen 断供时，动作模仿必须照常能跑 —— 它根本不用 HeyGen。

        gen_video 开头原本有个无条件的 `if not HEYGEN_API_KEY: raise`。动作模仿全量切到
        WaveSpeed 之后，这条就变成了：HeyGen 密钥一缺，连不用它的功能也跟着挂。
        """
        from content_domains import wavespeed
        with patch.object(video, "HEYGEN_API_KEY", ""), \
             patch.object(video, "_save_data_file", side_effect=["image.png", "reference.mp4"]), \
             patch.object(video, "_shrink_motion_reference", side_effect=lambda p: p), \
             patch.object(video, "_motion_reference_duration", return_value=6), \
             patch.object(video, "update_video_asset_phase"), \
             patch.object(video, "public_url", return_value="/video/out.mp4"), \
             patch.object(wavespeed, "available", return_value=True), \
             patch.object(wavespeed, "generate_motion", return_value={"video_file": "video/out.mp4"}):
            out = video.gen_video({"mode": "motion", "image_data": "i", "reference_video_data": "v"})
        self.assertEqual(out["video_file"], "video/out.mp4")

    def test_talking_still_requires_the_heygen_key(self):
        # 但口播确实走 HeyGen，密钥缺失就该明确报错，而不是跑到一半才炸
        with patch.object(video, "HEYGEN_API_KEY", ""):
            with self.assertRaisesRegex(ValueError, "未配置"):
                video.gen_video({"mode": "text", "image_data": "i", "text": "hi"})

    def test_motion_no_longer_calls_heygen(self):
        """gen_video 的 motion 分发块里只能有 WaveSpeed。

        （锚点不能用 `if mode == "motion":` —— 校验器里也有一处同样的字样，会先匹配到。
        这个坑我今天已经踩过一次，用 `_motion_reference_duration` 这个只出现在分发块里的调用做锚。）
        """
        src = Path(video.__file__).read_text(encoding="utf-8")
        start = src.index("reference_duration = _motion_reference_duration(")
        block = src[start:src.index("video_result.setdefault(", start)]
        self.assertIn("wavespeed.generate_motion", block)
        self.assertNotIn("generate_heygen_motion_video", block,
                         "动作模仿不该再走 HeyGen —— 那条能力已经拆成 AI 剧情视频")


if __name__ == "__main__":
    unittest.main()
