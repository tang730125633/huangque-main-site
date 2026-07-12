# -*- coding: utf-8 -*-
"""AI 剧情视频 / 动作模仿去线路化 —— 前端。

前端和后端必须对齐，否则就是「后端支持但用户点不到」或「用户点了后端不认」。
这些断言全都在守这个对齐，而不是守 UI 长什么样。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class LegacyMotionRetiredTests(unittest.TestCase):
    """老版动作模仿(mode=motion)已下线，视频功能收敛到电影化身。"""

    def test_motion_tab_and_panel_are_gone(self):
        self.assertNotIn('data-function="motion"', HTML)
        self.assertNotIn('id="motionPanel"', HTML)
        self.assertNotIn("selectedMotionLine", HTML)


class CinematicPanelTests(unittest.TestCase):
    def test_panel_and_tab_exist(self):
        self.assertIn('data-function="cinematic"', HTML)
        self.assertIn('id="cinematicPanel"', HTML)
        self.assertIn("$('cinematicPanel').classList.toggle('hidden',videoFunction!=='cinematic')",
                      HTML.replace('if($(\'cinematicPanel\')) ', ''))

    def test_avatar_cap_is_per_mode_and_never_exceeds_three(self):
        # HeyGen 硬上限：avatar_id 数组最多 3 个 look。三个玩法各自的数量在 CINE_MODES 里。
        self.assertIn("var CINE_MAX_AVATARS=3", HTML)
        self.assertIn("cap=cineCfg().avatars", HTML)
        self.assertIn("toast(cineCfg().label+'最多选 '+cap+' 个形象')", HTML)

    def test_both_resolutions_are_selectable(self):
        self.assertIn('data-cine-resolution="720p"', HTML)
        self.assertIn('data-cine-resolution="1080p"', HTML)
        # 1080p 不能是 disabled —— 这正是新功能相对动作模仿的卖点
        seg = HTML.split("data-cine-resolution=", 1)[1].split("</div>", 1)[0]
        self.assertNotIn("disabled", seg)

    def test_duration_options_are_within_heygen_range(self):
        # HeyGen cinematic: 4~15 秒
        got = [int(x) for x in re.findall(r'data-cine-duration="(\d+)"', HTML)]
        self.assertTrue(got)
        for d in got:
            self.assertTrue(4 <= d <= 15, "%d 秒超出 HeyGen 的 4~15 秒范围" % d)

    def test_prompt_templates_are_offered_and_editable(self):
        self.assertIn("var CINE_TEMPLATES=[", HTML)
        self.assertIn('id="cinePromptTemplates"', HTML)
        self.assertIn('<textarea id="cinePrompt"', HTML, "模板之外必须能自己写")

    def test_prompt_is_capped_at_two_thousand(self):
        # 与后端 CINEMATIC_PROMPT_MAX 对齐，否则用户写超了才在提交时被拒
        self.assertIn("this.value.slice(0,2000)", HTML)

    def test_reference_material_requirement_follows_the_mode(self):
        """动作模仿：必传、且正好一个视频，不收参考图。开放式：视频+图片都选填。"""
        self.assertIn("cfg.needRef ? '参考视频（必传）' : '参考素材（选填）'", HTML)
        self.assertIn("都不给就只按描述生成", HTML)
        self.assertIn("if(cfg.needRef && cineRefVideos.length!==1){ toast('请上传一个参考视频'); return; }", HTML)
        # 只有真传了才发字段 —— 空数组也不该发；参考图只在开放式发
        self.assertIn("if(cineRefVideos.length) body.reference_videos=", HTML)
        self.assertIn("if(!cfg.fixed && cineRefImages.length) body.reference_images=", HTML)

    def test_submits_to_the_cinematic_endpoint_with_an_avatar_id_array(self):
        self.assertIn("fetch('/api/gen/cinematic'", HTML)
        self.assertIn("avatar_ids:cineSelectedAvatarIds.map(", HTML)
        self.assertIn("cine_mode:cineMode", HTML)


class CreateAvatarTests(unittest.TestCase):
    def test_create_avatar_entry_shows_the_price(self):
        panel = HTML.split('id="cinematicPanel"')[1].split('id="tryonPanel"')[0]
        self.assertIn("创建新形象（5 点）", panel, "要让用户知道这一步花多少点")

    def test_create_avatar_hits_its_own_endpoint(self):
        self.assertIn("fetch('/api/gen/avatar'", HTML)

    def test_points_are_refreshed_on_both_success_and_failure(self):
        """失败会退点 —— 不刷新点数，用户会以为白扣了。"""
        block = HTML.split("function pollCreateAvatar")[1].split("function renderCineTemplates")[0]
        self.assertEqual(block.count("HQ.refreshPoints()"), 2)

    def test_stale_avatar_ids_are_dropped_from_the_selection(self):
        # 形象被删了还留在选中里 → 提交必被后端拒（get_video_avatar 找不到）
        self.assertIn("cineSelectedAvatarIds=cineSelectedAvatarIds.filter(", HTML)


if __name__ == "__main__":
    unittest.main()
