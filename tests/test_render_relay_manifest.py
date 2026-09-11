import importlib.util
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
