import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from content_domains import video_topview_seedance as topview


class Response:
    def __init__(self, payload=b"", content_type="application/json"):
        self.payload = payload
        self.headers = type("Headers", (), {"get_content_type": lambda _self: content_type})()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.payload


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def encoded(payload):
    return Response(json.dumps(payload).encode())


class TopviewSeedanceTests(unittest.TestCase):
    def test_reference_submit_once_then_poll_success(self):
        opener = Opener([
            Response(b"image", "image/jpeg"),
            encoded({"code": "200", "result": {"fileId": "f1", "uploadUrl": "https://upload.test/f1"}}),
            Response(),
            encoded({"code": "200", "result": True}),
            encoded({"code": "200", "result": {"taskId": "task1", "status": "init"}}),
            encoded({"code": "200", "result": {"status": "success", "costCredit": "10.00", "videos": [{"filePath": "https://cdn.test/out.mp4"}]}}),
        ])
        with patch.object(topview, "TOPVIEW_UID", "uid"), patch.object(topview, "_opener", return_value=opener):
            result = topview.generate(
                prompt="portrait waves", reference_images=["https://cos.test/ref.jpg"],
                api_key="secret", sleep=lambda _seconds: None,
            )
        posts = [request for request, _ in opener.requests if request.get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        submitted = json.loads(posts[0].data)
        self.assertEqual(submitted["model"], "Standard")
        self.assertEqual(submitted["firstFrameFileId"], "f1")
        self.assertEqual(submitted["prompt"], "portrait waves")
        self.assertNotIn("aspectRatio", submitted)
        self.assertEqual(submitted["sound"], "on")
        self.assertIn("/v2/common_task/image2video/task/query", opener.requests[-1][0].full_url)
        self.assertEqual(result["request_id"], "i2v:task1")
        self.assertEqual(result["source_video_url"], "https://cdn.test/out.mp4")
        self.assertEqual(result["provider_cost_credit"], "10.00")

    def test_submit_network_error_is_never_retried(self):
        opener = Opener([urllib.error.URLError("lost")])
        with patch.object(topview, "TOPVIEW_UID", "uid"):
            with self.assertRaises(topview.CreateOutcomeUnknown):
                topview._request_json(opener, "POST", "/submit", {}, api_key="secret", submit=True)
        self.assertEqual(len(opener.requests), 1)

    def test_first_phase_rejects_multiple_images(self):
        with self.assertRaisesRegex(ValueError, "仅支持 1 张"):
            topview._build_payload(
                topview.SEEDANCE_MODEL, "demo", 5, "9:16", "720p", True,
                ["https://a.test/1.jpg", "https://a.test/2.jpg"],
            )


if __name__ == "__main__":
    unittest.main()
