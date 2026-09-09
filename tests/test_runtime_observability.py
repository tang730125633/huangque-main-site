import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server.content_domains import runtime_observability as telemetry
from server.content_domains import video_minimax_h3 as adapter


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HQ_OBSERVABILITY_DB':str(Path(self.folder.name)/'observability.db'), 'HQ_ALERT_ENABLED':'0'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_trace_records_failure_without_sensitive_error_text(self):
        with self.assertRaises(TimeoutError):
            telemetry.call(7, 'provider_query', lambda: (_ for _ in ()).throw(TimeoutError('secret-value')), model='MiniMax-H3', api_key='secret-value')
        result = telemetry.traces(7)
        self.assertEqual(result[0]['state'], 'failed')
        self.assertGreaterEqual(result[0]['duration_sec'], 0)
        self.assertNotIn('secret-value', json.dumps(result))
        self.assertEqual(result[0]['error_type'], 'TimeoutError')
        self.assertEqual(telemetry.traces(8), [])

    def test_private_database_is_owner_only(self):
        path = Path(os.environ['HQ_OBSERVABILITY_DB'])
        with closing(telemetry.database()):
            pass
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_definitive_minimax_rejection_is_not_unknown(self):
        with self.assertRaises(adapter.MiniMaxRejected):
            telemetry.call(
                9, 'provider_submit',
                lambda: (_ for _ in ()).throw(adapter.MiniMaxRejected('rejected')),
            )
        self.assertEqual(telemetry.traces(9)[0]['state'], 'failed')

    def test_runtime_evidence_searches_actual_provider_and_model(self):
        telemetry.record(10, 'route', 'recorded', provider='Actual Provider',
                         model='actual-model-v2')
        self.assertEqual({'10'}, telemetry.search_task_ids('actual-model-v2'))
        self.assertEqual({'10'}, telemetry.search_task_ids('actual provider'))

    def test_generation_records_actual_transport_but_never_claims_delivery(self):
        with patch.object(adapter, '_request_json', return_value={'task_id':'test-order'}), patch.object(adapter, 'query_task', return_value={'task':{'status':'succeeded','content':{'url':'https://files.metaso.cn/result.mp4'}}}):
            adapter.generate('local test', job_id=10, api_key='unused')
        result = telemetry.traces(10)
        self.assertEqual({r['stage'] for r in result}, {'provider_submit','provider_accepted','provider_query','generation'})
        self.assertTrue(all(r['state']=='recorded' for r in result))
        self.assertTrue(all(r['transport']=='direct' for r in result))
        self.assertIn('metaso.cn', {r.get('host') for r in result})
        self.assertFalse(any(r.get('delivery_verified') for r in result))

    def test_submit_and_generation_failures_remain_failed(self):
        with patch.object(adapter, '_request_json', side_effect=TimeoutError('secret')):
            with self.assertRaises(TimeoutError):
                adapter.generate('local test', job_id=11)
        self.assertEqual(telemetry.traces(11)[0]['state'], 'unknown')
        with patch.object(adapter, '_request_json', return_value={'task_id':'test-order'}), patch.object(adapter, 'query_task', return_value={'task':{'status':'failed','error':'failed'}}):
            with self.assertRaises(adapter.MiniMaxProviderFailed):
                adapter.generate('local test', job_id=12)
        self.assertEqual(next(r for r in telemetry.traces(12) if r['stage']=='generation')['state'], 'failed')

    def test_disabled_notifications_never_send(self):
        telemetry.enqueue('service.incident.open','content',1)
        with patch.object(telemetry.urllib.request, 'build_opener') as opener:
            telemetry.dispatch()
        opener.assert_not_called()
        self.assertFalse(telemetry.alert_status()['enabled'])

    def test_local_receiver_retry_dedup_and_recovery(self):
        received=[]
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(503 if len(received)==1 else 204)
                self.end_headers()
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict(os.environ,{'HQ_ALERT_ENABLED':'1','HQ_ALERT_WEBHOOK_URL':'http://127.0.0.1:%d/events'%server.server_port}):
                telemetry.enqueue('service.incident.open','content',1)
                telemetry.enqueue('service.incident.open','content',1)
                telemetry.dispatch()
                self.assertEqual(telemetry.alert_status()['counts']['pending'],1)
                with closing(telemetry.database()) as connection:
                    connection.execute('UPDATE alert_outbox SET next_try=0');connection.commit()
                telemetry.dispatch()
                telemetry.enqueue('service.incident.recovered','content',2)
                telemetry.dispatch()
                self.assertEqual(telemetry.alert_status()['counts']['sent'],2)
                self.assertEqual(len(received),3)
                self.assertEqual(received[-1]['event'],'service.incident.recovered')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_exhausted_notification_is_visible_and_invalid_endpoint_does_not_send(self):
        telemetry.enqueue('service.incident.open','content',3)
        with patch.dict(os.environ,{'HQ_ALERT_ENABLED':'1','HQ_ALERT_WEBHOOK_URL':'http://example.invalid/events'}):
            self.assertFalse(telemetry.alert_status()['enabled'])
            with patch.object(telemetry.urllib.request,'build_opener') as opener:
                telemetry.dispatch()
            opener.assert_not_called()
        with patch.dict(os.environ,{'HQ_ALERT_ENABLED':'1','HQ_ALERT_WEBHOOK_URL':'http://127.0.0.1/events'}), patch.object(telemetry.urllib.request,'build_opener',side_effect=OSError('private endpoint text')):
            for _ in range(5):
                with closing(telemetry.database()) as connection:
                    connection.execute('UPDATE alert_outbox SET next_try=0');connection.commit()
                telemetry.dispatch()
            self.assertEqual(telemetry.alert_status()['counts']['failed'],1)

    def test_private_https_webhook_is_rejected(self):
        with patch('server.content_domains.safe_http.socket.getaddrinfo',
                   return_value=[(2, 1, 6, '', ('10.0.0.8', 443))]):
            self.assertFalse(telemetry.valid_endpoint('https://hooks.example/events'))
