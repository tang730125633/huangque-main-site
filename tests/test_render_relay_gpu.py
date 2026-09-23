import base64
import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class RenderRelayGpuTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("relay_gpu_test", ROOT / "deploy/render-relay/relay.py")
        self.relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.relay)
        self.temp = tempfile.TemporaryDirectory()
        self.relay.DB_PATH = str(Path(self.temp.name) / "jobs.db")
        @contextlib.contextmanager
        def db():
            connection = sqlite3.connect(self.relay.DB_PATH)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
        self.relay._db = db
        self.relay.init_db()
        self.relay.REQUIRE_GPU = True
        self.relay.NODE_TOKEN = "test-node"
        self.relay.RELAY_TOKEN = "test-client"
        self.server = self.relay._Server(("127.0.0.1", 0), self.relay.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def contract(self, templates=None):
        return {"ready": True, "contract_version": 1, "compositor": "webgpu-native",
                "encoder": "hevc_nvenc", "runtime_sha256": "a" * 64,
                "adapter": {"vendor": "nvidia", "device": "test", "isFallbackAdapter": False},
                "templates": templates or ["nine-grid-reveal"]}

    def call(self, route, body, *, node=False, raw=None, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{route}",
            data=raw if raw is not None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + ("test-node" if node else "test-client"),
                     "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def heartbeat(self, node="gpu", contract=None):
        return self.call("/v1/heartbeat", {"node": node, "gpu_render": contract or self.contract()}, node=True)

    def test_online_cpu_is_not_gpu_capability(self):
        self.call("/v1/heartbeat", {"node": "cpu", "gpu": {"name": "RTX"}}, node=True)
        self.assertEqual(503, self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})[0])
        self.assertEqual("gpu_unverified", self.call("/v1/claim", {"node": "cpu"}, node=True)[1]["deferred"])
        with self.relay._db() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def text_contract(self, revision="b"*64):
        return {**self.contract(), "text_style_delivery_protocol":2,
                "text_style_contract":{"version":1,"templates":{"nine-grid-reveal":revision}}}

    def test_text_styles_require_matching_api_fonts_and_durable_poller(self):
        payload={"template_id":"nine-grid-reveal","text_revision":"b"*64,
                 "text_overrides":{"top_text":{"font_size_px":72}}}
        self.heartbeat("old")
        self.assertEqual(503,self.call("/v1/jobs",payload)[0])
        no_poller=self.text_contract();no_poller.pop("text_style_delivery_protocol")
        self.heartbeat("old-poller",no_poller)
        self.assertEqual(503,self.call("/v1/jobs",payload)[0])
        self.heartbeat("wrong-fonts",self.text_contract("c"*64))
        self.assertEqual(503,self.call("/v1/jobs",payload)[0])
        self.heartbeat("new",self.text_contract())
        code,job=self.call("/v1/jobs",payload)
        self.assertEqual(202,code)
        self.assertIsNone(self.call("/v1/claim",{"node":"old","gpu_render":self.contract(),"delivery_protocol":2},node=True)[1]["job"])
        self.assertIsNone(self.call("/v1/claim",{"node":"new","gpu_render":self.text_contract()},node=True)[1]["job"])
        self.relay.PRIORITY_NODES={"old"}
        claimed=self.call("/v1/claim",{"node":"new","gpu_render":self.text_contract(),"delivery_protocol":2},node=True)[1]
        self.assertEqual(job["job_id"],claimed["job"]["job_id"])
        self.assertEqual(payload,claimed["job"]["payload"])

    def test_text_guard_survives_admission_switch_and_metadata_delivery(self):
        self.relay.REQUIRE_GPU=False
        payload={"template_id":"nine-grid-reveal","text_revision":"b"*64,
                 "text_overrides":{"top_text":{"color":"#ABCDEF"}}}
        self.heartbeat("old")
        self.assertEqual(503,self.call("/v1/jobs",payload)[0])
        self.heartbeat("new",self.text_contract())
        self.assertEqual(202,self.call("/v1/jobs",payload)[0])
        self.assertIsNone(self.call("/v1/claim",{"node":"old","gpu_render":self.contract()},node=True)[1]["job"])
        merged=self.relay._merge_completed_result({"file_url":"/original"},dict(payload,file_url="/untrusted"))
        self.assertEqual("/original",merged["file_url"])
        self.assertEqual(payload["text_overrides"],merged["text_overrides"])

    def test_preflight_rejects_old_upstream_that_discards_text_style(self):
        self.heartbeat("new",self.text_contract())
        payload={"template_id":"nine-grid-reveal","text_revision":"b"*64,"text_overrides":{"top_text":{"color":"#ABCDEF"}}}
        with mock.patch.object(self.relay,"_upstream",return_value=(200,json.dumps({"payload":{"template_id":"nine-grid-reveal"}}).encode())):
            self.assertEqual(409,self.call("/v1/preflight",payload)[0])

    def test_exact_template_support_and_frozen_claim(self):
        self.heartbeat()
        self.assertEqual(503, self.call("/v1/jobs", {"template_id": "ref-01-other"})[0])
        code, submitted = self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})
        self.assertEqual(202, code)
        code, claimed = self.call("/v1/claim", {"node": "gpu", "gpu_render": self.contract()}, node=True)
        self.assertEqual(submitted["job_id"], claimed["job"]["job_id"])
        with self.relay._db() as db:
            row = db.execute("SELECT gpu_contract FROM jobs WHERE id=?", (submitted["job_id"],)).fetchone()
        self.assertEqual("a" * 64, json.loads(row[0])["runtime_sha256"])

    def test_stale_and_software_capabilities_are_rejected(self):
        for invalid in ({"ready": False}, {"contract_version": True}, {"compositor": "cpu"},
                        {"adapter": {"vendor": "nvidia", "isFallbackAdapter": True}}):
            self.assertIsNone(self.relay._clean_render_contract({**self.contract(), **invalid}))
        self.relay._record_render_contract("gpu", self.contract(), 1)
        self.assertFalse(self.relay._gpu_capable("gpu", "nine-grid-reveal", 1000))

    def test_queue_is_bounded(self):
        self.heartbeat()
        self.relay.MAX_GPU_PENDING = 1
        self.assertEqual(202, self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})[0])
        self.assertEqual(429, self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})[0])

    def test_cpu_priority_does_not_starve_gpu(self):
        now = self.relay._now()
        self.relay.PRIORITY_NODES = {"cpu"}
        self.relay._LAST_CLAIM["cpu"] = now
        self.assertFalse(self.relay._priority_has_room(now, "nine-grid-reveal", True))

    def test_standby_does_not_block_primary_from_filling_another_slot(self):
        self.relay.PRIORITY_NODES = {'fast'}
        now = self.relay._now()
        self.heartbeat('fast'); self.heartbeat('standby')
        self.relay._LAST_CLAIM.update({'fast': now, 'standby': now})
        first = self.call('/v1/jobs', {'template_id': 'nine-grid-reveal'})[1]['job_id']
        self.assertEqual(first, self.call('/v1/claim', {'node': 'fast', 'gpu_render': self.contract()}, node=True)[1]['job']['job_id'])
        second = self.call('/v1/jobs', {'template_id': 'nine-grid-reveal'})[1]['job_id']
        standby = self.call('/v1/claim', {'node': 'standby', 'gpu_render': self.contract()}, node=True)[1]
        self.assertEqual('priority', standby['deferred'])
        fast = self.call('/v1/claim', {'node': 'fast', 'gpu_render': self.contract()}, node=True)[1]
        self.assertIsNotNone(fast['job'])
        self.assertEqual(second, fast['job']['job_id'])

    def test_standby_can_claim_after_primary_capacity_window_expires(self):
        self.relay.PRIORITY_NODES = {'fast'}
        now = self.relay._now()
        self.heartbeat('fast'); self.heartbeat('standby')
        self.relay._LAST_CLAIM['fast'] = now - self.relay.PRIORITY_WINDOW - 1
        job = self.call('/v1/jobs', {'template_id': 'nine-grid-reveal'})[1]['job_id']
        claimed = self.call('/v1/claim', {'node': 'standby', 'gpu_render': self.contract()}, node=True)[1]
        self.assertEqual(job, claimed['job']['job_id'])

    def test_health_exposes_configured_routing_priority(self):
        self.relay.PRIORITY_NODES = {'fast-b', 'fast-a'}
        self.heartbeat('fast-a')
        with mock.patch.object(self.relay, '_upstream', return_value=(200, b'{"ok":true,"templates":22}')):
            with urllib.request.urlopen(f'http://127.0.0.1:{self.server.server_port}/health', timeout=5) as response:
                body = json.load(response)
        self.assertEqual(['fast-a', 'fast-b'], body['priority_nodes'])
        self.assertEqual(self.relay.PRIORITY_WINDOW, body['priority_window_seconds'])

    def test_preflight_rejects_before_submission(self):
        checked = json.dumps({"ok": True, "payload": {"template_id": "nine-grid-reveal"}}).encode()
        with mock.patch.object(self.relay, "_upstream", return_value=(200, checked)):
            self.assertEqual(503, self.call("/v1/preflight", {})[0])
            self.heartbeat()
            self.assertEqual(200, self.call("/v1/preflight", {})[0])

    def test_completion_requires_artifact_and_claim_evidence(self):
        self.heartbeat()
        job = self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})[1]["job_id"]
        self.call("/v1/claim", {"node": "gpu", "gpu_render": self.contract()}, node=True)
        status, _ = self.call("/v1/result/" + job, {}, node=True, raw=b"fake")
        self.assertEqual(409, status)
        status, _ = self.call("/v1/report", {"job_id": job, "node": "gpu", "ok": True,
            "result": {"gpu_render": self.contract()}}, node=True)
        self.assertEqual(409, status)
        wrong = {**self.contract(), "runtime_sha256": "b" * 64}
        encoded = base64.b64encode(json.dumps(wrong).encode()).decode()
        status, _ = self.call("/v1/result/" + job, {}, node=True, raw=b"fake",
            headers={"X-HQ-Node": "gpu", "X-HQ-GPU-Render": encoded})
        self.assertEqual(409, status)

    def test_matching_gpu_upload_and_report_complete_without_changing_url(self):
        self.relay.OUT_DIR = str(Path(self.temp.name) / "out")
        self.heartbeat()
        job = self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})[1]["job_id"]
        self.call("/v1/claim", {"node": "gpu", "gpu_render": self.contract()}, node=True)
        encoded = base64.b64encode(json.dumps(self.contract()).encode()).decode()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            status, _ = self.call("/v1/result/" + job, {}, node=True, raw=b"test-artifact",
                headers={"X-HQ-Node": "gpu", "X-HQ-GPU-Render": encoded})
        self.assertEqual(200, status)
        status, _ = self.call("/v1/report", {"job_id": job, "node": "gpu", "ok": True,
            "result": {"gpu_render": self.contract(), "file_url": "http://worker/private.mp4",
                       "material_manifest": [{"id": "test-material"}]}}, node=True)
        self.assertEqual(200, status)
        with self.relay._db() as db:
            row = db.execute("SELECT status,result FROM jobs WHERE id=?", (job,)).fetchone()
        self.assertEqual("completed", row["status"])
        result = json.loads(row["result"])
        self.assertEqual("/v1/files/" + job + ".mp4", result["file_url"])
        self.assertEqual("a" * 64, result["gpu_render"]["runtime_sha256"])
        self.assertEqual([{"id": "test-material"}], result["material_manifest"])

    def test_required_job_survives_enforcement_off_and_cpu_priority(self):
        self.heartbeat()
        job = self.call("/v1/jobs", {"template_id": "nine-grid-reveal"})[1]["job_id"]
        self.relay.REQUIRE_GPU = False
        self.relay.PRIORITY_NODES = {"cpu"}
        claimed = self.call("/v1/claim", {"node": "cpu"}, node=True)[1]
        self.assertIsNone(claimed["job"])
        claimed = self.call("/v1/claim", {"node": "gpu", "gpu_render": self.contract()}, node=True)[1]
        self.assertEqual(job, claimed["job"]["job_id"])


if __name__ == "__main__":
    unittest.main()
