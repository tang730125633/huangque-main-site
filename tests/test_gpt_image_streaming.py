import http.client
import importlib
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FakeStream:
    def __init__(self, events, disconnect=False):
        self.lines = []
        for event in events:
            self.lines.extend([b"event: image\n", b"data: " + json.dumps(event).encode() + b"\n", b"\n"])
        self.disconnect = disconnect

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        if self.disconnect:
            self.disconnect = False
            raise http.client.RemoteDisconnected("remote closed")
        return b""


class GptImageStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(ROOT / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.egress = importlib.import_module("content_domains.egress")
        cls.image_source = (ROOT / "server" / "content_domains" / "image.py").read_text(encoding="utf-8")

    def test_completed_stream_event_returns_final_image(self):
        response = FakeStream([
            {"type": "image_generation.partial_image", "b64_json": "partial"},
            {"type": "image_generation.completed", "b64_json": "final"},
        ])
        self.assertEqual(self.egress._read_image_stream(response), {"data": [{"b64_json": "final"}]})

    def test_disconnect_after_valid_partial_preserves_last_image(self):
        response = FakeStream(
            [{"type": "image_generation.partial_image", "b64_json": "usable-partial"}],
            disconnect=True,
        )
        self.assertEqual(
            self.egress._read_image_stream(response),
            {"data": [{"b64_json": "usable-partial"}], "stream_incomplete": True},
        )

    def test_disconnect_before_any_image_is_not_hidden(self):
        with self.assertRaises(http.client.RemoteDisconnected):
            self.egress._read_image_stream(FakeStream([], disconnect=True))

    def test_official_text_generation_uses_streaming_transport(self):
        self.assertIn("egress.post_image_json", self.image_source)
        self.assertIn("streaming=True", self.image_source)


if __name__ == "__main__":
    unittest.main()
