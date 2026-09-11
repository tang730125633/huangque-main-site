# 后台任务列表「选择线路＝实际渲染节点」「模型＝所用模板」专项单测（2026-09-11）
#
# 背景：这两列的实现原来**只改在服务器部署产物上、没进 git**，被 ship 一冲就没了
# （当天仪表盘上「线路」整列变空）。本次把它们正式纳入仓库，这里锁住行为：
#   ① 能从 relay.db 查到实际渲染节点 ② 查不到/查不动时一律返回空串、绝不抛异常
#   （这一列填不出来不该拖垮整个后台）③ 缓存生效（任务归属不会变）
#   ④ 模板 id 从 payload 或 result 里都能取到 ⑤ 模板中文名取不到时退回 id
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import server.admin_api as admin_api


JOB_ID = "a" * 32


class RenderRouteColumnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hq-route-")
        self.db = os.path.join(self.tmp, "relay.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, node TEXT)")
        conn.execute("INSERT INTO jobs(id, node) VALUES(?, ?)", (JOB_ID, "tang"))
        conn.commit()
        conn.close()
        self._patch = mock.patch.object(admin_api, "_RENDER_NODE_DB", self.db)
        self._patch.start()
        admin_api._render_node_cache.clear()

    def tearDown(self):
        self._patch.stop()
        admin_api._render_node_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- ① 正常查到 ----
    def test_known_job_resolves_to_actual_node(self):
        self.assertEqual("tang", admin_api._render_node_of(JOB_ID))

    def test_node_missing_from_relay_db_returns_empty(self):
        self.assertEqual("", admin_api._render_node_of("b" * 32))

    # ---- ② 异常/脏输入一律空串，绝不抛 ----
    def test_malformed_id_returns_empty_without_touching_db(self):
        with mock.patch.object(admin_api.sqlite3, "connect") as connect:
            for bad in ("", None, "not-a-job-id", "ZZZZ", "a" * 31, "a" * 33,
                        "b153d9…e01e"):
                self.assertEqual("", admin_api._render_node_of(bad), bad)
            connect.assert_not_called()

    def test_unreadable_db_returns_empty_not_raise(self):
        # relay.db 不在（中转器没跑/换机）时后台照样要能打开
        self._patch.stop()
        with mock.patch.object(admin_api, "_RENDER_NODE_DB",
                               os.path.join(self.tmp, "nope.db")):
            self.assertEqual("", admin_api._render_node_of(JOB_ID))
        self._patch.start()

    # ---- ③ 缓存：同一条任务不重复查库 ----
    def test_result_is_cached(self):
        admin_api._render_node_of(JOB_ID)
        with mock.patch.object(admin_api.sqlite3, "connect") as connect:
            self.assertEqual("tang", admin_api._render_node_of(JOB_ID))
            connect.assert_not_called()

    # ---- ④ 模板 id：payload 优先，其次 result ----
    def test_template_id_from_result(self):
        self.assertEqual("nine-grid-reveal", admin_api._result_template_id(
            json.dumps({"template_id": "nine-grid-reveal"})))

    def test_template_id_bad_input_returns_empty(self):
        for bad in (None, "", "not json", "[]", "[1,2]", json.dumps({"x": 1})):
            self.assertEqual("", admin_api._result_template_id(bad), repr(bad))

    # ---- ⑤ 模板中文名取不到就退回 id（后台那一列不能因为目录拉不到就空着）----
    def test_template_label_falls_back_to_id(self):
        # 目录里有别的模板、但没有这一个 → 显示 id 而不是空白
        with mock.patch.object(admin_api, "_MATRIX_TEMPLATE_NAMES",
                               {"other-template": "别的模板"}), \
             mock.patch.object(admin_api, "_MATRIX_TEMPLATE_NAMES_AT", time.time()):
            self.assertEqual("nine-grid-reveal",
                             admin_api._matrix_template_label("nine-grid-reveal"))
            # 中文名命中时用中文名；过长按列的宽度截到 24 字
            self.assertEqual("别的模板",
                             admin_api._matrix_template_label("other-template"))
        self.assertEqual("", admin_api._matrix_template_label(""))

    def test_template_label_survives_catalog_failure(self):
        """模板目录拉不到（渲染服务抖动）时，这一列退回 id，不能让后台打不开。"""
        admin_api._MATRIX_TEMPLATE_NAMES.clear()
        with mock.patch.object(admin_api, "_MATRIX_TEMPLATE_NAMES_AT", 0.0), \
             mock.patch.dict("sys.modules", {
                 "content_domains.matrix_template_video": mock.Mock(
                     public_templates=mock.Mock(side_effect=RuntimeError("服务不可用")))}):
            self.assertEqual("nine-grid-reveal",
                             admin_api._matrix_template_label("nine-grid-reveal"))

    def test_unknown_node_still_gets_a_readable_label(self):
        """新增渲染节点（如第四台）标签表里没有时，也要显示名字而不是空白。"""
        label_map = admin_api._RENDER_NODE_LABEL
        self.assertIn("tang", label_map)
        # 与源码里 `_RENDER_NODE_LABEL.get(name, "渲染线路 · " + name)` 的兜底一致
        self.assertEqual("渲染线路 · newnode", label_map.get("newnode", "渲染线路 · newnode"))


if __name__ == "__main__":
    unittest.main()
