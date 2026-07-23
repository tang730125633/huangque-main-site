# -*- coding: utf-8 -*-
"""剧情视频的结果必须带 status/mode/type —— 否则前端永远显示「生成中」。

## 线上现象

    jobs 表:          id=1920  status=done      ✅
    video_assets 表:  job=1920 status=running   phase=downloading_video   video_file=NULL

任务早就跑完了，但前端读的是 video_assets，所以一直转圈。

## 根因

record_video_asset 从 result 里取字段写进 video_assets：

    result.get("status") or "pending"     ← gen_cinematic 的返回值里【没有 status】
    result.get("mode")                    ← 也【没有 mode】

而 UPSERT 用的是 COALESCE(excluded.status, video_assets.status) —— COALESCE 只挡 NULL，
挡不住 "pending" 这个非空值。于是 running 被 pending 覆盖…… 不，更糟：
它把 "pending" 写了进去，但资产行早先被 update_video_asset_phase 置成了 running，
最终停在 running/downloading_video，永远不会变成 done。

口播、换装、果肉视频的返回值里都有 `"type": "video", "status": "done", "mode": ...`，
只有 gen_cinematic 漏了 —— 我加新 kind 时没照着它们的形状写。
"""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


class CinematicResultShapeTests(unittest.TestCase):
    def _run(self):
        with patch.object(video, "get_video_avatar", return_value={"provider_avatar_id": "look1", "name": "我"}), \
             patch.object(video, "update_video_asset_phase"), \
             patch.object(video, "heygen_slot"), \
             patch.object(video, "_heygen_retry_429", side_effect=lambda fn, what="": fn()), \
             patch.object(video, "_heygen_create_cinematic_video", return_value="vid1"), \
             patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://x/y.mp4", "duration": 10}), \
             patch.object(video, "_download_video_file_direct", return_value="video/out.mp4"), \
             patch.object(video, "_extract_first_frame_cover", return_value=None), \
             patch.object(video, "public_url", return_value="https://cos.example/video/out.mp4") as publish, \
             patch.object(video, "_file_url", side_effect=lambda v: "/api/gen/file/" + str(v)):
            result = video.gen_cinematic({
                "_username": "kongli", "_job_id": 1, "avatar_ids": [1], "prompt": "海边跳舞",
                "resolution": "720p", "ratio": "9:16", "duration": 10,
            })
            publish.assert_called_once_with("video/out.mp4", "video/mp4", private=True)
            return result

    def test_result_carries_the_terminal_status(self):
        """漏了 status，record_video_asset 会写成 "pending"，资产行永远停在非终态 ——
        用户看到的就是「一直显示生成中」，哪怕 jobs 表早就 done 了。"""
        self.assertEqual(self._run()["status"], "done")

    def test_result_carries_mode_and_type(self):
        out = self._run()
        self.assertEqual(out["mode"], "cinematic", "没有 mode，资产卡片不知道自己是什么类型")
        self.assertEqual(out["type"], "video")

    def test_result_carries_the_video_file(self):
        # video_file 为空 → 资产卡片没有可播的片子
        self.assertEqual(self._run()["video_file"], "video/out.mp4")

    def test_result_uses_the_cos_video_url(self):
        self.assertEqual(self._run()["video_url"], "https://cos.example/video/out.mp4")

    def test_result_carries_text_for_the_asset_card(self):
        # video_assets 的文案列叫 text（不是 prompt），前端卡片读的也是 text
        self.assertEqual(self._run()["text"], "海边跳舞")

    def test_it_matches_the_shape_the_other_video_kinds_use(self):
        """口播/换装/果肉都返回 type+status+mode。新 kind 必须照着同一个形状写，
        否则就是静默地不进资产表（或进了但停在非终态）。"""
        out = self._run()
        for key in ("type", "status", "mode", "video_file", "video_url"):
            self.assertIn(key, out, "缺 %s —— 和其它视频 kind 的返回形状不一致" % key)


if __name__ == "__main__":
    unittest.main()
