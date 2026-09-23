"""Transport contract tests; no paid renderer, TTS or COS calls."""
import base64
import copy
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
import sys
import time
import unittest
import urllib.request
from unittest import mock

from tests import test_render_delivery_recovery as recovery
from tests.test_render_relay_manifest import load
from tests import test_matrix_text_delivery as text_delivery


class CombinedMetadataDeliveryTests(text_delivery.TextMetadataDeliveryTests):
    adaptation = "auto-v1"

    def test_matching_text_does_not_ack_wrong_adaptation(self):
        jid, claim, payload = self.job()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            self.upload(jid, claim["claim_token"])
        correct = self.metadata(payload)
        for value in (None, "auto-v2"):
            invalid = dict(correct, material_adaptation=value)
            self.assertEqual(409, self.report(jid, claim, invalid)[0])
            self.assertEqual("running", self.get_job(jid)["status"])
        self.assertEqual(200, self.report(jid, claim, correct)[0])
        self.assertEqual("completed", self.get_job(jid)["status"])


class AdaptationDeliveryTests(unittest.TestCase):
    setUp = recovery.DurableRelayTests.setUp
    tearDown = recovery.DurableRelayTests.tearDown
    contract = recovery.DurableRelayTests.contract
    call = recovery.DurableRelayTests.call
    heartbeat = recovery.DurableRelayTests.heartbeat
    upload = recovery.DurableRelayTests.upload

    def adaptive_contract(self):
        return {**self.contract(), "material_adaptation_contract": "auto-v1",
                "material_adaptation_delivery_protocol": 2}

    def job(self):
        self.relay.OUT_DIR = str(Path(self.temp.name) / "out")
        capability = self.adaptive_contract()
        self.heartbeat("gpu", capability)
        code, receipt = self.call("/v1/jobs", {
            "template_id": "nine-grid-reveal", "material_adaptation": "auto-v1"})
        self.assertEqual(202, code)
        claim = self.call("/v1/claim", {"node": "gpu", "gpu_render": capability,
            "delivery_protocol": 2}, node=True)[1]["job"]
        return receipt["job_id"], claim

    def get_job(self, jid):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/v1/jobs/{jid}",
            headers={"Authorization": "Bearer test-client"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_claim_upload_report_get_and_main_site_delivery(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        from content_domains import matrix_template_video as matrix
        jid, claim = self.job()
        # Transport fixture only: never claimed to be a real rendered video.
        artifact = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            self.assertEqual(200, self.upload(jid, claim["claim_token"], artifact)[0])
        waiting = self.get_job(jid)
        self.assertEqual("running", waiting["status"])
        self.assertNotIn("result", waiting)
        manifest = [{"source": "user", "sha256": "c"*64,
            "adaptation": {"mode": "image", "source_index": 0}}]
        report = {"job_id": jid, "node": "gpu", "claim_token": claim["claim_token"],
            "ok": True, "result": {"material_adaptation": "auto-v1",
                "material_manifest": manifest, "duration": 12, "width": 1080, "height": 1920,
                "file_url": "http://worker/private.mp4"}}
        self.assertEqual(200, self.call("/v1/report", report, node=True)[0])
        completed = self.get_job(jid)
        self.assertEqual("completed", completed["status"])
        self.assertEqual("auto-v1", completed["result"]["material_adaptation"])
        self.assertEqual("/v1/files/"+jid+".mp4", completed["result"]["file_url"])
        # Retrying the report is harmless and retains both fields and artifact.
        self.assertEqual(200, self.call("/v1/report", report, node=True)[0])
        payload = {"template_id": "nine-grid-reveal", "top_text": "完整标题", "bottom_text": "完整文案",
            "bgm": False, "material_adaptation": "auto-v1", "_matrix_unified_contract": 1,
            "_matrix_runtime": {"provider_job_id": jid}}
        with mock.patch.object(matrix, "_runtime", return_value={"created_at": int(time.time()), "payload": payload}), \
                mock.patch.object(matrix, "_persist_runtime", return_value=True), \
                mock.patch.object(matrix, "API_URL", f"http://127.0.0.1:{self.server.server_port}"), \
                mock.patch.object(matrix, "API_TOKEN", "test-client"), \
                mock.patch.object(matrix, "OUT_DIR", Path(self.temp.name)/"main-out"):
            result = matrix._generate({"_job_id": "101", "_username": "alice", "_matrix_unified_contract": 1})
            self.assertEqual(artifact, (matrix.OUT_DIR / result["video_file"]).read_bytes())
        self.assertEqual(manifest, result["material_manifest"])

    def test_missing_wrong_or_unexpected_echo_never_finishes_metadata(self):
        jid, claim = self.job()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            self.upload(jid, claim["claim_token"])
        for incoming in ({}, {"material_adaptation": "auto-v2"}, {"material_adaptation": True}, []):
            with self.subTest(incoming=incoming):
                code, body = self.call("/v1/report", {"job_id":jid,"node":"gpu",
                    "claim_token":claim["claim_token"],"ok":True,"result":incoming}, node=True)
                self.assertEqual(409, code)
                self.assertEqual("material_adaptation_mismatch", body["error"])
                state = self.call("/v1/delivery-status", {"job_id":jid,"node":"gpu",
                    "claim_token":claim["claim_token"]}, node=True)[1]
                self.assertFalse(state["metadata_done"])
                self.assertEqual("running", self.get_job(jid)["status"])
        with self.assertRaises(ValueError):
            self.relay._merge_completed_result({}, {"material_adaptation":"auto-v1"}, payload={})

    def test_new_renderer_with_old_poller_cannot_advertise_adaptation(self):
        old = self.adaptive_contract()
        old.pop("material_adaptation_delivery_protocol")
        self.heartbeat("old-poller", old)
        self.assertEqual(503, self.call("/v1/jobs", {
            "template_id":"nine-grid-reveal","material_adaptation":"auto-v1"})[0])
        capability = self.adaptive_contract()
        self.heartbeat("gpu", capability)
        code, receipt = self.call("/v1/jobs", {
            "template_id":"nine-grid-reveal","material_adaptation":"auto-v1"})
        self.assertEqual(202, code)
        self.assertIsNone(self.call("/v1/claim", {"node":"old-poller",
            "gpu_render":old,"delivery_protocol":2}, node=True)[1]["job"])
        claimed = self.call("/v1/claim", {"node":"gpu", "gpu_render":capability,
            "delivery_protocol":2}, node=True)[1]["job"]
        self.assertEqual(receipt["job_id"], claimed["job_id"])


class PollerOriginalMediaTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, {"NODE_RELAY_URL":"https://relay.test",
                "NODE_RELAY_TOKEN":"test-node", "NODE_LOCAL_TOKEN":"test-local"}):
            self.p = load("poller_adaptation_test", "deploy/render-relay/node_poller.py")

    def test_adaptive_images_keep_original_bytes_mime_and_identity(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAABgAAAAQCAIAAACDRijCAAAAI0lEQVR4nGPUWBDAQA3ARBVTGEYNIgaMBjZhMBpGhMHgCyMASQMBOE7/Ns0AAAAASUVORK5CYII=")
        video = b"original-video-transport-fixture"
        for duration, bgm, mixed in ((2.6, False, False), (8., True, False), (30.6, False, True)):
            with self.subTest(duration=duration, bgm=bgm, mixed=mixed):
                inputs = [(png,"image/png","image")] + ([(video,"video/mp4","video")] if mixed else [])
                payload = {"material_adaptation":"auto-v1", "duration":duration, "bgm":bgm,
                    "template_id":"bilingual-stagger-salon" if duration != 8 else "ref-test",
                    "user_materials":[{"sha256":hashlib.sha256(data).hexdigest(),"media_type":kind} for data,_,kind in inputs]}
                before = copy.deepcopy(payload)
                responses = []
                for data, mime, _ in inputs:
                    response = io.BytesIO(data); response.headers = {"Content-Type": mime}
                    responses.append(response)
                sent = []
                def upload(*args, **kw):
                    sent.append((kw["raw"].read(), kw["headers"]))
                with mock.patch.object(self.p, "_renderer_capability", return_value={"material_adaptation_contract":"auto-v1", "material_adaptation_delivery_protocol":2}), \
                        mock.patch.object(self.p.urllib.request,"urlopen", side_effect=responses), \
                        mock.patch.object(self.p,"_image_to_video", side_effect=AssertionError("legacy conversion")), \
                        mock.patch.object(self.p,"_call", side_effect=upload):
                    self.p.sync_user_assets(payload,"a"*32)
                self.assertEqual(before, payload)
                for (data,mime,_), (transferred, headers) in zip(inputs,sent):
                    self.assertEqual(data, transferred)
                    self.assertEqual(mime, headers["Content-Type"])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), headers["X-HQ-Asset-Sha256"])

    def test_capability_marks_only_an_upgraded_renderer(self):
        with mock.patch.object(self.p,"_call",return_value={"ok":True,"gpu_render":{
                "ready":True,"material_adaptation_contract":"auto-v1"}}):
            self.assertEqual(2,self.p._renderer_capability().get("material_adaptation_delivery_protocol"))
        with mock.patch.object(self.p,"_call",return_value={"ok":True,"gpu_render":{"ready":True}}):
            self.assertNotIn("material_adaptation_delivery_protocol",self.p._renderer_capability())

    def test_unknown_protocol_or_downgraded_local_renderer_fails_before_transfer(self):
        for value, capability in (("auto-v2",{}), ("auto-v1",{})):
            with self.subTest(value=value), mock.patch.object(self.p,"_renderer_capability",return_value=capability), \
                    mock.patch.object(self.p.urllib.request,"urlopen") as download:
                with self.assertRaises(RuntimeError):
                    self.p.sync_user_assets({"material_adaptation":value},"a"*32)
                download.assert_not_called()

    def test_unknown_health_keeps_the_same_claim_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.p.DeliveryStore(Path(root))
            jid = "a"*32
            store.ensure({"job_id":jid,"claim_token":"b"*32,"payload":{"material_adaptation":"auto-v1"}})
            with mock.patch.object(self.p,"delivery_status",return_value={"status":"running"}), \
                    mock.patch.object(self.p,"_renderer_capability",return_value=None), \
                    mock.patch.object(self.p,"report") as report, \
                    mock.patch.object(self.p,"run_local") as render:
                with self.assertRaises(ConnectionError):
                    self.p.process_delivery(store,store.get(jid))
                report.assert_not_called()
                render.assert_not_called()
            self.assertEqual("claimed",store.get(jid)["phase"])

    def test_adaptive_original_bytes_still_require_matching_sha(self):
        response = io.BytesIO(b"corrupt")
        response.headers = {"Content-Type":"image/png"}
        with mock.patch.object(self.p,"_renderer_capability",return_value={
                "material_adaptation_contract":"auto-v1","material_adaptation_delivery_protocol":2}), \
                mock.patch.object(self.p.urllib.request,"urlopen",return_value=response), \
                mock.patch.object(self.p,"_call") as upload:
            with self.assertRaisesRegex(RuntimeError,"校验失败"):
                self.p.sync_user_assets({"material_adaptation":"auto-v1", "user_materials":[
                    {"sha256":"a"*64,"media_type":"image"}]},"b"*32)
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
