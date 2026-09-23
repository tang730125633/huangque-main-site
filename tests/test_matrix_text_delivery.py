"""Real local relay transport; synthetic bytes and mocked COS, no paid rendering."""
import copy
import json
from pathlib import Path
import sys
import time
import unittest
import urllib.request
from unittest import mock

from tests import test_render_delivery_recovery as recovery


class TextMetadataDeliveryTests(unittest.TestCase):
    setUp = recovery.DurableRelayTests.setUp
    tearDown = recovery.DurableRelayTests.tearDown
    contract = recovery.DurableRelayTests.contract
    call = recovery.DurableRelayTests.call
    heartbeat = recovery.DurableRelayTests.heartbeat
    upload = recovery.DurableRelayTests.upload
    adaptation = None

    def job(self):
        self.relay.OUT_DIR = str(Path(self.temp.name) / 'out')
        revision = 'd' * 64
        payload = {'template_id': 'nine-grid-reveal', 'top_text': 'Test title',
                   'bottom_text': 'Test action', 'bgm': False,
                   'text_revision': revision, 'text_overrides': {'top1': {'font_size_px': 90}}}
        contract = {**self.contract(), 'text_style_delivery_protocol': 2,
                    'text_style_contract': {'version': 1, 'templates': {'nine-grid-reveal': revision}}}
        if self.adaptation:
            payload['material_adaptation'] = self.adaptation
            contract.update(material_adaptation_contract=self.adaptation,
                            material_adaptation_delivery_protocol=2)
        self.heartbeat('gpu', contract)
        code, receipt = self.call('/v1/jobs', payload)
        self.assertEqual(202, code)
        claim = self.call('/v1/claim', {'node': 'gpu', 'gpu_render': contract,
                                      'delivery_protocol': 2}, node=True)[1]['job']
        return receipt['job_id'], claim, payload

    def get_job(self, jid):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/v1/jobs/{jid}',
                                     headers={'Authorization': 'Bearer test-client'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def metadata(self, payload):
        return {k: copy.deepcopy(payload[k]) for k in
                ('text_revision', 'text_overrides', 'material_adaptation') if k in payload}

    def report(self, jid, claim, result):
        return self.call('/v1/report', {'job_id': jid, 'node': 'gpu', 'ok': True,
                         'claim_token': claim['claim_token'], 'result': result}, node=True)

    def test_upload_waits_for_metadata_and_main_polls_through_to_delivery(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
        from content_domains import matrix_template_video as matrix
        jid, claim, payload = self.job()
        artifact = b'\x00\x00\x00\x18ftypmp42' + b'\0' * 2048
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)):
            self.assertEqual(200, self.upload(jid, claim['claim_token'], artifact)[0])
        waiting = self.get_job(jid)
        self.assertEqual('running', waiting['status'])
        self.assertEqual('awaiting_metadata', waiting['phase'])
        self.assertNotIn('result', waiting)
        metadata = dict(self.metadata(payload), duration=12, file_url='http://worker/private.mp4')
        request = matrix._request
        polls = []

        def delayed_report(method, path, *args, **kwargs):
            response = request(method, path, *args, **kwargs)
            polls.append(response['status'])
            if len(polls) == 1:
                self.assertEqual(200, self.report(jid, claim, metadata)[0])
            return response

        lifecycle = {'created_at': int(time.time()),
                     'payload': dict(payload, _matrix_runtime={'provider_job_id': jid})}
        with mock.patch.object(matrix, '_runtime', return_value=lifecycle), \
                mock.patch.object(matrix, '_persist_runtime', return_value=True), \
                mock.patch.object(matrix, '_request', side_effect=delayed_report), \
                mock.patch.object(matrix, 'API_URL', f'http://127.0.0.1:{self.server.server_port}'), \
                mock.patch.object(matrix, 'API_TOKEN', 'test-client'), \
                mock.patch.object(matrix, 'OUT_DIR', Path(self.temp.name) / 'main-out'), \
                mock.patch.object(matrix, 'POLL_INTERVAL', 0), \
                mock.patch.object(matrix, 'public_url', return_value='/fixture.mp4'):
            result = matrix._generate({'_job_id': '123', '_username': 'alice'})
            self.assertEqual(artifact, (matrix.OUT_DIR / result['video_file']).read_bytes())
        self.assertEqual(['running', 'completed'], polls)
        self.assertEqual(payload['text_overrides'], result['text_overrides'])
        self.assertEqual(200, self.report(jid, claim, metadata)[0])
        completed = self.get_job(jid)
        self.assertEqual('/v1/files/' + jid + '.mp4', completed['result']['file_url'])

    def test_bad_text_reports_leave_metadata_pending_and_can_be_retried(self):
        jid, claim, payload = self.job()
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)):
            self.upload(jid, claim['claim_token'])
        correct = self.metadata(payload)
        for invalid in ({}, dict(correct, text_revision='e'*64),
                        dict(correct, text_overrides={'top1': {'font_size_px': 80}}),
                        dict(correct, text_overrides=None), []):
            with self.subTest(invalid=invalid):
                self.assertEqual(409, self.report(jid, claim, invalid)[0])
                state = self.call('/v1/delivery-status', {'job_id': jid, 'node': 'gpu',
                                  'claim_token': claim['claim_token']}, node=True)[1]
                self.assertFalse(state['metadata_done'])
                self.assertEqual('running', self.get_job(jid)['status'])
        self.assertEqual(200, self.report(jid, claim, correct)[0])
        before = self.get_job(jid)['result']
        self.assertEqual(409, self.report(jid, claim, dict(correct, text_revision='e'*64))[0])
        self.assertEqual(before, self.get_job(jid)['result'])

    def test_wrong_persisted_text_echo_is_not_exposed_even_with_metadata_ack(self):
        jid, claim, payload = self.job()
        with mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)):
            self.upload(jid, claim['claim_token'])
        with self.relay._db() as conn:
            conn.execute('UPDATE delivery_claims SET metadata_done=1 WHERE job_id=?', (jid,))
            wrong = dict(self.metadata(payload), text_revision='e'*64)
            conn.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(wrong), jid))
        self.assertEqual('running', self.get_job(jid)['status'])


if __name__ == '__main__':
    unittest.main()
