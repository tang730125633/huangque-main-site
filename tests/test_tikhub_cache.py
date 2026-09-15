# TikHub 采集缓存（M3F）：默认 SQLite 行为逐项不变 + Redis TTL 模式。
#
# 本机/CI 无 Redis 时，Redis 部分整体跳过（与 M3A 的 test_flags_store.py 同一纪律）：
#   HQ_REDIS_URL=redis://127.0.0.1:6379/0 python3 -m unittest discover -s tests -p 'test_tikhub_cache.py' -v
# Redis 用例一律使用独立测试前缀（HQ_TIKHUB_CACHE_PREFIX）并在 tearDown 清理，
# 绝不触碰生产键（生产前缀默认 hq:tikhub:cache:）。
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import tikhub  # noqa: E402
from content_domains import tikhub_cache_redis  # noqa: E402

REDIS_URL = os.environ.get("HQ_REDIS_URL")
TEST_PREFIX = "hq:test:tikhub-cache-m3f:"

SEARCH_RESULT = {"items": [{"id": "1", "title": "t"}], "cursor": 0, "has_more": False}
DETAIL_RESULT = {"platform": "douyin", "id": "1", "title": "d"}
COMMENTS_RESULT = {"items": [{"text": "c"}], "cursor": 0, "has_more": False}


