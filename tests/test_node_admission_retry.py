import importlib.util
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import unittest
import urllib.error
import threading
from unittest import mock


class NodeAdmissionRetryTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('admission_poller',Path(__file__).resolve().parents[1]/'deploy/render-relay/node_poller.py')
        self.p=importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ,{'NODE_RELAY_URL':'http://relay.invalid','NODE_RELAY_TOKEN':'test-node','NODE_LOCAL_TOKEN':'test-local'}):spec.loader.exec_module(self.p)

    def error(self,status,body):
        return urllib.error.HTTPError('http://127.0.0.1/v1/jobs',status,'rejected',{},io.BytesIO(json.dumps(body).encode()))

    def test_real_run_local_retries_only_transient_with_same_identity(self):
        e=self.error(503,{'error':'material_library_unavailable','reason_code':'probe_failed','retryable':True})
        with mock.patch.object(self.p,'_call',side_effect=[e,{'job_id':'local-one'},{'status':'completed','result':{'ok':True}}]) as call,mock.patch.object(self.p.time,'sleep'):
            result,error=self.p.run_local({'template_id':'nine-grid-reveal'},'relay-one')
        self.assertIsNone(error);self.assertEqual(result,{'ok':True})
        posts=[c for c in call.call_args_list if c.kwargs.get('body') is not None]
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[0].kwargs['headers'],posts[1].kwargs['headers'])
        self.assertEqual(posts[0].kwargs['body'],posts[1].kwargs['body'])

    def test_terminal_409_preserves_safe_reason_without_retry(self):
        e=self.error(409,{'error':'submission_failed','reason_code':'admission_failed','detail':'do-not-store-secret https://private.invalid/?token=example'})
        with mock.patch.object(self.p,'_call',side_effect=e) as call,mock.patch.object(self.p.time,'sleep') as sleep:
            with self.assertRaises(RuntimeError) as raised:self.p.run_local({},'relay-one')
        self.assertEqual(call.call_count,1);sleep.assert_not_called()
        self.assertIn('submission_failed',str(raised.exception));self.assertIn('409',str(raised.exception))
        self.assertNotIn('do-not-store-secret',str(raised.exception));self.assertNotIn('https://',str(raised.exception))

    def test_legacy_dependency_409_is_retryable_but_not_any_conflict(self):
        e=self.error(409,{'error':'submission_failed','detail':'素材库切片能力暂不可用'})
        with mock.patch.object(self.p,'_call',side_effect=[e,{'job_id':'one'},{'status':'completed','result':{}}]) as call,mock.patch.object(self.p.time,'sleep'):
            self.assertEqual(self.p.run_local({},'legacy'),({},None))
        self.assertEqual(call.call_count,3)

    def test_retries_have_attempt_limit(self):
        def reject(*a,**k):raise self.error(503,{'error':'material_library_unavailable','reason_code':'probe_timeout','retryable':True})
        with mock.patch.object(self.p,'_call',side_effect=reject) as call,mock.patch.object(self.p.time,'sleep'):
            with self.assertRaises(self.p.NodeSubmissionError) as raised:self.p.run_local({},'bounded')
        self.assertEqual(call.call_count,5);self.assertEqual(raised.exception.attempts,5)

    def test_wall_clock_budget_caps_retry(self):
        clock=[0.0]
        def reject(*a,**k):clock[0]+=29.5;raise self.error(503,{'error':'material_library_unavailable','reason_code':'probe_timeout','retryable':True})
        with mock.patch.object(self.p,'_call',side_effect=reject) as call,mock.patch.object(self.p.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(self.p.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            with self.assertRaises(self.p.NodeSubmissionError):self.p.run_local({},'deadline')
        self.assertEqual(call.call_count,1);self.assertEqual(clock[0],30.0)

    def test_auth_bad_contract_and_unknown_errors_are_not_retried(self):
        for status,body in [(401,{'error':'unauthorized'}),(409,{'error':'material_library_unavailable','reason_code':'auth_failed','retryable':False}),(503,{'error':'material_library_unavailable','reason_code':'contract_invalid','retryable':True}),(503,{'error':'material_library_unavailable','reason_code':'probe_failed','retryable':'true'}),(503,{'error':'unknown','retryable':True})]:
            with self.subTest(status=status,body=body),mock.patch.object(self.p,'_call',side_effect=self.error(status,body)) as call:
                with self.assertRaises(self.p.NodeSubmissionError):self.p.run_local({},'terminal')
                self.assertEqual(call.call_count,1)

    def test_uncertain_transport_is_not_blindly_reposted(self):
        with mock.patch.object(self.p,'_call',side_effect=urllib.error.URLError('connection lost')) as call:
            with self.assertRaises(self.p.NodeSubmissionError):self.p.run_local({},'unknown')
        self.assertEqual(call.call_count,1)

    def test_real_http_recovery_creates_one_job(self):
        attempts=[];created=set()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def send(self,status,value):
                raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']));key=self.headers['X-Request-Id'];attempts.append((key,body))
                if len(attempts)<=2:return self.send(503,{'error':'material_library_unavailable','reason_code':'probe_failed','retryable':True})
                created.add(key);self.send(202,{'job_id':'one'})
            def do_GET(self):self.send(200,{'status':'completed','result':{'ok':True}})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with mock.patch.object(self.p,'LOCAL','http://127.0.0.1:'+str(server.server_port)),mock.patch.object(self.p.time,'sleep'):
                self.assertEqual(self.p.run_local({'template_id':'test'},'stable'),({'ok':True},None))
            self.assertEqual(len(created),1);self.assertEqual(len(attempts),3);self.assertEqual(len(set(attempts)),1)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
