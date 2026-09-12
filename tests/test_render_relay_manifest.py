import importlib.util
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderRelayManifestTests(unittest.TestCase):
    def test_metadata_merge_preserves_delivery_and_adds_manifest(self):
        relay = load("render_relay", "deploy/render-relay/relay.py")
        existing = {
            "file_url": "/v1/files/job.mp4",
            "cos_key": "huangque/render/job.mp4",
            "file_size": 123,
        }
        manifest = [{
            "slot": 1, "source": "user", "sha256": "a" * 64,
            "media_type": "image", "clip_start_seconds": 0,
            "clip_duration_seconds": 2.5,
        }]
        merged = relay._merge_completed_result(existing, {
            "file_url": "/local/file.mp4",
            "material_manifest": manifest,
            "template_id": "ref-test",
        })
        self.assertEqual("/v1/files/job.mp4", merged["file_url"])
        self.assertEqual("huangque/render/job.mp4", merged["cos_key"])
        self.assertEqual(manifest, merged["material_manifest"])

    def test_node_reports_renderer_metadata_after_binary_upload(self):
        source = (ROOT / "deploy/render-relay/node_poller.py").read_text(
            encoding="utf-8"
        )
        upload = source.index("out = upload_result(")
        report = source.index("report(jid, True, result=result)")
        self.assertLess(upload, report)

    def test_relay_stores_assets_and_poller_syncs_before_render(self):
        relay = load("render_relay_assets", "deploy/render-relay/relay.py")
        data = b"owned-image"
        sha = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            relay.USER_ASSET_DIR = Path(temp)
            path = relay._store_user_asset(data, sha, "image/png")
            self.assertEqual(data, path.read_bytes())
            self.assertEqual((path, "image/png"), relay._find_user_asset(sha))

        source = (ROOT / "deploy/render-relay/node_poller.py").read_text(
            encoding="utf-8"
        )
        sync = source.index("sync_user_assets(payload, jid)")
        render = source.index("result, error = run_local(payload, jid)")
        self.assertLess(sync, render)

    def test_only_assigned_node_can_fetch_a_referenced_asset(self):
        relay = load("render_relay_asset_http", "deploy/render-relay/relay.py")
        data = b"owned-image"
        sha = hashlib.sha256(data).hexdigest()
        job_id = "a" * 32
        with tempfile.TemporaryDirectory() as temp:
            relay.DB_PATH = str(Path(temp) / "relay.db")
            relay.USER_ASSET_DIR = Path(temp) / "assets"
            relay.NODE_TOKEN = "node-secret"
            relay.init_db()
            relay._store_user_asset(data, sha, "image/png")
            with sqlite3.connect(relay.DB_PATH) as connection:
                connection.execute(
                    "INSERT INTO jobs(id,payload,status,node,claimed_at,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (job_id, json.dumps({"user_materials": [{"sha256": sha}]}),
                     "running", "yuelei", 1, 1, 1),
                )
            server = ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = "http://127.0.0.1:%d/v1/job-assets/%s/%s" % (
                    server.server_port, job_id, sha,
                )
                request = urllib.request.Request(url, headers={
                    "Authorization": "Bearer node-secret", "X-HQ-Node": "yuelei",
                })
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(data, response.read())
                    self.assertEqual("image/png", response.headers.get_content_type())
                denied = urllib.request.Request(url, headers={
                    "Authorization": "Bearer node-secret", "X-HQ-Node": "other",
                })
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(denied, timeout=5)
                self.assertEqual(404, raised.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_poller_converts_user_images_before_local_render(self):
        env = {
            "NODE_RELAY_URL": "https://relay.test",
            "NODE_RELAY_TOKEN": "node-token",
            "NODE_LOCAL_TOKEN": "local-token",
        }
        with mock.patch.dict(os.environ, env):
            poller = load("render_node_image", "deploy/render-relay/node_poller.py")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers.get.return_value = "image/jpeg"
        response.read.return_value = b"image"
        payload = {
            "duration": 8.0,
            "user_materials": [{"sha256": "a" * 64, "media_type": "image"}],
        }
        with mock.patch.object(
            poller.urllib.request, "urlopen", return_value=response,
        ), mock.patch.object(
            poller, "_image_to_video", return_value=b"converted-video",
        ), mock.patch.object(poller, "_call") as upload:
            poller.sync_user_assets(payload, "b" * 32)
        converted = hashlib.sha256(b"converted-video").hexdigest()
        self.assertEqual({
            "sha256": converted, "media_type": "video", "clip_start_seconds": 0,
        }, payload["user_materials"][0])
        self.assertEqual("video/mp4", upload.call_args.kwargs["headers"]["Content-Type"])
        self.assertEqual(converted, upload.call_args.kwargs["headers"]["X-HQ-Asset-Sha256"])


    def _health_body(self, upstream_result, name="render_relay_health_up"):
        """起一个真实的中转器 HTTP 服务，mock 掉上游，返回 /health 的响应体。"""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RELAY_DB"] = os.path.join(tmp, "relay.db")
            relay = load(name, "deploy/render-relay/relay.py")
            relay.init_db()   # 建表只在 main() 里做，测试要自己来
            with mock.patch.object(relay, "_upstream", **upstream_result):
                server = ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = "http://127.0.0.1:%d/health" % server.server_port
                    try:
                        with urllib.request.urlopen(url, timeout=5) as resp:
                            return json.loads(resp.read())
                    except urllib.error.HTTPError as exc:
                        return json.loads(exc.read())
                finally:
                    server.shutdown()
                    server.server_close()

    def test_health_reports_upstream_material_state_verbatim(self):
        """2026-09-12：/health 原先把素材库就绪、契约版本、worker 池写死成常量 ——
        clip 契约写 2（上游实际 3）、worker_count 写 1（上游实际 5）、素材策略串停在 v1。
        主站不读这些字段，但排查渠道故障第一眼看的就是这几行，假值会把方向带偏。
        现在必须如实透传上游的值。"""
        upstream = {
            "ok": True, "templates": 22,
            "worker_alive": True, "worker_count": 5,
            "cleanup_worker_alive": True, "worker_degraded": False,
            "degraded_jobs": 0,
            "material_library_ready": True, "pexels_material_ready": True,
            "material_source_policy": "huangque-bookends-extra-middle-pexels-v2",
            "material_selection_contract_version": 2,
            "material_clip_contract_version": 3,
        }
        body = self._health_body(
            {"return_value": (200, json.dumps(upstream).encode())},
        )
        # 上游说什么就是什么
        self.assertEqual(22, body["templates"])
        self.assertIs(True, body["ok"])
        self.assertEqual(5, body["worker_count"])
        self.assertEqual(3, body["material_clip_contract_version"])
        self.assertEqual(2, body["material_selection_contract_version"])
        self.assertEqual(
            "huangque-bookends-extra-middle-pexels-v2",
            body["material_source_policy"],
        )
        # 中转器自己的队列长度仍然是真实值
        self.assertEqual(0, body["pending_jobs"])
        self.assertEqual(0, body["running_jobs"])

    def test_health_omits_upstream_fields_when_upstream_is_down(self):
        """上游挂掉时这些键应当**不出现**，而不是回落到编造的常量 ——
        这正是以前把「上游挂了」误读成「素材库就绪、契约正常」的原因。"""
        body = self._health_body(
            {"side_effect": RuntimeError("upstream down")},
            name="render_relay_health_down",
        )
        self.assertIs(False, body["ok"])
        for field in (
            "worker_alive", "worker_count", "material_library_ready",
            "pexels_material_ready", "material_source_policy",
            "material_selection_contract_version", "material_clip_contract_version",
        ):
            self.assertNotIn(field, body, "%s 不该在上游挂掉时凭空出现" % field)


class RelayLoadBalanceTests(unittest.TestCase):
    """负载均衡：谁空谁先拿，避免一台连着吃好几条把某条挤慢。

    起因（2026-09-12）：三台 GPU 同时接活，一台连吃 3 条，其中一条渲染被挤到 333 秒，
    而另一台全程闲着。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "relay.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, status TEXT, node TEXT)")
        conn.commit()
        conn.close()
        self.relay = load("render_relay_lb", "deploy/render-relay/relay.py")
        self.relay.DB_PATH = str(self.db)
        self.relay._LAST_CLAIM.clear()
        self.relay._NODE_FAIL.clear()

    def tearDown(self):
        self.relay._LAST_CLAIM.clear()
        self.dir.cleanup()

    def _running(self, node, count):
        conn = sqlite3.connect(self.db)
        for i in range(count):
            conn.execute("INSERT INTO jobs(id, status, node) VALUES(?, 'running', ?)",
                         ("%s-%d" % (node, i), node))
        conn.commit()
        conn.close()

    def test_busier_node_yields_to_idler(self):
        self._running("yuelei", 3)
        now = self.relay._now()
        self.relay._LAST_CLAIM.update({"yuelei": now, "tang": now})
        self.assertTrue(self.relay._should_yield_to_idler("yuelei", now),
                        "手上 3 条、别人 0 条 → 该让位")

    def test_idlest_node_never_yields(self):
        self._running("yuelei", 3)
        now = self.relay._now()
        self.relay._LAST_CLAIM.update({"yuelei": now, "tang": now})
        self.assertFalse(self.relay._should_yield_to_idler("tang", now),
                         "最空的那台永远不让，否则会互相让到没人干活")

    def test_equal_load_does_not_yield(self):
        self._running("tang", 1)
        self._running("yuelei", 1)
        now = self.relay._now()
        self.relay._LAST_CLAIM.update({"tang": now, "yuelei": now})
        self.assertFalse(self.relay._should_yield_to_idler("tang", now), "负载一样时正常抢")

    def test_single_online_node_does_not_yield(self):
        self._running("tang", 4)
        now = self.relay._now()
        self.relay._LAST_CLAIM["tang"] = now
        self.assertFalse(self.relay._should_yield_to_idler("tang", now),
                         "只有自己在线时没什么可让的")

    def test_offline_node_is_not_counted(self):
        """掉线/摘出池的机器不该被算进分母，否则剩下的节点全都不敢接活。"""
        self._running("tang", 2)
        self._running("fang", 0)
        now = self.relay._now()
        self.relay._LAST_CLAIM["tang"] = now
        self.relay._LAST_CLAIM["fang"] = now - self.relay.NODE_ONLINE_SECONDS - 10
        self.assertFalse(self.relay._should_yield_to_idler("tang", now),
                         "fang 已掉线，不该拖住 tang")

    def test_database_fault_never_stops_dispatch(self):
        self.relay.DB_PATH = str(Path(self.dir.name) / "nope.db")
        now = self.relay._now()
        self.relay._LAST_CLAIM.update({"tang": now, "yuelei": now})
        self.assertFalse(self.relay._should_yield_to_idler("tang", now),
                         "均衡坏了也不能把派活搞停")


if __name__ == "__main__":
    unittest.main()