class CacheTestCase(unittest.TestCase):
    """公共夹具：把 SQLite 缓存库指向临时文件，默认模式保持 sqlite。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-tikhub-cache-")
        self.db_path = os.path.join(self.tmp, "tikhub_cache.db")
        self._saved_db = tikhub._CACHE_DB
        tikhub._CACHE_DB = self.db_path
        self._saved_mode = os.environ.pop("HQ_TIKHUB_CACHE", None)

    def tearDown(self):
        tikhub._CACHE_DB = self._saved_db
        if self._saved_mode is not None:
            os.environ["HQ_TIKHUB_CACHE"] = self._saved_mode
        else:
            os.environ.pop("HQ_TIKHUB_CACHE", None)

    def rows(self):
        """直接读源库：断言 k/v/exp 三列口径。"""
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT k, v, exp FROM cache").fetchall()


class SqliteModeTest(CacheTestCase):
    """默认 sqlite 模式：缓存语义与迁移前逐项一致。"""

    def test_default_mode_is_sqlite(self):
        self.assertEqual(tikhub_cache_redis.mode(), "sqlite")
        self.assertFalse(tikhub_cache_redis.enabled())

    def test_set_get_roundtrip_and_exp_column(self):
        now = int(time.time())
        tikhub._cache_set("det:douyin:7351", DETAIL_RESULT, 3600)
        self.assertEqual(tikhub._cache_get("det:douyin:7351"), DETAIL_RESULT)
        rows = {k: (v, exp) for k, v, exp in self.rows()}
        raw, exp = rows["det:douyin:7351"]
        self.assertEqual(json.loads(raw), DETAIL_RESULT)
        self.assertEqual(raw, json.dumps(DETAIL_RESULT, ensure_ascii=False))  # 编码口径不变
        self.assertTrue(now + 3599 <= exp <= now + 3601)

    def test_expired_entry_is_miss(self):
        tikhub._cache_set("cmt:douyin:1:0:20", COMMENTS_RESULT, 1)
        time.sleep(1.2)
        self.assertIsNone(tikhub._cache_get("cmt:douyin:1:0:20"))  # 过期即未命中

    def test_stale_row_is_swept_on_write(self):
        # 源里的过期行就是这么留下的：直接塞一条 exp 早已过去的行，再写一次即被顺手清掉。
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT, exp INTEGER)")
            conn.execute("INSERT OR REPLACE INTO cache(k, v, exp) VALUES(?,?,?)",
                         ("cmt:douyin:1:0:20", json.dumps(COMMENTS_RESULT),
                          int(time.time()) - 600))
            conn.commit()
        self.assertIsNone(tikhub._cache_get("cmt:douyin:1:0:20"))
        tikhub._cache_set("srch:douyin:kw:1:0", SEARCH_RESULT, 1800)
        self.assertEqual([r[0] for r in self.rows()],
                         ["srch:douyin:kw:1:0"])  # 写入时顺手清掉过期行

    def test_zero_ttl_entry_is_never_readable(self):
        tikhub._cache_set("det:xhs:abc", DETAIL_RESULT, 3600)
        tikhub._cache_set("det:xhs:abc", DETAIL_RESULT, 0)  # 源：覆盖成一条 exp<=now 的行
        self.assertIsNone(tikhub._cache_get("det:xhs:abc"))

    def test_corrupt_value_is_miss_without_raising(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT, exp INTEGER)")
            conn.execute("INSERT OR REPLACE INTO cache(k, v, exp) VALUES(?,?,?)",
                         ("det:douyin:9", "{not json", int(time.time()) + 600))
            conn.commit()
        self.assertIsNone(tikhub._cache_get("det:douyin:9"))

    def test_cache_failure_never_raises(self):
        tikhub._CACHE_DB = self.tmp  # 目录当库文件：sqlite 必然失败
        self.assertIsNone(tikhub._cache_get("det:douyin:1"))
        self.assertIsNone(tikhub._cache_set("det:douyin:1", DETAIL_RESULT, 60))

    def test_public_entrypoints_cache_and_fresh_bypass(self):
        with mock.patch.object(tikhub, "_search", return_value=SEARCH_RESULT) as upstream:
            self.assertEqual(tikhub.search("douyin", "美甲", page=1, video_only=False), SEARCH_RESULT)
            self.assertEqual(tikhub.search("douyin", "美甲", page=1, video_only=False), SEARCH_RESULT)
            self.assertEqual(upstream.call_count, 1)                       # 第二次命中缓存
            tikhub.search("douyin", "美甲", page=1, video_only=False, fresh=True)
            self.assertEqual(upstream.call_count, 2)                       # fresh 绕过缓存
            self.assertIsNotNone(tikhub._cache_get("srch:douyin:美甲:1:0"))

    def test_detail_and_comments_cache_and_channels_detail_is_never_cached(self):
        with mock.patch.object(tikhub, "_detail", return_value=DETAIL_RESULT) as upstream:
            tikhub.detail("douyin", "7351")
            tikhub.detail("douyin", "7351")
            self.assertEqual(upstream.call_count, 1)
            self.assertIsNotNone(tikhub._cache_get("det:douyin:7351"))
            upstream.reset_mock()
            tikhub.detail("channels", "sph-1")     # 视频号 detail 读写都不缓存（play_url 有时效）
            tikhub.detail("channels", "sph-1")
            self.assertEqual(upstream.call_count, 2)
            self.assertIsNone(tikhub._cache_get("det:channels:sph-1"))
        with mock.patch.object(tikhub, "_comments", return_value=COMMENTS_RESULT) as upstream:
            tikhub.comments("douyin", "7351", cursor=0, count=20)
            tikhub.comments("douyin", "7351", cursor=0, count=20)
            self.assertEqual(upstream.call_count, 1)
            self.assertIsNotNone(tikhub._cache_get("cmt:douyin:7351:0:20"))


class IllegalModeTest(CacheTestCase):
    """模式取值非法 → 立刻抛错（配置错误不得静默）。"""

    def test_illegal_mode_value_raises(self):
        os.environ["HQ_TIKHUB_CACHE"] = "postgres"
        with self.assertRaises(RuntimeError):
            tikhub_cache_redis.mode()
        with self.assertRaises(RuntimeError):
            tikhub._cache_get("det:douyin:1")
        with self.assertRaises(RuntimeError):
            tikhub._cache_set("det:douyin:1", DETAIL_RESULT, 60)

    def test_mode_is_case_insensitive(self):
        os.environ["HQ_TIKHUB_CACHE"] = " SQLITE "
        self.assertEqual(tikhub_cache_redis.mode(), "sqlite")


@unittest.skipUnless(REDIS_URL, "HQ_REDIS_URL 未配置：跳过 Redis 模式测试")
class RedisModeTest(CacheTestCase):
    """Redis 模式：set/get/del/过期语义 + 与 SQLite 路径的分发隔离。"""

    def setUp(self):
        super().setUp()
        self._saved_url = os.environ.get("HQ_REDIS_URL")
        self._saved_prefix = os.environ.get("HQ_TIKHUB_CACHE_PREFIX")
        os.environ["HQ_REDIS_URL"] = REDIS_URL
        os.environ["HQ_TIKHUB_CACHE"] = "redis"
        os.environ["HQ_TIKHUB_CACHE_PREFIX"] = TEST_PREFIX
        tikhub_cache_redis.close()

    def tearDown(self):
        try:
            conn = tikhub_cache_redis.client()
            for key in conn.scan_iter(match=TEST_PREFIX + "*", count=200):
                conn.delete(key)
            self.assertEqual(list(conn.scan_iter(match=TEST_PREFIX + "*")), [])
        finally:
            tikhub_cache_redis.close()
            if self._saved_url is not None:
                os.environ["HQ_REDIS_URL"] = self._saved_url
            else:
                os.environ.pop("HQ_REDIS_URL", None)
            if self._saved_prefix is not None:
                os.environ["HQ_TIKHUB_CACHE_PREFIX"] = self._saved_prefix
            else:
                os.environ.pop("HQ_TIKHUB_CACHE_PREFIX", None)
            super().tearDown()

    def test_key_namespace_and_roundtrip(self):
        self.assertTrue(tikhub_cache_redis.enabled())
        self.assertEqual(tikhub_cache_redis.key_for("det:douyin:1"),
                         TEST_PREFIX + "det:douyin:1")
        tikhub._cache_set("det:douyin:1", DETAIL_RESULT, 3600)
        self.assertEqual(tikhub._cache_get("det:douyin:1"), DETAIL_RESULT)
        conn = tikhub_cache_redis.client()
        self.assertEqual(json.loads(conn.get(TEST_PREFIX + "det:douyin:1")), DETAIL_RESULT)
        self.assertTrue(1 < conn.ttl(TEST_PREFIX + "det:douyin:1") <= 3600)  # TTL 落在 Redis 侧

    def test_ttl_expiry(self):
        tikhub._cache_set("srch:douyin:kw:1:1", SEARCH_RESULT, 1)
        self.assertEqual(tikhub._cache_get("srch:douyin:kw:1:1"), SEARCH_RESULT)
        time.sleep(1.2)
        self.assertIsNone(tikhub._cache_get("srch:douyin:kw:1:1"))
        self.assertFalse(tikhub_cache_redis.client().exists(TEST_PREFIX + "srch:douyin:kw:1:1"))

    def test_delete(self):
        tikhub._cache_set("cmt:douyin:1:0:20", COMMENTS_RESULT, 600)
        tikhub_cache_redis.delete("cmt:douyin:1:0:20")
        self.assertIsNone(tikhub._cache_get("cmt:douyin:1:0:20"))

    def test_non_positive_ttl_is_never_cached_and_clears_old_value(self):
        tikhub._cache_set("det:xhs:abc", DETAIL_RESULT, 600)
        tikhub._cache_set("det:xhs:abc", DETAIL_RESULT, 0)
        self.assertIsNone(tikhub._cache_get("det:xhs:abc"))
        tikhub._cache_set("det:xhs:def", DETAIL_RESULT, -5)
        self.assertIsNone(tikhub._cache_get("det:xhs:def"))

    def test_unparsable_ttl_is_dropped_without_touching_existing_key(self):
        tikhub._cache_set("det:xhs:keep", DETAIL_RESULT, 600)
        tikhub._cache_set("det:xhs:keep", {"other": True}, "abc")  # 源：int() 抛错被吞，原值保留
        self.assertEqual(tikhub._cache_get("det:xhs:keep"), DETAIL_RESULT)

    def test_corrupt_value_is_miss(self):
        tikhub_cache_redis.client().set(TEST_PREFIX + "det:douyin:5", "{not json", ex=60)
        self.assertIsNone(tikhub._cache_get("det:douyin:5"))

    def test_missing_redis_url_raises_on_client_but_degrades_cache_ops(self):
        os.environ.pop("HQ_REDIS_URL", None)
        tikhub_cache_redis.close()
        with self.assertRaises(RuntimeError):
            tikhub_cache_redis.client()
        self.assertFalse(tikhub_cache_redis.available())
        self.assertFalse(tikhub_cache_redis.describe()["url_configured"])
        self.assertIsNone(tikhub._cache_get("det:douyin:1"))          # 降级：未命中
        self.assertIsNone(tikhub._cache_set("det:douyin:1", DETAIL_RESULT, 60))  # 降级：丢弃
        os.environ["HQ_REDIS_URL"] = REDIS_URL
        tikhub_cache_redis.close()

    def test_redis_mode_never_touches_sqlite_file(self):
        tikhub._cache_set("det:douyin:7", DETAIL_RESULT, 600)
        self.assertEqual(tikhub._cache_get("det:douyin:7"), DETAIL_RESULT)
        self.assertFalse(os.path.exists(self.db_path))

    def test_sqlite_mode_writes_no_redis_key(self):
        os.environ["HQ_TIKHUB_CACHE"] = "sqlite"
        tikhub._cache_set("det:douyin:8", DETAIL_RESULT, 600)
        self.assertEqual(tikhub._cache_get("det:douyin:8"), DETAIL_RESULT)
        self.assertTrue(os.path.exists(self.db_path))
        self.assertEqual(list(tikhub_cache_redis.client().scan_iter(match=TEST_PREFIX + "*")), [])

    def test_public_entrypoints_share_redis_cache(self):
        with mock.patch.object(tikhub, "_search", return_value=SEARCH_RESULT) as upstream:
            tikhub.search("douyin", "美甲")
            tikhub.search("douyin", "美甲")
            self.assertEqual(upstream.call_count, 1)
            self.assertTrue(tikhub_cache_redis.client().exists(TEST_PREFIX + "srch:douyin:美甲:1:1"))
            tikhub.search("douyin", "美甲", fresh=True)
            self.assertEqual(upstream.call_count, 2)


if __name__ == "__main__":
    unittest.main()
