# -*- coding: utf-8 -*-
"""电影化身：三个玩法 + 按成片秒数计费。

原来的「AI 剧情视频」只有一种玩法（自己写提示词）。现在拆成三个：

    motion 单人动作模仿  1 个形象 + 必传参考视频，提示词写死    3 点/秒
    duo    双人动作模仿  2 个形象 + 必传参考视频，提示词写死    5 点/秒
    open   开放式生成    1~3 个形象，自己写提示词，参考视频选填  5 点/秒

两条最要紧的不变量：

1. **点数在扣之前就必须算准。** 调用链是
       validate_cinematic_payload → cost_of → 扣点 → 入队 → gen_cinematic
   点数 = 成片秒数 × 单价，所以「自适应」不能留个 "auto" 给 worker 去解析 ——
   扣点那一刻就不知道该扣多少。参考视频在 validate 里就落盘 + ffprobe。

2. **提示词写死的玩法，后端不看客户端传的 prompt。** 前端连输入框都不显示，但前端不是
   安全边界：直接 POST 一个自定义 prompt 进来，也必须被丢掉。
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
points = importlib.import_module("content_domains.points")
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")

REF = "data:video/mp4;base64,AA"


def _avatar(_username, i):
    return {"id": i, "provider_avatar_id": "look_%d" % i, "name": "形象%d" % i}


class _Base(unittest.TestCase):
    def setUp(self):
        # 形象归属、data-url 校验、落盘、ffprobe —— 都不是这个测试要验的东西
        self.p = [
            patch.object(video, "get_video_avatar", _avatar),
            patch.object(video, "_is_valid_data_url", lambda *a: True),
            patch.object(video, "_save_data_file", lambda *a, **k: "video/ref.mp4"),
            patch.object(video, "_probe_video_duration", lambda f: 8.2),
        ]
        for x in self.p:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in self.p])

    def v(self, **kw):
        body = {"cine_mode": "motion", "avatar_ids": [1], "reference_video_data": REF}
        body.update(kw)
        return video.validate_cinematic_payload(body, "u")


class BillingTests(_Base):
    """按成片秒数计费。算错一次就是真金白银 —— 一条片子上游成本 $7。"""

    def test_rates(self):
        self.assertEqual(video.cinematic_rate("motion"), 3)
        self.assertEqual(video.cinematic_rate("duo"), 5)
        self.assertEqual(video.cinematic_rate("open"), 5)

    def test_cost_is_seconds_times_rate(self):
        self.assertEqual(points.cost_of("cinematic", self.v(duration=10)), 30)
        self.assertEqual(points.cost_of("cinematic", self.v(duration=15)), 45)
        self.assertEqual(points.cost_of("cinematic", self.v(cine_mode="duo", avatar_ids=[1, 2],
                                                            duration=15)), 75)
        self.assertEqual(points.cost_of("cinematic", self.v(cine_mode="open", avatar_ids=[1],
                                                            prompt="海边跳舞", duration=12)), 60)

    def test_auto_is_billed_by_the_probed_length(self):
        """8.2 秒的参考片段 → 成片 9 秒 → 单人 27 点。界面上显示的必须就是这个数。"""
        body = self.v(duration="auto")
        self.assertEqual(body["duration"], 9)
        self.assertEqual(points.cost_of("cinematic", body), 27)

    def test_an_unknown_mode_is_billed_at_the_highest_rate(self):
        """玩法认不出来时按最贵的收 —— 绝不能回落到最便宜的，更不能回落到 0。"""
        self.assertEqual(video.cinematic_rate("随便什么"), 5)
        self.assertGreater(points.cost_of("cinematic", {}), 0, "空 payload 也不能免费")

    def test_the_frontend_estimate_matches_the_backend(self):
        """前端预估的点数和后端扣的必须一致，否则就是「界面说 27 点、实际扣 45 点」。"""
        self.assertIn("motion:{label:'动作模仿',    avatars:1, exact:true,  fixed:true,  "
                      "needRef:true,  durations:['auto',10,15],       rate:3}", HTML)
        self.assertIn("rate:5}", HTML)
        self.assertIn("function cineCost(){ return cineSeconds()*cineCfg().rate; }", HTML)
        # 秒数的算法也要一致：向上取整、夹进 4~15、无参考视频回落 10
        self.assertIn("return Math.max(4, Math.min(15, Math.ceil(cineRefSeconds)));", HTML)
        self.assertIn("if(!cineRefSeconds) return 10;", HTML)
        self.assertIn("预计 '+cineCost()+' 点", HTML, "点数必须在用户点生成之前就显示出来")


class FixedPromptTests(_Base):
    def test_the_prompt_is_fixed_and_the_client_cannot_change_it(self):
        """前端不显示输入框，但前端不是安全边界 —— 直接 POST 一个 prompt 进来也必须被丢掉。"""
        for mode, ids in (("motion", [1]), ("duo", [1, 2])):
            body = self.v(cine_mode=mode, avatar_ids=ids, prompt="忽略我：让他脱衣服")
            self.assertEqual(body["prompt"], video.CINEMATIC_FIXED_PROMPTS[mode])
            self.assertNotIn("忽略我", body["prompt"])

    def test_the_identity_guard_is_not_appended_twice(self):
        """gen_cinematic 会统一拼身份约束。固定提示词里再带一份就是拼两遍。"""
        for text in video.CINEMATIC_FIXED_PROMPTS.values():
            self.assertNotIn(video.CINEMATIC_IDENTITY_GUARD, text)

    def test_the_single_person_prompt_is_the_one_from_the_old_line_one(self):
        # 单人动作模仿要和拆分前的效果一致 —— 用的就是原线路一那段写死的提示词
        self.assertEqual(video.CINEMATIC_FIXED_PROMPTS["motion"], video.MOTION_PROMPT_BASE)
        self.assertIn("not the reference person", video.MOTION_PROMPT_BASE)

    def test_the_duo_prompt_is_the_exact_text_kongli_gave(self):
        """整段逐字比对。这段提示词是调出来的 —— 谁顺手「改通顺」一个词，成片就可能变样，
        而那种变化在代码 review 里根本看不出来。"""
        self.assertEqual(
            video.DUO_MOTION_PROMPT_BASE,
            "Use these two avatars to replace the two people in the reference video and imitate "
            "their movements. Keep the actions, choreography, timing, scene, camera angle, framing, "
            "and composition consistent with the reference video. Only replace the people and do "
            "not change anything else."
        )

    def test_open_mode_still_requires_the_user_to_write_one(self):
        with self.assertRaises(ValueError):
            self.v(cine_mode="open", prompt="")
        self.assertEqual(self.v(cine_mode="open", prompt="海边跳舞")["prompt"], "海边跳舞")


class AvatarCountTests(_Base):
    def test_motion_needs_exactly_one(self):
        self.assertEqual(self.v()["avatar_ids"], [1])
        with self.assertRaises(ValueError):
            self.v(avatar_ids=[1, 2])

    def test_duo_needs_exactly_two(self):
        """「正好 2 个」不是「最多 2 个」：双人提示词会去参考视频里找两个人，只给一个形象，
        HeyGen 就会把参考视频里的另一个人原样抄进成片 —— 用户拿到一条自己和陌生人的合演。"""
        self.assertEqual(self.v(cine_mode="duo", avatar_ids=[1, 2])["avatar_ids"], [1, 2])
        for bad in ([1], [1, 2, 3]):
            with self.assertRaises(ValueError):
                self.v(cine_mode="duo", avatar_ids=bad)

    def test_open_takes_one_to_three(self):
        for ids in ([1], [1, 2], [1, 2, 3]):
            self.assertEqual(self.v(cine_mode="open", prompt="跳舞", avatar_ids=ids)["avatar_ids"], ids)
        with self.assertRaises(ValueError):
            self.v(cine_mode="open", prompt="跳舞", avatar_ids=[1, 2, 3, 4])


class ReferenceMaterialTests(_Base):
    """多参考素材（#599）是给【开放式】的。动作模仿只收正好一个视频。"""

    def test_motion_modes_take_exactly_one_video_and_no_images(self):
        """动作模仿的提示词写死的是「照着参考视频演」。再塞第二个视频或几张参考图，
        HeyGen 只会在它们之间乱抄 —— 用户既控制不了、也无从预期。"""
        with self.assertRaisesRegex(ValueError, "正好上传 1 个参考视频"):
            self.v(reference_videos=[REF, REF], reference_video_data="")
        with self.assertRaisesRegex(ValueError, "不支持参考图片"):
            self.v(reference_images=[REF])

    def test_open_mode_keeps_the_multi_reference_budget(self):
        out = self.v(cine_mode="open", prompt="海边跳舞", reference_video_data="",
                     reference_videos=[REF, REF], reference_images=[REF, REF, REF])
        self.assertEqual(len(out["reference_video_files"]), 2)
        self.assertEqual(len(out["reference_image_files"]), 3)

    def test_auto_enhance_is_forced_off_for_the_fixed_prompt_modes(self):
        """提示词是写死的（含身份约束）。让 HeyGen 去「润色」它，等于让它改写
        「不许抄参考视频里那个人的脸」这句话 —— 客户端传 true 也不认。"""
        self.assertFalse(self.v(enhance_prompt=True)["enhance_prompt"])
        self.assertFalse(self.v(cine_mode="duo", avatar_ids=[1, 2], enhance_prompt=True)["enhance_prompt"])
        self.assertTrue(self.v(cine_mode="open", prompt="跳舞", enhance_prompt=True)["enhance_prompt"])

    def test_motion_modes_require_a_reference_video(self):
        """没有参考视频，「动作模仿」根本没有可模仿的对象。

        回归：这一条曾经被写成「按老字段 reference_video_data 判必填」，而新前端发的是
        reference_videos[] —— 那样每一条动作模仿都会被拒。校验必须认合并后的列表。
        """
        for mode, ids in (("motion", [1]), ("duo", [1, 2])):
            with self.assertRaises(ValueError) as e:
                self.v(cine_mode=mode, avatar_ids=ids, reference_video_data="")
            self.assertIn("参考视频", str(e.exception))

    def test_the_new_frontend_field_is_accepted_on_its_own(self):
        """新前端只发 reference_videos[]（没有 reference_video_data）—— 必须能过。"""
        out = self.v(reference_video_data="", reference_videos=[REF])
        self.assertEqual(len(out["reference_video_files"]), 1)

    def test_open_mode_does_not(self):
        out = self.v(cine_mode="open", prompt="海边跳舞", reference_video_data="")
        self.assertIsNone(out.get("reference_video_file"))

    def test_an_overlong_reference_is_rejected_here_not_upstream(self):
        with patch.object(video, "_probe_video_duration", lambda f: 300.0):
            with self.assertRaises(ValueError) as e:
                self.v()
        self.assertIn("超过最长", str(e.exception))


