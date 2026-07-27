# -*- coding: utf-8 -*-
"""视频类任务判失败时，video_asset 必须同步成失败终态——否则前端历史卡片一直「生成中」。

## 线上现象（fang 的电影化身 #2137）
    jobs:         id=2137 status=error   error="The read operation timed out"
    video_assets: job=2137 status=running phase=uploading_reference_asset   ← 卡住
前端历史/资产页读的是 video_assets，所以一直转圈；哪怕 /api/gen/job 端点靠 #608 已返 failed。

## 根因
run_job 的失败分支原来用 `record_video_asset(dict(payload) + status=failed)` 同步。record_video_asset
是 INSERT，而 video_assets.mode 有 NOT NULL 约束——**cinematic 的 payload 只有 cine_mode、
xiaole_video 只有 channel、tryon 无 mode** → mode=None → IntegrityError，被上层 `except: pass`
吞掉 → video_asset 永远停在 running。成功路径没事(gen_cinematic 返回 mode='cinematic')，只有失败路径踩。
影响一次性积压了 389 条卡住的失败任务。

## 修
抽 `_mark_video_asset_failed`，用 `update_video_asset_phase`(UPDATE 现有行，不碰 NOT NULL)。
run_job 失败分支 / reaper / reclaim_orphaned_running 三处统一调用。
"""
import pathlib
import unittest

CORE = (pathlib.Path(__file__).resolve().parents[1] / "server/content_domains/core.py").read_text(encoding="utf-8")
STARTUP_RECOVERY = (
    pathlib.Path(__file__).resolve().parents[1]
    / "server/content_domains/startup_recovery.py"
).read_text(encoding="utf-8")


class FailedAssetSyncTests(unittest.TestCase):
    def test_helper_uses_update_not_insert(self):
        """_mark_video_asset_failed 必须用 update_video_asset_phase(UPDATE)——
        record_video_asset(INSERT) 会因 mode NOT NULL 对 cinematic/xiaole 抛 IntegrityError。"""
        block = CORE[CORE.index("def _mark_video_asset_failed"):]
        block = block[:block.index("def run_job")]
        self.assertIn("update_video_asset_phase(job_id, \"failed\", status=\"failed\"", block)
        self.assertNotIn("video_domain.record_video_asset", block)   # 实际调用不能是 record_video_asset
        # 只对视频类 kind 生效
        self.assertIn('kind not in {"video", "tryon", "xiaole_video", "sora_video", "cinematic", "script_to_video"}', block)

    def test_run_job_error_branch_syncs_via_helper(self):
        """失败分支不再 record_video_asset(dict(payload))——那正是 mode=None 抛 NOT NULL 的地方。"""
        run_job = CORE[CORE.index("def run_job("):CORE.index("# ============ 超时清道夫")]
        err = run_job[run_job.index("claimed = _fail_job_and_schedule_refund("):]
        err = err[:err.index("finally:")]
        self.assertIn("_mark_video_asset_failed(job_id, kind, e)", err)
        self.assertNotIn("record_video_asset", err)
        self.assertNotIn("failed = dict(payload)", err)

    def test_reaper_syncs_video_asset_on_kill(self):
        """reaper 判超时杀任务时也要同步 video_asset，否则超时任务一样卡「生成中」。"""
        reaper = CORE[CORE.index("def reaper"):]
        reaper = reaper[:reaper.index("def reclaim_orphaned_running")]
        self.assertIn('_mark_video_asset_failed(r["id"], r["kind"]', reaper)

    def test_reclaim_orphan_syncs_video_asset(self):
        """启动回收重启孤儿时也要同步 video_asset。SELECT 要带上 kind。"""
        recl = STARTUP_RECOVERY[STARTUP_RECOVERY.index("def reclaim_orphaned_running"):]
        self.assertIn("SELECT id, username, cost, kind FROM jobs", recl)
        self.assertIn('mark_video_asset_failed(row["id"], row["kind"]', recl)


if __name__ == "__main__":
    unittest.main()
