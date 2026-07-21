# -*- coding: utf-8 -*-
"""生成记录：历史不该被在飞任务顶掉；已用时间不该被刷新清零。

线上两个问题，根因都在「谁有权决定 #outputHistory 里显示什么」：

1. **一开始生成，历史记录就全没了。**
   每条提交路径都是 `renderVideoHistory([currentVideoDraft])` —— 把整个列表【替换】成
   一条本地草稿。草稿是还没落库的东西，它没资格覆盖服务端的历史。
   → 拆成两层：videoHistoryItems（服务端）+ videoDrafts（本地在飞），渲染时合并。

2. **刷新页面后，已用时间从 0 重新数。**
   `startVidTick(){ _vidStart=Date.now(); }` —— 计时锚点是「本次开始轮询的时刻」。
   一条已经跑了 6 分钟的任务，刷新后显示「已用 3 秒」。
   → 锚在任务的 created_at 上。它由服务端给（/api/gen/job/<id> 一直在返回，只是没人读），
     跨刷新、跨设备都是同一个值。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
JOBS_STORE = (ROOT / "server/content_domains/jobs_store.py").read_text(encoding="utf-8")  # #608 把 public_dict 挪到这


class HistoryIsNotClobberedByTheDraftTests(unittest.TestCase):
    def test_history_and_drafts_are_separate_layers(self):
        self.assertIn("var videoHistoryItems=[]", HTML)
        self.assertIn("var videoDrafts=[]", HTML)
        self.assertIn("function renderVideoOutput()", HTML)

    def test_no_submit_path_replaces_the_whole_list_with_a_draft(self):
        """这是根因本身：renderVideoHistory([草稿]) 会把历史整个换掉。"""
        self.assertNotIn("renderVideoHistory([currentVideoDraft])", HTML)
        self.assertNotIn("renderVideoHistory(drafts)", HTML)

    def test_render_video_history_is_only_driven_by_the_merger(self):
        """renderVideoHistory 降级成纯渲染器 —— 只能由 renderVideoOutput 调，
        任何别的地方直接调它，就又能绕过历史层把列表整个换掉。"""
        calls = re.findall(r"[^n]renderVideoHistory\((.*?)\);", HTML)
        self.assertEqual(calls, ["videoDrafts.concat(rest)"])

    def test_the_merge_dedupes_on_job_id(self):
        """草稿一拿到 job_id，服务端历史里也会有同一条。不去重就会显示两遍。"""
        block = HTML.split("function renderVideoOutput")[1].split("function renderVideoDrafts")[0]
        self.assertIn("drafted[String(d.job_id)]=1", block)
        self.assertIn("videoHistoryItems.filter", block)
        self.assertIn("videoDrafts.concat(rest)", block)

    def test_loading_the_history_does_not_wipe_an_in_flight_draft(self):
        """「正在读取生成记录...」是整块覆盖的 —— 有在飞任务时糊上去，
        用户会眼睁睁看着自己刚提交的卡片消失一下。"""
        block = HTML.split("function loadVideoHistory")[1].split("function ")[0]
        self.assertIn("if(!videoDrafts.length) renderVideoHistoryState('正在读取生成记录...'", block)
        self.assertIn("return requestVideoHistoryItems()", block)
        self.assertIn("videoHistoryItems=items; renderVideoOutput();", block)
        # 历史拉失败时，也不能把在飞的那条一起弄没
        self.assertIn("if(videoDrafts.length) renderVideoOutput();", block)

    def test_a_finished_job_lands_in_the_history_layer(self):
        block = HTML.split("if(d.status==='done' && d.result)")[1].split("if(d.status==='error'")[0]
        self.assertIn("videoHistoryItems.unshift(", block)
        self.assertIn("renderVideoDrafts([])", block, "成品进了历史，草稿就该收掉")


class ElapsedTimeSurvivesARefreshTests(unittest.TestCase):
    def test_the_timer_is_anchored_on_created_at_not_on_now(self):
        self.assertIn("function _vidAnchor()", HTML)
        self.assertIn("var d=videoDrafts[0], t=d && Number(d.created_at);", HTML)
        self.assertIn("_fmtUsed((Date.now()-_vidAnchor())/1000)", HTML)

    def test_now_is_only_a_fallback(self):
        """提交请求还没回来时草稿上没有 created_at —— 这时才回落到 Date.now()。"""
        block = HTML.split("function startVidTick")[1].split("function stopVidTick")[0]
        self.assertIn("_vidStart=Date.now();", block)
        self.assertNotIn("_fmtUsed((Date.now()-_vidStart)", block, "别再拿开始轮询的时刻当锚点")

    def test_the_draft_takes_created_at_from_the_server(self):
        """本地 tracker 里的时间可能早就丢了（甚至是恢复任务时 Date.now() 现编的）。
        服务端的 created_at 才是权威。"""
        block = HTML.split("function updateJobProgress")[1].split("function ")[0]
        self.assertIn("if(job && Number(job.created_at)>0) currentVideoDraft.created_at=Number(job.created_at);",
                      block)

    def test_the_backend_actually_returns_created_at(self):
        """前端要读的这个字段，后端得真的给。#608 把 _job_public_dict 挪成 jobs_store.public_dict
        并改成 dict 推导投影一组列 —— created_at 必须在那组列里。"""
        block = JOBS_STORE.split("def public_dict")[1].split("\ndef ")[0]
        self.assertIn('"created_at"', block)


class AvatarGridScrollsAfterTwoRowsTests(unittest.TestCase):
    """形象越建越多，选择框就越拉越长 —— 限制成最多两排，多的滚动。"""

    def test_the_grid_scrolls(self):
        m = re.search(r"\.avatar-grid\{([^}]*)\}", HTML)
        self.assertIn("overflow-y:auto", m.group(1))

    def test_the_row_height_is_measured_not_hardcoded(self):
        """卡片高度 = 16:9 的展示框（宽度随网格列宽走）+ 文字区，窗口一缩放它就变了。
        写死 px 会切在半张卡上。"""
        block = HTML.split("function limitAvatarGridRows")[1].split("function limitAllAvatarGrids")[0]
        self.assertIn("card.getBoundingClientRect().height", block)
        self.assertIn("var AVATAR_GRID_ROWS=2", HTML)
        self.assertIn("h*AVATAR_GRID_ROWS + gap*(AVATAR_GRID_ROWS-1)", block)

    def test_an_empty_grid_is_not_clipped(self):
        block = HTML.split("function limitAvatarGridRows")[1].split("function limitAllAvatarGrids")[0]
        self.assertIn("if(!card){ grid.style.maxHeight=''; return; }", block)

    def test_it_is_recomputed_when_the_layout_can_change(self):
        """两处会让行高失效：窗口缩放（列宽变），以及面板从 hidden 变可见
        （hidden 时 getBoundingClientRect 全是 0，量不到）。"""
        self.assertIn("_avatarGridResize=setTimeout(limitAllAvatarGrids,120)", HTML)
        after_panels = HTML.split("$('omniPanel').classList.toggle('hidden'")[1][:400]
        self.assertIn("limitAllAvatarGrids();", after_panels)

    def test_both_avatar_grids_are_covered(self):
        self.assertIn("['motionAvatarGrid','cineAvatarGrid'].forEach", HTML)
        self.assertEqual(HTML.count("limitAvatarGridRows(grid);"), 2, "两个网格的 render 各调一次")


if __name__ == "__main__":
    unittest.main()
