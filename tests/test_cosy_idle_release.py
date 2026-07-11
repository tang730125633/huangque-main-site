# -*- coding: utf-8 -*-
"""CosyVoice 空闲音色自动释放：一个月内既没配音也没复刻的个人音色 → 删阿里 + 清本地 + 腾槽位。

守的不变量：
1. 只回收 CosyVoice 复刻音色(provider_voice 以复刻模型名打头)——公共预置/豆包音色绝不动
2. 配音或复刻都刷新 last_used_at；新鲜音色不被回收
3. 阿里删除失败(可能已被自动清理)不阻断本地清理(幂等)
4. 释放后腾空该用户槽位，可再复刻
5. 每日守卫：24h 内不重复扫库
"""
import importlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
audio = importlib.import_module("content_domains.audio")
cosyvoice = importlib.import_module("content_domains.cosyvoice")

CLONE = cosyvoice.CLONE_MODEL
NOW = 1_700_000_000
DAY = 86400


class _TempAudioDB:
    """临时 audio_assets.db，含 audio_voices + audio_voice_slots 最小表。"""

    def __init__(self):
        self.path = tempfile.mktemp(suffix=".db")
        c = sqlite3.connect(self.path)
        c.execute("""CREATE TABLE audio_voices(id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT, scope TEXT, voice_key TEXT, display_name TEXT, provider_voice TEXT,
            slot_id TEXT, created_at INTEGER, updated_at INTEGER, last_used_at INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE audio_voice_slots(id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT, slot_id TEXT, status TEXT, voice_id INTEGER,
            clone_started_at INTEGER, clone_upload_at INTEGER, updated_at INTEGER)""")
        c.commit(); c.close()

    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def add_voice(self, **kw):
        d = {"username": "u", "scope": "personal", "voice_key": "vip_x", "display_name": "n",
             "provider_voice": CLONE + "-bailian-abc", "slot_id": "slot1",
             "created_at": NOW, "updated_at": NOW, "last_used_at": NOW}
        d.update(kw)
        c = self.conn()
        c.execute("""INSERT INTO audio_voices(username,scope,voice_key,display_name,provider_voice,slot_id,created_at,updated_at,last_used_at)
            VALUES(:username,:scope,:voice_key,:display_name,:provider_voice,:slot_id,:created_at,:updated_at,:last_used_at)""", d)
        c.execute("INSERT INTO audio_voice_slots(username,slot_id,status,voice_id,updated_at) VALUES(?,?,?,?,?)",
                  (d["username"], d["slot_id"], "ready", 1, NOW))
        c.commit(); c.close()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.db = _TempAudioDB()
        self.p_adb = patch.object(audio, "adb", self.db.conn); self.p_adb.start()
        self.p_key = patch.object(cosyvoice, "DASHSCOPE_API_KEY", "k"); self.p_key.start()
        self.deleted = []
        self.p_del = patch.object(cosyvoice, "delete_voice", lambda v: self.deleted.append(v)); self.p_del.start()
        self.p_now = patch.object(audio.time, "time", lambda: NOW); self.p_now.start()

    def tearDown(self):
        for p in (self.p_adb, self.p_key, self.p_del, self.p_now):
            p.stop()

    def _voices(self):
        c = self.db.conn(); rows = c.execute("SELECT * FROM audio_voices").fetchall(); c.close(); return rows

    def _slot_status(self, slot_id="slot1"):
        c = self.db.conn(); r = c.execute("SELECT status, voice_id FROM audio_voice_slots WHERE slot_id=?", (slot_id,)).fetchone(); c.close(); return r

    def test_idle_clone_voice_released(self):
        self.db.add_voice(last_used_at=NOW - 40 * DAY)   # 40 天没用
        n = audio.release_idle_cosy_voices(days=30)
        self.assertEqual(n, 1)
        self.assertEqual(self.deleted, [CLONE + "-bailian-abc"])   # 阿里侧删了
        self.assertEqual(len(self._voices()), 0)                    # 本地行清了
        st = self._slot_status()
        self.assertEqual(st["status"], "active")                    # 槽位腾空可再复刻
        self.assertIsNone(st["voice_id"])

    def test_fresh_voice_kept(self):
        self.db.add_voice(last_used_at=NOW - 5 * DAY)     # 5 天前用过
        self.assertEqual(audio.release_idle_cosy_voices(days=30), 0)
        self.assertEqual(self.deleted, [])
        self.assertEqual(len(self._voices()), 1)

    def test_public_preset_never_touched(self):
        self.db.add_voice(scope="public", provider_voice="S_d21F8OR62", voice_key="S_d21F8OR62", last_used_at=NOW - 999 * DAY)
        self.assertEqual(audio.release_idle_cosy_voices(days=30), 0)
        self.assertEqual(self.deleted, [])

    def test_old_doubao_voice_never_touched(self):
        self.db.add_voice(provider_voice="S_oldspeaker", last_used_at=NOW - 999 * DAY)
        self.assertEqual(audio.release_idle_cosy_voices(days=30), 0)
        self.assertEqual(self.deleted, [])

    def test_missing_last_used_falls_back_to_updated_at(self):
        """老数据 last_used_at=0 → 用 updated_at 判定，不因缺时间戳误删新音色。"""
        self.db.add_voice(last_used_at=0, updated_at=NOW - 2 * DAY)
        self.assertEqual(audio.release_idle_cosy_voices(days=30), 0)   # updated_at 才 2 天，保留
        self.db.add_voice(voice_key="vip_y", last_used_at=0, updated_at=NOW - 50 * DAY)
        self.assertEqual(audio.release_idle_cosy_voices(days=30), 1)   # updated_at 50 天，回收

    def test_ali_delete_failure_still_cleans_local(self):
        self.db.add_voice(last_used_at=NOW - 40 * DAY)
        with patch.object(cosyvoice, "delete_voice", side_effect=RuntimeError("已被自动清理")):
            n = audio.release_idle_cosy_voices(days=30)
        self.assertEqual(n, 1)                       # 阿里删失败不阻断
        self.assertEqual(len(self._voices()), 0)     # 本地仍清

    def test_disabled_is_noop(self):
        self.db.add_voice(last_used_at=NOW - 999 * DAY)
        with patch.object(cosyvoice, "DASHSCOPE_API_KEY", ""):
            self.assertEqual(audio.release_idle_cosy_voices(), 0)
        self.assertEqual(len(self._voices()), 1)

    def test_default_release_period_is_90_days(self):
        self.assertEqual(audio.COSY_IDLE_RELEASE_DAYS, 90)

    def test_warn_before_release_sends_notification(self):
        """回收前 warn_days 天推提醒（同一音色只推一次），且不删音色。"""
        self.db.add_voice(last_used_at=NOW - 85 * DAY)   # 90-7=83 天阈值，85>83 → 该提醒但未到 90 天回收
        pushed = []
        import content_domains.notifications as _notif
        with patch.object(_notif, "push", lambda *a, **k: pushed.append(k.get("dedup_key")) or True):
            n_warn = audio.warn_idle_cosy_voices(days=90, warn_days=7)
            n_rel = audio.release_idle_cosy_voices(days=90)
        self.assertEqual(n_warn, 1)                 # 提醒了
        self.assertEqual(n_rel, 0)                  # 还没到 90 天，不删
        self.assertEqual(len(self._voices()), 1)    # 音色还在

    def test_daily_guard_runs_once_per_day(self):
        audio._last_idle_sweep[0] = 0
        calls = []
        with patch.object(audio, "release_idle_cosy_voices", lambda *a, **k: calls.append(1) or 0):
            audio.maybe_release_idle_voices()               # 首次跑
            audio.maybe_release_idle_voices()               # 同一秒再调 → 跳过
        self.assertEqual(len(calls), 1)


class TouchTests(unittest.TestCase):
    def setUp(self):
        self.db = _TempAudioDB()
        self.p = patch.object(audio, "adb", self.db.conn); self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_synth_touch_updates_personal_voice(self):
        self.db.add_voice(last_used_at=NOW - 40 * DAY)
        with patch.object(audio.time, "time", lambda: NOW):
            audio._touch_voice_used("u", "vip_x")
        c = self.db.conn(); r = c.execute("SELECT last_used_at FROM audio_voices").fetchone(); c.close()
        self.assertEqual(r["last_used_at"], NOW)

    def test_touch_ignores_empty_voice_key(self):
        audio._touch_voice_used("u", "")   # 不抛


if __name__ == "__main__":
    unittest.main()
