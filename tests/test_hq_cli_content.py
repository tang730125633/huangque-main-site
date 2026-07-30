import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio, core, upstream_guard


class _Points:
    def __init__(self):
        self.deductions = []

    def cost_of(self, kind, payload):
        return 24

    def get_points(self, username):
        return 100


class _DispatchNothing:
    class RevisionConflict(Exception):
        pass

    def dispatch_http(self, *args, **kwargs):
        return False

    def _http_error(self, handler, error, **kwargs):
        handler._send(400, {"detail": str(error)})


class HQCLIContentTests(unittest.TestCase):
    def setUp(self):
        self.points = _Points()
        self.originals = {
            "internal": core.AUTH_INTERNAL_TOKEN,
            "verify": core.verify,
            "domains": core._domains,
            "short_drama": core._short_drama_domain,
            "digital_ip": core._digital_ip_domain,
            "require_enabled": core.feature_flags.require_enabled,
            "security": core.miniprogram_security.check_payload,
            "shutting_down": core.is_shutting_down,
            "upstream": upstream_guard.exhausted_reason,
            "handlers": core.HANDLERS,
        }
        core.AUTH_INTERNAL_TOKEN = "test-cli-secret"
        core.verify = lambda token: {"username": "alice", "must_change": False}
        core._domains = lambda: (audio, self.points, object())
        core._short_drama_domain = lambda: _DispatchNothing()
        core._digital_ip_domain = lambda: _DispatchNothing()
        core.feature_flags.require_enabled = lambda kind: None
        core.miniprogram_security.check_payload = lambda payload: None
        core.is_shutting_down = lambda: False
        upstream_guard.exhausted_reason = lambda kind, payload: None
        core.HANDLERS = {"image": lambda payload: payload}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        core.AUTH_INTERNAL_TOKEN = self.originals["internal"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["domains"]
        core._short_drama_domain = self.originals["short_drama"]
        core._digital_ip_domain = self.originals["digital_ip"]
        core.feature_flags.require_enabled = self.originals["require_enabled"]
        core.miniprogram_security.check_payload = self.originals["security"]
        core.is_shutting_down = self.originals["shutting_down"]
        upstream_guard.exhausted_reason = self.originals["upstream"]
        core.HANDLERS = self.originals["handlers"]

    def _post(self, path, payload, internal=True, expected=None):
        headers = {"Authorization": "Bearer bridge-token", "Content-Type": "application/json"}
        if internal:
            headers["X-HQ-Internal-Token"] = "test-cli-secret"
        if expected is not None:
            headers["X-HQ-Expected-Cost"] = str(expected)
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.getcode(), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_server_quote_and_expected_cost_gate_precede_any_deduction(self):
        generation = {"prompt": "a yellow bird", "provider": "openai", "ratio": "1:1", "quality": "hd", "count": 1}
        status, quote = self._post("/api/gen/cli/quote", {"kind": "image", "payload": generation})
        self.assertEqual((200, 24, 100), (status, quote["cost"], quote["points"]))
        self.assertEqual(403, self._post("/api/gen/cli/quote", {"kind": "image", "payload": generation}, internal=False)[0])
        status, result = self._post("/api/gen/image", generation, expected=25)
        self.assertEqual(409, status)
        self.assertEqual("quote_cost_changed", result["code"])
        self.assertEqual([], self.points.deductions)

    def test_audio_validation_rejects_bad_knobs_before_generation(self):
        with self.assertRaisesRegex(ValueError, "pitch"):
            audio.validate_audio_payload({"text": "hello", "pitch": 99})
        clean = audio.validate_audio_payload({"text": " hello ", "speed": 1.2, "pitch": 0, "volume": 0})
        self.assertEqual(("hello", 1.2), (clean["text"], clean["speed"]))


if __name__ == "__main__":
    unittest.main()
