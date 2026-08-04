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

SRC = (Path(__file__).resolve().parents[1] / "server/content_domains/video.py").read_text(encoding="utf-8")


class PipelineWiringTests(unittest.TestCase):
    """新 kind 的管线是「一环都不能少」的：少接一环就是静默故障。"""

    def test_handlers_expose_both_new_kinds(self):
        # 路由是自动的：/api/gen/<kind> 由 HANDLERS 派生
        self.assertIn("avatar", registry.HANDLERS)
        self.assertIn("cinematic", registry.HANDLERS)

    def test_costs_are_registered(self):
        """⚠️ cost_of() 回落到 COST.get(kind, 0) —— 新 kind 忘了登记就是【免费】。"""
        self.assertEqual(points.cost_of("avatar", {}), 2)
        # 获客按 6 + floor(数量×页数÷4) 计费；前端固定 20 条、1 页时是 11 点。
        self.assertEqual(points.cost_of("leads", {}), 9)
        self.assertEqual(points.cost_of("leads", {"count": 20, "pages": 1}), 11)
        self.assertEqual(points.cost_of("leads", {"count": 100, "pages": 3}), 28)
        # cinematic 改成按成片秒数计费了（见 test_cinematic_billing），这里只守住底线：
        # 无论 payload 多空、玩法认不认得出来，都不能算出 0 点 —— 那是白送一条 $7 的视频。
        for kind in ("avatar", "cinematic"):
            self.assertGreater(points.cost_of(kind, {}), 0, "%s 免费了 —— COST 里漏登记" % kind)

    def test_reaper_grace_is_long_enough(self):
        # cinematic 现在跟全站的 15 分钟死线走（见 test_video_gen_deadline）：
        # reaper 的宽限必须【后】于引擎自己的死线触发，否则白扣一次上游的钱。
        # 电影化身有【自己的】死线(30 分钟)和宽限(35 分钟)，不再跟口播那条 VIDEO_REAPER_GRACE 走。
        self.assertEqual(core.KIND_GRACE["cinematic"], core.CINEMATIC_REAPER_GRACE)
        self.assertGreater(core.KIND_GRACE["cinematic"], core.CINEMATIC_GEN_DEADLINE)
        self.assertGreater(core.VIDEO_REAPER_GRACE, core.VIDEO_GEN_DEADLINE)
        self.assertGreaterEqual(core.KIND_GRACE["avatar"], 120)   # avatar 实测 25s

    def test_each_kind_lands_in_its_own_pool(self):
        self.assertIs(core._pick_job_queue("cinematic"), core._cinematic_job_queue)
        self.assertIs(core._pick_job_queue("avatar"), core._avatar_job_queue)
        # 别掉进快池——那是给秒级任务的，8 分钟的视频会把它堵死
        self.assertIsNot(core._pick_job_queue("cinematic"), core._fast_job_queue)
        self.assertIsNot(core._pick_job_queue("avatar"), core._fast_job_queue)

    def test_avatar_pool_is_no_longer_serial(self):
        """2026-07-12 实测：5 路并发 5/5 成功、0×429、零降速。详见 test_avatar_concurrency。
        串行只有 144 个/小时 —— 500 人集中建形象要排 3.5 小时，而它是电影化身的【入口】。"""
        self.assertGreaterEqual(core.AVATAR_JOB_WORKERS, 5)

    def test_the_gate_does_not_throttle_the_workers_it_is_supposed_to_serve(self):
        """并发闸必须【容得下】所有 worker，否则 worker 数就白设了。

        闸是【削峰】用的，不是挡并发的。20 路并发实测（10 口播 + 10 剧情视频同时生成）：
        20/20 全部成功、零降速（口播 114s vs 单条基线 104s）—— HeyGen 的渲染容量远大于 20，
        官方文档说的「Max Concurrent Video Jobs = 10」不是硬限制。

        所以闸只需要 ≥ 所有打 HeyGen 的 worker 之和；配小了，worker 会卡在信号量上排队，
        「口播 10 + 剧情 10」就变成了实际只有 10 个在跑。
        """
        # 只算【真的占闸】的池。建形象不占（见下一条）—— 把它算进来，它就得跟视频 worker 抢槽。
        gated = core.TALKING_JOB_WORKERS + core.CINEMATIC_JOB_WORKERS
        self.assertGreaterEqual(video.HEYGEN_MAX_CONCURRENCY, gated,
                                "闸(%d) 小于占闸的 worker 总数(%d) —— worker 数白设了"
                                % (video.HEYGEN_MAX_CONCURRENCY, gated))
        self.assertEqual(video._heygen_gen_sem._initial_value, video.HEYGEN_MAX_CONCURRENCY)

    def test_building_an_avatar_does_not_take_a_video_slot(self):
        """闸是给【视频提交】削峰的。建形象不是 video job —— 它不吃 HeyGen 的视频并发额度，
        让它去抢那 31 个槽，只会把视频 worker 饿着。

        而且它本来也不需要：10 路并发建形象实测 0×429（瓶颈是我们自己的出境隧道，不是 HeyGen）。
        真撞上突发限流，_heygen_retry_429 已经包在建形象的提交上了。
        """
        block = SRC.split("def gen_avatar")[1].split("\ndef ")[0]
        self.assertNotIn("heygen_slot", block)

    def test_cinematic_gets_a_real_pool_now_that_20_way_is_proven(self):
        # 20 路并发实测通过 → 剧情视频不再需要「份额上限」，给满 10 个
        self.assertEqual(core.CINEMATIC_JOB_WORKERS, 10)

    def test_talking_pool_and_backlog_match_capacity_config(self):
        self.assertEqual(core.TALKING_JOB_WORKERS, 20)
        self.assertEqual(video.HEYGEN_MAX_CONCURRENCY, 31)
        self.assertEqual(core.JOB_QUEUE_MAX, 64)
        self.assertEqual(core.TALKING_JOB_QUEUE_MAX, 192)
        self.assertEqual(core._talking_job_queue.maxsize, 192)
        for q in (core._job_queue, core._fast_job_queue, core._image_job_queue,
                  core._cinematic_job_queue, core._avatar_job_queue):
            self.assertEqual(q.maxsize, 64)
        self.assertEqual(core._PENDING_RECOVERY_LIMIT, 192)

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
        self.assertIn('{"video", "tryon", "xiaole_video", "sora_video", "cinematic"}', src)

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

    def test_legacy_resolutions_are_accepted_but_forced_to_720p(self):
        for r in ("720p", "1080p"):
            self.assertEqual(video.validate_cinematic_payload(self._body(resolution=r))["resolution"], "720p")

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
        self.assertEqual(body["resolution"], "720p")

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


class VideoModeTests(unittest.TestCase):
    """口播只剩 text/audio，都走 HeyGen。"""

    def test_talking_still_requires_the_heygen_key(self):
        # 口播走 HeyGen，密钥缺失就该明确报错，而不是跑到一半才炸
        with patch.object(video, "HEYGEN_API_KEY", ""):
            with self.assertRaisesRegex(ValueError, "未配置"):
                video.gen_video({"mode": "text", "image_data": "i", "text": "hi"})

    def test_motion_mode_is_retired(self):
        # 老版动作模仿已下线：mode=motion 不再是合法提交
        self.assertNotIn("motion", video.VALID_VIDEO_MODES)
        with self.assertRaises(ValueError):
            video.validate_video_payload({
                "mode": "motion", "image_data": "data:image/png;base64,iVBORw0KGgo=",
                "reference_video_data": "data:video/mp4;base64,AAAA",
            })


if __name__ == "__main__":
    unittest.main()