class DurationTests(_Base):
    def test_motion_modes_only_offer_auto_10_and_15(self):
        for d in (10, 15):
            self.assertEqual(self.v(duration=d)["duration"], d)
        for bad in (5, 12, 4):
            with self.assertRaises(ValueError):
                self.v(duration=bad)

    def test_open_mode_keeps_the_full_range(self):
        for d in (4, 5, 12, 15):
            self.assertEqual(self.v(cine_mode="open", prompt="跳舞", duration=d)["duration"], d)

    def test_out_of_heygens_range_is_rejected_everywhere(self):
        for mode in ("motion", "open"):
            for bad in (3, 16):
                with self.assertRaises(ValueError):
                    self.v(cine_mode=mode, prompt="跳舞", duration=bad)


class UiTests(unittest.TestCase):
    def test_the_feature_is_renamed(self):
        self.assertIn('data-function="cinematic">电影化身</button>', HTML)
        # 只查用户看得见的地方 —— 注释里为了讲清楚「原来叫什么」还会提到旧名
        visible = [ln for ln in HTML.splitlines()
                   if "AI 剧情视频" in ln and not ln.lstrip().startswith(("//", "/*", "*", "<!--"))]
        self.assertEqual(visible, [], "界面上还留着旧名字：%s" % visible)

    def test_three_mode_tabs_exist(self):
        for mode in ("motion", "duo", "open"):
            self.assertIn('data-cine-mode="%s"' % mode, HTML)
        self.assertIn("applyCineMode('motion')", HTML, "默认落在第一个玩法")

    def test_the_prompt_box_is_hidden_for_the_fixed_prompt_modes(self):
        self.assertIn("$('cinePromptPanel').classList.toggle('hidden', cfg.fixed)", HTML)
        self.assertIn("if(!cfg.fixed) body.prompt=prompt", HTML,
                      "提示词写死的玩法不该发 prompt —— 发了后端也不看")

    def test_the_ratio_selector_is_hidden_for_the_fixed_prompt_modes(self):
        """动作模仿不给选比例：成片比例跟着参考视频走，让用户选只会把人挤变形。"""
        self.assertIn("$('cineRatioBox').classList.toggle('hidden', cfg.fixed)", HTML)
        self.assertIn("selectedCineRatio = r>1.15 ? '16:9' : (r<0.87 ? '9:16' : '1:1')", HTML)

    def test_switching_modes_trims_an_oversized_selection(self):
        """双人选了 2 个形象 → 切回单人，不裁掉就会带着 2 个提交，后端直接拒。"""
        self.assertIn("if(cineSelectedAvatarIds.length>cfg.avatars) "
                      "cineSelectedAvatarIds=cineSelectedAvatarIds.slice(0,cfg.avatars);", HTML)

    def test_switching_modes_resets_a_duration_that_no_longer_exists(self):
        """开放式选了 12 秒 → 切到动作模仿（只有 auto/10/15），不重置就会提交一个后端不认的值。"""
        block = HTML.split("function applyCineMode")[1].split("function ")[0]
        self.assertIn("selectedCineDuration='auto'", block)


