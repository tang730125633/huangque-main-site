import base64
import json
import tempfile
import time
import unittest
import concurrent.futures
import importlib.util
import os
import http.server
import threading
from pathlib import Path
from unittest import mock

from tests import test_render_relay_gpu as relay_tests
from tests import test_node_status_resilience as poller_tests


class DurableRelayTests(unittest.TestCase):
    setUp = relay_tests.RenderRelayGpuTests.setUp
    tearDown = relay_tests.RenderRelayGpuTests.tearDown
    contract = relay_tests.RenderRelayGpuTests.contract
    call = relay_tests.RenderRelayGpuTests.call
    heartbeat = relay_tests.RenderRelayGpuTests.heartbeat
    def durable_job(self):
        self.relay.OUT_DIR = str(Path(self.temp.name)/'out')
        self.heartbeat('gpu')
        jid = self.call('/v1/jobs', {'template_id':'nine-grid-reveal'})[1]['job_id']
        claim = self.call('/v1/claim', {'node':'gpu','gpu_render':self.contract(), 'delivery_protocol':2}, node=True)[1]['job']
        return jid, claim

    def test_durable_claim_cannot_be_reassigned_after_timeout(self):
        jid, claim = self.durable_job()
        self.assertTrue(claim.get('claim_token'))
        with self.relay._db() as c:
            c.execute('update jobs set claimed_at=1 where id=?',(jid,))
        self.heartbeat('other')
        other = self.call('/v1/claim', {'node':'other','gpu_render':self.contract(),'delivery_protocol':2},node=True)[1]
        self.assertIsNone(other['job'])
        recovered = self.call('/v1/recover', {'node':'gpu'}, node=True)
        self.assertEqual(recovered[0],200)
        self.assertEqual(recovered[1]['jobs'][0]['job_id'],jid)
        self.assertEqual(recovered[1]['jobs'][0]['claim_token'],claim['claim_token'])
        self.assertEqual(self.call('/v1/recover',{'node':'other'},node=True)[1]['jobs'],[])

    def upload(self,jid,token,data=b'complete-existing-video',node='gpu'):
        import hashlib
        return self.call('/v1/result/'+jid,{},node=True,raw=data,headers={
            'X-HQ-Node':node,'X-HQ-Claim-Token':token,
            'X-HQ-Artifact-Sha256':hashlib.sha256(data).hexdigest(),
            'X-HQ-GPU-Render':base64.b64encode(json.dumps(self.contract()).encode()).decode()})

    def test_duplicate_upload_is_idempotent_and_conflicting_bytes_rejected(self):
        jid, claim=self.durable_job();token=claim.get('claim_token','missing')
        with mock.patch('subprocess.run',return_value=mock.Mock(returncode=0)) as cos:
            self.assertEqual(self.upload(jid,token)[0],200)
            self.assertEqual(self.upload(jid,token)[0],200)
            self.assertEqual(cos.call_count,1)
            self.assertEqual(self.upload(jid,token,b'different')[0],409)
        self.assertEqual((Path(self.relay.OUT_DIR)/(jid+'.mp4')).read_bytes(),b'complete-existing-video')

    def test_claim_token_and_owner_fence_upload_and_report(self):
        jid, claim=self.durable_job();token=claim.get('claim_token','missing')
        with mock.patch('subprocess.run',return_value=mock.Mock(returncode=0)) as cos:
            self.assertEqual(self.upload(jid,'wrong')[0],409)
            self.assertEqual(self.upload(jid,token,node='other')[0],409)
            self.assertEqual(cos.call_count,0)
        self.assertEqual(self.call('/v1/report',{'job_id':jid,'node':'gpu','ok':False,'claim_token':'wrong'},node=True)[0],409)
        self.assertEqual(self.call('/v1/delivery-status',{'job_id':jid,'node':'other','claim_token':token},node=True)[0],409)


    def test_parallel_replays_have_one_cos_write(self):
        jid,claim=self.durable_job()
        with mock.patch('subprocess.run',return_value=mock.Mock(returncode=0)) as cos:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                statuses=list(pool.map(lambda _:self.upload(jid,claim['claim_token'])[0],range(2)))
            self.assertEqual(statuses,[200,200]);self.assertEqual(cos.call_count,1)

    def test_failed_report_cannot_downgrade_received_artifact(self):
        jid,claim=self.durable_job()
        with mock.patch('subprocess.run',return_value=mock.Mock(returncode=0)):
            self.assertEqual(self.upload(jid,claim['claim_token'])[0],200)
        body={'job_id':jid,'node':'gpu','claim_token':claim['claim_token'],'ok':False,'error':'late_transport_error'}
        self.assertEqual(self.call('/v1/report',body,node=True)[0],200)
        state=self.call('/v1/delivery-status',body,node=True)[1]
        self.assertEqual(state['status'],'completed')

    def test_metadata_ack_removes_recovery_and_preserves_first_completion_time(self):
        jid,claim=self.durable_job()
        with mock.patch('subprocess.run',return_value=mock.Mock(returncode=0)):
            self.upload(jid,claim['claim_token'])
        with self.relay._db() as c:before=c.execute('select updated_at from jobs where id=?',(jid,)).fetchone()[0]
        self.assertEqual(len(self.call('/v1/recover',{'node':'gpu'},node=True)[1]['jobs']),1)
        body={'job_id':jid,'node':'gpu','claim_token':claim['claim_token'],'ok':True,'result':{'material_manifest':[{'source':'shared'}]}}
        with mock.patch.object(self.relay,'_now',return_value=before+100):
            self.assertEqual(self.call('/v1/report',body,node=True)[0],200)
        self.assertEqual(self.call('/v1/recover',{'node':'gpu'},node=True)[1]['jobs'],[])
        with self.relay._db() as c:self.assertEqual(c.execute('select updated_at from jobs where id=?',(jid,)).fetchone()[0],before)

    def test_owned_unfinished_jobs_bound_new_claims(self):
        self.heartbeat('gpu')
        for _ in range(8):self.call('/v1/jobs',{'template_id':'nine-grid-reveal'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims=list(pool.map(lambda _:self.call('/v1/claim',{'node':'gpu','gpu_render':self.contract(),'delivery_protocol':2,'slots':5},node=True)[1],range(8)))
        self.assertEqual(sum(x.get('job') is not None for x in claims),5)

    def test_real_http_lost_upload_ack_then_restart_keeps_one_local_render(self):
        jid,claim=self.durable_job()
        result={'file_url':'/v1/files/existing.mp4','duration':12,'gpu_render':self.contract(),
                'material_manifest':[{'source':'shared'}]}
        posts=[]
        class Local(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                posts.append(self.headers['X-Request-Id'])
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(b'{"job_id":"existing"}')
            def do_GET(self):
                self.send_response(200)
                if self.path.startswith('/v1/jobs/'):
                    self.send_header('Content-Type','application/json');self.end_headers()
                    self.wfile.write(json.dumps({'status':'completed','result':result}).encode())
                else:
                    self.send_header('Content-Type','video/mp4');self.end_headers();self.wfile.write(b'existing-mp4')
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Local);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            spec=importlib.util.spec_from_file_location('integrated_poller',Path(__file__).resolve().parents[1]/'deploy/render-relay/node_poller.py')
            p=importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ,{'NODE_RELAY_URL':f'http://127.0.0.1:{self.server.server_port}','NODE_RELAY_TOKEN':'test-node','NODE_LOCAL_TOKEN':'test-local','NODE_LOCAL_RENDER':f'http://127.0.0.1:{server.server_port}','NODE_NAME':'gpu'}):spec.loader.exec_module(p)
            store=p.DeliveryStore(Path(self.temp.name)/'outbox');store.ensure(claim)
            real_call=p._call
            def lose_ack(url,*args,**kwargs):
                response=real_call(url,*args,**kwargs)
                if '/v1/result/' in url:raise TimeoutError('ack lost')
                return response
            with mock.patch('subprocess.run',return_value=mock.Mock(returncode=0)) as cos,mock.patch.object(p.time,'sleep'):
                with mock.patch.object(p,'_call',side_effect=lose_ack):
                    with self.assertRaises(TimeoutError):p.process_delivery(store,store.get(jid))
                restored=p.DeliveryStore(store.root)
                p.process_delivery(restored,restored.get(jid))
                self.assertEqual(cos.call_count,1)
            self.assertEqual(posts,['relay'+jid])
            self.assertEqual(restored.get(jid)['phase'],'complete')
            self.assertEqual(self.call('/v1/recover',{'node':'gpu'},node=True)[1]['jobs'],[])
            with self.relay._db() as c:
                saved=json.loads(c.execute('select result from jobs where id=?',(jid,)).fetchone()[0])
                self.assertEqual(saved['material_manifest'],[{'source':'shared'}])
        finally:
            server.shutdown();server.server_close();thread.join()


class DurablePollerTests(unittest.TestCase):
    setUp = poller_tests.NodeStatusResilienceTests.setUp
    def test_completed_output_survives_upload_disconnect_and_process_restart(self):
        with tempfile.TemporaryDirectory() as t:
            store=self.p.DeliveryStore(Path(t))
            job={'job_id':'a'*32,'claim_token':'b'*32,'payload':{'template_id':'test'}}
            store.ensure(job)
            result={'file_url':'/v1/files/existing.mp4','duration':12}
            with mock.patch.object(self.p,'sync_user_assets'),mock.patch.object(self.p,'run_local',return_value=(result,None)) as render,mock.patch.object(self.p,'delivery_status',return_value={'status':'running'}),mock.patch.object(self.p,'_download_local_result',return_value=b'existing-mp4'),mock.patch.object(self.p,'upload_result',side_effect=ConnectionRefusedError),mock.patch.object(self.p,'report') as report:
                with self.assertRaises(ConnectionRefusedError):self.p.process_delivery(store,store.get(job['job_id']))
                render.assert_called_once();report.assert_not_called()
            reloaded=self.p.DeliveryStore(Path(t))
            self.assertEqual(reloaded.get(job['job_id'])['phase'],'ready')
            with mock.patch.object(self.p,'run_local') as render,mock.patch.object(self.p,'delivery_status',return_value={'status':'running'}),mock.patch.object(self.p,'upload_result',return_value={'ok':True}) as upload,mock.patch.object(self.p,'report') as report:
                self.p.process_delivery(reloaded,reloaded.get(job['job_id']))
                render.assert_not_called();self.assertEqual(upload.call_args.kwargs['data'],b'existing-mp4');report.assert_called_once()
            self.assertEqual(reloaded.get(job['job_id'])['phase'],'complete')

    def test_lost_upload_ack_reconciles_without_second_upload(self):
        with tempfile.TemporaryDirectory() as t:
            store=self.p.DeliveryStore(Path(t));jid='a'*32
            store.ensure({'job_id':jid,'claim_token':'b'*32,'payload':{}})
            store.update(jid,phase='ready',result={'file_url':'/v1/files/existing.mp4'})
            with mock.patch.object(self.p,'delivery_status',return_value={'status':'completed'}),mock.patch.object(self.p,'upload_result') as upload,mock.patch.object(self.p,'run_local') as render,mock.patch.object(self.p,'report') as report:
                self.p.process_delivery(store,store.get(jid))
                upload.assert_not_called();render.assert_not_called();report.assert_called_once()
            self.assertEqual(store.get(jid)['phase'],'complete')

    def test_known_local_id_survives_restart_without_submit(self):
        with mock.patch.object(self.p,'_submit_local') as submit,mock.patch.object(self.p,'_call',return_value={'status':'completed','result':{'ok':True}}),mock.patch.object(self.p.time,'sleep'):
            self.assertEqual(self.p.run_local({},'a'*32,local_id='existing',durable=True),({'ok':True},None))
        submit.assert_not_called()

    def test_tracking_timeout_is_unknown_not_failed(self):
        with mock.patch.object(self.p,'_call',return_value={'status':'running'}),mock.patch.object(self.p,'_submit_local') as submit:
            with self.assertRaisesRegex(RuntimeError,'local_completion_unknown'):
                self.p.run_local({},'a'*32,local_id='existing',started_at=time.time()-4000,durable=True)
        submit.assert_not_called()

    def test_completed_relay_never_resubmits_even_without_local_metadata(self):
        with tempfile.TemporaryDirectory() as t:
            store=self.p.DeliveryStore(Path(t));jid='a'*32;store.ensure({'job_id':jid,'claim_token':'b'*32,'payload':{}})
            with mock.patch.object(self.p,'delivery_status',return_value={'status':'completed'}),mock.patch.object(self.p,'report'),mock.patch.object(self.p,'run_local') as render,mock.patch.object(self.p,'sync_user_assets') as sync:
                self.p.process_delivery(store,store.get(jid));render.assert_not_called();sync.assert_not_called()

    def test_journal_identity_and_active_reservation(self):
        with tempfile.TemporaryDirectory() as t:
            store=self.p.DeliveryStore(Path(t));job={'job_id':'a'*32,'claim_token':'b'*32,'payload':{'x':1}};store.ensure(job)
            with self.assertRaisesRegex(ValueError,'identity_conflict'):store.ensure({**job,'payload':{'x':2}})
            self.assertIsNotNone(store.next_ready());self.assertIsNone(store.next_ready());self.assertEqual(store.outstanding(),1)
            with store.process_lock():
                with self.assertRaises(OSError):
                    with self.p.DeliveryStore(Path(t)).process_lock():pass

    def test_completed_journals_leave_hot_queue_and_drop_private_payload(self):
        with tempfile.TemporaryDirectory() as t:
            store=self.p.DeliveryStore(Path(t));job={'job_id':'a'*32,'claim_token':'b'*32,'payload':{'private_text':'private-input'}};store.ensure(job)
            store.update(job['job_id'],phase='complete')
            self.assertEqual(list(Path(t).glob('*.json')),[])
            self.assertEqual(store.outstanding(),0)
            completed=store.get(job['job_id']);self.assertEqual(completed['phase'],'complete')
            self.assertNotIn('private-input',json.dumps(completed));self.assertNotIn(job['claim_token'],json.dumps(completed))
            store.ensure(job)
            with self.assertRaisesRegex(ValueError,'identity_conflict'):store.ensure({**job,'payload':{'private_text':'changed'}})
