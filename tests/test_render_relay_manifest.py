import importlib.util
import unittest
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


if __name__ == "__main__":
    unittest.main()