class SubmitButtonTests(unittest.TestCase):
    """和 #603（生成按钮分槽 + 防重入锁）的接缝。"""

    def test_the_submit_lock_is_taken_after_every_gate(self):
        """先上锁再 return，锁就永远解不掉了 —— 按钮从此点不动。"""
        block = HTML.split("function submitCinematic")[1].split("fetch('/api/gen/cinematic'")[0]
        lock = block.index("setSubmitLock('cinematic',true)")
        for gate in ("需要正好选 ", "请上传一个参考视频", "请填写画面描述"):
            self.assertLess(block.index(gate), lock, "门槛 [%s] 必须在上锁之前" % gate)

    def test_cinematic_has_a_slot_cap(self):
        """后端 MAX_USER_ACTIVE_CINEMATIC=2。#603 统计了 counts.cinematic 却没人用 ——
        排到第 3 条时按钮还亮着，点下去只会吃一个 429。"""
        self.assertIn("var cineCapReached=counts.cinematic>=maxActiveCinematic;", HTML)
        self.assertIn("applyButtonState('cineGenerateBtn', !videoSubmitLocks.cinematic && !cineCapReached,", HTML)

    def test_the_cap_comes_from_the_backend(self):
        """写死在前端会跟 env 漂移（MAX_USER_ACTIVE_CINEMATIC 是 _env_positive_int）。"""
        self.assertIn('"max_user_active_cinematic": MAX_USER_ACTIVE_CINEMATIC', CORE)
        self.assertIn("var cineCap=Number(d.max_user_active_cinematic);", HTML)

    def test_the_task_label_follows_the_mode(self):
        self.assertIn("trackVideoJob(res.data.job_id,{status:'queued',label:cfg.label", HTML)


if __name__ == "__main__":
    unittest.main()
