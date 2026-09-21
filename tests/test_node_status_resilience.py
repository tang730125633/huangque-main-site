import importlib.util
import io
import contextlib
import http.server
import json
import os
from pathlib import Path
import unittest
import threading
import time
import urllib.error
from unittest import mock


class NodeStatusResilienceTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('status_poller',Path(__file__).resolve().parents[1]/'deploy/render-relay/node_poller.py')
        self.p=importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ,{'NODE_RELAY_URL':'http://relay.invalid','NODE_RELAY_TOKEN':'test-node','NODE_LOCAL_TOKEN':'test-local'}):spec.loader.exec_module(self.p)

    def error(self,status):
        return urllib.error.HTTPError('http://127.0.0.1/v1/jobs/local-one',status,'unavailable',{},io.BytesIO(b'{}'))

    def test_timeout_after_acceptance_keeps_tracking_without_second_submit(self):
        with mock.patch.object(self.p,'_submit_local',return_value={'job_id':'local-one'}) as submit,mock.patch.object(self.p,'_call',side_effect=[TimeoutError('timed out'),{'status':'running'},{'status':'completed','result':{'file_url':'/v1/files/local-one.mp4'}}]) as call,mock.patch.object(self.p.time,'sleep'):
            result,error=self.p.run_local({'template_id':'test'},'relay-one')
        self.assertIsNone(error);self.assertEqual(result['file_url'],'/v1/files/local-one.mp4');submit.assert_called_once()
        self.assertEqual(call.call_count,3)
        self.assertTrue(all(c.args[0].endswith('/v1/jobs/local-one') for c in call.call_args_list))
        self.assertTrue(all(c.kwargs.get('method','GET')=='GET' and c.kwargs.get('body') is None for c in call.call_args_list))

    def test_transient_http_and_connection_reset_recover(self):
        for error in [self.error(503),self.error(429),ConnectionResetError('reset')]:
            with self.subTest(error=type(error).__name__),mock.patch.object(self.p,'_submit_local',return_value={'job_id':'local-one'}),mock.patch.object(self.p,'_call',side_effect=[error,{'status':'completed','result':{'ok':True}}]),mock.patch.object(self.p.time,'sleep'):
                self.assertEqual(self.p.run_local({},'relay-one'),({'ok':True},None))

    def test_explicit_backend_failure_remains_terminal(self):
        with mock.patch.object(self.p,'_submit_local',return_value={'job_id':'local-one'}),mock.patch.object(self.p,'_call',return_value={'status':'failed','error':'render_failed'}) as call,mock.patch.object(self.p.time,'sleep'):
            self.assertEqual(self.p.run_local({},'relay-one'),(None,'render_failed'))
        self.assertEqual(call.call_count,1)

    def test_status_retries_do_not_extend_total_deadline(self):
        clock=[0.0]
        def read(*a,**k):clock[0]+=k['timeout'];raise TimeoutError('timed out')
        with mock.patch.object(self.p,'JOB_TIMEOUT',10),mock.patch.object(self.p,'_submit_local',return_value={'job_id':'one'}) as submit,mock.patch.object(self.p.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(self.p.time,'sleep',side_effect=lambda x:clock.__setitem__(0,clock[0]+x)),mock.patch.object(self.p,'_call',side_effect=read):
            result,error=self.p.run_local({},'relay-one')
        self.assertIsNone(result);self.assertIn('状态跟踪',error);self.assertLessEqual(clock[0],10);submit.assert_called_once()

    def test_local_result_download_retry_reuses_existing_file(self):
        with mock.patch.object(self.p,'_call',side_effect=[TimeoutError('timed out'),b'existing-mp4',{'ok':True,'cos_uploaded':True}]) as call,mock.patch.object(self.p.time,'sleep'):
            out=self.p.upload_result('relay-one','/v1/files/local-one.mp4',{'duration':12})
        self.assertTrue(out['ok']);self.assertEqual(call.call_count,3)
        self.assertEqual(call.call_args_list[0].args[0],call.call_args_list[1].args[0])
        self.assertTrue(call.call_args_list[2].args[0].endswith('/v1/result/relay-one'))
        self.assertFalse(any(c.args[0].endswith('/v1/jobs') for c in call.call_args_list))

    def test_auth_missing_job_and_other_permanent_http_fail_without_retry(self):
        for status in (400, 401, 403, 404, 409):
            with self.subTest(status=status), mock.patch.object(self.p, '_submit_local', return_value={'job_id':'one'}), mock.patch.object(self.p, '_call', side_effect=self.error(status)) as call, mock.patch.object(self.p.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'node_status_read_failed http_' + str(status)):
                    self.p.run_local({}, 'relay-one')
                self.assertEqual(call.call_count, 1)

    def test_transport_logs_do_not_include_exception_message(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(self.p, '_submit_local', return_value={'job_id':'one'}), mock.patch.object(self.p, '_call', side_effect=[urllib.error.URLError(TimeoutError('private-url?token=secret')), {'status':'completed','result':{'ok':True}}]), mock.patch.object(self.p.time, 'sleep'):
            self.assertEqual(self.p.run_local({}, 'relay-one'), ({'ok':True}, None))
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('private-url', output.getvalue())

    def test_download_attempts_are_bounded_and_never_post_on_failure(self):
        with mock.patch.object(self.p, '_call', side_effect=ConnectionResetError('reset')) as call, mock.patch.object(self.p.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'node_result_read_failed'):
                self.p.upload_result('relay-one', '/v1/files/one.mp4', {})
        self.assertEqual(call.call_count, 3)
        self.assertTrue(all(len(c.args) == 2 for c in call.call_args_list))

    def test_upload_uncertain_ack_is_not_blindly_repeated(self):
        with mock.patch.object(self.p, '_call', side_effect=[b'mp4', TimeoutError('timed out')]) as call:
            with self.assertRaises(TimeoutError):
                self.p.upload_result('relay-one', '/v1/files/one.mp4', {})
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[1].args[2], 'POST')

    def test_result_download_deadline_is_not_reset(self):
        clock = [0.0]
        def read(*args, **kwargs):
            clock[0] += kwargs['timeout']
            raise TimeoutError('timeout')
        with mock.patch.object(self.p, 'RENDER_TIMEOUT', 1), mock.patch.object(self.p.time, 'monotonic', side_effect=lambda:clock[0]), mock.patch.object(self.p.time, 'sleep', side_effect=lambda seconds:clock.__setitem__(0, clock[0]+seconds)), mock.patch.object(self.p, '_call', side_effect=read) as call:
            with self.assertRaisesRegex(RuntimeError, 'node_result_read_deadline'):
                self.p.upload_result('relay-one', '/v1/files/one.mp4', {})
        self.assertEqual(clock[0], 121)
        self.assertEqual(call.call_count, 1)

    def test_real_http_timeout_then_existing_job_completes(self):
        paths = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                paths.append(self.path)
                if len(paths) == 1:
                    time.sleep(0.15)
                body = json.dumps({'status':'completed','result':{'file_url':'/v1/files/existing.mp4'}}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_call = self.p._call
        real_sleep = time.sleep
        attempts = [0]
        def call(url, token, **kwargs):
            attempts[0] += 1
            kwargs['timeout'] = 0.03 if attempts[0] == 1 else 2
            return real_call(url, token, **kwargs)
        try:
            with mock.patch.object(self.p, 'LOCAL', 'http://127.0.0.1:' + str(server.server_port)), mock.patch.object(self.p, '_submit_local', return_value={'job_id':'existing'}) as submit, mock.patch.object(self.p, '_call', side_effect=call), mock.patch.object(self.p.time, 'sleep', side_effect=lambda seconds:real_sleep(0.15 if seconds == 0.15 else 0)):
                result, error = self.p.run_local({}, 'relay-one')
            self.assertIsNone(error)
            self.assertEqual(result['file_url'], '/v1/files/existing.mp4')
            submit.assert_called_once()
            self.assertEqual(paths, ['/v1/jobs/existing', '/v1/jobs/existing'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__=='__main__':unittest.main()
